"""Operator chat domain service shared by the MAX bot and web portal.

All assignment decisions are made under ``BEGIN IMMEDIATE`` so two bot/web
workers cannot assign the same operator capacity or queue item concurrently.
The module never logs message bodies or customer profile fields.
"""

from __future__ import annotations

import io
import json
import os
import random
import sqlite3
import warnings
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image, ImageOps, UnidentifiedImageError

import database as db

ACTIVE_STATUSES = ("waiting", "active")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 7680
MAX_IMAGE_PIXELS = 40_000_000
MAX_OUTBOX_ATTEMPTS = 5
MAX_IMAGE_UPLOAD_HOSTS = frozenset({"iu.oneme.ru"})
OPERATOR_PREFIX = "Оператор: "
MAX_OPERATOR_BODY = 4000 - len(OPERATOR_PREFIX)
REPORT_REASONS = {
    "unwanted_image": "Нежелательное изображение",
    "insults": "Оскорбления",
    "spam": "Спам",
    "irrelevant_image": "Изображение не относится к вопросу",
    "other": "Другое",
}
MODULE_KEYS = (
    "auth", "appeal", "appeal_status", "readings", "faq", "ai",
    "receipt", "appointment",
)


class ReportDecisionConflictError(ValueError):
    """A resolved report cannot be changed to the opposite decision."""


class DeliveryInProgressError(ValueError):
    """The dialog has an operator reply currently crossing the MAX boundary."""


class UndeliveredMessagesError(ValueError):
    """Manual close must not silently discard operator-authored content."""

    def __init__(self, *, total: int, images: int) -> None:
        self.total = total
        self.images = images
        super().__init__(
            f"Не доставлено сообщений: {total}, из них изображений: {images}. "
            "Повторите отправку или удалите недоставленные изображения."
        )


def is_allowed_max_image_upload_url(value: Any) -> bool:
    """Validate the documented signed MAX image-upload endpoint without logging it."""
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.hostname.lower() in MAX_IMAGE_UPLOAD_HOSTS
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


@contextmanager
def _connection():
    conn = db.get_conn()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def _enqueue_outbox_locked(
    conn: Any,
    *,
    event_key: str,
    chat_id: int,
    dialog_id: int | None = None,
    kind: str,
    body: str,
    message_id: int | None = None,
    image_path: str | None = None,
    buttons: list[list[dict[str, Any]]] | None = None,
    now: datetime | None = None,
) -> int:
    conn.execute(
        """INSERT OR IGNORE INTO operator_outbox
           (event_key,dialog_id,chat_id,message_id,kind,body,image_path,buttons_json,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,'pending',?)""",
        (
            event_key, dialog_id, chat_id, message_id, kind, body, image_path,
            json.dumps(buttons, ensure_ascii=False) if buttons else None, _iso(now),
        ),
    )
    row = conn.execute("SELECT id FROM operator_outbox WHERE event_key=?", (event_key,)).fetchone()
    return int(row["id"])


def _rating_buttons(dialog_id: int) -> list[list[dict[str, Any]]]:
    return [[
        {"type": "callback", "text": str(value), "payload": f"operator_rate:{dialog_id}-{value}"}
        for value in range(1, 6)
    ], [{"type": "callback", "text": "🏠 Главное меню", "payload": "main_menu"}]]


def _main_menu_buttons() -> list[list[dict[str, Any]]]:
    return [[{"type": "callback", "text": "🏠 Главное меню", "payload": "main_menu"}]]


def get_settings() -> dict[str, Any]:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM operator_chat_settings WHERE id=1").fetchone()
    return dict(row)


def update_settings(**values: Any) -> None:
    current = get_settings()
    numeric = {
        "heartbeat_timeout_min": (1, 60),
        "heartbeat_timeout_sec": (10, 120),
        "reconnect_grace_sec": (30, 900),
        "max_active_dialogs": (1, 50),
        "inactivity_timeout_min": (5, 1440),
        "warning_before_min": (1, 1439),
        "retention_days": (1, 3650),
        "report_threshold": (1, 100),
        "evidence_retention_days": (1, 3650),
    }
    clean: dict[str, Any] = {
        "enabled": int(bool(values["enabled"])),
        "require_auth": int(bool(values["require_auth"])),
    }
    for key, (low, high) in numeric.items():
        raw = values.get(key)
        value = int(current[key] if raw is None else raw)
        if not low <= value <= high:
            raise ValueError(f"{key}: допустимое значение {low}–{high}")
        clean[key] = value
    if clean["warning_before_min"] >= clean["inactivity_timeout_min"]:
        raise ValueError("Предупреждение должно быть раньше закрытия")
    with _connection() as conn:
        conn.execute(
            """UPDATE operator_chat_settings SET enabled=:enabled,
               require_auth=:require_auth, heartbeat_timeout_min=:heartbeat_timeout_min,
               heartbeat_timeout_sec=:heartbeat_timeout_sec,
               reconnect_grace_sec=:reconnect_grace_sec,
               max_active_dialogs=:max_active_dialogs,
               inactivity_timeout_min=:inactivity_timeout_min,
               warning_before_min=:warning_before_min, retention_days=:retention_days,
               report_threshold=:report_threshold,
               evidence_retention_days=:evidence_retention_days
               WHERE id=1""",
            clean,
        )


def get_module_settings() -> dict[str, bool]:
    result = {key: True for key in MODULE_KEYS}
    try:
        with _connection() as conn:
            rows = conn.execute("SELECT module_key, enabled FROM module_settings").fetchall()
    except sqlite3.OperationalError:
        # Startup/tests may render a menu before init_db; preserve the legacy
        # fail-open menu until the idempotent migration runs.
        return result
    result.update({row["module_key"]: bool(row["enabled"]) for row in rows})
    return result


def module_enabled(key: str) -> bool:
    return get_module_settings().get(key, False)


def update_module_settings(values: dict[str, bool]) -> None:
    with _connection() as conn:
        for key in MODULE_KEYS:
            conn.execute(
                "INSERT INTO module_settings(module_key,enabled) VALUES(?,?) "
                "ON CONFLICT(module_key) DO UPDATE SET enabled=excluded.enabled",
                (key, int(bool(values.get(key)))),
            )


def _fresh_cutoff(settings: dict[str, Any], now: datetime) -> str:
    return _iso(now - timedelta(seconds=int(settings["heartbeat_timeout_sec"])))


def _requeue_cutoff(settings: dict[str, Any], now: datetime) -> str:
    seconds = int(settings["heartbeat_timeout_sec"]) + int(settings["reconnect_grace_sec"])
    return _iso(now - timedelta(seconds=seconds))


def is_client_blocked(chat_id: int) -> bool:
    try:
        with _connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM operator_chat_blocks WHERE chat_id=? AND active=1", (chat_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def has_active_operators(now: datetime | None = None) -> bool:
    now = now or utc_now()
    settings = get_settings()
    if not settings["enabled"]:
        return False
    with _connection() as conn:
        row = conn.execute(
            """SELECT 1 FROM operator_shifts s JOIN users u ON u.id=s.user_id
               WHERE s.active=1 AND s.heartbeat_at>=? AND u.role='operator' LIMIT 1""",
            (_fresh_cutoff(settings, now),),
        ).fetchone()
    return row is not None


def start_shift(user_id: int, now: datetime | None = None) -> None:
    now_s = _iso(now)
    with _connection() as conn:
        role = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not role or role["role"] != "operator":
            raise PermissionError("Смену может начать только оператор")
        conn.execute(
            """INSERT INTO operator_shifts(user_id,active,started_at,heartbeat_at,ended_at)
               VALUES(?,1,?,?,NULL) ON CONFLICT(user_id) DO UPDATE SET
               active=1, started_at=excluded.started_at,
               heartbeat_at=excluded.heartbeat_at, ended_at=NULL""",
            (user_id, now_s, now_s),
        )


def heartbeat(user_id: int, now: datetime | None = None) -> bool:
    now_s = _iso(now)
    with _connection() as conn:
        cur = conn.execute(
            "UPDATE operator_shifts SET heartbeat_at=? WHERE user_id=? AND active=1",
            (now_s, user_id),
        )
    return bool(cur.rowcount)


def end_shift(user_id: int, now: datetime | None = None) -> None:
    with _connection() as conn:
        active = conn.execute(
            "SELECT COUNT(*) n FROM operator_dialogs WHERE operator_id=? AND status='active'",
            (user_id,),
        ).fetchone()["n"]
        if active:
            raise ValueError("Сначала завершите активные диалоги")
        conn.execute(
            "UPDATE operator_shifts SET active=0, ended_at=? WHERE user_id=?",
            (_iso(now), user_id),
        )


def _supersede_operator_delivery_locked(
    conn: Any, dialog_id: int, reason: str, now: datetime,
) -> bool:
    """Cancel prior-owner delivery after ensuring no live send is in flight.

    Returns false while a sender owns a non-expired two-minute lease.  Once a
    lease is stale, its CAS finalizer cannot overwrite the superseding state.
    """
    live_send = conn.execute(
        """SELECT 1 FROM operator_outbox o
           WHERE o.dialog_id=? AND o.status='sending'
             AND o.lease_at>? LIMIT 1""",
        (dialog_id, _iso(now - timedelta(minutes=2))),
    ).fetchone()
    if live_send:
        return False
    conn.execute(
        """UPDATE operator_outbox SET status='failed',attempts=?,next_retry_at=NULL,
           lease_at=NULL,last_error=? WHERE dialog_id=? AND status!='delivered'
           AND message_id IN (SELECT id FROM operator_messages
                              WHERE dialog_id=? AND sender='operator')""",
        (MAX_OUTBOX_ATTEMPTS, reason, dialog_id, dialog_id),
    )
    conn.execute(
        """UPDATE operator_messages SET delivery_status='failed',last_error=?
           WHERE dialog_id=? AND sender='operator' AND delivery_status!='delivered'""",
        (reason, dialog_id),
    )
    return True


def _supersede_dialog_events_locked(conn: Any, dialog_id: int, reason: str) -> None:
    """Dead-letter obsolete assignment/warning events without blocking successors."""
    conn.execute(
        """UPDATE operator_outbox SET status='failed',attempts=?,next_retry_at=NULL,
           lease_at=NULL,last_error=? WHERE dialog_id=? AND status!='delivered'
           AND message_id IS NULL AND (event_key LIKE ? OR event_key LIKE ?)""",
        (
            MAX_OUTBOX_ATTEMPTS, reason, dialog_id,
            f"dialog:{dialog_id}:assigned:%", f"dialog:{dialog_id}:warning:%",
        ),
    )


def transfer_all_dialogs(user_id: int, now: datetime | None = None) -> list[int]:
    """Atomically end a shift and return all assigned dialogs to FIFO."""
    now = now or utc_now()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        role = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not role or role["role"] != "operator":
            raise PermissionError
        rows = conn.execute(
            "SELECT id FROM operator_dialogs WHERE operator_id=? AND status='active' ORDER BY id",
            (user_id,),
        ).fetchall()
        next_seq = int(conn.execute(
            "SELECT COALESCE(MAX(queue_seq),0)+1 n FROM operator_dialogs"
        ).fetchone()["n"])
        ids: list[int] = []
        for row in rows:
            if not _supersede_operator_delivery_locked(
                conn, int(row["id"]), "superseded_by_transfer", now,
            ):
                raise DeliveryInProgressError(
                    "Ответ оператора сейчас отправляется. Повторите передачу через несколько секунд"
                )
        for offset, row in enumerate(rows):
            dialog_id = int(row["id"])
            _supersede_dialog_events_locked(conn, dialog_id, "superseded_by_transfer")
            conn.execute(
                """UPDATE operator_dialogs SET status='waiting',operator_id=NULL,queue_seq=?,
                   assigned_at=NULL,warned_at=NULL,last_activity_at=?,reassignment_pending=1
                   WHERE id=? AND operator_id=? AND status='active'""",
                (next_seq + offset, _iso(now), dialog_id, user_id),
            )
            ids.append(dialog_id)
        conn.execute(
            "UPDATE operator_shifts SET active=0,ended_at=? WHERE user_id=?",
            (_iso(now), user_id),
        )
    return ids


def _choose_operator(conn: Any, settings: dict[str, Any], now: datetime) -> int | None:
    rows = conn.execute(
        """SELECT u.id, COUNT(d.id) load FROM users u
           JOIN operator_shifts s ON s.user_id=u.id
           LEFT JOIN operator_dialogs d ON d.operator_id=u.id AND d.status='active'
           WHERE u.role='operator' AND s.active=1 AND s.heartbeat_at>=?
           GROUP BY u.id HAVING COUNT(d.id) < ? ORDER BY load, u.id""",
        (_fresh_cutoff(settings, now), int(settings["max_active_dialogs"])),
    ).fetchall()
    if not rows:
        return None
    minimum = rows[0]["load"]
    candidates = [row["id"] for row in rows if row["load"] == minimum]
    return random.choice(candidates)  # nosec B311 - fairness, not cryptography


def request_dialog(
    chat_id: int,
    *,
    profile: dict[str, Any] | None,
    faq_context: str | None,
    ai_messages: list[dict[str, str]],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    settings = get_settings()
    if is_client_blocked(chat_id):
        return {"status": "blocked"}
    if not settings["enabled"] or not has_active_operators(now):
        return {"status": "unavailable"}
    authenticated = bool(profile and profile.get("ls"))
    if settings["require_auth"] and not authenticated:
        return {"status": "auth_required"}
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        any_active = conn.execute(
            """SELECT 1 FROM operator_shifts s JOIN users u ON u.id=s.user_id
               WHERE s.active=1 AND s.heartbeat_at>=? AND u.role='operator' LIMIT 1""",
            (_fresh_cutoff(settings, now),),
        ).fetchone()
        if not any_active:
            return {"status": "unavailable"}
        blocked = conn.execute(
            "SELECT 1 FROM operator_chat_blocks WHERE chat_id=? AND active=1",
            (chat_id,),
        ).fetchone()
        if blocked:
            return {"status": "blocked"}
        existing = conn.execute(
            "SELECT * FROM operator_dialogs WHERE chat_id=? AND status IN ('waiting','active')",
            (chat_id,),
        ).fetchone()
        if existing:
            return dict(existing)
        operator_id = _choose_operator(conn, settings, now)
        status = "active" if operator_id else "waiting"
        queue_seq = None
        if status == "waiting":
            queue_seq = conn.execute(
                "SELECT COALESCE(MAX(queue_seq),0)+1 n FROM operator_dialogs"
            ).fetchone()["n"]
        cur = conn.execute(
            """INSERT INTO operator_dialogs
               (chat_id,operator_id,status,queue_seq,authenticated,client_fio,client_ls,
                client_address,faq_context,ai_context_json,created_at,assigned_at,last_activity_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                chat_id, operator_id, status, queue_seq, int(authenticated),
                profile.get("fio") if authenticated else None,
                profile.get("ls") if authenticated else None,
                profile.get("address") if authenticated else None,
                faq_context,
                json.dumps(ai_messages[-5:], ensure_ascii=False),
                _iso(now), _iso(now) if operator_id else None, _iso(now),
            ),
        )
        dialog_id = cur.lastrowid
        if status == "active":
            conn.execute(
                "INSERT INTO operator_messages(dialog_id,sender,message_type,body,created_at) "
                "VALUES(?,'system','text',?,?)",
                (dialog_id, "Оператор подключился к диалогу.", _iso(now)),
            )
            _enqueue_outbox_locked(
                conn, event_key=f"dialog:{dialog_id}:assigned:1", chat_id=chat_id,
                dialog_id=dialog_id,
                kind="text",
                body="👨‍💻 Оператор подключился к диалогу. Напишите ваш вопрос.",
                now=now,
            )
        row = conn.execute("SELECT * FROM operator_dialogs WHERE id=?", (dialog_id,)).fetchone()
    return dict(row)


def queue_position(dialog_id: int) -> int | None:
    with _connection() as conn:
        row = conn.execute("SELECT queue_seq,status FROM operator_dialogs WHERE id=?", (dialog_id,)).fetchone()
        if not row or row["status"] != "waiting":
            return None
        return conn.execute(
            "SELECT COUNT(*) n FROM operator_dialogs WHERE status='waiting' AND queue_seq<=?",
            (row["queue_seq"],),
        ).fetchone()["n"]


def cancel_waiting(chat_id: int, now: datetime | None = None) -> bool:
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM operator_dialogs WHERE chat_id=? AND status='waiting'", (chat_id,),
        ).fetchone()
        if not row:
            return False
        cur = conn.execute(
            "UPDATE operator_dialogs SET status='cancelled',closed_at=? WHERE id=? AND status='waiting'",
            (_iso(now), row["id"]),
        )
        if cur.rowcount:
            _enqueue_outbox_locked(
                conn, event_key=f"dialog:{row['id']}:cancelled_by_client",
                dialog_id=row["id"], chat_id=chat_id, kind="buttons",
                body="Ожидание оператора отменено.", buttons=_main_menu_buttons(), now=now,
            )
    return bool(cur.rowcount)


def assign_waiting(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or utc_now()
    settings = get_settings()
    assigned: list[dict[str, Any]] = []
    if not settings["enabled"]:
        return assigned
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        while True:
            waiting = conn.execute(
                """SELECT * FROM operator_dialogs WHERE status='waiting' AND last_activity_at>?
                   ORDER BY queue_seq,created_at,id LIMIT 1""",
                (_iso(now - timedelta(minutes=int(settings["inactivity_timeout_min"]))),),
            ).fetchone()
            operator_id = _choose_operator(conn, settings, now)
            if not waiting or not operator_id:
                break
            was_reassignment = bool(waiting["reassignment_pending"])
            cur = conn.execute(
                """UPDATE operator_dialogs SET status='active',operator_id=?,assigned_at=?,
                   last_activity_at=?,warned_at=NULL,reassignment_pending=0
                   WHERE id=? AND status='waiting'""",
                (operator_id, _iso(now), _iso(now), waiting["id"]),
            )
            if not cur.rowcount:
                continue
            conn.execute(
                "INSERT INTO operator_messages(dialog_id,sender,message_type,body,created_at) "
                "VALUES(?,'system','text',?,?)",
                (
                    waiting["id"],
                    "Оператор сменился и подключился к диалогу."
                    if was_reassignment else "Оператор подключился к диалогу.",
                    _iso(now),
                ),
            )
            assignment_no = conn.execute(
                "SELECT COUNT(*) n FROM operator_messages WHERE dialog_id=? AND sender='system'",
                (waiting["id"],),
            ).fetchone()["n"]
            _enqueue_outbox_locked(
                conn, event_key=f"dialog:{waiting['id']}:assigned:{assignment_no}",
                dialog_id=waiting["id"], chat_id=waiting["chat_id"], kind="text",
                body=(
                    "Извините, оператор не на связи, мы направили вас к другому оператору. "
                    "Напишите ваш вопрос."
                    if was_reassignment else
                    "👨‍💻 Оператор подключился к диалогу. Напишите ваш вопрос."
                ),
                now=now,
            )
            assigned.append({"dialog_id": waiting["id"], "chat_id": waiting["chat_id"], "operator_id": operator_id})
    return assigned


def get_open_dialog_for_chat(chat_id: int) -> dict[str, Any] | None:
    try:
        with _connection() as conn:
            row = conn.execute(
                "SELECT * FROM operator_dialogs WHERE chat_id=? AND status IN ('waiting','active')",
                (chat_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def terminal_notification_owned(chat_id: int) -> bool:
    """Whether durable outbox owns the latest dialog's terminal notification."""
    try:
        with _connection() as conn:
            dialog = conn.execute(
                """SELECT id FROM operator_dialogs WHERE chat_id=?
                   AND status NOT IN ('waiting','active') ORDER BY id DESC LIMIT 1""",
                (chat_id,),
            ).fetchone()
            if not dialog:
                return False
            row = conn.execute(
                """SELECT status,next_retry_at FROM operator_outbox
                   WHERE dialog_id=? AND kind='buttons' AND message_id IS NULL
                     AND (event_key=? OR event_key=? OR event_key=? OR event_key=?
                          OR event_key LIKE 'report:%:closed')
                   ORDER BY id DESC LIMIT 1""",
                (
                    dialog["id"], f"dialog:{dialog['id']}:closed",
                    f"dialog:{dialog['id']}:timed_out",
                    f"dialog:{dialog['id']}:cancelled_by_client",
                    f"dialog:{dialog['id']}:waiting_timeout",
                ),
            ).fetchone()
    except sqlite3.OperationalError:
        return False
    if not row:
        return False
    return row["status"] in {"pending", "sending", "delivered"} or (
        row["status"] == "failed" and row["next_retry_at"] is not None
    )


def ensure_terminal_notification(chat_id: int, now: datetime | None = None) -> dict[str, Any] | None:
    """Return or durably restore the latest dialog's terminal notification."""
    now = now or utc_now()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        dialog = conn.execute(
            """SELECT * FROM operator_dialogs WHERE chat_id=?
               AND status NOT IN ('waiting','active') ORDER BY id DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
        if not dialog:
            return None
        existing = conn.execute(
            """SELECT * FROM operator_outbox WHERE dialog_id=? AND kind='buttons'
               AND message_id IS NULL
               AND (event_key=? OR event_key=? OR event_key=? OR event_key=?
                    OR event_key LIKE 'report:%:closed')
               ORDER BY id DESC LIMIT 1""",
            (
                dialog["id"], f"dialog:{dialog['id']}:closed",
                f"dialog:{dialog['id']}:timed_out",
                f"dialog:{dialog['id']}:cancelled_by_client",
                f"dialog:{dialog['id']}:waiting_timeout",
            ),
        ).fetchone()
        if existing:
            if existing["status"] == "failed" and existing["next_retry_at"] is None:
                conn.execute(
                    """UPDATE operator_outbox SET status='pending',attempts=0,last_error=NULL,
                       next_retry_at=NULL,lease_at=NULL WHERE id=?""",
                    (existing["id"],),
                )
                existing = conn.execute(
                    "SELECT * FROM operator_outbox WHERE id=?", (existing["id"],),
                ).fetchone()
            return dict(existing)

        report = conn.execute(
            "SELECT id FROM operator_client_reports WHERE dialog_id=? ORDER BY id DESC LIMIT 1",
            (dialog["id"],),
        ).fetchone()
        if report:
            event_key = f"report:{report['id']}:closed"
            body, buttons = "Диалог с оператором завершён.", _main_menu_buttons()
        elif dialog["status"] == "timed_out":
            event_key = f"dialog:{dialog['id']}:timed_out"
            body = "Диалог закрыт по бездействию. Оцените работу оператора от 1 до 5."
            buttons = _rating_buttons(dialog["id"])
        elif dialog["status"] == "closed":
            event_key = f"dialog:{dialog['id']}:closed"
            body = "Оператор завершил диалог. Оцените его работу от 1 до 5."
            buttons = _rating_buttons(dialog["id"])
        else:
            event_key = f"dialog:{dialog['id']}:waiting_timeout"
            body, buttons = "Диалог с оператором завершён.", _main_menu_buttons()
        outbox_id = _enqueue_outbox_locked(
            conn, event_key=event_key, dialog_id=dialog["id"], chat_id=chat_id,
            kind="buttons", body=body, buttons=buttons, now=now,
        )
        row = conn.execute("SELECT * FROM operator_outbox WHERE id=?", (outbox_id,)).fetchone()
        return dict(row)


def add_message(dialog_id: int, sender: str, body: str | None, *, user_id: int | None = None,
                image_path: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    body = (body or "").strip()
    if not body and not image_path:
        raise ValueError("Пустое сообщение")
    if sender == "operator" and len(body) > MAX_OPERATOR_BODY:
        raise ValueError(f"Сообщение должно быть не длиннее {MAX_OPERATOR_BODY} символов")
    if sender != "operator" and len(body) > 4000:
        raise ValueError("Сообщение слишком длинное")
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        dialog = conn.execute("SELECT * FROM operator_dialogs WHERE id=?", (dialog_id,)).fetchone()
        if not dialog or dialog["status"] != "active":
            raise ValueError("Диалог не активен")
        if sender == "operator" and dialog["operator_id"] != user_id:
            raise PermissionError("Диалог назначен другому оператору")
        now_s = _iso(now)
        delivery_status = "pending" if sender == "operator" else "delivered"
        cur = conn.execute(
            """INSERT INTO operator_messages
               (dialog_id,sender,sender_user_id,message_type,body,image_path,created_at,delivery_status)
               VALUES(?,?,?,?,?,?,?,?)""",
            (dialog_id, sender, user_id, "image" if image_path else "text", body or None, image_path, now_s, delivery_status),
        )
        message_id = int(cur.lastrowid)
        if sender == "operator":
            _enqueue_outbox_locked(
                conn, event_key=f"message:{message_id}", chat_id=dialog["chat_id"],
                dialog_id=dialog_id,
                message_id=message_id, kind="image" if image_path else "text",
                body=f"{OPERATOR_PREFIX}{body}" if body else "Оператор отправил изображение.",
                image_path=image_path, now=now,
            )
        if sender != "operator":
            warning_rows = conn.execute(
                """SELECT id,status,lease_at FROM operator_outbox
                   WHERE dialog_id=? AND event_key LIKE ? AND status!='delivered'""",
                (dialog_id, f"dialog:{dialog_id}:warning:%"),
            ).fetchall()
            had_live_warning = any(
                item["status"] == "sending"
                and item["lease_at"]
                and item["lease_at"] > _iso(now - timedelta(minutes=2))
                for item in warning_rows
            )
            # A row with a live lease may already be inside the external MAX
            # request.  Keep that lease intact so causal ordering makes the
            # correction wait for its CAS finalizer.  Pending/retryable/stale
            # warnings are safe to supersede immediately.
            for warning in warning_rows:
                is_live = (
                    warning["status"] == "sending"
                    and warning["lease_at"]
                    and warning["lease_at"] > _iso(now - timedelta(minutes=2))
                )
                if not is_live:
                    conn.execute(
                        """UPDATE operator_outbox SET status='failed',attempts=?,
                           next_retry_at=NULL,lease_at=NULL,
                           last_error='superseded_by_client_activity'
                           WHERE id=? AND status!='delivered'""",
                        (MAX_OUTBOX_ATTEMPTS, warning["id"]),
                    )
            conn.execute(
                "UPDATE operator_dialogs SET last_activity_at=?,warned_at=NULL WHERE id=?",
                (now_s, dialog_id),
            )
            if had_live_warning:
                _enqueue_outbox_locked(
                    conn,
                    event_key=f"dialog:{dialog_id}:warning_correction:{message_id}",
                    dialog_id=dialog_id, chat_id=dialog["chat_id"], kind="text",
                    body="Активность получена. Диалог остаётся открытым.",
                    now=now,
                )
        row = conn.execute("SELECT * FROM operator_messages WHERE id=?", (message_id,)).fetchone()
    return dict(row)


def list_operator_dialogs(user_id: int) -> list[dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute(
            """SELECT d.*,
               (SELECT COUNT(*) FROM operator_messages m WHERE m.dialog_id=d.id
                AND m.sender='client' AND m.read_at IS NULL) unread
               FROM operator_dialogs d WHERE d.operator_id=? AND d.status='active'
               ORDER BY d.assigned_at,d.id""",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_dialog_for_operator(dialog_id: int, user_id: int) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM operator_dialogs WHERE id=? AND operator_id=? AND status='active'",
            (dialog_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def get_reportable_image(dialog_id: int, user_id: int, message_id: int) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            """SELECT m.* FROM operator_messages m
               JOIN operator_dialogs d ON d.id=m.dialog_id
               WHERE m.id=? AND m.dialog_id=? AND m.sender='client'
                 AND m.message_type='image' AND d.operator_id=? AND d.status='active'""",
            (message_id, dialog_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def list_messages(dialog_id: int, user_id: int, after_id: int = 0) -> list[dict[str, Any]]:
    if not get_dialog_for_operator(dialog_id, user_id):
        raise PermissionError
    with _connection() as conn:
        rows = conn.execute(
            """SELECT * FROM operator_messages WHERE dialog_id=? AND (
                 id>? OR id IN (SELECT id FROM operator_messages
                   WHERE dialog_id=? AND sender='operator' ORDER BY id DESC LIMIT 20)
               ) ORDER BY id""",
            (dialog_id, after_id, dialog_id),
        ).fetchall()
        conn.execute(
            "UPDATE operator_messages SET read_at=? WHERE dialog_id=? AND sender='client' AND read_at IS NULL",
            (_iso(), dialog_id),
        )
    return [dict(row) for row in rows]


def close_dialog(dialog_id: int, operator_id: int, now: datetime | None = None) -> int:
    now = now or utc_now()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT chat_id FROM operator_dialogs WHERE id=? AND operator_id=? AND status='active'",
            (dialog_id, operator_id),
        ).fetchone()
        if not row:
            raise PermissionError
        live_send = conn.execute(
            """SELECT 1 FROM operator_outbox WHERE dialog_id=? AND status='sending'
               AND lease_at>? LIMIT 1""",
            (dialog_id, _iso(now - timedelta(minutes=2))),
        ).fetchone()
        if live_send:
            raise DeliveryInProgressError(
                "Ответ оператора сейчас отправляется. Повторите завершение через несколько секунд"
            )
        undelivered = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN image_path IS NOT NULL THEN 1 ELSE 0 END) images
               FROM operator_messages
               WHERE dialog_id=? AND sender='operator' AND delivery_status!='delivered'""",
            (dialog_id,),
        ).fetchone()
        if undelivered["total"]:
            raise UndeliveredMessagesError(
                total=int(undelivered["total"]), images=int(undelivered["images"] or 0),
            )
        _supersede_dialog_events_locked(conn, dialog_id, "superseded_by_close")
        conn.execute(
            "UPDATE operator_dialogs SET status='closed',closed_at=?,closed_by=? WHERE id=?",
            (_iso(now), operator_id, dialog_id),
        )
        chat_id = row["chat_id"]
        _enqueue_outbox_locked(
            conn, event_key=f"dialog:{dialog_id}:closed", chat_id=chat_id, kind="buttons",
            dialog_id=dialog_id,
            body="Оператор завершил диалог. Оцените его работу от 1 до 5.",
            buttons=_rating_buttons(dialog_id), now=now,
        )
    return chat_id


def rate_dialog(chat_id: int, dialog_id: int, rating: int, now: datetime | None = None) -> bool:
    if rating not in range(1, 6):
        raise ValueError
    with _connection() as conn:
        cur = conn.execute(
            """UPDATE operator_dialogs SET rating=?,rated_at=?
               WHERE id=? AND chat_id=? AND status IN ('closed','timed_out') AND rating IS NULL""",
            (rating, _iso(now), dialog_id, chat_id),
        )
    return bool(cur.rowcount)


def rating_report() -> list[dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute(
            """SELECT u.id,u.name,ROUND(AVG(d.rating),2) average_rating,COUNT(d.rating) rating_count
               FROM users u LEFT JOIN operator_dialogs d ON d.operator_id=u.id AND d.rating IS NOT NULL
               WHERE u.role='operator' GROUP BY u.id,u.name ORDER BY u.name"""
        ).fetchall()
    return [dict(row) for row in rows]


def list_history(limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with _connection() as conn:
        rows = conn.execute(
            """SELECT d.*,u.name operator_name FROM operator_dialogs d
               LEFT JOIN users u ON u.id=d.operator_id
               WHERE d.status IN ('closed','timed_out','cancelled')
               ORDER BY COALESCE(d.closed_at,d.created_at) DESC,d.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_history_dialog(dialog_id: int) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            """SELECT d.*,u.name operator_name FROM operator_dialogs d
               LEFT JOIN users u ON u.id=d.operator_id
               WHERE d.id=? AND d.status IN ('closed','timed_out','cancelled')""",
            (dialog_id,),
        ).fetchone()
    return dict(row) if row else None


def list_history_messages(dialog_id: int) -> list[dict[str, Any]]:
    if not get_history_dialog(dialog_id):
        raise LookupError
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM operator_messages WHERE dialog_id=? ORDER BY id", (dialog_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_client_report(
    dialog_id: int,
    operator_id: int,
    reason: str,
    comment: str | None,
    *,
    image_message_id: int | None = None,
    evidence_path: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Close an assigned dialog and create a pending, immutable report snapshot."""
    reason = reason.strip()
    comment = (comment or "").strip()
    if reason not in REPORT_REASONS:
        raise ValueError("Неизвестная причина жалобы")
    if reason == "other" and not comment:
        raise ValueError("Для причины «Другое» обязателен комментарий")
    if len(comment) > 2000:
        raise ValueError("Комментарий слишком длинный")
    now = now or utc_now()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        dialog = conn.execute(
            """SELECT * FROM operator_dialogs
               WHERE id=? AND operator_id=? AND status='active'""",
            (dialog_id, operator_id),
        ).fetchone()
        if not dialog:
            raise PermissionError
        if not _supersede_operator_delivery_locked(
            conn, dialog_id, "cancelled_by_report", now,
        ):
            raise DeliveryInProgressError(
                "Ответ оператора сейчас отправляется. Повторите жалобу через несколько секунд"
            )
        if image_message_id is not None:
            evidence = conn.execute(
                """SELECT 1 FROM operator_messages WHERE id=? AND dialog_id=?
                   AND sender='client' AND message_type='image'""",
                (image_message_id, dialog_id),
            ).fetchone()
            if not evidence:
                raise ValueError("Изображение не относится к этому диалогу")
        messages = conn.execute(
            """SELECT id,sender,message_type,body,created_at FROM operator_messages
               WHERE dialog_id=? ORDER BY id DESC LIMIT 20""",
            (dialog_id,),
        ).fetchall()
        snapshot = [dict(row) for row in reversed(messages)]
        cur = conn.execute(
            """INSERT INTO operator_client_reports
               (dialog_id,operator_id,client_chat_id,reason,comment,image_message_id,
                evidence_path,snapshot_json,status,created_at)
               VALUES(?,?,?,?,?,?,?,?, 'pending',?)""",
            (
                dialog_id, operator_id, dialog["chat_id"], reason, comment or None,
                image_message_id, evidence_path,
                json.dumps(snapshot, ensure_ascii=False), _iso(now),
            ),
        )
        report_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE operator_dialogs SET status='closed',closed_at=?,closed_by=? WHERE id=?",
            (_iso(now), operator_id, dialog_id),
        )
        _supersede_dialog_events_locked(conn, dialog_id, "cancelled_by_report")
        _enqueue_outbox_locked(
            conn, event_key=f"report:{report_id}:closed", dialog_id=dialog_id,
            chat_id=dialog["chat_id"], kind="buttons",
            body="Диалог с оператором завершён.", buttons=_main_menu_buttons(), now=now,
        )
        row = conn.execute(
            "SELECT * FROM operator_client_reports WHERE id=?", (report_id,),
        ).fetchone()
    return dict(row)


def list_client_reports(status: str | None = None) -> list[dict[str, Any]]:
    params: tuple[Any, ...] = ()
    where = ""
    if status in {"pending", "confirmed", "rejected"}:
        where = "WHERE r.status=?"
        params = (status,)
    with _connection() as conn:
        rows = conn.execute(
            f"""SELECT r.*,u.name operator_name,reviewer.name reviewer_name
                FROM operator_client_reports r
                LEFT JOIN users u ON u.id=r.operator_id
                LEFT JOIN users reviewer ON reviewer.id=r.decided_by
                {where} ORDER BY r.created_at DESC,r.id DESC""",  # nosec B608 - fixed allowlisted clause
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_client_report(report_id: int) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            """SELECT r.*,u.name operator_name,reviewer.name reviewer_name
               FROM operator_client_reports r
               LEFT JOIN users u ON u.id=r.operator_id
               LEFT JOIN users reviewer ON reviewer.id=r.decided_by
               WHERE r.id=?""",
            (report_id,),
        ).fetchone()
    return dict(row) if row else None


def decide_client_report(
    report_id: int, admin_id: int, decision: str, now: datetime | None = None,
) -> dict[str, Any]:
    if decision not in {"confirmed", "rejected"}:
        raise ValueError
    now = now or utc_now()
    settings = get_settings()
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        report = conn.execute(
            "SELECT * FROM operator_client_reports WHERE id=?", (report_id,),
        ).fetchone()
        if not report:
            raise LookupError
        if report["status"] != "pending":
            if report["status"] != decision:
                raise ReportDecisionConflictError(
                    "Решение по жалобе уже принято и не может быть изменено"
                )
            count = conn.execute(
                """SELECT COUNT(*) n FROM operator_client_reports
                   WHERE client_chat_id=? AND status='confirmed'""",
                (report["client_chat_id"],),
            ).fetchone()["n"]
            active_block = conn.execute(
                "SELECT 1 FROM operator_chat_blocks WHERE chat_id=? AND active=1",
                (report["client_chat_id"],),
            ).fetchone()
            result = dict(report)
            result.update(confirmed_count=int(count), blocked=active_block is not None)
            return result
        confirmed_before = conn.execute(
            """SELECT COUNT(*) n FROM operator_client_reports
               WHERE client_chat_id=? AND status='confirmed'""",
            (report["client_chat_id"],),
        ).fetchone()["n"]
        will_block = (
            decision == "confirmed"
            and int(confirmed_before) + 1 >= int(settings["report_threshold"])
        )
        affected_dialogs = []
        if will_block:
            affected_dialogs = conn.execute(
                """SELECT id FROM operator_dialogs
                   WHERE chat_id=? AND status IN ('waiting','active')""",
                (report["client_chat_id"],),
            ).fetchall()
            for dialog in affected_dialogs:
                if not _supersede_operator_delivery_locked(
                    conn, dialog["id"], "cancelled_by_block", now,
                ):
                    raise DeliveryInProgressError(
                        "Сообщение клиенту сейчас отправляется. "
                        "Повторите решение позже"
                    )
                _supersede_dialog_events_locked(conn, dialog["id"], "cancelled_by_block")
        transitioned = conn.execute(
            """UPDATE operator_client_reports SET status=?,decided_at=?,decided_by=?
               WHERE id=? AND status='pending'""",
            (decision, _iso(now), admin_id, report_id),
        )
        if transitioned.rowcount != 1:
            raise ReportDecisionConflictError("Решение по жалобе уже изменилось")
        count = conn.execute(
            """SELECT COUNT(*) n FROM operator_client_reports
               WHERE client_chat_id=? AND status='confirmed'""",
            (report["client_chat_id"],),
        ).fetchone()["n"]
        blocked = False
        if decision == "confirmed" and count >= int(settings["report_threshold"]):
            blocked = True
            conn.execute(
                """INSERT INTO operator_chat_blocks(chat_id,active,blocked_at,blocked_by)
                   VALUES(?,1,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                   active=1,blocked_at=excluded.blocked_at,blocked_by=excluded.blocked_by,
                   unblocked_at=NULL,unblocked_by=NULL""",
                (report["client_chat_id"], _iso(now), admin_id),
            )
            for dialog in affected_dialogs:
                conn.execute(
                    "UPDATE operator_dialogs SET status='cancelled',closed_at=? WHERE id=?",
                    (_iso(now), dialog["id"]),
                )
        saved = conn.execute(
            "SELECT * FROM operator_client_reports WHERE id=?", (report_id,),
        ).fetchone()
    result = dict(saved)
    result.update(confirmed_count=int(count), blocked=blocked)
    return result


def list_blocked_clients() -> list[dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute(
            """SELECT b.*,(SELECT COUNT(*) FROM operator_client_reports r
                 WHERE r.client_chat_id=b.chat_id AND r.status='confirmed') confirmed_count
               FROM operator_chat_blocks b WHERE b.active=1 ORDER BY b.blocked_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def unblock_client(chat_id: int, admin_id: int, now: datetime | None = None) -> bool:
    with _connection() as conn:
        cur = conn.execute(
            """UPDATE operator_chat_blocks SET active=0,unblocked_at=?,unblocked_by=?
               WHERE chat_id=? AND active=1""",
            (_iso(now), admin_id, chat_id),
        )
    return bool(cur.rowcount)


def process_timeouts(now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return notification work after atomically updating stale/warn/closed rows."""
    now = now or utc_now()
    settings = get_settings()
    result: dict[str, list[dict[str, Any]]] = {
        "requeued": [], "warned": [], "closed": [], "waiting_closed": [], "assigned": [],
    }
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        stale = conn.execute(
            """SELECT d.id,d.chat_id,d.assigned_at FROM operator_dialogs d LEFT JOIN operator_shifts s ON s.user_id=d.operator_id
               WHERE d.status='active' AND (s.user_id IS NULL OR s.active=0 OR s.heartbeat_at<?)""",
            (_requeue_cutoff(settings, now),),
        ).fetchall()
        for row in stale:
            if not _supersede_operator_delivery_locked(
                conn, row["id"], "superseded_by_requeue", now,
            ):
                continue
            _supersede_dialog_events_locked(conn, row["id"], "superseded_by_requeue")
            queue_seq = conn.execute("SELECT COALESCE(MAX(queue_seq),0)+1 n FROM operator_dialogs").fetchone()["n"]
            conn.execute(
                """UPDATE operator_dialogs SET status='waiting',operator_id=NULL,queue_seq=?,
                   assigned_at=NULL,warned_at=NULL,last_activity_at=?,reassignment_pending=1
                   WHERE id=?""",
                (queue_seq, _iso(now), row["id"]),
            )
            result["requeued"].append(dict(row))
        warn_at = _iso(now - timedelta(minutes=int(settings["inactivity_timeout_min"] - settings["warning_before_min"])))
        close_at = _iso(now - timedelta(minutes=int(settings["inactivity_timeout_min"])))
        warns = conn.execute(
            "SELECT id,chat_id,last_activity_at FROM operator_dialogs WHERE status='active' AND warned_at IS NULL AND last_activity_at<=? AND last_activity_at>?",
            (warn_at, close_at),
        ).fetchall()
        for row in warns:
            conn.execute("UPDATE operator_dialogs SET warned_at=? WHERE id=?", (_iso(now), row["id"]))
            result["warned"].append(dict(row))
            _enqueue_outbox_locked(
                conn, event_key=f"dialog:{row['id']}:warning:{row['last_activity_at']}", chat_id=row["chat_id"],
                dialog_id=row["id"],
                kind="text",
                body=f"Диалог будет закрыт через {settings['warning_before_min']} мин. без новых сообщений.",
                now=now,
            )
        closes = conn.execute(
            "SELECT id,chat_id FROM operator_dialogs WHERE status='active' AND last_activity_at<=?",
            (close_at,),
        ).fetchall()
        for row in closes:
            if not _supersede_operator_delivery_locked(
                conn, row["id"], "superseded_by_timeout", now,
            ):
                continue
            _supersede_dialog_events_locked(conn, row["id"], "superseded_by_timeout")
            conn.execute("UPDATE operator_dialogs SET status='timed_out',closed_at=? WHERE id=?", (_iso(now), row["id"]))
            result["closed"].append(dict(row))
            _enqueue_outbox_locked(
                conn, event_key=f"dialog:{row['id']}:timed_out", chat_id=row["chat_id"],
                dialog_id=row["id"],
                kind="buttons",
                body="Диалог закрыт по бездействию. Оцените работу оператора от 1 до 5.",
                buttons=_rating_buttons(row["id"]), now=now,
            )
        waiting_cutoff = _iso(now - timedelta(minutes=int(settings["inactivity_timeout_min"])))
        expired_waiting = conn.execute(
            "SELECT id,chat_id FROM operator_dialogs WHERE status='waiting' AND last_activity_at<=?",
            (waiting_cutoff,),
        ).fetchall()
        for row in expired_waiting:
            conn.execute(
                "UPDATE operator_dialogs SET status='cancelled',closed_at=? WHERE id=?",
                (_iso(now), row["id"]),
            )
            result["waiting_closed"].append(dict(row))
            _enqueue_outbox_locked(
                conn, event_key=f"dialog:{row['id']}:waiting_timeout", chat_id=row["chat_id"],
                dialog_id=row["id"],
                kind="buttons", body="Время ожидания оператора истекло. Пожалуйста, попробуйте позже.",
                buttons=_main_menu_buttons(),
                now=now,
            )
    result["assigned"] = assign_waiting(now=now)
    return result


def get_outbox_item_for_message(message_id: int) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM operator_outbox WHERE message_id=?", (message_id,),
        ).fetchone()
    return dict(row) if row else None


def retry_message(message_id: int, operator_id: int, now: datetime | None = None) -> bool:
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT o.id FROM operator_outbox o JOIN operator_messages m ON m.id=o.message_id
               JOIN operator_dialogs d ON d.id=m.dialog_id
               WHERE m.id=? AND m.sender_user_id=? AND d.operator_id=? AND d.status='active'
                 AND o.status='failed'""",
            (message_id, operator_id, operator_id),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            """UPDATE operator_outbox SET status='pending',attempts=0,next_retry_at=?,
               lease_at=NULL,last_error=NULL WHERE id=?""",
            (_iso(now), row["id"]),
        )
        conn.execute(
            "UPDATE operator_messages SET delivery_status='pending',last_error=NULL WHERE id=?",
            (message_id,),
        )
    return True


def delete_undelivered_image(message_id: int, operator_id: int) -> str | None:
    """Cancel and delete the operator's own unsent image, returning its file name."""
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT m.image_path,m.delivery_status,o.id outbox_id,o.status outbox_status
               FROM operator_messages m
               JOIN operator_dialogs d ON d.id=m.dialog_id
               JOIN operator_outbox o ON o.message_id=m.id
               WHERE m.id=? AND m.sender='operator' AND m.message_type='image'
                 AND m.sender_user_id=? AND d.operator_id=? AND d.status='active'""",
            (message_id, operator_id, operator_id),
        ).fetchone()
        if not row:
            raise PermissionError
        if row["delivery_status"] == "delivered":
            raise ValueError("Доставленное изображение удалить нельзя")
        if row["outbox_status"] == "sending":
            raise ValueError("Изображение сейчас отправляется. Повторите позже")
        conn.execute("DELETE FROM operator_outbox WHERE id=?", (row["outbox_id"],))
        conn.execute("DELETE FROM operator_messages WHERE id=?", (message_id,))
    return row["image_path"]


Delivery = Callable[[dict[str, Any]], tuple[bool, str | None]]


def set_outbox_upload_token(outbox_id: int, token: str) -> str:
    """Persist an image token once so later retries never create a new upload."""
    if not token:
        raise ValueError("empty upload token")
    with _connection() as conn:
        conn.execute(
            "UPDATE operator_outbox SET upload_token=? WHERE id=? AND upload_token IS NULL",
            (token, outbox_id),
        )
        row = conn.execute("SELECT upload_token FROM operator_outbox WHERE id=?", (outbox_id,)).fetchone()
    if not row:
        raise LookupError(outbox_id)
    return str(row["upload_token"])


def extract_image_upload_token(payload: Any) -> str | None:
    """Read both documented MAX image upload response variants."""
    if not isinstance(payload, dict):
        return None
    direct = payload.get("token")
    if isinstance(direct, str) and direct:
        return direct
    photos = payload.get("photos")
    if isinstance(photos, dict):
        for photo in photos.values():
            if isinstance(photo, dict):
                token = photo.get("token")
                if isinstance(token, str) and token:
                    return token
    return None


def _retryable_delivery_error(error: str) -> bool:
    return bool(
        error in {
            "max_timeout", "max_transport", "transport_error",
            "attachment_not_ready",
        }
        or error == "max_http_429"
        or error.startswith("max_http_5")
    )


def deliver_outbox(deliver: Delivery, *, now: datetime | None = None, max_items: int = 50,
                   only_id: int | None = None) -> list[dict[str, Any]]:
    """Claim due rows with per-recipient causal ordering and bounded retries.

    Delivery is necessarily at-least-once: if MAX accepts a request and this
    process dies before committing ``delivered``, the expired lease is retried.
    MAX exposes no idempotency key for this endpoint, so that narrow ambiguous
    success-before-DB-commit window can produce one duplicate.
    """
    now = now or utc_now()
    results: list[dict[str, Any]] = []
    lease_value = _iso(now)
    for _ in range(max_items):
        with _connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT o.* FROM operator_outbox o
                   WHERE ((o.status='pending' OR (o.status='failed' AND o.next_retry_at<=?))
                      OR (o.status='sending' AND o.lease_at<=?))
                   AND (? IS NULL OR o.id=?)
                   AND NOT EXISTS (
                     SELECT 1 FROM operator_outbox earlier
                     WHERE earlier.chat_id=o.chat_id AND earlier.id<o.id
                       AND earlier.status!='delivered'
                       AND NOT (earlier.status='failed' AND earlier.next_retry_at IS NULL)
                   ) ORDER BY o.id LIMIT 1""",
                (
                    _iso(now), _iso(now - timedelta(minutes=2)),
                    only_id, only_id,
                ),
            ).fetchone()
            if not row:
                break
            if ":warning:" in row["event_key"]:
                dialog = conn.execute(
                    "SELECT last_activity_at FROM operator_dialogs WHERE id=?",
                    (row["dialog_id"],),
                ).fetchone()
                expected = (
                    f"dialog:{row['dialog_id']}:warning:{dialog['last_activity_at']}"
                    if dialog else None
                )
                if row["event_key"] != expected:
                    conn.execute(
                        """UPDATE operator_outbox SET status='failed',attempts=?,
                           next_retry_at=NULL,last_error='superseded_by_client_activity'
                           WHERE id=?""",
                        (MAX_OUTBOX_ATTEMPTS, row["id"]),
                    )
                    continue
            claimed = conn.execute(
                "UPDATE operator_outbox SET status='sending',lease_at=?,attempts=attempts+1 WHERE id=? AND status=?",
                (lease_value, row["id"], row["status"]),
            )
            if not claimed.rowcount:
                continue
            item = dict(row)
            item["attempts"] = int(row["attempts"]) + 1
        try:
            success, error = deliver(item)
        except Exception:  # noqa: BLE001 - delivery boundary must persist safe failure
            success, error = False, "transport_error"
        safe_error = (error or "delivery_failed")[:80]
        with _connection() as conn:
            status = "delivered" if success else "failed"
            retryable = not success and _retryable_delivery_error(safe_error)
            retry_at = (
                _iso(now + timedelta(seconds=min(300, 2 ** min(item["attempts"], 8))))
                if retryable and item["attempts"] < MAX_OUTBOX_ATTEMPTS else None
            )
            attempts = item["attempts"] if retry_at is not None or success else MAX_OUTBOX_ATTEMPTS
            finalized = conn.execute(
                """UPDATE operator_outbox SET status=?,attempts=?,last_error=?,next_retry_at=?,
                   delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END
                   WHERE id=? AND status='sending' AND lease_at=?""",
                (
                    status, attempts, None if success else safe_error, retry_at,
                    status, _iso(now), item["id"], lease_value,
                ),
            )
            if item.get("message_id") and finalized.rowcount == 1:
                conn.execute(
                    """UPDATE operator_messages SET delivery_status=?,delivery_attempts=delivery_attempts+1,
                       last_error=?,delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END
                       WHERE id=?""",
                    (status, None if success else safe_error, status, _iso(now), item["message_id"]),
                )
                if success:
                    conn.execute(
                        """UPDATE operator_dialogs SET last_activity_at=?,warned_at=NULL
                           WHERE id=(SELECT dialog_id FROM operator_messages WHERE id=?)""",
                        (_iso(now), item["message_id"]),
                    )
            if finalized.rowcount != 1:
                current = conn.execute(
                    "SELECT status,last_error,next_retry_at FROM operator_outbox WHERE id=?",
                    (item["id"],),
                ).fetchone()
                status = current["status"] if current else "failed"
                safe_error = (
                    current["last_error"] if current and current["last_error"]
                    else "delivery_superseded"
                )
                retry_at = current["next_retry_at"] if current else None
        results.append({
            "id": item["id"], "status": status,
            "error": None if status == "delivered" else safe_error,
            "retryable": bool(retry_at),
        })
        if only_id is not None:
            break
    return results


def cleanup_history(image_root: str | os.PathLike[str], now: datetime | None = None) -> int:
    now = now or utc_now()
    settings = get_settings()
    cutoff = _iso(now - timedelta(days=int(settings["retention_days"])))
    evidence_cutoff = _iso(
        now - timedelta(days=int(settings["evidence_retention_days"]))
    )
    root = Path(image_root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _connection() as conn:
        rows = conn.execute(
            """SELECT m.id,m.dialog_id,m.image_path FROM operator_messages m
               JOIN operator_dialogs d ON d.id=m.dialog_id
               WHERE d.status NOT IN ('waiting','active') AND d.closed_at<? ORDER BY m.id LIMIT 500""",
            (cutoff,),
        ).fetchall()
    for row in rows:
        removable = True
        if row["image_path"]:
            candidate = (root / row["image_path"]).resolve()
            if candidate.parent != root:
                removable = False
            else:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    removable = False
        with _connection() as conn:
            if removable:
                conn.execute("DELETE FROM operator_messages WHERE id=?", (row["id"],))
            else:
                conn.execute(
                    "UPDATE operator_messages SET body=NULL,cleanup_pending=1 WHERE id=?",
                    (row["id"],),
                )
    with _connection() as conn:
        evidence_rows = conn.execute(
            """SELECT id,evidence_path FROM operator_client_reports
               WHERE created_at<? AND (snapshot_json!='[]' OR evidence_path IS NOT NULL)
               ORDER BY id LIMIT 500""",
            (evidence_cutoff,),
        ).fetchall()
    for row in evidence_rows:
        removed = False
        candidate = (root / row["evidence_path"]).resolve() if row["evidence_path"] else None
        if candidate is None:
            removed = True
        elif candidate.parent == root:
            try:
                candidate.unlink(missing_ok=True)
                removed = True
            except OSError:
                pass
        with _connection() as conn:
            conn.execute(
                "UPDATE operator_client_reports SET snapshot_json='[]',image_message_id=NULL WHERE id=?",
                (row["id"],),
            )
            if removed:
                conn.execute(
                    "UPDATE operator_client_reports SET evidence_path=NULL WHERE id=?",
                    (row["id"],),
                )
    with _connection() as conn:
        conn.execute("DELETE FROM operator_outbox WHERE created_at<?", (cutoff,))
        ids = [row["id"] for row in conn.execute(
            "SELECT id FROM operator_dialogs WHERE status NOT IN ('waiting','active') AND closed_at<?",
            (cutoff,),
        ).fetchall()]
        scrubbed = 0
        for dialog_id in ids:
            remaining = conn.execute(
                "SELECT COUNT(*) n FROM operator_messages WHERE dialog_id=?", (dialog_id,),
            ).fetchone()["n"]
            if not remaining:
                conn.execute(
                    """UPDATE operator_dialogs SET chat_id=0,client_fio=NULL,client_ls=NULL,client_address=NULL,
                       faq_context=NULL,ai_context_json='[]' WHERE id=?""", (dialog_id,),
                )
                scrubbed += 1
        referenced = {
            row["image_path"] for row in conn.execute(
                "SELECT image_path FROM operator_messages WHERE image_path IS NOT NULL"
            ).fetchall()
        }
        referenced.update(
            row["evidence_path"] for row in conn.execute(
                "SELECT evidence_path FROM operator_client_reports WHERE evidence_path IS NOT NULL"
            ).fetchall()
        )
    orphan_cutoff = now.timestamp() - 3600
    removed_orphans = 0
    for candidate in root.glob("*.jpg"):
        if removed_orphans >= 100 or candidate.name in referenced:
            continue
        try:
            if candidate.is_symlink() or candidate.stat().st_mtime > orphan_cutoff:
                continue
            candidate.unlink()
            removed_orphans += 1
        except OSError:
            continue
    return scrubbed


def normalize_image(data: bytes, content_type: str | None = None) -> bytes:
    """Decode JPEG/PNG/WebP by content and store a metadata-free JPEG."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение должно быть не более 5 МБ")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("Поддерживаются только JPEG, PNG и WebP")
                width, height = probe.size
                if (
                    width <= 0 or height <= 0
                    or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION
                    or width * height > MAX_IMAGE_PIXELS
                ):
                    raise ValueError("Недопустимые размеры изображения")
                probe.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()
                normalized = ImageOps.exif_transpose(decoded).convert("RGB")
                output = io.BytesIO()
                normalized.save(output, format="JPEG", quality=90, optimize=True)
    except (
        Image.DecompressionBombError, Image.DecompressionBombWarning,
        UnidentifiedImageError, OSError, SyntaxError,
    ) as exc:
        raise ValueError("Повреждённое изображение") from exc
    result = output.getvalue()
    if len(result) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение после обработки превышает 5 МБ")
    return result


def normalize_jpeg(data: bytes, content_type: str | None) -> bytes:
    """Compatibility wrapper; validation deliberately does not trust MIME."""
    return normalize_image(data, content_type)


def validate_jpeg(data: bytes, content_type: str | None) -> None:
    normalize_image(data, content_type)


