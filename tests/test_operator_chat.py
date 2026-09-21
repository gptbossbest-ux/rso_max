from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import database as db
import web
from rso_bot import operator_chat


@pytest.fixture()
def operator_db(monkeypatch):
    path = os.path.abspath(f".operator-test-{uuid.uuid4().hex}.sqlite")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def _operator(name: str) -> int:
    ok, _ = db.create_user(name, "sufficient-password", name, "operator")
    assert ok
    return db.get_user(name)["id"]


def _settings(**overrides):
    values = {
        "enabled": True,
        "require_auth": False,
        "heartbeat_timeout_min": 5,
        "max_active_dialogs": 1,
        "inactivity_timeout_min": 30,
        "warning_before_min": 5,
        "retention_days": 30,
    }
    values.update(overrides)
    operator_chat.update_settings(**values)


def test_no_active_operator_rejects_without_queue(operator_db):
    _settings()
    assert operator_chat.request_dialog(10, profile=None, faq_context=None, ai_messages=[]) == {"status": "unavailable"}
    assert operator_chat.get_open_dialog_for_chat(10) is None


def test_auth_required_and_anonymous(operator_db):
    op = _operator("op1")
    operator_chat.start_shift(op)
    _settings(require_auth=True)
    assert operator_chat.request_dialog(11, profile=None, faq_context=None, ai_messages=[])["status"] == "auth_required"
    _settings(require_auth=False)
    row = operator_chat.request_dialog(11, profile=None, faq_context="FAQ", ai_messages=[])
    assert row["status"] == "active"
    assert row["authenticated"] == 0
    assert row["client_fio"] is None


def test_least_loaded_fifo_capacity_and_cancel(operator_db, monkeypatch):
    _settings(max_active_dialogs=1)
    first, second = _operator("first"), _operator("second")
    operator_chat.start_shift(first)
    operator_chat.start_shift(second)
    monkeypatch.setattr(operator_chat.random, "choice", lambda values: min(values))
    d1 = operator_chat.request_dialog(1, profile=None, faq_context=None, ai_messages=[])
    d2 = operator_chat.request_dialog(2, profile=None, faq_context=None, ai_messages=[])
    d3 = operator_chat.request_dialog(3, profile=None, faq_context=None, ai_messages=[])
    d4 = operator_chat.request_dialog(4, profile=None, faq_context=None, ai_messages=[])
    assert (d1["operator_id"], d2["operator_id"]) == (first, second)
    assert d3["status"] == d4["status"] == "waiting"
    assert operator_chat.queue_position(d3["id"]) == 1
    assert operator_chat.queue_position(d4["id"]) == 2
    assert operator_chat.cancel_waiting(3)
    assert operator_chat.queue_position(d4["id"]) == 1
    operator_chat.close_dialog(d1["id"], first)
    assigned = operator_chat.assign_waiting()
    assert assigned == [{"dialog_id": d4["id"], "chat_id": 4, "operator_id": first}]


def test_stale_operator_requeues_warns_and_times_out(operator_db):
    _settings(heartbeat_timeout_min=5, inactivity_timeout_min=30, warning_before_min=5)
    op = _operator("stale")
    base = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(op, now=base)
    operator_chat.request_dialog(5, profile=None, faq_context=None, ai_messages=[], now=base)
    events = operator_chat.process_timeouts(now=base + timedelta(minutes=6))
    assert [item["chat_id"] for item in events["requeued"]] == [5]
    assert operator_chat.get_open_dialog_for_chat(5)["status"] == "waiting"


def test_messages_object_auth_close_and_unique_rating(operator_db):
    _settings(max_active_dialogs=2)
    owner, other = _operator("owner"), _operator("other")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(6, profile=None, faq_context=None, ai_messages=[])
    operator_chat.add_message(dialog["id"], "client", "hello")
    with pytest.raises(PermissionError):
        operator_chat.list_messages(dialog["id"], other)
    operator_chat.add_message(dialog["id"], "operator", "reply", user_id=owner)
    assert len(operator_chat.list_messages(dialog["id"], owner)) == 3
    with pytest.raises(PermissionError):
        operator_chat.close_dialog(dialog["id"], other)
    assert operator_chat.close_dialog(dialog["id"], owner) == 6
    assert operator_chat.rate_dialog(6, dialog["id"], 5)
    assert not operator_chat.rate_dialog(6, dialog["id"], 1)
    report = next(row for row in operator_chat.rating_report() if row["id"] == owner)
    assert report["average_rating"] == 5
    assert report["rating_count"] == 1


def test_context_is_limited_and_jpeg_validation(operator_db):
    _settings()
    op = _operator("context")
    operator_chat.start_shift(op)
    history = [{"role": "user", "text": str(index)} for index in range(8)]
    row = operator_chat.request_dialog(7, profile={"ls": "1", "fio": "F", "address": "A"}, faq_context="full", ai_messages=history)
    assert row["ai_context_json"].count('"role"') == 5
    operator_chat.validate_jpeg(b"\xff\xd8\xffpayload", "image/jpeg")
    with pytest.raises(ValueError):
        operator_chat.validate_jpeg(b"GIF89a", "image/gif")
    with pytest.raises(ValueError):
        operator_chat.validate_jpeg(b"\xff\xd8\xff" + b"x" * operator_chat.MAX_IMAGE_BYTES, "image/jpeg")


def test_concurrent_requests_do_not_overbook_operator(operator_db):
    _settings(max_active_dialogs=1)
    owner = _operator("concurrent")
    operator_chat.start_shift(owner)
    barrier = threading.Barrier(2)
    results = []

    def create(chat_id):
        barrier.wait()
        results.append(operator_chat.request_dialog(chat_id, profile=None, faq_context=None, ai_messages=[]))

    threads = [threading.Thread(target=create, args=(chat_id,)) for chat_id in (100, 101)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(row["status"] for row in results) == ["active", "waiting"]


def test_module_flags_persist(operator_db):
    operator_chat.update_module_settings({key: key == "faq" for key in operator_chat.MODULE_KEYS})
    flags = operator_chat.get_module_settings()
    assert flags["faq"] is True
    assert flags["ai"] is False
    db.init_db()
    assert operator_chat.get_module_settings() == flags


def _login_session(client, user_id: int, username: str, role: str):
    row = db.get_user_by_id(user_id)
    with client.session_transaction() as saved:
        saved["user"] = {
            "id": user_id, "username": username, "name": username,
            "role": role, "session_version": row["session_version"],
            "must_change_password": False,
        }
        saved["_csrf_token"] = "csrf"


def test_web_api_enforces_assignment_and_csrf(operator_db, monkeypatch):
    _settings(max_active_dialogs=2)
    owner, other = _operator("web-owner"), _operator("web-other")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(20, profile=None, faq_context=None, ai_messages=[])
    client = web.app.test_client()
    _login_session(client, other, "web-other", "operator")
    assert client.get(f"/operator-chat/api/dialogs/{dialog['id']}/messages").status_code == 404
    _login_session(client, owner, "web-owner", "operator")
    assert client.post(f"/operator-chat/api/dialogs/{dialog['id']}/messages", data={"body": "reply"}).status_code == 400
    monkeypatch.setattr(web, "_send_max_text", lambda *_: True)
    response = client.post(
        f"/operator-chat/api/dialogs/{dialog['id']}/messages",
        data={"body": "reply"}, headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 200
    assert response.json["delivered"] is True


def test_retention_removes_content_but_preserves_rating(operator_db):
    _settings(retention_days=1)
    owner = _operator("retention")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=old)
    dialog = operator_chat.request_dialog(
        30, profile={"ls": "123", "fio": "Name", "address": "Address"},
        faq_context="secret path", ai_messages=[{"role": "user", "text": "question"}], now=old,
    )
    operator_chat.add_message(dialog["id"], "client", "private", now=old)
    operator_chat.close_dialog(dialog["id"], owner, now=old)
    operator_chat.rate_dialog(30, dialog["id"], 4, now=old)
    assert operator_chat.cleanup_history(".", now=old + timedelta(days=2)) == 1
    conn = db.get_conn()
    saved = conn.execute("SELECT * FROM operator_dialogs WHERE id=?", (dialog["id"],)).fetchone()
    count = conn.execute("SELECT COUNT(*) n FROM operator_messages WHERE dialog_id=?", (dialog["id"],)).fetchone()["n"]
    conn.close()
    assert saved["rating"] == 4
    assert saved["client_ls"] is None
    assert count == 0
