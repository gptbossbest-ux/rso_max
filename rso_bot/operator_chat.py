"""Operator chat domain service shared by the MAX bot and web portal.

All assignment decisions are made under ``BEGIN IMMEDIATE`` so two bot/web
workers cannot assign the same operator capacity or queue item concurrently.
The module never logs message bodies or customer profile fields.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import database as db

ACTIVE_STATUSES = ("waiting", "active")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
JPEG_SIGNATURES = (b"\xff\xd8\xff",)
MODULE_KEYS = (
    "auth", "appeal", "appeal_status", "readings", "faq", "ai",
    "receipt", "appointment",
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


def get_settings() -> dict[str, Any]:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM operator_chat_settings WHERE id=1").fetchone()
    return dict(row)


def update_settings(**values: Any) -> None:
    numeric = {
        "heartbeat_timeout_min": (1, 60),
        "max_active_dialogs": (1, 50),
        "inactivity_timeout_min": (5, 1440),
        "warning_before_min": (1, 1439),
        "retention_days": (1, 3650),
    }
    clean: dict[str, Any] = {
        "enabled": int(bool(values["enabled"])),
        "require_auth": int(bool(values["require_auth"])),
    }
    for key, (low, high) in numeric.items():
        value = int(values[key])
        if not low <= value <= high:
            raise ValueError(f"{key}: допустимое значение {low}–{high}")
        clean[key] = value
    if clean["warning_before_min"] >= clean["inactivity_timeout_min"]:
        raise ValueError("Предупреждение должно быть раньше закрытия")
    with _connection() as conn:
        conn.execute(
            """UPDATE operator_chat_settings SET enabled=:enabled,
               require_auth=:require_auth, heartbeat_timeout_min=:heartbeat_timeout_min,
               max_active_dialogs=:max_active_dialogs,
               inactivity_timeout_min=:inactivity_timeout_min,
               warning_before_min=:warning_before_min, retention_days=:retention_days
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
    return _iso(now - timedelta(minutes=int(settings["heartbeat_timeout_min"])))


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
        cur = conn.execute(
            "UPDATE operator_dialogs SET status='cancelled',closed_at=? "
            "WHERE chat_id=? AND status='waiting'",
            (_iso(now), chat_id),
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
                "SELECT * FROM operator_dialogs WHERE status='waiting' ORDER BY queue_seq,created_at,id LIMIT 1"
            ).fetchone()
            operator_id = _choose_operator(conn, settings, now)
            if not waiting or not operator_id:
                break
            cur = conn.execute(
                """UPDATE operator_dialogs SET status='active',operator_id=?,assigned_at=?,
                   last_activity_at=?,warned_at=NULL WHERE id=? AND status='waiting'""",
                (operator_id, _iso(now), _iso(now), waiting["id"]),
            )
            if not cur.rowcount:
                continue
            conn.execute(
                "INSERT INTO operator_messages(dialog_id,sender,message_type,body,created_at) "
                "VALUES(?,'system','text',?,?)",
                (waiting["id"], "Оператор подключился к диалогу.", _iso(now)),
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


def add_message(dialog_id: int, sender: str, body: str | None, *, user_id: int | None = None,
                image_path: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    body = (body or "").strip()
    if not body and not image_path:
        raise ValueError("Пустое сообщение")
    if len(body) > 4000:
        raise ValueError("Сообщение слишком длинное")
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        dialog = conn.execute("SELECT * FROM operator_dialogs WHERE id=?", (dialog_id,)).fetchone()
        if not dialog or dialog["status"] != "active":
            raise ValueError("Диалог не активен")
        if sender == "operator" and dialog["operator_id"] != user_id:
            raise PermissionError("Диалог назначен другому оператору")
        now_s = _iso(now)
        cur = conn.execute(
            """INSERT INTO operator_messages
               (dialog_id,sender,sender_user_id,message_type,body,image_path,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (dialog_id, sender, user_id, "image" if image_path else "text", body or None, image_path, now_s),
        )
        conn.execute(
            "UPDATE operator_dialogs SET last_activity_at=?,warned_at=NULL WHERE id=?",
            (now_s, dialog_id),
        )
        row = conn.execute("SELECT * FROM operator_messages WHERE id=?", (cur.lastrowid,)).fetchone()
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


def list_messages(dialog_id: int, user_id: int, after_id: int = 0) -> list[dict[str, Any]]:
    if not get_dialog_for_operator(dialog_id, user_id):
        raise PermissionError
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM operator_messages WHERE dialog_id=? AND id>? ORDER BY id",
            (dialog_id, after_id),
        ).fetchall()
        conn.execute(
            "UPDATE operator_messages SET read_at=? WHERE dialog_id=? AND sender='client' AND read_at IS NULL",
            (_iso(), dialog_id),
        )
    return [dict(row) for row in rows]


def close_dialog(dialog_id: int, operator_id: int, now: datetime | None = None) -> int:
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT chat_id FROM operator_dialogs WHERE id=? AND operator_id=? AND status='active'",
            (dialog_id, operator_id),
        ).fetchone()
        if not row:
            raise PermissionError
        conn.execute(
            "UPDATE operator_dialogs SET status='closed',closed_at=?,closed_by=? WHERE id=?",
            (_iso(now), operator_id, dialog_id),
        )
        chat_id = row["chat_id"]
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


def process_timeouts(now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return notification work after atomically updating stale/warn/closed rows."""
    now = now or utc_now()
    settings = get_settings()
    result: dict[str, list[dict[str, Any]]] = {"requeued": [], "warned": [], "closed": [], "assigned": []}
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        stale = conn.execute(
            """SELECT d.id,d.chat_id FROM operator_dialogs d LEFT JOIN operator_shifts s ON s.user_id=d.operator_id
               WHERE d.status='active' AND (s.user_id IS NULL OR s.active=0 OR s.heartbeat_at<?)""",
            (_fresh_cutoff(settings, now),),
        ).fetchall()
        for row in stale:
            queue_seq = conn.execute("SELECT COALESCE(MAX(queue_seq),0)+1 n FROM operator_dialogs").fetchone()["n"]
            conn.execute(
                "UPDATE operator_dialogs SET status='waiting',operator_id=NULL,queue_seq=?,assigned_at=NULL,warned_at=NULL WHERE id=?",
                (queue_seq, row["id"]),
            )
            result["requeued"].append(dict(row))
        warn_at = _iso(now - timedelta(minutes=int(settings["inactivity_timeout_min"] - settings["warning_before_min"])))
        close_at = _iso(now - timedelta(minutes=int(settings["inactivity_timeout_min"])))
        warns = conn.execute(
            "SELECT id,chat_id FROM operator_dialogs WHERE status='active' AND warned_at IS NULL AND last_activity_at<=? AND last_activity_at>?",
            (warn_at, close_at),
        ).fetchall()
        for row in warns:
            conn.execute("UPDATE operator_dialogs SET warned_at=? WHERE id=?", (_iso(now), row["id"]))
            result["warned"].append(dict(row))
        closes = conn.execute(
            "SELECT id,chat_id FROM operator_dialogs WHERE status='active' AND last_activity_at<=?",
            (close_at,),
        ).fetchall()
        for row in closes:
            conn.execute("UPDATE operator_dialogs SET status='timed_out',closed_at=? WHERE id=?", (_iso(now), row["id"]))
            result["closed"].append(dict(row))
    result["assigned"] = assign_waiting(now=now)
    return result


def cleanup_history(image_root: str | os.PathLike[str], now: datetime | None = None) -> int:
    settings = get_settings()
    cutoff = _iso((now or utc_now()) - timedelta(days=int(settings["retention_days"])))
    paths: list[str] = []
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT m.image_path FROM operator_messages m JOIN operator_dialogs d ON d.id=m.dialog_id
               WHERE d.status NOT IN ('waiting','active') AND d.closed_at<? AND m.image_path IS NOT NULL""",
            (cutoff,),
        ).fetchall()
        paths = [row["image_path"] for row in rows]
        ids = [row["id"] for row in conn.execute(
            "SELECT id FROM operator_dialogs WHERE status NOT IN ('waiting','active') AND closed_at<?",
            (cutoff,),
        ).fetchall()]
        cur_count = len(ids)
        for dialog_id in ids:
            conn.execute("DELETE FROM operator_messages WHERE dialog_id=?", (dialog_id,))
            conn.execute(
                """UPDATE operator_dialogs SET client_fio=NULL,client_ls=NULL,client_address=NULL,
                   faq_context=NULL,ai_context_json='[]' WHERE id=?""", (dialog_id,),
            )
    root = Path(image_root).resolve()
    for relative in paths:
        candidate = (root / relative).resolve()
        if candidate.parent == root:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
    return cur_count


def validate_jpeg(data: bytes, content_type: str | None) -> None:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение должно быть не более 5 МБ")
    if content_type not in {"image/jpeg", "image/jpg"} or not data.startswith(JPEG_SIGNATURES):
        raise ValueError("Поддерживаются только JPG-изображения")


