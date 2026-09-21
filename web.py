"""
web.py — Flask операторский портал РСО Портал, Горизонт 1 Этап 5.

Изменения относительно предыдущей версии:
  - Переход на новую схему appeals (ticket_no, appeal_responses)
  - Чтение через database.* напрямую; мутации через client_api (→ FastAPI → уведомления)
  - operator_id добавлен в сессию
  - Дашборд обновлён: resolved + pending_confirmation
  - Карточка обращения: история из appeal_responses вместо comments
  - appeals_legacy доступна через /legacy (только просмотр, для истории)

Запуск:
    python web.py            (разработка)
    gunicorn web:app         (production, добавить в systemd)
"""
from __future__ import annotations

# ── Сертификаты Минцифры (platform-api2.max.ru) ──────────────────────────────
# См. подробный комментарий в bot.py. Здесь нужно до импорта client_api
# (который тянет httpx) и до локальных `import httpx` внутри broadcast()/
# appointment_cancel().
import truststore

truststore.inject_into_ssl()

import json
import logging
import os
import secrets
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from logging.handlers import RotatingFileHandler

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

import client_api
import database as db
from config import (
    API,
    DB_PATH,
    ENABLE_1C_INTEGRATION,
    LOG_BACKUP_COUNT,
    LOG_FILE,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    OPERATOR_CHAT_IMAGE_DIR,
    SECRET_KEY,
    TOKEN,
    YANDEXGPT_API_KEY,
    YANDEXGPT_FOLDER_ID,
)
from rso_bot import operator_chat

# ── Логгер ────────────────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("rso.web")
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
            LOG_FILE.replace(".log", "_web.log"),
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

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


@app.get("/healthz")
def healthz():
    """Lightweight health-check for the local reverse proxy and Docker."""
    return {"status": "ok"}

# ── Константы ─────────────────────────────────────────────────────────────────

STATUSES = {
    "new":                  "Новая",
    "in_work":              "В работе",
    "pending_confirmation": "На подтверждении",
    "resolved":             "Решена",
    "closed":               "Закрыта",
}

STATUS_COLORS = {
    "new":                  "error",
    "in_work":              "warning",
    "pending_confirmation": "pending",
    "resolved":             "success",
    "closed":               "muted",
}

CATEGORIES = {
    "заявка":  "Заявка на обслуживание",
    "авария":  "Аварийная ситуация",
    "качество": "Качество услуг",
    "прочее":  "Прочее",
}

CHANNELS = {
    "max":      "MAX",
    "telegram": "Telegram",
    "lk":       "Личный кабинет",
    "widget":   "Виджет",
}

# ── Брутфорс-защита логина ────────────────────────────────────────────────────

_login_attempts: dict = defaultdict(lambda: {"attempts": 0, "blocked_until": None})
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_BLOCK_MINUTES = 15
_ACCOUNTS_PAGE_SIZE = 50
_CSRF_SESSION_KEY = "_csrf_token"


def _check_login_block(ip: str) -> str | None:
    info = _login_attempts[ip]
    if info["blocked_until"] and datetime.now() < info["blocked_until"]:
        remaining = int((info["blocked_until"] - datetime.now()).total_seconds() / 60) + 1
        return f"Слишком много попыток. Попробуйте через {remaining} мин."
    return None


def _fail_login(ip: str) -> str:
    info = _login_attempts[ip]
    info["attempts"] += 1
    left = _MAX_LOGIN_ATTEMPTS - info["attempts"]
    if info["attempts"] >= _MAX_LOGIN_ATTEMPTS:
        info["blocked_until"] = datetime.now() + timedelta(minutes=_LOGIN_BLOCK_MINUTES)
        info["attempts"] = 0
        return f"Превышено число попыток. Вход заблокирован на {_LOGIN_BLOCK_MINUTES} мин."
    return f"Неверный логин или пароль. Осталось попыток: {left}"


def _ok_login(ip: str) -> None:
    _login_attempts.pop(ip, None)


# ── Декораторы доступа ────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _validated_session_user()
        if user is None:
            return redirect(url_for("login"))
        if user["must_change_password"] and request.endpoint != "change_own_password":
            return redirect(url_for("change_own_password"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _validated_session_user()
        if user is None:
            return redirect(url_for("login"))
        if user["must_change_password"]:
            return redirect(url_for("change_own_password"))
        if user["role"] != "admin":
            flash("Доступ запрещён — требуются права администратора", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


def operator_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _validated_session_user()
        if user is None:
            return redirect(url_for("login"))
        if user["must_change_password"]:
            return redirect(url_for("change_own_password"))
        if user["role"] != "operator":
            abort(403)
        return f(*args, **kwargs)
    return decorated


def csrf_protected(f):
    """Require a session-bound token for browser form mutations."""

    @wraps(f)
    def decorated(*args, **kwargs):
        expected = session.get(_CSRF_SESSION_KEY)
        submitted = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
        if (
            not isinstance(expected, str)
            or not expected
            or not secrets.compare_digest(expected, submitted)
        ):
            abort(400)
        return f(*args, **kwargs)

    return decorated


def _validated_session_user():
    """Сверяет cookie-сессию с текущим пользователем и версией в БД."""
    saved = session.get("user")
    if not saved:
        return None
    current = db.get_user_by_id(saved.get("id"))
    if (
        current is None
        or current["username"] != saved.get("username")
        or current["session_version"] != saved.get("session_version")
    ):
        session.clear()
        return None
    session["user"].update(
        role=current["role"],
        name=current["name"],
        must_change_password=bool(current["must_change_password"]),
    )
    return current


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _operator_id() -> int | None:
    """ID текущего оператора из сессии (для передачи в FastAPI)."""
    return session.get("user", {}).get("id")


def _csrf_token() -> str:
    token = session.get(_CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[_CSRF_SESSION_KEY] = token
    return token


def _enrich_appeals(rows) -> list[dict]:
    """Дополняет список обращений читаемыми метками."""
    result = []
    for r in rows:
        d = dict(r)
        d["status_label"]   = STATUSES.get(d.get("status", ""), d.get("status", ""))
        d["status_color"]   = STATUS_COLORS.get(d.get("status", ""), "muted")
        d["category_label"] = CATEGORIES.get(d.get("category", ""), d.get("category", ""))
        d["channel_label"]  = CHANNELS.get(d.get("channel", ""), d.get("channel", ""))
        result.append(d)
    return result


# ── Авторизация ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr

    block_msg = _check_login_block(ip)
    if block_msg:
        flash(block_msg, "error")
        return render_template("login.html")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.get_user(username)

        if user and check_password_hash(user["password"], password):
            _ok_login(ip)
            session.clear()
            session["user"] = {
                "id":       user["id"],      # нужен для operator_id в API
                "username": user["username"],
                "name":     user["name"],
                "role":     user["role"],
                "session_version": user["session_version"],
                "must_change_password": bool(user["must_change_password"]),
            }
            log.info("Вход: %s  ip=%s", username, ip)
            if user["must_change_password"]:
                return redirect(url_for("change_own_password"))
            return redirect(url_for("index"))

        msg = _fail_login(ip)
        flash(msg, "error")
        log.warning("Неудачный вход: %s  ip=%s", username, ip)

    return render_template("login.html")


@app.route("/logout")
def logout():
    user = session.get("user", {})
    session.clear()
    log.info("Выход: %s", user.get("username", "?"))
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_own_password():
    """Обязательная смена bootstrap-пароля текущим пользователем."""
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 12:
            flash("Пароль должен содержать не менее 12 символов", "error")
        else:
            user_id = session["user"]["id"]
            db.change_password(user_id, password)
            session.clear()
            flash("Пароль изменён. Войдите снова.", "success")
            return redirect(url_for("login"))
    return render_template("change_password.html", user=session["user"])


# ── Дашборд ───────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    counts = db.get_counts()
    return render_template(
        "dashboard.html",
        counts=counts,
        statuses=STATUSES,
        status_colors=STATUS_COLORS,
        user=session["user"],
    )


# ── ИИ-помощник ─────────────────────────────────────────────────────────────────────────

@app.route("/ai-settings", methods=["GET", "POST"])
@admin_required
def ai_settings_page():
    if request.method == "POST":
        expected = session.get(_CSRF_SESSION_KEY)
        submitted = request.form.get("csrf_token", "")
        if not isinstance(expected, str) or not expected or not secrets.compare_digest(expected, submitted):
            abort(400)
        try:
            db.update_ai_settings(
                enabled=request.form.get("enabled") == "on",
                model=request.form.get("model", ""),
                system_prompt=request.form.get("system_prompt", ""),
                daily_limit=int(request.form.get("daily_limit", "")),
                temperature=float(request.form.get("temperature", "")),
                max_output_tokens=int(request.form.get("max_output_tokens", "")),
            )
        except (ValueError, TypeError) as exc:
            flash(str(exc) or "Проверьте значения настроек", "error")
        except sqlite3.Error:
            log.exception("Не удалось сохранить настройки ИИ-помощника")
            flash("Не удалось сохранить настройки", "error")
        else:
            flash("Настройки ИИ-помощника сохранены", "success")
            return redirect(url_for("ai_settings_page"))
    return render_template(
        "ai_settings.html",
        settings=db.get_ai_settings(),
        api_key_configured=bool(YANDEXGPT_API_KEY),
        folder_configured=bool(YANDEXGPT_FOLDER_ID),
        csrf_token=_csrf_token(),
        user=session["user"],
    )


# ── Очередь обращений ─────────────────────────────────────────────────────────

@app.route("/appeals")
@login_required
def appeals_list():
    status_filter   = request.args.get("status")   or None
    category_filter = request.args.get("category") or None
    priority_filter = request.args.get("priority") or None
    date_from       = request.args.get("date_from") or None
    date_to         = request.args.get("date_to")   or None

    rows = db.list_appeals(
        status=status_filter,
        category=category_filter,
        priority=priority_filter,
        date_from=date_from,
        date_to=date_to,
    )
    appeals = _enrich_appeals(rows)
    counts  = db.get_counts()

    return render_template(
        "appeals_list.html",
        appeals=appeals,
        counts=counts,
        statuses=STATUSES,
        status_colors=STATUS_COLORS,
        categories=CATEGORIES,
        channels=CHANNELS,
        current_filters={
            "status":   status_filter,
            "category": category_filter,
            "priority": priority_filter,
            "date_from": date_from,
            "date_to":   date_to,
        },
        user=session["user"],
    )


# ── Карточка обращения ────────────────────────────────────────────────────────

@app.route("/appeals/<ticket_no>")
@login_required
def appeal_detail(ticket_no: str):
    appeal = db.get_appeal_by_ticket(ticket_no)
    if not appeal:
        flash(f"Обращение {ticket_no} не найдено", "error")
        return redirect(url_for("appeals_list"))

    appeal_d = dict(appeal)
    appeal_d["status_label"]   = STATUSES.get(appeal_d["status"], appeal_d["status"])
    appeal_d["status_color"]   = STATUS_COLORS.get(appeal_d["status"], "muted")
    appeal_d["category_label"] = CATEGORIES.get(appeal_d.get("category", ""), "")
    appeal_d["channel_label"]  = CHANNELS.get(appeal_d.get("channel", ""), "")

    # ФИО из таблицы licschet (лёгкий lookup, не JOIN)
    ls_info = db.get_ls(appeal_d["ls"]) if appeal_d.get("ls") else None
    fio     = ls_info["fio"]     if ls_info else "—"
    address = ls_info["address"] if ls_info else "—"

    responses = db.get_all_appeal_responses(appeal["id"])

    return render_template(
        "appeal_detail.html",
        appeal=appeal_d,
        fio=fio,
        address=address,
        responses=responses,
        statuses=STATUSES,
        status_colors=STATUS_COLORS,
        user=session["user"],
    )


# ── Ответ клиенту ─────────────────────────────────────────────────────────────

@app.route("/appeals/<int:appeal_id>/respond", methods=["POST"])
@login_required
def appeal_respond(appeal_id: int):
    body = request.form.get("body", "").strip()
    if not body:
        flash("Введите текст ответа", "error")
        return _redirect_to_appeal(appeal_id)

    data, err = client_api.respond_to_appeal(appeal_id, body, _operator_id())
    if err:
        flash(f"Ошибка отправки ответа: {err}", "error")
        log.error("appeal_respond id=%s err=%s", appeal_id, err)
    else:
        flash("Ответ отправлен клиенту", "success")
        log.info("Ответ по appeal_id=%s оператор=%s", appeal_id, _operator_id())

    return _redirect_to_appeal(appeal_id)


# ── Смена статуса ─────────────────────────────────────────────────────────────

@app.route("/appeals/<int:appeal_id>/status", methods=["POST"])
@login_required
def appeal_status(appeal_id: int):
    new_status = request.form.get("status", "").strip()
    if new_status not in STATUSES:
        flash("Неверный статус", "error")
        return _redirect_to_appeal(appeal_id)

    # Принудительное закрытие требует комментария
    if new_status == "closed":
        comment = request.form.get("force_comment", "").strip()
        if not comment:
            flash("Для принудительного закрытия необходимо ввести комментарий", "error")
            return _redirect_to_appeal(appeal_id)
        # Сначала добавляем комментарий как ответ оператора
        client_api.respond_to_appeal(
            appeal_id,
            f"[Принудительное закрытие] {comment}",
            _operator_id(),
        )

    data, err = client_api.change_appeal_status(appeal_id, new_status, _operator_id())
    if err:
        flash(f"Ошибка смены статуса: {err}", "error")
        log.error("appeal_status id=%s status=%s err=%s", appeal_id, new_status, err)
    else:
        flash(f"Статус изменён: {STATUSES[new_status]}", "success")
        log.info("Статус appeal_id=%s → %s оператор=%s", appeal_id, new_status, _operator_id())

    return _redirect_to_appeal(appeal_id)


def _redirect_to_appeal(appeal_id: int):
    """Редирект на карточку обращения по id."""
    appeal = db.get_appeal_by_id(appeal_id)
    if appeal:
        return redirect(url_for("appeal_detail", ticket_no=appeal["ticket_no"]))
    return redirect(url_for("appeals_list"))


# ── appeals_legacy (только просмотр) ─────────────────────────────────────────

@app.route("/legacy")
@login_required
def legacy_list():
    """Просмотр архивных обращений из appeals_legacy (только чтение)."""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM appeals_legacy ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        legacy = [dict(r) for r in rows]
    except Exception:
        legacy = []
    finally:
        conn.close()
    return render_template(
        "legacy_list.html",
        legacy=legacy,
        user=session["user"],
    )


# ── Показания ─────────────────────────────────────────────────────────────────

@app.route("/pokazaniya")
@login_required
def pokazaniya():
    rows = db.get_pokazaniya_with_prev()
    return render_template(
        "pokazaniya.html",
        rows=rows,
        user=session["user"],
        integration_1c_enabled=ENABLE_1C_INTEGRATION,
    )


# ── Оповещения ────────────────────────────────────────────────────────────────

@app.route("/broadcast")
@admin_required
def broadcast_page():
    users = db.get_all_bot_users()
    return render_template("broadcast.html", users=users, user=session["user"])


@app.route("/broadcast", methods=["POST"])
@admin_required
def broadcast():
    from config import TOKEN, API
    import httpx as _httpx

    text = request.form.get("text", "").strip()
    if not text:
        flash("Введите текст сообщения", "error")
        return redirect(url_for("broadcast_page"))

    users = db.get_all_bot_users()
    sent = failed = 0
    for u in users:
        try:
            r = _httpx.post(
                f"{API}/messages",
                headers={"Authorization": TOKEN},  # без Bearer — см. dev.max.ru/docs-api
                params={"chat_id": u["chat_id"]},
                json={"text": text},
                timeout=5,
            )
            if r.status_code == 200:
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    flash(f"Рассылка завершена: отправлено {sent}, ошибок {failed}", "success")
    return redirect(url_for("broadcast_page"))


# ── Загрузка данных ───────────────────────────────────────────────────────────

@app.route("/upload", methods=["GET"])
@admin_required
def upload_page():
    return render_template("upload.html", user=session["user"])


@app.route("/upload", methods=["POST"])
@admin_required
def upload_file():
    if "file" not in request.files:
        flash("Файл не выбран", "error")
        return redirect(url_for("upload_page"))
    file = request.files["file"]
    if not file.filename or not file.filename.endswith(".xlsx"):
        flash("Допускается только .xlsx", "error")
        return redirect(url_for("upload_page"))

    upload_dir = os.path.dirname(os.path.abspath(DB_PATH))
    filepath = os.path.join(upload_dir, "Данные_по_ЛС.xlsx")
    try:
        os.makedirs(upload_dir, exist_ok=True)
        file.save(filepath)
        db.import_from_excel(filepath)
    except Exception as exc:
        log.exception("Ошибка импорта Excel")
        flash(f"Ошибка импорта: {exc}", "error")
        return redirect(url_for("upload_page"))

    flash("Файл успешно загружен и импортирован", "success")
    return redirect(url_for("upload_page"))


# ── Управление лицевыми счетами ──────────────────────────────────────────────

@app.route("/accounts")
@admin_required
def accounts_page():
    query = db.normalize_lschet_search(request.args.get("q"))
    try:
        requested_page = int(request.args.get("page", "1"))
    except ValueError:
        requested_page = 1
    total = db.count_lschet(query)
    total_pages = max(1, (total + _ACCOUNTS_PAGE_SIZE - 1) // _ACCOUNTS_PAGE_SIZE)
    page = min(max(requested_page, 1), total_pages)
    accounts = db.list_lschet(
        query,
        limit=_ACCOUNTS_PAGE_SIZE,
        offset=(page - 1) * _ACCOUNTS_PAGE_SIZE,
    )
    return render_template(
        "accounts.html",
        accounts=accounts,
        csrf_token=_csrf_token(),
        page=page,
        query=query,
        total=total,
        total_pages=total_pages,
        user=session["user"],
    )


@app.route("/accounts/create", methods=["POST"])
@admin_required
@csrf_protected
def account_create():
    number = request.form.get("number", "")
    fio = request.form.get("fio", "")
    address = request.form.get("address", "")
    try:
        created = db.create_lschet(number, fio, address)
    except ValueError as exc:
        flash(str(exc), "error")
    except sqlite3.DatabaseError:
        log.error("Ошибка БД при добавлении лицевого счёта")
        flash("Не удалось добавить лицевой счёт. Попробуйте позже.", "error")
    else:
        if created:
            flash("Лицевой счёт добавлен", "success")
        else:
            flash("Лицевой счёт с таким номером уже существует", "error")
    return redirect(url_for("accounts_page"))


# ── Управление пользователями ─────────────────────────────────────────────────

@app.route("/users")
@admin_required
def users_page():
    users = db.get_all_users()
    return render_template("users.html", users=users, user=session["user"])


@app.route("/users/create", methods=["POST"])
@admin_required
def user_create():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    name     = request.form.get("name", "").strip()
    role     = request.form.get("role", "operator")
    if not username or not password or not name:
        flash("Заполните все поля", "error")
    else:
        ok, msg = db.create_user(username, password, name, role)
        flash(msg, "success" if ok else "error")
    return redirect(url_for("users_page"))


@app.route("/users/delete/<int:user_id>", methods=["POST"])
@admin_required
def user_delete(user_id: int):
    if user_id == 1:
        flash("Нельзя удалить главного администратора", "error")
    else:
        db.delete_user(user_id)
        flash("Пользователь удалён", "success")
    return redirect(url_for("users_page"))


@app.route("/users/password/<int:user_id>", methods=["POST"])
@admin_required
def user_password(user_id: int):
    pw = request.form.get("password", "").strip()
    if len(pw) < 6:
        flash("Пароль минимум 6 символов", "error")
    else:
        db.change_password(user_id, pw)
        flash("Пароль изменён", "success")
    return redirect(url_for("users_page"))


@app.route("/users/role/<int:user_id>", methods=["POST"])
@admin_required
def user_role(user_id: int):
    if user_id == 1:
        flash("Нельзя изменить роль главного администратора", "error")
    else:
        db.change_role(user_id, request.form.get("role", "operator"))
        flash("Роль изменена", "success")
    return redirect(url_for("users_page"))


# ── Операторские диалоги ────────────────────────────────────────────────────

def _max_headers() -> dict[str, str]:
    return {"Authorization": TOKEN, "Content-Type": "application/json"}


def _send_max_text(chat_id: int, text: str) -> bool:
    import httpx
    try:
        response = httpx.post(
            f"{API}/messages", headers=_max_headers(), params={"chat_id": chat_id},
            json={"text": text}, timeout=5,
        )
        return response.status_code == 200
    except httpx.HTTPError:
        log.warning("MAX недоступен при отправке сообщения операторского диалога chat_id=%s", chat_id)
        return False


def _send_max_buttons(chat_id: int, text: str, buttons: list[list[dict]]) -> bool:
    import httpx
    try:
        response = httpx.post(
            f"{API}/messages", headers=_max_headers(), params={"chat_id": chat_id},
            json={"text": text, "attachments": [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]},
            timeout=5,
        )
        return response.status_code == 200
    except httpx.HTTPError:
        log.warning("MAX недоступен при отправке кнопок chat_id=%s", chat_id)
        return False


def _assign_and_notify() -> None:
    for item in operator_chat.assign_waiting():
        _send_max_text(item["chat_id"], "👨‍💻 Оператор подключился к диалогу.")


def _send_max_jpeg(chat_id: int, path: str, caption: str = "") -> bool:
    import httpx
    headers = {"Authorization": TOKEN}
    try:
        meta = httpx.post(f"{API}/uploads", headers=headers, params={"type": "image"}, timeout=10)
        meta.raise_for_status()
        upload_url = meta.json().get("url")
        if not isinstance(upload_url, str) or not upload_url:
            return False
        with open(path, "rb") as stream:
            uploaded = httpx.post(
                upload_url, headers=headers,
                files={"data": ("image.jpg", stream, "image/jpeg")}, timeout=30,
            )
        uploaded.raise_for_status()
        token = uploaded.json().get("token")
        if not isinstance(token, str) or not token:
            return False
        response = httpx.post(
            f"{API}/messages", headers=_max_headers(), params={"chat_id": chat_id},
            json={"text": caption or "Оператор отправил изображение.",
                  "attachments": [{"type": "image", "payload": {"token": token}}]},
            timeout=10,
        )
        return response.status_code == 200
    except (httpx.HTTPError, OSError, ValueError, TypeError):
        log.warning("MAX недоступен при отправке изображения chat_id=%s", chat_id)
        return False


@app.route("/operator-chat")
@operator_required
def operator_chat_page():
    return render_template(
        "operator_chat.html", user=session["user"], csrf_token=_csrf_token(),
        dialogs=operator_chat.list_operator_dialogs(_operator_id()),
    )


@app.post("/operator-chat/shift/start")
@operator_required
@csrf_protected
def operator_shift_start():
    operator_chat.start_shift(_operator_id())
    _assign_and_notify()
    return jsonify(ok=True)


@app.post("/operator-chat/shift/end")
@operator_required
@csrf_protected
def operator_shift_end():
    try:
        operator_chat.end_shift(_operator_id())
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 409
    return jsonify(ok=True)


@app.post("/operator-chat/heartbeat")
@operator_required
@csrf_protected
def operator_heartbeat():
    active = operator_chat.heartbeat(_operator_id())
    if active:
        _assign_and_notify()
    return jsonify(ok=active)


@app.get("/operator-chat/api/dialogs")
@operator_required
def operator_dialogs_api():
    return jsonify(operator_chat.list_operator_dialogs(_operator_id()))


@app.get("/operator-chat/api/dialogs/<int:dialog_id>/messages")
@operator_required
def operator_messages_api(dialog_id: int):
    try:
        messages = operator_chat.list_messages(
            dialog_id, _operator_id(), int(request.args.get("after", "0")),
        )
    except (PermissionError, ValueError):
        abort(404)
    return jsonify(messages)


@app.post("/operator-chat/api/dialogs/<int:dialog_id>/messages")
@operator_required
@csrf_protected
def operator_send_message(dialog_id: int):
    dialog = operator_chat.get_dialog_for_operator(dialog_id, _operator_id())
    if not dialog:
        abort(404)
    body = request.form.get("body", "").strip()
    upload = request.files.get("image")
    relative_path = None
    absolute_path = None
    try:
        if upload and upload.filename:
            data = upload.read(operator_chat.MAX_IMAGE_BYTES + 1)
            operator_chat.validate_jpeg(data, upload.mimetype)
            os.makedirs(OPERATOR_CHAT_IMAGE_DIR, mode=0o700, exist_ok=True)
            relative_path = f"{uuid.uuid4().hex}.jpg"
            absolute_path = os.path.join(OPERATOR_CHAT_IMAGE_DIR, relative_path)
            with open(absolute_path, "xb") as stream:
                stream.write(data)
        message = operator_chat.add_message(
            dialog_id, "operator", body, user_id=_operator_id(), image_path=relative_path,
        )
    except (ValueError, PermissionError) as exc:
        if absolute_path:
            try:
                os.remove(absolute_path)
            except OSError:
                pass
        return jsonify(ok=False, error=str(exc)), 400
    delivered = (
        _send_max_jpeg(dialog["chat_id"], absolute_path, f"Оператор: {body}" if body else "")
        if absolute_path else _send_max_text(dialog["chat_id"], f"Оператор: {body}")
    )
    return jsonify(ok=True, delivered=delivered, message=message)


@app.post("/operator-chat/api/dialogs/<int:dialog_id>/close")
@operator_required
@csrf_protected
def operator_close_dialog(dialog_id: int):
    try:
        chat_id = operator_chat.close_dialog(dialog_id, _operator_id())
    except PermissionError:
        abort(404)
    buttons = [[{"type": "callback", "text": str(value), "payload": f"operator_rate:{dialog_id}-{value}"} for value in range(1, 6)]]
    _send_max_buttons(chat_id, "Оператор завершил диалог. Оцените его работу от 1 до 5.", buttons)
    _assign_and_notify()
    return jsonify(ok=True)


@app.get("/operator-chat/images/<int:message_id>")
@operator_required
def operator_chat_image(message_id: int):
    conn = db.get_conn()
    row = conn.execute(
        """SELECT m.image_path,d.operator_id,d.status FROM operator_messages m
           JOIN operator_dialogs d ON d.id=m.dialog_id WHERE m.id=?""", (message_id,),
    ).fetchone()
    conn.close()
    if not row or row["operator_id"] != _operator_id() or row["status"] != "active" or not row["image_path"]:
        abort(404)
    root = os.path.realpath(OPERATOR_CHAT_IMAGE_DIR)
    path = os.path.realpath(os.path.join(root, row["image_path"]))
    if os.path.dirname(path) != root:
        abort(404)
    return send_file(path, mimetype="image/jpeg", conditional=True)


@app.route("/operator-chat/settings", methods=["GET", "POST"])
@admin_required
def operator_chat_settings_page():
    if request.method == "POST":
        expected = session.get(_CSRF_SESSION_KEY, "")
        if not secrets.compare_digest(expected, request.form.get("csrf_token", "")):
            abort(400)
        try:
            operator_chat.update_settings(
                enabled=request.form.get("enabled") == "on",
                require_auth=request.form.get("require_auth") == "on",
                heartbeat_timeout_min=request.form.get("heartbeat_timeout_min"),
                max_active_dialogs=request.form.get("max_active_dialogs"),
                inactivity_timeout_min=request.form.get("inactivity_timeout_min"),
                warning_before_min=request.form.get("warning_before_min"),
                retention_days=request.form.get("retention_days"),
            )
            operator_chat.update_module_settings({key: request.form.get(f"module_{key}") == "on" for key in operator_chat.MODULE_KEYS})
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
        else:
            flash("Настройки сохранены", "success")
            return redirect(url_for("operator_chat_settings_page"))
    return render_template(
        "operator_chat_settings.html", user=session["user"], csrf_token=_csrf_token(),
        settings=operator_chat.get_settings(), modules=operator_chat.get_module_settings(),
        ratings=operator_chat.rating_report(),
    )


APPOINTMENT_STATUSES = {
    "active":    "Активна",
    "cancelled": "Отменена",
    "visited":   "Явка",
    "no_show":   "Неявка",
}

APPOINTMENT_STATUS_COLORS = {
    "active":    "warning",
    "cancelled": "muted",
    "visited":   "success",
    "no_show":   "error",
}

WEEKDAYS = {
    0: "Понедельник", 1: "Вторник", 2: "Среда", 3: "Четверг",
    4: "Пятница", 5: "Суббота", 6: "Воскресенье",
}


def _enrich_appointments(rows) -> list[dict]:
    result = []
    for r in rows:
        d = dict(r)
        d["status_label"] = APPOINTMENT_STATUSES.get(d.get("status", ""), d.get("status", ""))
        d["status_color"] = APPOINTMENT_STATUS_COLORS.get(d.get("status", ""), "muted")
        result.append(d)
    return result


# ── Записи на приём (REQ-СОТ-05) ─────────────────────────────────────────────

@app.route("/appointments")
@login_required
def appointments_list():
    branch_id_raw = request.args.get("branch_id") or None
    date_filter   = request.args.get("date") or None
    status_filter = request.args.get("status") or None
    branch_id     = int(branch_id_raw) if branch_id_raw else None

    rows = db.get_appointments(branch_id=branch_id, date=date_filter, status=status_filter)
    appointments = _enrich_appointments(rows)
    branches = db.get_branches()

    return render_template(
        "appointments.html",
        appointments=appointments,
        branches=branches,
        statuses=APPOINTMENT_STATUSES,
        current_filters={
            "branch_id": branch_id_raw,
            "date":      date_filter,
            "status":    status_filter,
        },
        user=session["user"],
    )


@app.route("/appointments/<int:appointment_id>/cancel", methods=["POST"])
@login_required
def appointment_cancel(appointment_id: int):
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Для отмены записи необходимо указать причину", "error")
        return redirect(url_for("appointments_list"))

    appt = db.get_appointment(appointment_id)
    if not appt or appt["status"] != "active":
        flash("Запись не найдена или уже неактивна", "error")
        return redirect(url_for("appointments_list"))

    db.cancel_appointment(appointment_id, "operator", reason)
    log.info("Запись id=%s отменена оператором id=%s: %s", appointment_id, _operator_id(), reason)

    # Уведомляем клиента в MAX
    from config import TOKEN, API
    import httpx as _httpx
    try:
        _httpx.post(
            f"{API}/messages",
            headers={"Authorization": TOKEN},  # без Bearer — см. dev.max.ru/docs-api
            params={"chat_id": appt["chat_id"]},
            json={"text": f"❌ Ваша запись на приём {appt['slot_date']} {appt['slot_time']} "
                          f"отменена.\nПричина: {reason}"},
            timeout=5,
        )
    except Exception as exc:
        log.warning("Не удалось уведомить клиента об отмене записи id=%s: %s", appointment_id, exc)

    flash("Запись отменена, клиент уведомлён", "success")
    return redirect(url_for("appointments_list"))


@app.route("/appointments/<int:appointment_id>/mark", methods=["POST"])
@login_required
def appointment_mark(appointment_id: int):
    status = request.form.get("status", "")
    if status not in ("visited", "no_show"):
        flash("Неверный статус отметки", "error")
        return redirect(url_for("appointments_list"))

    appt = db.get_appointment(appointment_id)
    if not appt:
        flash("Запись не найдена", "error")
        return redirect(url_for("appointments_list"))

    db.mark_appointment(appointment_id, status)
    label = "Явка" if status == "visited" else "Неявка"
    flash(f"Отмечено: {label}", "success")
    log.info("Запись id=%s отмечена как %s оператором id=%s", appointment_id, status, _operator_id())
    return redirect(url_for("appointments_list"))


# ── Филиалы и расписание (REQ-АДМ-06) ─────────────────────────────────────────

@app.route("/branches")
@admin_required
def branches_page():
    branches = db.get_branches(active_only=False)
    return render_template("branches.html", branches=branches, user=session["user"])


@app.route("/branches/create", methods=["POST"])
@admin_required
def branch_create():
    name = request.form.get("name", "").strip()
    address = request.form.get("address", "").strip()
    if not name or not address:
        flash("Заполните название и адрес филиала", "error")
    else:
        db.create_branch(name, address)
        flash("Филиал добавлен", "success")
    return redirect(url_for("branches_page"))


@app.route("/branches/<int:branch_id>/update", methods=["POST"])
@admin_required
def branch_update(branch_id: int):
    name = request.form.get("name", "").strip()
    address = request.form.get("address", "").strip()
    is_active = 1 if request.form.get("is_active") == "on" else 0
    if not name or not address:
        flash("Заполните название и адрес филиала", "error")
    else:
        db.update_branch(branch_id, name, address, is_active)
        flash("Филиал обновлён", "success")
    return redirect(url_for("branch_detail", branch_id=branch_id))


@app.route("/branches/<int:branch_id>")
@admin_required
def branch_detail(branch_id: int):
    branch = db.get_branch(branch_id)
    if not branch:
        flash("Филиал не найден", "error")
        return redirect(url_for("branches_page"))

    schedules = db.get_branch_schedules(branch_id)
    exceptions = db.get_exceptions(branch_id)

    return render_template(
        "branch_detail.html",
        branch=branch,
        schedules=schedules,
        exceptions=exceptions,
        weekdays=WEEKDAYS,
        user=session["user"],
    )


@app.route("/branches/<int:branch_id>/schedule/create", methods=["POST"])
@admin_required
def schedule_create(branch_id: int):
    try:
        weekday = int(request.form.get("weekday", ""))
        time_from = request.form.get("time_from", "").strip()
        time_to = request.form.get("time_to", "").strip()
        slot_duration = int(request.form.get("slot_duration_min", "30"))
        capacity = int(request.form.get("capacity", "1"))
        horizon = int(request.form.get("booking_horizon_days", "14"))
    except ValueError:
        flash("Проверьте корректность введённых чисел", "error")
        return redirect(url_for("branch_detail", branch_id=branch_id))

    if slot_duration <= 0 or capacity <= 0 or horizon < 0:
        flash("Длительность и вместимость должны быть больше нуля", "error")
        return redirect(url_for("branch_detail", branch_id=branch_id))

    if not time_from or not time_to:
        flash("Укажите время начала и окончания приёма", "error")
        return redirect(url_for("branch_detail", branch_id=branch_id))

    if time_from >= time_to:
        flash("Время начала должно быть раньше времени окончания", "error")
        return redirect(url_for("branch_detail", branch_id=branch_id))

    try:
        db.create_schedule(branch_id, weekday, time_from, time_to, slot_duration, capacity, horizon)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("branch_detail", branch_id=branch_id))
    flash("Расписание добавлено", "success")
    return redirect(url_for("branch_detail", branch_id=branch_id))


@app.route("/branches/<int:branch_id>/schedule/<int:schedule_id>/delete", methods=["POST"])
@admin_required
def schedule_delete(branch_id: int, schedule_id: int):
    db.delete_schedule(schedule_id)
    flash("Расписание удалено", "success")
    return redirect(url_for("branch_detail", branch_id=branch_id))


@app.route("/branches/<int:branch_id>/exceptions/create", methods=["POST"])
@admin_required
def exception_create(branch_id: int):
    date = request.form.get("date", "").strip()
    reason = request.form.get("reason", "").strip() or None
    if not date:
        flash("Укажите дату исключения", "error")
    else:
        db.add_exception(branch_id, date, reason)
        flash("Исключение добавлено", "success")
    return redirect(url_for("branch_detail", branch_id=branch_id))


@app.route("/branches/<int:branch_id>/exceptions/<date>/delete", methods=["POST"])
@admin_required
def exception_delete(branch_id: int, date: str):
    db.remove_exception(branch_id, date)
    flash("Исключение удалено", "success")
    return redirect(url_for("branch_detail", branch_id=branch_id))


# ── Вспомогательные функции: домовые чаты / сценарии / скрипты ──────────────

def _scenario_to_dict(row) -> dict:
    """Дополняет строку сценария распарсенными keywords для шаблонов."""
    d = dict(row)
    try:
        keywords = json.loads(d.get("keywords") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        keywords = []
    d["keywords_list"] = keywords
    d["keywords_text"] = "\n".join(keywords)
    return d


def _parse_keywords_textarea(raw: str) -> list[str]:
    """Разбирает textarea (одно слово/фраза на строку) в список без пустых строк."""
    return [line.strip() for line in raw.splitlines() if line.strip()]


# ── Домовые чаты (раздел 5, 7.4 ТЗ) ──────────────────────────────────────────

@app.route("/house-chats")
@login_required
def house_chats_list():
    all_chats = db.get_house_chats(active_only=False)
    active = [dict(c) for c in all_chats if c["is_active"]]
    inactive = [dict(c) for c in all_chats if not c["is_active"]]
    return render_template(
        "house_chats_list.html",
        active=active,
        inactive=inactive,
        user=session["user"],
    )


@app.route("/house-chats/create", methods=["POST"])
@admin_required
def house_chat_create():
    address = request.form.get("address", "").strip()
    messenger = request.form.get("messenger", "max").strip()
    chat_id = request.form.get("chat_id", "").strip()

    if not address or not chat_id:
        flash("Заполните адрес и Chat ID", "error")
        return redirect(url_for("house_chats_list"))

    try:
        db.add_house_chat(address, messenger, chat_id)
        flash(f"Чат подключён: {address}", "success")
        log.info("Домовой чат создан: address=%s  chat_id=%s  оператор=%s",
                 address, chat_id, _operator_id())
    except Exception as exc:
        # Уникальный индекс на (address WHERE is_active=1) — по этому адресу
        # уже есть активный чат
        flash(f"Не удалось подключить чат: {exc}", "error")
        log.warning("Ошибка создания домового чата: %s", exc)

    return redirect(url_for("house_chats_list"))


@app.route("/house-chats/<int:house_chat_id>")
@login_required
def house_chat_detail(house_chat_id: int):
    hc = db.get_house_chat(house_chat_id)
    if not hc:
        flash("Чат не найден", "error")
        return redirect(url_for("house_chats_list"))

    linked = [_scenario_to_dict(s) for s in db.get_linked_scenarios_full(house_chat_id)]
    unlinked = db.get_unlinked_scenarios_for_chat(house_chat_id)
    exclusions = db.list_chat_exclusions(house_chat_id)

    return render_template(
        "house_chat_detail.html",
        hc=dict(hc),
        linked_scenarios=linked,
        unlinked_scenarios=unlinked,
        exclusions=exclusions,
        user=session["user"],
    )


@app.route("/house-chats/<int:house_chat_id>/deactivate", methods=["POST"])
@admin_required
def house_chat_deactivate(house_chat_id: int):
    db.deactivate_house_chat(house_chat_id)
    flash("Чат отключён", "success")
    log.info("Домовой чат id=%s отключён оператором=%s", house_chat_id, _operator_id())
    return redirect(url_for("house_chats_list"))


@app.route("/house-chats/<int:house_chat_id>/scenarios/link", methods=["POST"])
@admin_required
def house_chat_link_scenario(house_chat_id: int):
    scenario_id = request.form.get("scenario_id", "").strip()
    if not scenario_id:
        flash("Выберите сценарий", "error")
    else:
        db.link_scenario_to_chat(int(scenario_id), house_chat_id)
        flash("Сценарий привязан", "success")
    return redirect(url_for("house_chat_detail", house_chat_id=house_chat_id))


@app.route("/house-chats/<int:house_chat_id>/scenarios/<int:scenario_id>/unlink", methods=["POST"])
@admin_required
def house_chat_unlink_scenario(house_chat_id: int, scenario_id: int):
    db.unlink_scenario_from_chat(scenario_id, house_chat_id)
    flash("Сценарий отвязан", "success")
    return redirect(url_for("house_chat_detail", house_chat_id=house_chat_id))


@app.route("/house-chats/<int:house_chat_id>/exclusions/add", methods=["POST"])
@admin_required
def house_chat_add_exclusion(house_chat_id: int):
    messenger = request.form.get("messenger", "max").strip()
    user_id = request.form.get("user_id", "").strip()
    reason = request.form.get("reason", "").strip() or None

    if not user_id:
        flash("Укажите User ID", "error")
    else:
        db.add_chat_exclusion(house_chat_id, messenger, user_id, reason)
        flash("Исключение добавлено", "success")
    return redirect(url_for("house_chat_detail", house_chat_id=house_chat_id))


@app.route("/house-chats/<int:house_chat_id>/exclusions/<int:exclusion_id>/remove", methods=["POST"])
@admin_required
def house_chat_remove_exclusion(house_chat_id: int, exclusion_id: int):
    db.remove_chat_exclusion_by_id(exclusion_id)
    flash("Исключение удалено", "success")
    return redirect(url_for("house_chat_detail", house_chat_id=house_chat_id))


@app.route("/house-chats/broadcast")
@admin_required
def house_chats_broadcast_page():
    chats = [dict(c) for c in db.get_house_chats(active_only=True)]
    return render_template("house_chats_broadcast.html", chats=chats, user=session["user"])


@app.route("/house-chats/broadcast", methods=["POST"])
@admin_required
def house_chats_broadcast():
    chat_ids = request.form.getlist("chat_ids")
    text = request.form.get("text", "").strip()

    if not chat_ids:
        flash("Выберите хотя бы один чат", "error")
        return redirect(url_for("house_chats_broadcast_page"))
    if not text:
        flash("Введите текст сообщения", "error")
        return redirect(url_for("house_chats_broadcast_page"))

    from config import TOKEN, API
    import httpx as _httpx

    sent = failed = 0
    for hc_id in chat_ids:
        hc = db.get_house_chat(int(hc_id))
        if not hc:
            failed += 1
            continue
        try:
            r = _httpx.post(
                f"{API}/messages",
                headers={"Authorization": TOKEN},
                params={"chat_id": hc["chat_id"]},
                json={"text": text},
                timeout=5,
            )
            sent += 1 if r.status_code == 200 else 0
            failed += 0 if r.status_code == 200 else 1
        except Exception as exc:
            log.warning("Рассылка в домовой чат id=%s не удалась: %s", hc_id, exc)
            failed += 1

    flash(f"Рассылка завершена: отправлено {sent}, ошибок {failed}", "success")
    log.info("Рассылка в домовые чаты: %d получателей, оператор=%s", len(chat_ids), _operator_id())
    return redirect(url_for("house_chats_broadcast_page"))


# ── Сценарии мониторинга ──────────────────────────────────────────────────────

@app.route("/scenarios")
@login_required
def scenarios_list():
    scenarios = [_scenario_to_dict(s) for s in db.get_all_scenarios()]
    return render_template("scenarios_list.html", scenarios=scenarios, user=session["user"])


@app.route("/scenarios/new")
@admin_required
def scenario_new_form():
    return render_template("scenario_form.html", scenario=None, user=session["user"])


@app.route("/scenarios/new", methods=["POST"])
@admin_required
def scenario_new():
    title = request.form.get("title", "").strip()
    keywords = _parse_keywords_textarea(request.form.get("keywords", ""))
    response_text = request.form.get("response_text", "").strip()
    suggest_appeal = request.form.get("suggest_appeal") == "on"

    if not title or not keywords or not response_text:
        flash("Заполните название, хотя бы одно ключевое слово и текст ответа", "error")
        return redirect(url_for("scenario_new_form"))

    db.create_scenario(title, keywords, response_text, suggest_appeal)
    flash(f"Сценарий «{title}» создан", "success")
    return redirect(url_for("scenarios_list"))


@app.route("/scenarios/<int:scenario_id>/edit")
@admin_required
def scenario_edit_form(scenario_id: int):
    scenario = db.get_scenario(scenario_id)
    if not scenario:
        flash("Сценарий не найден", "error")
        return redirect(url_for("scenarios_list"))
    return render_template(
        "scenario_form.html",
        scenario=_scenario_to_dict(scenario),
        user=session["user"],
    )


@app.route("/scenarios/<int:scenario_id>/edit", methods=["POST"])
@admin_required
def scenario_edit(scenario_id: int):
    title = request.form.get("title", "").strip()
    keywords = _parse_keywords_textarea(request.form.get("keywords", ""))
    response_text = request.form.get("response_text", "").strip()
    suggest_appeal = request.form.get("suggest_appeal") == "on"
    is_active = request.form.get("is_active") == "on"

    if not title or not keywords or not response_text:
        flash("Заполните название, хотя бы одно ключевое слово и текст ответа", "error")
        return redirect(url_for("scenario_edit_form", scenario_id=scenario_id))

    db.update_scenario(scenario_id, title, keywords, response_text, suggest_appeal, is_active)
    flash("Сценарий обновлён", "success")
    return redirect(url_for("scenarios_list"))


@app.route("/scenarios/<int:scenario_id>/delete", methods=["POST"])
@admin_required
def scenario_delete(scenario_id: int):
    db.delete_scenario(scenario_id)
    flash("Сценарий удалён", "success")
    log.info("Сценарий id=%s удалён оператором=%s", scenario_id, _operator_id())
    return redirect(url_for("scenarios_list"))


# ── Скрипты FAQ ────────────────────────────────────────────────────────────────

@app.route("/scripts")
@login_required
def scripts_list():
    scripts = db.get_all_scripts()
    return render_template("scripts_list.html", scripts=scripts, user=session["user"])


@app.route("/scripts/create", methods=["POST"])
@admin_required
def script_create():
    title = request.form.get("title", "").strip()
    if not title:
        flash("Введите название скрипта", "error")
        return redirect(url_for("scripts_list"))
    sid = db.create_script(title)
    flash(f"Скрипт «{title}» создан", "success")
    return redirect(url_for("script_editor", script_id=sid))


@app.route("/scripts/<int:script_id>")
@admin_required
def script_editor(script_id: int):
    script = db.get_script(script_id)
    if not script:
        flash("Скрипт не найден", "error")
        return redirect(url_for("scripts_list"))

    nodes = db.get_script_nodes(script_id)
    edges = db.get_script_edges(script_id)
    nodes_by_id = {n["id"]: n for n in nodes}

    # Корневой узел — тот, на который нет входящих рёбер
    targets = {e["to_node_id"] for e in edges}
    roots = [n["id"] for n in nodes if n["id"] not in targets]
    root_id = min(roots) if roots else None

    from collections import defaultdict as _dd
    from_counts, to_counts = _dd(int), _dd(int)
    for e in edges:
        from_counts[e["from_node_id"]] += 1
        to_counts[e["to_node_id"]] += 1

    return render_template(
        "script_editor.html",
        script=script,
        nodes=nodes,
        edges=edges,
        nodes_by_id=nodes_by_id,
        root_id=root_id,
        from_counts=from_counts,
        to_counts=to_counts,
        user=session["user"],
    )


@app.route("/scripts/<int:script_id>/update", methods=["POST"])
@admin_required
def script_update(script_id: int):
    title = request.form.get("title", "").strip()
    try:
        sort_order = int(request.form.get("sort_order", "0"))
    except ValueError:
        sort_order = 0
    is_active = request.form.get("is_active") == "on"

    if not title:
        flash("Название не может быть пустым", "error")
    else:
        db.update_script(script_id, title, sort_order, is_active)
        flash("Скрипт обновлён", "success")
    return redirect(url_for("script_editor", script_id=script_id))


@app.route("/scripts/<int:script_id>/delete", methods=["POST"])
@admin_required
def script_delete(script_id: int):
    db.delete_script(script_id)
    flash("Скрипт удалён", "success")
    log.info("Скрипт id=%s удалён оператором=%s", script_id, _operator_id())
    return redirect(url_for("scripts_list"))


@app.route("/scripts/<int:script_id>/nodes/add", methods=["POST"])
@admin_required
def script_node_add(script_id: int):
    title = request.form.get("title", "").strip()
    is_terminal = request.form.get("is_terminal") == "on"
    if not title:
        flash("Текст узла не может быть пустым", "error")
    else:
        db.add_script_node(script_id, title, is_terminal)
        flash("Узел добавлен", "success")
    return redirect(url_for("script_editor", script_id=script_id))


@app.route("/scripts/<int:script_id>/nodes/<int:node_id>/update", methods=["POST"])
@admin_required
def script_node_update(script_id: int, node_id: int):
    title = request.form.get("title", "").strip()
    is_terminal = request.form.get("is_terminal") == "on"
    if not title:
        flash("Текст узла не может быть пустым", "error")
    else:
        db.update_script_node(node_id, title, is_terminal)
        flash("Узел сохранён", "success")
    return redirect(url_for("script_editor", script_id=script_id))


@app.route("/scripts/<int:script_id>/nodes/<int:node_id>/delete", methods=["POST"])
@admin_required
def script_node_delete(script_id: int, node_id: int):
    db.delete_script_node(node_id)
    flash("Узел удалён", "success")
    return redirect(url_for("script_editor", script_id=script_id))


@app.route("/scripts/<int:script_id>/edges/add", methods=["POST"])
@admin_required
def script_edge_add(script_id: int):
    try:
        from_node_id = int(request.form.get("from_node_id", ""))
        to_node_id = int(request.form.get("to_node_id", ""))
    except ValueError:
        flash("Выберите оба узла перехода", "error")
        return redirect(url_for("script_editor", script_id=script_id))

    label = request.form.get("label", "").strip()
    if not label:
        flash("Текст кнопки не может быть пустым", "error")
        return redirect(url_for("script_editor", script_id=script_id))

    edge_id, err = db.add_script_edge(script_id, from_node_id, label, to_node_id)
    if err:
        flash(err, "error")
    else:
        flash("Переход добавлен", "success")
    return redirect(url_for("script_editor", script_id=script_id))


@app.route("/scripts/<int:script_id>/edges/<int:edge_id>/delete", methods=["POST"])
@admin_required
def script_edge_delete(script_id: int, edge_id: int):
    db.delete_script_edge(edge_id)
    flash("Переход удалён", "success")
    return redirect(url_for("script_editor", script_id=script_id))


# ── Context processors ────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    """Переменные доступные во всех шаблонах."""
    return {
        "STATUSES":      STATUSES,
        "STATUS_COLORS": STATUS_COLORS,
        "CATEGORIES":    CATEGORIES,
        "CHANNELS":      CHANNELS,
        "APPOINTMENT_STATUSES": APPOINTMENT_STATUSES,
    }


# ── Точка входа ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    app.run(debug=False, port=5000)
