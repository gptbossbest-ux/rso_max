"""
bot.py — MAX-бот РСО Портал, Горизонт 1.

Архитектура:
  - Диспетчеризация через таблицы, а не цепочки if/elif:
      _CALLBACK_PREFIXES — payload вида "префикс:аргумент"
      _CALLBACK_STATIC   — payload без аргумента
      _MESSAGE_HANDLERS  — состояние сессии → обработчик текста
      _AFTER_LS_ACTIONS  — отложенное действие после ввода ЛС
    Добавление нового пункта меню = одна запись в таблице + функция.
  - Состояние сессии живёт в user_states (в памяти процесса),
    протухшие сессии убирает cleanup_user_states() по расписанию.
  - Обращения создаются через внутренний FastAPI (client_api),
    справочники и показания читаются напрямую из БД (database).

Инварианты:
  - Какое поле показаний ожидается, определяется ТОЛЬКО состоянием
    (WAITING_VALUE1 / WAITING_VALUE2), отдельных флагов нет.
  - Ключи незавершённых флоу перечислены в _FLOW_KEYS и сбрасываются
    централизованно через _clear_flow().
  - Все сообщения клиенту идут через send_message / send_buttons,
    прямых вызовов httpx к /messages вне _send_raw нет.
"""
from __future__ import annotations

# ── Сертификаты Минцифры (platform-api2.max.ru) ──────────────────────────────
# httpx использует свой пакет доверенных сертификатов (certifi), а не системное
# хранилище Windows/macOS/Linux. truststore.inject_into_ssl() переключает
# стандартный модуль ssl на системное хранилище — туда, куда встал сертификат
# Минцифры после установки с gosuslugi.ru/tls. Должно быть выполнено ДО
# первого импорта httpx, иначе не подействует.
import truststore

truststore.inject_into_ssl()

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

import client_api
import database as db
from config import (
    API,
    AUTH_BLOCK_MINUTES,
    ENABLE_1C_INTEGRATION,
    LOG_BACKUP_COUNT,
    LOG_FILE,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    MAX_AUTH_ATTEMPTS,
    SESSION_TTL_MINUTES,
    TIMEZONE_OFFSET,
    TOKEN,
)
from rso_bot import max_transport
from rso_bot import scheduler as bot_scheduler
from rso_bot import states as session_states
from rso_bot.flows import appeals, appointments, auth, faq, readings, receipts
from rso_bot.jobs import appointment_reminders
from rso_bot.session import SessionManager

# ── Логгер ────────────────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("rso.bot")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    try:
        fh = RotatingFileHandler(
            LOG_FILE.replace(".log", "_bot.log"),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as exc:
        logger.warning("Не удалось открыть файл лога: %s", exc)
    return logger


log = _setup_logger()

# Compatibility exports used by handlers and existing integrations.
S = session_states.S
_FLOW_KEYS = session_states.FLOW_KEYS
_METER_INPUT_KEYS = session_states.METER_INPUT_KEYS

# ── Константы ─────────────────────────────────────────────────────────────────

_MAX_HEADERS = {"Authorization": TOKEN}  # без префикса Bearer — см. dev.max.ru/docs-api (обновление платформы)
BOT_HEALTH_FILE = os.getenv("BOT_HEALTH_FILE", "")

# Категории обращений — метка → значение для API
CATEGORIES: dict[str, str] = {
    "🔧 Заявка на обслуживание": "заявка",
    "🚨 Аварийная ситуация":     "авария",
    "📉 Качество услуг":         "качество",
    "💬 Прочее":                  "прочее",
}

# ── Хранилище сессий и защита от брутфорса ───────────────────────────────────

# {chat_id: {state, ls, fio, last_active, ...}}
user_states: dict = {}

# {chat_id: {attempts, blocked_until}} — compatibility alias for integrations.
_auth_attempts: dict = auth.auth_attempts


def _now() -> datetime:
    return datetime.now(timezone(timedelta(hours=TIMEZONE_OFFSET)))


_session_manager = SessionManager(user_states, clock=lambda: _now())


def _get_session_manager() -> SessionManager:
    """Return a manager bound to the current ``user_states`` object."""
    global _session_manager
    if _session_manager.states is not user_states:
        _session_manager = SessionManager(user_states, clock=lambda: _now())
    return _session_manager


def _touch(state: dict) -> dict:
    """Обновляет last_active и возвращает state."""
    return _get_session_manager().touch(state)


def _get_state(chat_id: int) -> dict:
    """Возвращает состояние сессии, создаёт пустое если нет."""
    return _get_session_manager().get_state(chat_id)


def cleanup_user_states(
    states: dict | None = None,
    ttl_minutes: int = SESSION_TTL_MINUTES,
) -> int:
    """
    Удаляет из user_states сессии старше ttl_minutes минут.
    Вызывается APScheduler-задачей раз в час.
    Возвращает количество удалённых записей.

    states=None — работать с модульным user_states (значение по умолчанию
    не указано напрямую, чтобы не привязываться к объекту на момент
    определения функции).
    """
    removed = _get_session_manager().cleanup(states, ttl_minutes)
    if removed:
        log.info("cleanup_user_states: удалено %d устаревших сессий", removed)
    return removed


# ── Низкоуровневые функции отправки ──────────────────────────────────────────

def _send_raw(chat_id: int, body: dict) -> bool:
    return max_transport.send_raw(
        API,
        _MAX_HEADERS,
        chat_id,
        body,
        http_client=httpx,
        logger=log,
    )


def send_message(chat_id: int, text: str) -> bool:
    return max_transport.send_message(chat_id, text, sender=_send_raw)


def send_buttons(
    chat_id: int,
    text: str,
    buttons: list[list[dict]],
) -> bool:
    """
    buttons — вложенный список [{type, text, payload}, ...]:
      [  [btn1, btn2],   ← ряд 1
         [btn3],         ← ряд 2  ]
    """
    return max_transport.send_buttons(chat_id, text, buttons, sender=_send_raw)


def _cb(label: str, payload: str) -> dict:
    """Вспомогательная функция — создаёт callback-кнопку."""
    return {"type": "callback", "text": label, "payload": payload}


# ── Главное меню ──────────────────────────────────────────────────────────────

def send_main_menu(chat_id: int, text: str = "Выберите действие:") -> None:
    """
    Главное меню — показывается сразу без авторизации (раздел 6.2 ТЗ).
    ЛС-зависимые функции спрашивают ЛС внутри своего флоу.
    """
    rows = [
        [_cb("📝 Подать обращение",         "appeal_start")],
        [_cb("📋 Проверить статус обращения", "my_appeals")],
        [_cb("📊 Передать показания",       "pokazaniya")],
        [_cb("📚 Ответ на типовой вопрос",  "scripts_list")],
        [_cb("📄 Последняя квитанция",      "kvitanciya")],
        [_cb("🗓️ Записаться на приём",      "appointment_start")],
    ]
    if ENABLE_1C_INTEGRATION and not _get_saved_ls(chat_id):
        rows.insert(0, [_cb("🔐 Авторизоваться", "auth_1c")])
    send_buttons(chat_id, text, rows)


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _get_saved_ls(chat_id: int) -> str | None:
    """Compatibility wrapper for loading a saved account."""
    return auth.get_saved_ls(chat_id, _account_dependencies())


def _save_ls(chat_id: int, ls: str, fio: str | None = None) -> None:
    auth.save_ls(chat_id, ls, fio, _account_dependencies())


def _validate_ls(ls_number: str) -> auth.LsValidation:
    """Compatibility wrapper for account validation."""
    return auth.validate_ls(ls_number, _account_dependencies())


def _check_ls_brute(chat_id: int) -> str | None:
    """Compatibility wrapper for manual account brute-force protection."""
    return auth.check_ls_brute(chat_id, _brute_force_dependencies())


def _fail_ls(chat_id: int) -> str:
    """Compatibility wrapper for recording an invalid account."""
    return auth.fail_ls(chat_id, _brute_force_dependencies())


def _reset_ls_brute(chat_id: int) -> None:
    auth.reset_ls_brute(chat_id, _brute_force_dependencies())


def _account_dependencies() -> auth.AccountDependencies:
    """Build account storage dependencies from current runtime patch points."""
    return auth.AccountDependencies(
        get_state=_get_state,
        get_bot_user=db.get_bot_user,
        upsert_bot_user=db.upsert_bot_user,
        get_ls=db.get_ls,
        integration_enabled=ENABLE_1C_INTEGRATION,
        logger=log,
    )


def _brute_force_dependencies() -> auth.BruteForceDependencies:
    """Build brute-force policy from current config and clock patch points."""
    return auth.BruteForceDependencies(
        attempts=_auth_attempts,
        now=_now,
        max_attempts=MAX_AUTH_ATTEMPTS,
        block_minutes=AUTH_BLOCK_MINUTES,
    )


# ── Управление состоянием сессии ──────────────────────────────────────────────

def _clear_flow(st: dict) -> None:
    """Сбрасывает состояние всех незавершённых флоу и возвращает в меню."""
    _get_session_manager().clear_flow(st)


def _reset_meter_input(st: dict) -> None:
    """Сбрасывает введённые значения показаний, ставит ожидание Т1."""
    _get_session_manager().reset_meter_input(st)


def _request_ls(chat_id: int, after: str) -> None:
    """Compatibility wrapper for requesting an account."""
    auth.request_ls(chat_id, after, _auth_flow_dependencies())


def _start_1c_auth(chat_id: int, after: str | None = None) -> None:
    """Compatibility wrapper for starting two-step 1C authorization."""
    auth.start_1c_auth(chat_id, after, _auth_flow_dependencies())


# ── Ввод и валидация показаний ────────────────────────────────────────────────

def _parse_reading(chat_id: int, text: str) -> float | None:
    """Compatibility wrapper for parsing an entered meter reading."""
    return readings.parse_reading(chat_id, text, _reading_dependencies())


def _current_reading(ls: str, meter: dict, col: str, initial_key: str) -> float:
    """Compatibility wrapper for resolving a meter's current reading."""
    return readings.current_reading(
        ls, meter, col, initial_key, _reading_dependencies()
    )


# ── Флоу подачи обращения (раздел 6.4, шаги 1–4) ────────────────────────────

def _appeal_dependencies() -> appeals.AppealDependencies:
    """Build appeal dependencies from runtime patch points in this entry point."""
    return appeals.AppealDependencies(
        create_appeal=client_api.create_appeal,
        list_appeals_by_ls=client_api.list_appeals_by_ls,
        confirm_appeal=client_api.confirm_appeal,
        reopen_appeal=client_api.reopen_appeal,
        get_state=_get_state,
        touch=_touch,
        get_saved_ls=_get_saved_ls,
        request_ls=_request_ls,
        check_ls_brute=_check_ls_brute,
        validate_ls=_validate_ls,
        fail_ls=_fail_ls,
        reset_ls_brute=_reset_ls_brute,
        save_ls=_save_ls,
        submit_appeal=_submit_appeal,
        make_callback=_cb,
        send_message=send_message,
        send_buttons=send_buttons,
        send_main_menu=send_main_menu,
        logger=log,
        categories=CATEGORIES,
        category_state=S.APPEAL_CATEGORY,
        body_state=S.APPEAL_BODY,
        reopen_comment_state=S.REOPEN_COMMENT,
        menu_state=S.MENU,
    )


def _start_appeal(chat_id: int) -> None:
    """Compatibility wrapper for starting the extracted appeal flow."""
    appeals.start_appeal(chat_id, _appeal_dependencies())


def _appeal_set_category(chat_id: int, category: str) -> None:
    """Compatibility wrapper for choosing an appeal category."""
    appeals.set_category(chat_id, category, _appeal_dependencies())


def _appeal_got_body(chat_id: int, text: str) -> None:
    """Compatibility wrapper for accepting an appeal body."""
    appeals.got_body(chat_id, text, _appeal_dependencies())


def _appeal_got_ls(chat_id: int, ls_input: str) -> None:
    """Compatibility wrapper for validating an appeal account number."""
    appeals.got_ls(chat_id, ls_input, _appeal_dependencies())


def _submit_appeal(chat_id: int, ls: str) -> None:
    """Compatibility wrapper for submitting an extracted appeal flow."""
    appeals.submit_appeal(chat_id, ls, _appeal_dependencies())


# ── Флоу «Мои обращения» ─────────────────────────────────────────────────────

def _show_my_appeals(chat_id: int) -> None:
    """Compatibility wrapper for listing active appeals."""
    appeals.show_my_appeals(chat_id, _appeal_dependencies())


# ── Движок FAQ (раздел 6.3) ──────────────────────────────────────────────────

def _faq_dependencies() -> faq.FaqDependencies:
    """Build FAQ dependencies from runtime patch points in this entry point."""
    return faq.FaqDependencies(
        list_scripts=client_api.list_scripts,
        get_script_tree=client_api.get_script_tree,
        get_state=_get_state,
        touch=_touch,
        make_callback=_cb,
        send_message=send_message,
        send_buttons=send_buttons,
        send_main_menu=send_main_menu,
        logger=log,
        script_list_state=S.SCRIPT_LIST,
        script_node_state=S.SCRIPT_NODE,
        menu_state=S.MENU,
    )


def _show_scripts_list(chat_id: int) -> None:
    """Compatibility wrapper for the extracted FAQ flow."""
    faq.show_scripts_list(chat_id, _faq_dependencies())


def _open_script(chat_id: int, script_id: int) -> None:
    """Compatibility wrapper for opening an FAQ tree."""
    faq.open_script(chat_id, script_id, _faq_dependencies())


def _show_script_node(chat_id: int) -> None:
    """Compatibility wrapper for rendering the active FAQ node."""
    faq.show_script_node(chat_id, _faq_dependencies())


def _navigate_script_node(chat_id: int, node_id: int) -> None:
    """Compatibility wrapper for moving through the active FAQ tree."""
    faq.navigate_script_node(chat_id, node_id, _faq_dependencies())


# ── Показания (адаптировано из предыдущей версии) ────────────────────────────

def _reading_dependencies() -> readings.ReadingDependencies:
    """Build reading dependencies from runtime patch points in this entry point."""
    return readings.ReadingDependencies(
        get_meters=db.get_schetchiki,
        get_last_reading=db.get_last_pokazaniya,
        add_reading=db.add_pokazaniya,
        get_state=_get_state,
        touch=_touch,
        reset_meter_input=_reset_meter_input,
        clear_flow=_clear_flow,
        get_saved_ls=_get_saved_ls,
        request_ls=_request_ls,
        parse_input=_parse_reading,
        get_current_value=_current_reading,
        show_meter_select=_show_meter_select,
        ask_meter_value=_ask_meter_value,
        confirm_meter_reading=_confirm_meter_reading,
        make_callback=_cb,
        send_message=send_message,
        send_buttons=send_buttons,
        send_main_menu=send_main_menu,
        logger=log,
        integration_enabled=ENABLE_1C_INTEGRATION,
        meter_select_state=S.METER_SELECT,
        waiting_value1_state=S.WAITING_VALUE1,
        waiting_value2_state=S.WAITING_VALUE2,
        confirm_state=S.CONFIRM_POKAZANIYA,
    )

def _start_pokazaniya(chat_id: int) -> None:
    """Compatibility wrapper for starting the extracted readings flow."""
    readings.start(chat_id, _reading_dependencies())


def _show_meter_select(chat_id: int, ls: str) -> None:
    """Compatibility wrapper for displaying meter selection."""
    readings.show_meter_select(chat_id, ls, _reading_dependencies())


def _ask_meter_value(chat_id: int) -> None:
    """Compatibility wrapper for prompting for the next meter value."""
    readings.ask_meter_value(chat_id, _reading_dependencies())


def _confirm_meter_reading(chat_id: int) -> None:
    """Compatibility wrapper for displaying reading confirmation."""
    readings.confirm_meter_reading(chat_id, _reading_dependencies())


# ── Запись на приём (раздел 6-7 ТЗ) ──────────────────────────────────────────

def _appointment_dependencies() -> appointments.AppointmentDependencies:
    """Build appointment dependencies from entry-point runtime patch points."""
    return appointments.AppointmentDependencies(
        get_active_appointment=db.get_active_appointment,
        get_branches=db.get_branches,
        get_available_dates=db.get_available_dates,
        get_branch=db.get_branch,
        get_available_slots=db.get_available_slots,
        create_appointment=db.create_appointment,
        get_appointment=db.get_appointment,
        cancel_appointment=db.cancel_appointment,
        get_state=_get_state,
        touch=_touch,
        get_saved_ls=_get_saved_ls,
        request_ls=_request_ls,
        clear_flow=_clear_flow,
        show_active_appointment=_show_active_appointment,
        show_branch_select=_show_branch_select,
        show_date_select=_show_date_select,
        show_time_select=_show_time_select,
        show_appointment_confirm=_show_appointment_confirm,
        finalize_appointment=_finalize_appointment,
        make_callback=_cb,
        send_message=send_message,
        send_buttons=send_buttons,
        send_main_menu=send_main_menu,
        parse_date=datetime.strptime,
        database_errors=(Exception,),
        logger=log,
        menu_state=S.MENU,
        branch_state=S.APPOINTMENT_BRANCH,
        date_state=S.APPOINTMENT_DATE,
        time_state=S.APPOINTMENT_TIME,
        theme_state=S.APPOINTMENT_THEME,
        confirm_state=S.APPOINTMENT_CONFIRM,
    )


def _start_appointment_flow(chat_id: int, ls: str | None = None) -> None:
    """Compatibility wrapper for starting the extracted appointment flow."""
    appointments.start_appointment_flow(chat_id, ls, _appointment_dependencies())


def _show_active_appointment(chat_id: int, appointment) -> None:
    """Compatibility wrapper for displaying an active appointment."""
    appointments.show_active_appointment(
        chat_id, appointment, _appointment_dependencies()
    )


def _show_branch_select(chat_id: int, ls: str) -> None:
    """Compatibility wrapper for selecting an appointment branch."""
    appointments.show_branch_select(chat_id, ls, _appointment_dependencies())


def _show_date_select(chat_id: int, branch_id: int) -> None:
    """Compatibility wrapper for selecting an appointment date."""
    appointments.show_date_select(chat_id, branch_id, _appointment_dependencies())


def _format_date_label(date_str: str) -> str:
    """Compatibility wrapper for the appointment date label."""
    return appointments.format_date_label(date_str, _appointment_dependencies())


def _show_time_select(chat_id: int, branch_id: int, slot_date: str) -> None:
    """Compatibility wrapper for selecting an appointment time."""
    appointments.show_time_select(
        chat_id, branch_id, slot_date, _appointment_dependencies()
    )


def _ask_theme(chat_id: int, slot_time: str) -> None:
    """Compatibility wrapper for the optional appointment theme step."""
    appointments.ask_theme(chat_id, slot_time, _appointment_dependencies())


def _show_appointment_confirm(chat_id: int) -> None:
    """Compatibility wrapper for rendering appointment confirmation."""
    appointments.show_appointment_confirm(chat_id, _appointment_dependencies())


def _finalize_appointment(chat_id: int) -> None:
    """Compatibility wrapper for persisting an appointment."""
    appointments.finalize_appointment(chat_id, _appointment_dependencies())


def _cancel_own_appointment(chat_id: int, appointment_id: int) -> None:
    """Compatibility wrapper for cancelling the caller's appointment."""
    appointments.cancel_own_appointment(
        chat_id, appointment_id, _appointment_dependencies()
    )


# ── PDF квитанция ─────────────────────────────────────────────────────────────

def _receipt_path(ls: str) -> Path:
    """Return the runtime path used for an account's PDF receipt."""
    return receipts.local_receipt_path(Path("KV"), ls)


def _read_receipt_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _receipt_upload_dependencies() -> receipts.ReceiptUploadDependencies:
    """Resolve filesystem and MAX collaborators at call time."""
    return receipts.ReceiptUploadDependencies(
        receipt_path=_receipt_path,
        read_bytes=_read_receipt_bytes,
        http_client=httpx,
        api_base=API,
        headers=_MAX_HEADERS,
        send_raw=_send_raw,
        send_message=send_message,
        sleep=time.sleep,
        logger=log,
    )


def _receipt_flow_dependencies() -> receipts.ReceiptFlowDependencies:
    """Resolve account/session hooks while preserving legacy patch points."""
    return receipts.ReceiptFlowDependencies(
        get_saved_ls=_get_saved_ls,
        request_ls=_request_ls,
        clear_flow=_clear_flow,
        send_message=send_message,
        send_main_menu=send_main_menu,
        send_pdf=_send_pdf,
        deliver_receipt=_deliver_kvitanciya,
    )

def _deliver_kvitanciya(chat_id: int, ls: str) -> None:
    """Compatibility wrapper for delivering a receipt and reopening the menu."""
    receipts.deliver(chat_id, ls, _receipt_flow_dependencies())


def _send_pdf(chat_id: int, ls: str) -> None:
    """Compatibility wrapper for the two-step MAX PDF upload contract."""
    receipts.send_pdf(chat_id, ls, _receipt_upload_dependencies())


# ── Обработчик callback-кнопок ────────────────────────────────────────────────

def _ack_callback(callback_id: str) -> None:
    """Убирает «часики» на нажатой кнопке. Ошибка не критична."""
    max_transport.ack_callback(
        API,
        _MAX_HEADERS,
        callback_id,
        http_client=httpx,
        logger=log,
    )


# -- Обработчики callback с аргументом (payload вида "префикс:значение") -------

def _cb_confirm_appeal(chat_id: int, st: dict, arg: str) -> None:
    """Compatibility wrapper for confirming appeal closure."""
    appeals.confirm_appeal(chat_id, st, arg, _appeal_dependencies())


def _cb_reopen_appeal(chat_id: int, st: dict, arg: str) -> None:
    """Compatibility wrapper for starting appeal reopening."""
    appeals.begin_reopen(chat_id, st, arg, _appeal_dependencies())


def _cb_select_meter(chat_id: int, st: dict, arg: str) -> None:
    """Compatibility wrapper for selecting a meter."""
    readings.select_meter(chat_id, st, arg, _reading_dependencies())


def _cb_select_date(chat_id: int, st: dict, arg: str) -> None:
    """Compatibility wrapper for the appointment date callback."""
    appointments.select_date(chat_id, st, arg, _appointment_dependencies())


# Префикс payload → (обработчик, приводить ли аргумент к int)
_CALLBACK_PREFIXES: dict[str, callable] = {
    "confirm":     _cb_confirm_appeal,
    "reopen":      _cb_reopen_appeal,
    "cat":         lambda chat_id, st, arg: _appeal_set_category(chat_id, arg),
    "script":      lambda chat_id, st, arg: _open_script(chat_id, int(arg)),
    "script_node": lambda chat_id, st, arg: _navigate_script_node(chat_id, int(arg)),
    "meter":       _cb_select_meter,
    "appt_branch": lambda chat_id, st, arg: _show_date_select(chat_id, int(arg)),
    "appt_date":   _cb_select_date,
    "appt_time":   lambda chat_id, st, arg: _ask_theme(chat_id, arg),
    "appt_cancel": lambda chat_id, st, arg: _cancel_own_appointment(chat_id, int(arg)),
}


# -- Обработчики статичных callback ------------------------------------------

def _cb_kvitanciya(chat_id: int, st: dict) -> None:
    receipts.start(chat_id, st, _receipt_flow_dependencies())


def _cb_skip_theme(chat_id: int, st: dict) -> None:
    appointments.skip_theme(chat_id, st, _appointment_dependencies())


def _cb_appt_confirm(chat_id: int, st: dict) -> None:
    appointments.confirm(chat_id, st, _appointment_dependencies())


def _cb_main_menu(chat_id: int, st: dict) -> None:
    _clear_flow(st)
    _touch(st)
    send_main_menu(chat_id)


def _cb_meter_confirm(chat_id: int, st: dict) -> None:
    """Compatibility wrapper for persisting a confirmed reading."""
    readings.confirm(chat_id, st, _reading_dependencies())


def _cb_meter_retry(chat_id: int, st: dict) -> None:
    """Compatibility wrapper for restarting input for the selected meter."""
    readings.retry(chat_id, st, _reading_dependencies())


_CALLBACK_STATIC: dict[str, callable] = {
    "auth_1c":          lambda chat_id, st: _start_1c_auth(chat_id),
    "appeal_start":      lambda chat_id, st: _start_appeal(chat_id),
    "my_appeals":        lambda chat_id, st: _show_my_appeals(chat_id),
    "pokazaniya":        lambda chat_id, st: _start_pokazaniya(chat_id),
    "scripts_list":      lambda chat_id, st: _show_scripts_list(chat_id),
    "kvitanciya":        _cb_kvitanciya,
    "appointment_start": lambda chat_id, st: _start_appointment_flow(chat_id),
    "appt_skip_theme":   _cb_skip_theme,
    "appt_confirm":      _cb_appt_confirm,
    "main_menu":         _cb_main_menu,
    "cancel":            _cb_main_menu,
    "meter_confirm":     _cb_meter_confirm,
    "meter_retry":       _cb_meter_retry,
}


def handle_callback(update: dict) -> None:
    chat_id = update["message"]["recipient"]["chat_id"]
    payload = update["callback"]["payload"]

    _ack_callback(update["callback"]["callback_id"])

    st = _get_state(chat_id)
    _touch(st)
    log.debug("callback chat_id=%s payload=%s", chat_id, payload)

    # Payload с аргументом: "префикс:значение"
    if ":" in payload:
        prefix, _, arg = payload.partition(":")
        handler = _CALLBACK_PREFIXES.get(prefix)
        if handler:
            try:
                handler(chat_id, st, arg)
            except (ValueError, KeyError, IndexError) as exc:
                log.warning("callback %s: некорректный аргумент '%s': %s",
                            prefix, arg, exc)
                send_main_menu(chat_id)
            return

    # Статичный payload без аргумента
    handler = _CALLBACK_STATIC.get(payload)
    if handler:
        handler(chat_id, st)
        return

    log.warning("Неизвестный payload: %s  chat_id=%s", payload, chat_id)


# ── Обработчик входящих сообщений ─────────────────────────────────────────────

# Действие после успешной валидации ЛС (ключ after_ls → функция)
_AFTER_LS_ACTIONS: dict[str, callable] = {
    "my_appeals": lambda chat_id, ls: _show_my_appeals(chat_id),
    "pokazaniya": lambda chat_id, ls: _show_meter_select(chat_id, ls),
    "kvitanciya": _deliver_kvitanciya,
    "appointment": lambda chat_id, ls: _start_appointment_flow(chat_id, ls),
    "appeal":     lambda chat_id, ls: _submit_appeal(chat_id, ls),
}


def _auth_flow_dependencies() -> auth.AuthFlowDependencies:
    """Build auth flow dependencies from runtime patch points in this entry point."""
    return auth.AuthFlowDependencies(
        get_state=_get_state,
        touch=_touch,
        clear_flow=_clear_flow,
        send_message=send_message,
        send_main_menu=send_main_menu,
        validate_ls=_validate_ls,
        check_ls_brute=_check_ls_brute,
        fail_ls=_fail_ls,
        reset_ls_brute=_reset_ls_brute,
        save_ls=_save_ls,
        start_1c_auth=_start_1c_auth,
        request_1c_auth_code=client_api.request_1c_auth_code,
        verify_1c_auth_code=client_api.verify_1c_auth_code,
        continuations=_AFTER_LS_ACTIONS,
        integration_enabled=ENABLE_1C_INTEGRATION,
        logger=log,
    )


def _on_await_ls(chat_id: int, st: dict, text: str) -> None:
    """Compatibility wrapper for manual account input."""
    auth.on_await_ls(chat_id, st, text, _auth_flow_dependencies())


def _on_await_ls_1c(chat_id: int, st: dict, text: str) -> None:
    """Compatibility wrapper for requesting a one-time 1C code."""
    auth.on_await_ls_1c(chat_id, st, text, _auth_flow_dependencies())


def _on_await_code_1c(chat_id: int, st: dict, text: str) -> None:
    """Compatibility wrapper for verifying a one-time 1C code."""
    auth.on_await_code_1c(chat_id, st, text, _auth_flow_dependencies())


def _on_reopen_comment(chat_id: int, st: dict, text: str) -> None:
    """Compatibility wrapper for submitting an appeal reopen comment."""
    appeals.on_reopen_comment(chat_id, st, text, _appeal_dependencies())


def _on_appointment_theme(chat_id: int, st: dict, text: str) -> None:
    """Compatibility wrapper for typed appointment themes."""
    appointments.on_theme(chat_id, st, text, _appointment_dependencies())


def _on_value1(chat_id: int, st: dict, text: str) -> None:
    """Compatibility wrapper for T1 or single-tariff input."""
    readings.on_value1(chat_id, st, text, _reading_dependencies())


def _on_value2(chat_id: int, st: dict, text: str) -> None:
    """Compatibility wrapper for T2 input."""
    readings.on_value2(chat_id, st, text, _reading_dependencies())


# Состояние сессии → обработчик текстового ввода
_MESSAGE_HANDLERS: dict[str, callable] = {
    S.APPEAL_BODY:       lambda chat_id, st, text: _appeal_got_body(chat_id, text),
    S.AWAIT_LS:          _on_await_ls,
    S.AWAIT_LS_1C:       _on_await_ls_1c,
    S.AWAIT_CODE_1C:     _on_await_code_1c,
    S.REOPEN_COMMENT:    _on_reopen_comment,
    S.APPOINTMENT_THEME: _on_appointment_theme,
    S.WAITING_VALUE1:    _on_value1,
    S.WAITING_VALUE2:    _on_value2,
}

_RESET_COMMANDS = ("/start", "/help", "/menu")


def handle_message(message: dict) -> None:
    chat_id = message["recipient"]["chat_id"]
    text    = (message.get("body") or {}).get("text", "").strip()

    current_state = _get_state(chat_id).get("state", S.MENU)
    log.debug(
        "msg chat_id=%s state=%s text=<скрыто> length=%s",
        chat_id,
        current_state,
        len(text),
    )

    if not text:
        return

    if text in _RESET_COMMANDS:
        st = _get_state(chat_id)
        _clear_flow(st)
        _touch(st)
        send_main_menu(chat_id, "Добрый день! Выберите действие:")
        return

    st = _get_state(chat_id)
    _touch(st)

    handler = _MESSAGE_HANDLERS.get(st.get("state", S.MENU))
    if handler:
        try:
            handler(chat_id, st, text)
        except (KeyError, IndexError) as exc:
            # Сессия рассинхронизирована (например, после cleanup_user_states)
            log.warning("handle_message: состояние повреждено, сброс. chat_id=%s: %s",
                        chat_id, exc)
            _clear_flow(st)
            send_main_menu(chat_id, "Начнём заново. Выберите действие:")
        return

    # Состояние без текстового обработчика — показываем меню
    send_main_menu(chat_id)


# ── APScheduler задачи ────────────────────────────────────────────────────────

def _task_auto_resolve_pending() -> None:
    """
    Задача APScheduler: pending_confirmation → resolved через 24 ч.
    Запускается раз в час. Функция auto_resolve_pending() в database.py.
    """
    try:
        resolved = db.auto_resolve_pending()
        if resolved:
            log.info("APScheduler auto_resolve_pending: %d обращений → resolved", resolved)
    except Exception as exc:
        log.error("APScheduler auto_resolve_pending ошибка: %s", exc)


def _task_cleanup_user_states() -> None:
    """
    Задача APScheduler: удаление устаревших сессий из user_states.
    Запускается раз в час. Функция cleanup_user_states() определена выше.
    """
    try:
        cleanup_user_states(user_states, SESSION_TTL_MINUTES)
    except Exception as exc:
        log.error("APScheduler cleanup_user_states ошибка: %s", exc)


def _format_appointment_reminder(appointment, when_label: str) -> str:
    """Совместимая обёртка форматирования напоминания."""
    return appointment_reminders.format_appointment_reminder(appointment, when_label)


def _appointment_reminder_dependencies(
    now=None,
) -> appointment_reminders.AppointmentReminderDependencies:
    """Resolve reminder dependencies at call time for runtime patch compatibility."""
    now_provider = now or appointment_reminders.moscow_now
    return appointment_reminders.AppointmentReminderDependencies(
        get_appointments_for_reminder_24h=lambda: db.get_appointments_for_reminder_24h(
            now_provider()
        ),
        get_appointments_for_reminder_day=lambda: db.get_appointments_for_reminder_day(
            now_provider()
        ),
        send_message=send_message,
        mark_reminded=db.mark_reminded,
        logger=log,
    )


def _task_appointment_reminder_24h() -> None:
    """
    Задача APScheduler: напоминание за 24ч до приёма (REQ-АВТ-07-06).
    Запускается раз в час — этого достаточно, т.к. окно выборки в БД
    (23–25ч от текущего момента) шире часового шага, дублей не будет
    благодаря флагу reminded_24h.

    Доставка выполняется с ограниченным числом повторов. Флаг ставится только
    после подтверждённой отправки; ошибка одной записи не прерывает пакет.
    """
    appointment_reminders.task_appointment_reminder_24h(
        _appointment_reminder_dependencies()
    )


def _task_appointment_reminder_day() -> None:
    """
    Задача APScheduler: напоминание в день приёма (REQ-АВТ-07-07).
    Запускается ежедневно в 09:00 по московскому времени (cron).
    """
    appointment_reminders.task_appointment_reminder_day(
        _appointment_reminder_dependencies()
    )


def _scheduler_dependencies() -> bot_scheduler.SchedulerDependencies:
    """Resolve scheduler callbacks and runtime objects without importing bot.py."""
    return bot_scheduler.SchedulerDependencies(
        auto_resolve_pending=_task_auto_resolve_pending,
        cleanup_user_states=_task_cleanup_user_states,
        appointment_reminder_24h=_task_appointment_reminder_24h,
        appointment_reminder_day=_task_appointment_reminder_day,
        scheduler_factory=BackgroundScheduler,
        logger=log,
    )


# ── Мониторинг домовых чатов (раздел 5, 7.4 ТЗ) ──────────────────────────────
#
# Таблицы house_chats / chat_scenarios / chat_exclusions созданы в БД
# (Этап 1), но до этой правки логика определения типа чата и сопоставления
# ключевых слов в polling отсутствовала — ЛЮБОЕ входящее сообщение уходило
# в handle_message() и в итоге получало главное меню, даже если оно пришло
# из группового домового чата. Это и была причина бага.

_UNKNOWN_CHAT_TYPE_WARNED: set[int] = set()  # чтобы не спамить лог на каждое сообщение


def _get_chat_type(message: dict) -> str:
    """
    Определяет тип чата: 'dialog' (личный) | 'chat' (группа) | 'channel'.

    ВАЖНО: точное имя поля не подтверждено официальной документацией
    напрямую (страница объекта Recipient на dev.max.ru не отдала тело
    при проверке) — определено по косвенным источникам (схема TamTam,
    на которой основан MAX Bot API, и сторонние клиентские библиотеки).
    Если это поле в реальном payload называется иначе — при первом же
    сообщении из группы в лог упадёт WARNING с полным recipient,
    после чего нужно поправить имя ключа ниже в одну строку.
    """
    recipient = message.get("recipient", {}) or {}
    chat_type = recipient.get("chat_type") or message.get("chat_type")

    if chat_type is None:
        chat_id = recipient.get("chat_id")
        if chat_id not in _UNKNOWN_CHAT_TYPE_WARNED:
            _UNKNOWN_CHAT_TYPE_WARNED.add(chat_id)
            log.warning(
                "Не удалось определить chat_type для chat_id=%s — recipient=%s. "
                "Уточни точное имя поля в реальном payload и поправь _get_chat_type(). "
                "По умолчанию считаем 'dialog' (личный чат), чтобы не сломать "
                "существующий клиентский флоу.",
                chat_id, recipient,
            )
        return "dialog"

    return chat_type


def _handle_group_message(message: dict) -> None:
    """
    Обрабатывает сообщение из группового домового чата (chat_type != 'dialog').

    Логика:
      1. Если chat_id не зарегистрирован в house_chats — бот молчит.
         (Раньше падало в handle_message() → главное меню — это и был баг.)
      2. Если отправитель в chat_exclusions — бот молчит (раздел 7.4).
      3. Ищем среди активных сценариев чата первый с совпадением ключевого
         слова (регистронезависимо, подстрокой) — отвечаем response_text.
      4. Если совпадений нет — бот молчит (не спамит группу меню/подсказками).
    """
    recipient = message.get("recipient", {}) or {}
    chat_id = recipient.get("chat_id")
    if chat_id is None:
        return

    house_chat = db.get_house_chat_by_chat_id(str(chat_id))
    if not house_chat:
        log.debug("Сообщение из незарегистрированного группового чата chat_id=%s — игнорируем", chat_id)
        return

    sender = message.get("sender") or {}
    sender_id = sender.get("user_id")
    if sender_id is not None and db.is_user_excluded(house_chat["id"], "max", str(sender_id)):
        log.debug("Отправитель user_id=%s в списке исключений чата id=%s — игнорируем",
                  sender_id, house_chat["id"])
        return

    text = ((message.get("body") or {}).get("text") or "").lower()
    if not text:
        return

    scenarios = db.get_scenarios_for_chat(house_chat["id"])
    for scenario in scenarios:
        try:
            keywords = json.loads(scenario["keywords"])
        except (TypeError, ValueError, json.JSONDecodeError):
            log.warning("Некорректный JSON в keywords сценария id=%s — пропускаем", scenario["id"])
            continue

        if any(kw.lower() in text for kw in keywords if kw):
            send_message(chat_id, scenario["response_text"])
            log.info(
                "Сценарий '%s' сработал в чате id=%s (house_chat) по ключевому слову",
                scenario["title"], house_chat["id"],
            )
            if scenario["suggest_appeal"]:
                send_message(
                    chat_id,
                    "Если вопрос не решён — напишите мне в личные сообщения, "
                    "оформим обращение с отслеживанием статуса.",
                )
            return  # первый подошедший сценарий — и хватит


# ── Polling ────────────────────────────────────────────────────────────────────

def _mark_poll_healthy() -> None:
    """Atomically update the optional polling heartbeat used by Docker."""
    if not BOT_HEALTH_FILE:
        return
    temporary = f"{BOT_HEALTH_FILE}.tmp.{os.getpid()}"
    try:
        health_dir = os.path.dirname(BOT_HEALTH_FILE)
        if health_dir:
            os.makedirs(health_dir, exist_ok=True)
        with open(temporary, "w", encoding="ascii") as heartbeat:
            heartbeat.write(str(time.time()))
        os.replace(temporary, BOT_HEALTH_FILE)
    except OSError as exc:
        log.warning("Не удалось обновить heartbeat polling: %s", exc)
        try:
            os.unlink(temporary)
        except OSError:
            pass

def poll() -> None:
    marker = None
    log.info("Бот запущен (polling)")
    while True:
        try:
            resp = httpx.get(
                f"{API}/updates",
                headers=_MAX_HEADERS,
                params={"marker": marker, "timeout": 25},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            for update in data.get("updates", []):
                utype = update.get("update_type")
                try:
                    if utype == "bot_started":
                        chat_id = update["chat_id"]
                        _get_state(chat_id)
                        send_main_menu(chat_id, "Добрый день! Выберите действие:")
                    elif utype == "message_created":
                        message = update["message"]
                        if _get_chat_type(message) == "dialog":
                            handle_message(message)
                        else:
                            _handle_group_message(message)
                    elif utype == "message_callback":
                        handle_callback(update)
                except Exception as exc:
                    log.exception("Ошибка обработки update: %s", exc)
            marker = data.get("marker")
            _mark_poll_healthy()
        except httpx.TimeoutException:
            log.debug("poll timeout — норма")
        except Exception as exc:
            log.error("poll error: %s", exc)
            time.sleep(3)


# ── Точка входа ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()

    # ── APScheduler ───────────────────────────────────────────────────────────
    scheduler = bot_scheduler.create_scheduler(_scheduler_dependencies())
    bot_scheduler.start_scheduler(scheduler, log)

    # ── Основной цикл (блокирует главный поток) ───────────────────────────────
    try:
        poll()
    except (KeyboardInterrupt, SystemExit):
        log.info("Получен сигнал завершения")
    finally:
        bot_scheduler.shutdown_scheduler(scheduler, log)
