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

import logging
import os
import time
import json
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

import client_api
import database as db
from config import (
    API,
    AUTH_BLOCK_MINUTES,
    LOG_BACKUP_COUNT,
    LOG_FILE,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    MAX_AUTH_ATTEMPTS,
    SESSION_TTL_MINUTES,
    SYNC_INTERVAL_MINUTES,
    TIMEZONE_OFFSET,
    TOKEN,
)

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

# ── Константы ─────────────────────────────────────────────────────────────────

_MAX_HEADERS = {"Authorization": TOKEN}  # без префикса Bearer — см. dev.max.ru/docs-api (обновление платформы)

# Категории обращений — метка → значение для API
CATEGORIES: dict[str, str] = {
    "🔧 Заявка на обслуживание": "заявка",
    "🚨 Аварийная ситуация":     "авария",
    "📉 Качество услуг":         "качество",
    "💬 Прочее":                  "прочее",
}

# Состояния сессии
class S:
    MENU    = "menu"

    # Флоу обращения
    APPEAL_CATEGORY = "appeal_category"
    APPEAL_BODY     = "appeal_body"
    AWAIT_LS        = "await_ls"        # универсальное ожидание ЛС (см. _AFTER_LS_ACTIONS)

    # Возврат обращения из pending_confirmation
    REOPEN_COMMENT  = "reopen_comment"

    # Движок скриптов
    SCRIPT_LIST = "script_list"
    SCRIPT_NODE = "script_node"

    # Показания
    METER_SELECT        = "meter_select"
    WAITING_VALUE1      = "waiting_value1"
    WAITING_VALUE2      = "waiting_value2"
    CONFIRM_POKAZANIYA  = "confirm_pokazaniya"

    # Запись на приём (Этап C, раздел 6-7 ТЗ)
    APPOINTMENT_BRANCH   = "appointment_branch"    # выбор филиала
    APPOINTMENT_DATE     = "appointment_date"      # выбор даты
    APPOINTMENT_TIME     = "appointment_time"      # выбор времени
    APPOINTMENT_THEME    = "appointment_theme"     # ввод темы (опционально)
    APPOINTMENT_CONFIRM  = "appointment_confirm"   # подтверждение записи

# ── Хранилище сессий и защита от брутфорса ───────────────────────────────────

# {chat_id: {state, ls, fio, last_active, ...}}
user_states: dict = {}

# {chat_id: {attempts, blocked_until}}
_auth_attempts: dict = {}


def _now() -> datetime:
    return datetime.now(timezone(timedelta(hours=TIMEZONE_OFFSET)))


def _touch(state: dict) -> dict:
    """Обновляет last_active и возвращает state."""
    state["last_active"] = _now()
    return state


def _get_state(chat_id: int) -> dict:
    """Возвращает состояние сессии, создаёт пустое если нет."""
    if chat_id not in user_states:
        user_states[chat_id] = _touch({"state": S.MENU})
    return user_states[chat_id]


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
    if states is None:
        states = user_states

    cutoff = _now() - timedelta(minutes=ttl_minutes)
    to_remove = [
        cid for cid, st in states.items()
        if isinstance(st, dict)
        and st.get("last_active", _now()) < cutoff
    ]
    for cid in to_remove:
        del states[cid]
    if to_remove:
        log.info("cleanup_user_states: удалено %d устаревших сессий", len(to_remove))
    return len(to_remove)


# ── Низкоуровневые функции отправки ──────────────────────────────────────────

def _send_raw(chat_id: int, body: dict) -> bool:
    try:
        r = httpx.post(
            f"{API}/messages",
            headers=_MAX_HEADERS,
            params={"chat_id": chat_id},
            json=body,
            timeout=5,
        )
        if r.status_code != 200:
            log.warning("MAX API %s для chat_id=%s", r.status_code, chat_id)
            return False
        return True
    except Exception as exc:
        log.error("_send_raw chat_id=%s: %s", chat_id, exc)
        return False


def send_message(chat_id: int, text: str) -> bool:
    return _send_raw(chat_id, {"text": text})


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
    return _send_raw(chat_id, {
        "text": text,
        "attachments": [{
            "type": "inline_keyboard",
            "payload": {"buttons": buttons},
        }],
    })


def _cb(label: str, payload: str) -> dict:
    """Вспомогательная функция — создаёт callback-кнопку."""
    return {"type": "callback", "text": label, "payload": payload}


# ── Главное меню ──────────────────────────────────────────────────────────────

def send_main_menu(chat_id: int, text: str = "Выберите действие:") -> None:
    """
    Главное меню — показывается сразу без авторизации (раздел 6.2 ТЗ).
    ЛС-зависимые функции спрашивают ЛС внутри своего флоу.
    """
    send_buttons(chat_id, text, [
        [_cb("📝 Подать обращение",         "appeal_start")],
        [_cb("📋 Проверить статус обращения", "my_appeals")],
        [_cb("📊 Передать показания",       "pokazaniya")],
        [_cb("📚 Ответ на типовой вопрос",  "scripts_list")],
        [_cb("📄 Последняя квитанция",      "kvitanciya")],
        [_cb("🗓️ Записаться на приём",      "appointment_start")],
    ])


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _get_saved_ls(chat_id: int) -> str | None:
    """Возвращает сохранённый ЛС из сессии или таблицы bot_users."""
    st = _get_state(chat_id)
    if st.get("ls"):
        return st["ls"]
    row = db.get_bot_user(chat_id)
    if row and row["ls"]:
        st["ls"] = row["ls"]
        st["fio"] = row["fio"]
        return row["ls"]
    return None


def _save_ls(chat_id: int, ls: str, fio: str | None = None) -> None:
    st = _get_state(chat_id)
    st["ls"] = ls
    if fio:
        st["fio"] = fio
    db.upsert_bot_user(chat_id, ls, fio or "")


def _validate_ls(ls_number: str) -> bool:
    """True если ЛС существует в БД."""
    return db.get_ls(ls_number) is not None


def _check_ls_brute(chat_id: int) -> str | None:
    """
    Проверяет блокировку перебора ЛС.
    Возвращает None если можно продолжать,
    или строку с сообщением об ошибке если заблокировано.
    """
    info = _auth_attempts.get(chat_id, {"attempts": 0, "blocked_until": None})
    if info["blocked_until"] and _now() < info["blocked_until"]:
        remaining = int((info["blocked_until"] - _now()).total_seconds() / 60) + 1
        return f"Слишком много неудачных попыток.\nПопробуйте через {remaining} мин."
    return None


def _fail_ls(chat_id: int) -> str:
    """Фиксирует неудачную попытку ЛС, возвращает сообщение."""
    info = _auth_attempts.setdefault(chat_id, {"attempts": 0, "blocked_until": None})
    info["attempts"] += 1
    left = MAX_AUTH_ATTEMPTS - info["attempts"]
    if info["attempts"] >= MAX_AUTH_ATTEMPTS:
        info["blocked_until"] = _now() + timedelta(minutes=AUTH_BLOCK_MINUTES)
        info["attempts"] = 0
        return f"Превышено число попыток. Введите ЛС через {AUTH_BLOCK_MINUTES} мин."
    return f"Лицевой счёт не найден. Осталось попыток: {left}"


def _reset_ls_brute(chat_id: int) -> None:
    _auth_attempts.pop(chat_id, None)


# ── Управление состоянием сессии ──────────────────────────────────────────────

# Ключи, относящиеся к незавершённым флоу. Сбрасываются при возврате в меню.
_FLOW_KEYS = (
    "appeal", "script", "after_ls", "reopen_appeal_id",
    "meters", "meter_idx", "new_value1", "new_value2",
    "appt_branch_id", "appt_date", "appt_time", "appt_theme",
)

# Ключи ввода показаний — сбрасываются при переходе к следующему счётчику.
_METER_INPUT_KEYS = ("new_value1", "new_value2")


def _clear_flow(st: dict) -> None:
    """Сбрасывает состояние всех незавершённых флоу и возвращает в меню."""
    for key in _FLOW_KEYS:
        st.pop(key, None)
    st["state"] = S.MENU


def _reset_meter_input(st: dict) -> None:
    """Сбрасывает введённые значения показаний, ставит ожидание Т1."""
    for key in _METER_INPUT_KEYS:
        st.pop(key, None)
    st["state"] = S.WAITING_VALUE1


def _request_ls(chat_id: int, after: str) -> None:
    """
    Запрашивает ЛС у клиента и запоминает, какое действие выполнить после
    успешной валидации (см. _AFTER_LS_ACTIONS).
    """
    st = _get_state(chat_id)
    st["state"] = S.AWAIT_LS
    st["after_ls"] = after
    _touch(st)
    send_message(chat_id, "Введите номер вашего лицевого счёта:")


# ── Ввод и валидация показаний ────────────────────────────────────────────────

def _parse_reading(chat_id: int, text: str) -> float | None:
    """Парсит показание. Возвращает None и уведомляет клиента, если не число."""
    try:
        return float(text.replace(",", "."))
    except ValueError:
        send_message(chat_id, "Введите числовое значение.")
        return None


def _current_reading(ls: str, meter: dict, col: str, initial_key: str) -> float:
    """
    Текущее показание по колонке col: из последней записи в pokazaniya,
    иначе — начальное значение из справочника счётчиков.
    """
    last = db.get_last_pokazaniya(ls, meter["meter_number"])
    if last and last[col]:
        return float(last[col])
    return float(meter.get(initial_key, "0") or "0")


# ── Флоу подачи обращения (раздел 6.4, шаги 1–4) ────────────────────────────

def _start_appeal(chat_id: int) -> None:
    """Шаг 1: предлагаем выбрать категорию."""
    st = _get_state(chat_id)
    st["state"] = S.APPEAL_CATEGORY
    st.pop("appeal", None)
    _touch(st)

    rows = [[_cb(label, f"cat:{val}")] for label, val in CATEGORIES.items()]
    rows.append([_cb("❌ Отмена", "cancel")])
    send_buttons(chat_id, "Выберите категорию обращения:", rows)


def _appeal_set_category(chat_id: int, category: str) -> None:
    """Шаг 2: категория выбрана — просим описание."""
    st = _get_state(chat_id)
    st["state"] = S.APPEAL_BODY
    st["appeal"] = {"category": category}
    _touch(st)
    send_message(chat_id, "Опишите вашу проблему или вопрос:")


def _appeal_got_body(chat_id: int, text: str) -> None:
    """Шаг 3: описание получено — проверяем ЛС."""
    st = _get_state(chat_id)
    st["appeal"]["body"] = text
    _touch(st)

    ls = _get_saved_ls(chat_id)
    if ls:
        _submit_appeal(chat_id, ls)
    else:
        _request_ls(chat_id, "appeal")


def _appeal_got_ls(chat_id: int, ls_input: str) -> None:
    """Шаг 4: ЛС введён — валидируем и отправляем."""
    block_msg = _check_ls_brute(chat_id)
    if block_msg:
        send_message(chat_id, block_msg)
        return

    if not _validate_ls(ls_input):
        send_message(chat_id, _fail_ls(chat_id))
        send_message(chat_id, "Введите номер лицевого счёта повторно:")
        return

    _reset_ls_brute(chat_id)
    _save_ls(chat_id, ls_input)
    _submit_appeal(chat_id, ls_input)


def _submit_appeal(chat_id: int, ls: str) -> None:
    """Финал флоу: POST /api/v1/appeals → показываем ticket_no."""
    st = _get_state(chat_id)
    appeal = st.get("appeal", {})

    data, err = client_api.create_appeal(
        ls=ls,
        channel="max",
        category=appeal.get("category", "прочее"),
        body=appeal.get("body", ""),
        chat_id=chat_id,
    )

    st["state"] = S.MENU
    st.pop("appeal", None)
    _touch(st)

    if err:
        log.error("create_appeal chat_id=%s err=%s", chat_id, err)
        send_message(chat_id, "⚠️ Сервис временно недоступен. Попробуйте позже.")
    else:
        ticket = data["ticket_no"]
        send_message(chat_id,
            f"✅ Обращение принято!\n"
            f"Номер: {ticket}\n\n"
            f"Мы свяжемся с вами в ближайшее время."
        )
        log.info("Создано обращение %s  chat_id=%s", ticket, chat_id)

    send_main_menu(chat_id)


# ── Флоу «Мои обращения» ─────────────────────────────────────────────────────

def _show_my_appeals(chat_id: int) -> None:
    ls = _get_saved_ls(chat_id)
    if not ls:
        _request_ls(chat_id, "my_appeals")
        return

    data, err = client_api.list_appeals_by_ls(ls)
    if err:
        send_message(chat_id, "⚠️ Сервис временно недоступен. Попробуйте позже.")
        send_main_menu(chat_id)
        return

    appeals = data.get("appeals", [])
    active = [a for a in appeals if a.get("status") not in ("resolved", "closed")]

    if not active:
        send_message(chat_id, "✅ У вас нет активных обращений.")
    else:
        status_labels = {
            "new":                  "🆕 Новое",
            "in_work":              "⚙️ В работе",
            "pending_confirmation": "⏳ На подтверждении",
        }
        lines = ["📋 Ваши активные обращения:\n"]
        for a in active:
            body = a.get("body") or ""
            preview = body[:60] + ("..." if len(body) > 60 else "")
            status = a.get("status", "")
            lines.append(
                f"№ {a.get('ticket_no', '?')} — {status_labels.get(status, status)}\n"
                f"📝 {preview}\n"
                f"📅 {(a.get('created_at') or '')[:16]}\n"
            )
        send_message(chat_id, "\n".join(lines))

    send_main_menu(chat_id)


# ── Движок скриптов (раздел 6.3) ─────────────────────────────────────────────

def _show_scripts_list(chat_id: int) -> None:
    """Загружает список активных скриптов и показывает их кнопками."""
    scripts, err = client_api.list_scripts()
    if err or not scripts:
        if err:
            send_message(chat_id, "⚠️ Сервис временно недоступен.")
        else:
            send_message(chat_id, "📚 Раздел FAQ пуст.")
        send_main_menu(chat_id)
        return

    st = _get_state(chat_id)
    st["state"] = S.SCRIPT_LIST
    _touch(st)

    rows = [[_cb(s["title"], f"script:{s['id']}")] for s in scripts]
    rows.append([_cb("🏠 Главное меню", "main_menu")])
    send_buttons(chat_id, "📚 Выберите тему:", rows)


def _open_script(chat_id: int, script_id: int) -> None:
    """Загружает дерево скрипта и показывает корневой узел."""
    tree, err = client_api.get_script_tree(script_id)
    if err or not tree:
        send_message(chat_id, "⚠️ Не удалось загрузить скрипт.")
        send_main_menu(chat_id)
        return

    # Строим словари для быстрого доступа
    nodes = {n["id"]: n for n in tree.get("nodes", [])}
    edges_by_from: dict[int, list] = {}
    for e in tree.get("edges", []):
        edges_by_from.setdefault(e["from_node_id"], []).append(e)

    if not nodes:
        send_message(chat_id, "Скрипт пуст.")
        send_main_menu(chat_id)
        return

    # Корневой узел = узел без входящих рёбер.
    # min() вместо roots[0] — детерминированный выбор, если корней несколько.
    all_to = {e["to_node_id"] for e in tree.get("edges", [])}
    roots = [nid for nid in nodes if nid not in all_to]
    if len(roots) > 1:
        log.warning("Скрипт id=%s: найдено %d корневых узлов, берём минимальный",
                    script_id, len(roots))
    root_id = min(roots) if roots else min(nodes)

    st = _get_state(chat_id)
    st["state"] = S.SCRIPT_NODE
    st["script"] = {
        "nodes":         nodes,
        "edges_by_from": edges_by_from,
        "current":       root_id,
    }
    _touch(st)
    _show_script_node(chat_id)


def _show_script_node(chat_id: int) -> None:
    """Отображает текущий узел скрипта."""
    st = _get_state(chat_id)
    script = st.get("script", {})
    nodes         = script.get("nodes", {})
    edges_by_from = script.get("edges_by_from", {})
    current_id    = script.get("current")

    node = nodes.get(current_id)
    if not node:
        send_message(chat_id, "Скрипт завершён.")
        send_main_menu(chat_id)
        return

    text = node["title"]
    edges = edges_by_from.get(current_id, [])

    if node.get("is_terminal") or not edges:
        # Конечный узел — показываем текст и возвращаем в меню
        send_message(chat_id, f"📌 {text}")
        st["state"] = S.MENU
        st.pop("script", None)
        _touch(st)
        send_main_menu(chat_id, "Выберите следующее действие:")
    else:
        rows = [[_cb(e["label"], f"script_node:{e['to_node_id']}")] for e in edges]
        rows.append([_cb("🏠 Главное меню", "main_menu")])
        send_buttons(chat_id, f"📌 {text}", rows)


def _navigate_script_node(chat_id: int, node_id: int) -> None:
    """Переходит к указанному узлу скрипта."""
    st = _get_state(chat_id)
    if "script" not in st:
        send_main_menu(chat_id)
        return
    st["script"]["current"] = node_id
    _touch(st)
    _show_script_node(chat_id)


# ── Показания (адаптировано из предыдущей версии) ────────────────────────────

def _start_pokazaniya(chat_id: int) -> None:
    ls = _get_saved_ls(chat_id)
    if not ls:
        _request_ls(chat_id, "pokazaniya")
        return
    _show_meter_select(chat_id, ls)


def _show_meter_select(chat_id: int, ls: str) -> None:
    meters = db.get_schetchiki(ls)
    if not meters:
        send_message(chat_id, "По вашему счёту счётчики не найдены.")
        send_main_menu(chat_id)
        return

    meters_list = [dict(m) for m in meters]
    st = _get_state(chat_id)
    st["state"] = S.METER_SELECT
    st["meters"] = meters_list
    _touch(st)

    rows = []
    for i, m in enumerate(meters_list):
        suffix = " (2Т)" if m["meter_type"] == "Двухтарифный" else ""
        rows.append([_cb(f"{m['resource_type']} №{m['meter_number']}{suffix}",
                         f"meter:{i}")])
    rows.append([_cb("🏠 Главное меню", "main_menu")])
    send_buttons(chat_id, "Выберите счётчик:", rows)


def _ask_meter_value(chat_id: int) -> None:
    st = _get_state(chat_id)
    meters  = st["meters"]
    idx     = st["meter_idx"]
    meter   = meters[idx]

    # Какое поле запрашиваем — определяется состоянием, а не отдельным флагом
    waiting_v2 = st.get("state") == S.WAITING_VALUE2

    resource = meter["resource_type"]
    number   = meter["meter_number"]
    is_two   = meter["meter_type"] == "Двухтарифный"

    last = db.get_last_pokazaniya(st["ls"], number)
    if last:
        if is_two and last["value2"]:
            current_info = f"Текущие: Т1={last['value1']}, Т2={last['value2']}"
        else:
            current_info = f"Текущее показание: {last['value1']}"
        current_info += f" (от {(last['created_at'] or '')[:10]})"
    else:
        initial = meter.get("initial2" if waiting_v2 else "initial1", "0")
        current_info = f"Начальное: {initial}"

    if waiting_v2:
        prompt = f"{resource} №{number}\n{current_info}\nВведите Т2 (ночь):"
    elif is_two:
        prompt = f"{resource} №{number} (двухтарифный)\n{current_info}\nВведите Т1 (день):"
    else:
        prompt = f"{resource} №{number}\n{current_info}\nВведите показание:"

    send_message(chat_id, prompt)


def _confirm_meter_reading(chat_id: int) -> None:
    st = _get_state(chat_id)
    meter = st["meters"][st["meter_idx"]]
    v1 = st.get("new_value1")
    v2 = st.get("new_value2")

    if meter["meter_type"] == "Двухтарифный":
        summary = f"{meter['resource_type']} №{meter['meter_number']}: Т1={v1}, Т2={v2}"
    else:
        summary = f"{meter['resource_type']} №{meter['meter_number']}: {v1}"

    st["state"] = S.CONFIRM_POKAZANIYA
    _touch(st)
    send_buttons(chat_id,
        f"Проверьте показания:\n{summary}",
        [
            [_cb("✅ Подтвердить", "meter_confirm")],
            [_cb("✏️ Скорректировать", "meter_retry")],
        ]
    )


# ── Запись на приём (раздел 6-7 ТЗ) ──────────────────────────────────────────

def _start_appointment_flow(chat_id: int, ls: str | None = None) -> None:
    """
    Точка входа флоу записи на приём.
    Если у клиента уже есть активная запись — показываем её вместо выбора
    филиала (REQ-КЛ-06-09), с возможностью отменить.
    """
    ls = ls or _get_saved_ls(chat_id)
    if not ls:
        _request_ls(chat_id, "appointment")
        return

    existing = db.get_active_appointment(ls)
    if existing:
        _show_active_appointment(chat_id, existing)
        return

    _show_branch_select(chat_id, ls)


def _show_active_appointment(chat_id: int, appointment) -> None:
    """Показывает текущую активную запись клиента с возможностью отмены."""
    st = _get_state(chat_id)
    st["state"] = S.MENU
    _touch(st)

    theme_line = f"\nТема: {appointment['theme']}" if appointment["theme"] else ""
    send_buttons(
        chat_id,
        f"У вас уже есть активная запись на приём:\n\n"
        f"📍 {appointment['branch_name']}\n"
        f"🏠 {appointment['branch_address']}\n"
        f"📅 {appointment['slot_date']}  🕐 {appointment['slot_time']}"
        f"{theme_line}",
        [
            [_cb("❌ Отменить запись", f"appt_cancel:{appointment['id']}")],
            [_cb("🏠 Главное меню", "main_menu")],
        ]
    )


def _show_branch_select(chat_id: int, ls: str) -> None:
    """Шаг 1: выбор филиала."""
    branches = db.get_branches()
    if not branches:
        send_message(chat_id, "На данный момент запись на приём недоступна.")
        send_main_menu(chat_id)
        return

    st = _get_state(chat_id)
    st["state"] = S.APPOINTMENT_BRANCH
    st["ls"] = ls
    _touch(st)

    rows = [[_cb(f"📍 {b['name']}", f"appt_branch:{b['id']}")] for b in branches]
    rows.append([_cb("❌ Отмена", "main_menu")])
    send_buttons(chat_id, "Выберите филиал:", rows)


def _show_date_select(chat_id: int, branch_id: int) -> None:
    """Шаг 2: выбор даты."""
    dates = db.get_available_dates(branch_id)
    if not dates:
        send_message(chat_id, "На выбранный филиал сейчас нет свободных дат для записи.")
        send_main_menu(chat_id)
        return

    branch = db.get_branch(branch_id)
    st = _get_state(chat_id)
    st["state"] = S.APPOINTMENT_DATE
    st["appt_branch_id"] = branch_id
    _touch(st)

    # Показываем не более 10 ближайших дат одним списком кнопок
    rows = [[_cb(_format_date_label(d), f"appt_date:{d}")] for d in dates[:10]]
    rows.append([_cb("❌ Отмена", "main_menu")])
    send_buttons(chat_id, f"Филиал: {branch['name']}\nВыберите дату:", rows)


_WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _format_date_label(date_str: str) -> str:
    """Форматирует 'YYYY-MM-DD' в читаемую метку с днём недели."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.strftime('%d.%m')} ({_WEEKDAYS_SHORT[d.weekday()]})"


def _show_time_select(chat_id: int, branch_id: int, slot_date: str) -> None:
    """Шаг 3: выбор времени."""
    slots = db.get_available_slots(branch_id, slot_date)
    if not slots:
        send_message(chat_id, "На эту дату свободных слотов не осталось. Выберите другую дату.")
        _show_date_select(chat_id, branch_id)
        return

    st = _get_state(chat_id)
    st["state"] = S.APPOINTMENT_TIME
    st["appt_date"] = slot_date
    _touch(st)

    # Кнопки временем, по 3 в ряд для компактности
    rows = []
    row = []
    for i, slot in enumerate(slots, 1):
        row.append(_cb(slot, f"appt_time:{slot}"))
        if i % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_cb("❌ Отмена", "main_menu")])
    send_buttons(chat_id, f"Дата: {_format_date_label(slot_date)}\nВыберите время:", rows)


def _ask_theme(chat_id: int, slot_time: str) -> None:
    """Шаг 4: тема обращения (опционально, REQ-КЛ-06-08)."""
    st = _get_state(chat_id)
    st["state"] = S.APPOINTMENT_THEME
    st["appt_time"] = slot_time
    _touch(st)

    send_buttons(
        chat_id,
        "Укажите тему обращения (необязательно) — так сотрудник сможет заранее подготовиться.\n\n"
        "Напишите тему текстом или нажмите «Пропустить».",
        [[_cb("⏭ Пропустить", "appt_skip_theme")]]
    )


def _show_appointment_confirm(chat_id: int) -> None:
    """Шаг 5: подтверждение перед сохранением."""
    st = _get_state(chat_id)
    branch = db.get_branch(st["appt_branch_id"])
    theme = st.get("appt_theme")
    theme_line = f"\nТема: {theme}" if theme else ""

    st["state"] = S.APPOINTMENT_CONFIRM
    _touch(st)

    send_buttons(
        chat_id,
        f"Проверьте данные записи:\n\n"
        f"📍 {branch['name']}\n"
        f"🏠 {branch['address']}\n"
        f"📅 {_format_date_label(st['appt_date'])}\n"
        f"🕐 {st['appt_time']}"
        f"{theme_line}",
        [
            [_cb("✅ Подтвердить запись", "appt_confirm")],
            [_cb("❌ Отмена", "main_menu")],
        ]
    )


def _finalize_appointment(chat_id: int) -> None:
    """Финал флоу: сохраняем запись в БД."""
    st = _get_state(chat_id)
    aid, err = db.create_appointment(
        ls=st["ls"],
        branch_id=st["appt_branch_id"],
        slot_date=st["appt_date"],
        slot_time=st["appt_time"],
        channel="max",
        chat_id=chat_id,
        theme=st.get("appt_theme"),
    )

    ls = st.get("ls")
    _clear_flow(st)
    _touch(st)

    if err:
        send_message(chat_id, f"⚠️ {err}")
        log.warning("Запись не создана: ls=%s  ошибка=%s", ls, err)
    else:
        send_message(chat_id, "✅ Вы успешно записаны на приём! Напомним о визите заранее.")
        log.info("Запись создана: id=%s  chat_id=%s", aid, chat_id)

    send_main_menu(chat_id)


def _cancel_own_appointment(chat_id: int, appointment_id: int) -> None:
    """Клиент отменяет свою запись (REQ-КЛ-06-07)."""
    appointment = db.get_appointment(appointment_id)
    if not appointment or appointment["status"] != "active":
        send_message(chat_id, "Запись не найдена или уже отменена.")
        send_main_menu(chat_id)
        return

    db.cancel_appointment(appointment_id, "client", "Отменено клиентом через бот")
    send_message(chat_id, "Запись отменена.")
    send_main_menu(chat_id)


# ── PDF квитанция ─────────────────────────────────────────────────────────────

def _deliver_kvitanciya(chat_id: int, ls: str) -> None:
    """Отправляет квитанцию и возвращает клиента в главное меню."""
    send_message(chat_id, "Ищу квитанцию, подождите...")
    _send_pdf(chat_id, ls)
    send_main_menu(chat_id)


def _send_pdf(chat_id: int, ls: str) -> None:
    pdf_path = os.path.join("KV", f"{ls}.pdf")
    if not os.path.exists(pdf_path):
        send_message(chat_id, f"Квитанция для ЛС {ls} не найдена.")
        return
    try:
        r1 = httpx.post(f"{API}/uploads", headers=_MAX_HEADERS,
                        params={"type": "file"}, timeout=10)
        r1.raise_for_status()
        upload_url = r1.json()["url"]

        with open(pdf_path, "rb") as f:
            r2 = httpx.post(upload_url, headers=_MAX_HEADERS,
                            files={"data": (f"{ls}.pdf", f, "application/pdf")},
                            timeout=30)
        r2.raise_for_status()
        token = r2.json()["token"]

        # MAX требует паузу между загрузкой файла и отправкой сообщения с ним
        time.sleep(2)
        _send_raw(chat_id, {
            "text": f"Квитанция по ЛС {ls}:",
            "attachments": [{"type": "file", "payload": {"token": token}}],
        })
        log.info("PDF отправлен: ЛС=%s  chat_id=%s", ls, chat_id)
    except httpx.HTTPStatusError as exc:
        log.error("PDF: MAX API вернул %s для ЛС=%s", exc.response.status_code, ls)
        send_message(chat_id, "Не удалось отправить квитанцию. Попробуйте позже.")
    except (KeyError, ValueError) as exc:
        log.error("PDF: неожиданный ответ MAX API для ЛС=%s: %s", ls, exc)
        send_message(chat_id, "Не удалось отправить квитанцию. Попробуйте позже.")
    except Exception as exc:
        log.error("PDF ошибка: ЛС=%s  %s", ls, exc)
        send_message(chat_id, "Не удалось отправить квитанцию. Попробуйте позже.")


# ── Обработчик callback-кнопок ────────────────────────────────────────────────

def _ack_callback(callback_id: str) -> None:
    """Убирает «часики» на нажатой кнопке. Ошибка не критична."""
    try:
        httpx.post(f"{API}/answers", headers=_MAX_HEADERS,
                   json={"callback_id": callback_id, "notification": ""},
                   timeout=3)
    except Exception as exc:
        log.debug("_ack_callback %s: %s", callback_id, exc)


# -- Обработчики callback с аргументом (payload вида "префикс:значение") -------

def _cb_confirm_appeal(chat_id: int, st: dict, arg: str) -> None:
    """Клиент подтверждает закрытие обращения."""
    data, err = client_api.confirm_appeal(int(arg), "max", chat_id)
    if err:
        send_message(chat_id, f"⚠️ Не удалось подтвердить закрытие: {err}")
    else:
        send_message(chat_id, f"✅ Обращение №{data['ticket_no']} закрыто.\nСпасибо!")
    send_main_menu(chat_id)


def _cb_reopen_appeal(chat_id: int, st: dict, arg: str) -> None:
    """Клиент хочет вернуть обращение в работу — спрашиваем причину."""
    st["state"] = S.REOPEN_COMMENT
    st["reopen_appeal_id"] = int(arg)
    _touch(st)
    send_message(chat_id, "Опишите, пожалуйста, причину возврата обращения в работу:")


def _cb_select_meter(chat_id: int, st: dict, arg: str) -> None:
    """Выбор счётчика — начинаем ввод с Т1."""
    st["meter_idx"] = int(arg)
    _reset_meter_input(st)
    _touch(st)
    _ask_meter_value(chat_id)


def _cb_select_date(chat_id: int, st: dict, arg: str) -> None:
    """Выбор даты записи на приём."""
    branch_id = st.get("appt_branch_id")
    if not branch_id:
        send_main_menu(chat_id)
        return
    _show_time_select(chat_id, branch_id, arg)


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
    ls = _get_saved_ls(chat_id)
    if not ls:
        _request_ls(chat_id, "kvitanciya")
    else:
        _deliver_kvitanciya(chat_id, ls)


def _cb_skip_theme(chat_id: int, st: dict) -> None:
    st["appt_theme"] = None
    _touch(st)
    _show_appointment_confirm(chat_id)


def _cb_appt_confirm(chat_id: int, st: dict) -> None:
    if st.get("state") == S.APPOINTMENT_CONFIRM:
        _finalize_appointment(chat_id)


def _cb_main_menu(chat_id: int, st: dict) -> None:
    _clear_flow(st)
    _touch(st)
    send_main_menu(chat_id)


def _cb_meter_confirm(chat_id: int, st: dict) -> None:
    """
    Показания подтверждены — сохраняем и возвращаемся к списку счётчиков,
    чтобы клиент сам решил, вводить ли ещё один счётчик или закончить
    (кнопка «🏠 Главное меню» есть в списке выбора).
    """
    if st.get("state") != S.CONFIRM_POKAZANIYA:
        return

    meter = st["meters"][st["meter_idx"]]
    db.add_pokazaniya(
        chat_id, st["ls"], meter["resource_type"], meter["meter_number"],
        st.get("new_value1"), st.get("new_value2"),
    )
    log.info("Показания приняты: ЛС=%s  счётчик=%s  chat_id=%s",
             st["ls"], meter["meter_number"], chat_id)

    ls = st["ls"]
    _clear_flow(st)
    _touch(st)

    send_message(chat_id, f"✅ Показания по счётчику {meter['resource_type']} №{meter['meter_number']} приняты!")
    _show_meter_select(chat_id, ls)


def _cb_meter_retry(chat_id: int, st: dict) -> None:
    """Клиент решил ввести показания заново по текущему счётчику."""
    if st.get("state") != S.CONFIRM_POKAZANIYA:
        return
    _reset_meter_input(st)
    _touch(st)
    _ask_meter_value(chat_id)


_CALLBACK_STATIC: dict[str, callable] = {
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


def _on_await_ls(chat_id: int, st: dict, text: str) -> None:
    """Клиент ввёл ЛС — валидируем и выполняем отложенное действие."""
    block_msg = _check_ls_brute(chat_id)
    if block_msg:
        send_message(chat_id, block_msg)
        return

    if not _validate_ls(text):
        send_message(chat_id, _fail_ls(chat_id))
        send_message(chat_id, "Введите номер лицевого счёта повторно:")
        return

    _reset_ls_brute(chat_id)
    _save_ls(chat_id, text)

    after = st.pop("after_ls", None)
    st["state"] = S.MENU
    _touch(st)

    action = _AFTER_LS_ACTIONS.get(after)
    if action:
        action(chat_id, text)
    else:
        # Отложенное действие не задано — просто подтверждаем и показываем меню
        log.warning("after_ls не задан или неизвестен: %r  chat_id=%s", after, chat_id)
        send_message(chat_id, "✅ Лицевой счёт сохранён.")
        send_main_menu(chat_id)


def _on_reopen_comment(chat_id: int, st: dict, text: str) -> None:
    """Клиент написал причину возврата обращения в работу."""
    appeal_id = st.pop("reopen_appeal_id", None)
    st["state"] = S.MENU
    _touch(st)

    if appeal_id:
        data, err = client_api.reopen_appeal(appeal_id, text)
        if err:
            send_message(chat_id, f"⚠️ Не удалось вернуть обращение: {err}")
        else:
            send_message(chat_id,
                f"↩️ Обращение №{data['ticket_no']} возвращено в работу.\n"
                f"Ваш комментарий: {text}"
            )
    send_main_menu(chat_id)


def _on_appointment_theme(chat_id: int, st: dict, text: str) -> None:
    """Клиент ввёл тему приёма текстом вместо кнопки «Пропустить»."""
    st["appt_theme"] = text[:200]   # защита от чрезмерно длинного текста
    _touch(st)
    _show_appointment_confirm(chat_id)


def _on_value1(chat_id: int, st: dict, text: str) -> None:
    """Ввод Т1 (или единственного показания для однотарифного счётчика)."""
    meter = st["meters"][st["meter_idx"]]
    val = _parse_reading(chat_id, text)
    if val is None:
        return

    current = _current_reading(st.get("ls", ""), meter, "value1", "initial1")
    if val < current:
        send_message(chat_id,
            f"Показание {val} не может быть меньше текущего {current}.\n"
            f"Введите корректное значение:")
        return

    st["new_value1"] = str(val)

    if meter["meter_type"] == "Двухтарифный":
        # Переходим к вводу Т2 — состояние определяет, какое поле ждём
        st["state"] = S.WAITING_VALUE2
        _touch(st)
        _ask_meter_value(chat_id)
    else:
        st["new_value2"] = None
        _touch(st)
        _confirm_meter_reading(chat_id)


def _on_value2(chat_id: int, st: dict, text: str) -> None:
    """Ввод Т2 для двухтарифного счётчика."""
    meter = st["meters"][st["meter_idx"]]
    val = _parse_reading(chat_id, text)
    if val is None:
        return

    current = _current_reading(st.get("ls", ""), meter, "value2", "initial2")
    if val < current:
        send_message(chat_id,
            f"Показание Т2 {val} не может быть меньше текущего {current}.\n"
            f"Введите корректное значение:")
        return

    st["new_value2"] = str(val)
    _touch(st)
    _confirm_meter_reading(chat_id)


# Состояние сессии → обработчик текстового ввода
_MESSAGE_HANDLERS: dict[str, callable] = {
    S.APPEAL_BODY:       lambda chat_id, st, text: _appeal_got_body(chat_id, text),
    S.AWAIT_LS:          _on_await_ls,
    S.REOPEN_COMMENT:    _on_reopen_comment,
    S.APPOINTMENT_THEME: _on_appointment_theme,
    S.WAITING_VALUE1:    _on_value1,
    S.WAITING_VALUE2:    _on_value2,
}

_RESET_COMMANDS = ("/start", "/help", "/menu")


def handle_message(message: dict) -> None:
    chat_id = message["recipient"]["chat_id"]
    text    = (message.get("body") or {}).get("text", "").strip()

    log.debug("msg chat_id=%s text='%s'", chat_id, text[:50])

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


def _task_sync_readings_to_1c() -> None:
    """
    Задача APScheduler: синхронизация показаний в 1С.
    Запускается каждые SYNC_INTERVAL_MINUTES минут.

    TODO Этап 4 (ожидает миграции схемы):
      1. Добавить колонку sent_to_1c INTEGER DEFAULT 0 в pokazaniya
         (см. TODO в database.py, функция cleanup_old_cache).
      2. Реализовать client_1c.send_readings(rows) — POST к 1С HTTP-сервису.
      3. После успешной отправки: UPDATE pokazaniya SET sent_to_1c=1 WHERE id IN (...).
    До завершения миграции функция является именованной заглушкой —
    регистрируется в scheduler, выполняется без эффекта, видна в get_jobs().
    """
    log.debug(
        "_task_sync_readings_to_1c: ожидает добавления колонки sent_to_1c "
        "в таблицу pokazaniya (Этап 4 миграция схемы)"
    )


def _format_appointment_reminder(appointment, when_label: str) -> str:
    """Единый текст напоминания для обоих типов (24ч / день приёма)."""
    theme_line = f"\nТема: {appointment['theme']}" if appointment["theme"] else ""
    return (
        f"⏰ Напоминаем: {when_label} у вас запись на приём.\n\n"
        f"📍 {appointment['branch_name']}\n"
        f"🏠 {appointment['branch_address']}\n"
        f"📅 {appointment['slot_date']}  🕐 {appointment['slot_time']}"
        f"{theme_line}"
    )


def _task_appointment_reminder_24h() -> None:
    """
    Задача APScheduler: напоминание за 24ч до приёма (REQ-АВТ-07-06).
    Запускается раз в час — этого достаточно, т.к. окно выборки в БД
    (23–25ч от текущего момента) шире часового шага, дублей не будет
    благодаря флагу reminded_24h.

    Ошибка доставки не блокирует простановку флага (см. notifier.py —
    тот же принцип: не удалось отправить — логируем и идём дальше,
    а не ретраим бесконечно на каждом следующем прогоне).
    """
    try:
        appointments = db.get_appointments_for_reminder_24h()
    except Exception as exc:
        log.error("APScheduler appointment_reminder_24h: ошибка чтения БД: %s", exc)
        return

    for appt in appointments:
        ok = send_message(
            appt["chat_id"],
            _format_appointment_reminder(appt, "завтра"),
        )
        if not ok:
            log.warning(
                "Напоминание за 24ч не доставлено: appointment_id=%s  chat_id=%s",
                appt["id"], appt["chat_id"],
            )
        db.mark_reminded(appt["id"], "24h")

    if appointments:
        log.info("APScheduler appointment_reminder_24h: обработано %d записей", len(appointments))


def _task_appointment_reminder_day() -> None:
    """
    Задача APScheduler: напоминание в день приёма (REQ-АВТ-07-07).
    Запускается ежедневно в 09:00 по московскому времени (cron).
    """
    try:
        appointments = db.get_appointments_for_reminder_day()
    except Exception as exc:
        log.error("APScheduler appointment_reminder_day: ошибка чтения БД: %s", exc)
        return

    for appt in appointments:
        ok = send_message(
            appt["chat_id"],
            _format_appointment_reminder(appt, "сегодня"),
        )
        if not ok:
            log.warning(
                "Напоминание в день приёма не доставлено: appointment_id=%s  chat_id=%s",
                appt["id"], appt["chat_id"],
            )
        db.mark_reminded(appt["id"], "day")

    if appointments:
        log.info("APScheduler appointment_reminder_day: обработано %d записей", len(appointments))


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
        except httpx.TimeoutException:
            log.debug("poll timeout — норма")
        except Exception as exc:
            log.error("poll error: %s", exc)
            time.sleep(3)


# ── Точка входа ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()

    # ── APScheduler ───────────────────────────────────────────────────────────
    scheduler = BackgroundScheduler(timezone="Europe/Moscow")

    scheduler.add_job(
        _task_auto_resolve_pending,
        trigger="interval",
        hours=1,
        id="auto_resolve_pending",
        max_instances=1,        # не запускать параллельно если предыдущий ещё работает
        misfire_grace_time=300, # до 5 мин опоздания — всё равно выполнить
    )
    scheduler.add_job(
        _task_cleanup_user_states,
        trigger="interval",
        hours=1,
        id="cleanup_user_states",
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _task_sync_readings_to_1c,
        trigger="interval",
        minutes=SYNC_INTERVAL_MINUTES,
        id="sync_readings_to_1c",
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        _task_appointment_reminder_24h,
        trigger="interval",
        hours=1,
        id="appointment_reminder_24h",
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _task_appointment_reminder_day,
        trigger="cron",
        hour=9,
        minute=0,
        timezone="Europe/Moscow",
        id="appointment_reminder_day",
        max_instances=1,
        misfire_grace_time=1800,  # до 30 мин опоздания (например, после рестарта сервера утром)
    )
    # Задача активируется в Этапе 4 после добавления колонки sent_to_1c:
    # scheduler.add_job(
    #     db.cleanup_old_cache,
    #     trigger="interval",
    #     hours=24,
    #     id="cleanup_old_cache",
    #     kwargs={"days": 90},
    #     max_instances=1,
    # )

    scheduler.start()
    log.info(
        "APScheduler запущен: %d задач — %s",
        len(scheduler.get_jobs()),
        [j.id for j in scheduler.get_jobs()],
    )

    # ── Основной цикл (блокирует главный поток) ───────────────────────────────
    try:
        poll()
    except (KeyboardInterrupt, SystemExit):
        log.info("Получен сигнал завершения")
    finally:
        scheduler.shutdown(wait=False)
        log.info("APScheduler остановлен, бот завершён")
