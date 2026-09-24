from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

import bot
import database as db
import web
from rso_bot import operator_chat


def _jpeg_bytes(size=(2, 2)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "red").save(output, format="JPEG")
    return output.getvalue()


def _image_bytes(format_name: str, size=(2, 2)) -> bytes:
    output = BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 180)).save(output, format=format_name)
    return output.getvalue()


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
        "heartbeat_timeout_sec": 30,
        "reconnect_grace_sec": 120,
        "max_active_dialogs": 1,
        "inactivity_timeout_min": 30,
        "warning_before_min": 5,
        "retention_days": 30,
        "report_threshold": 3,
        "evidence_retention_days": 30,
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
    operator_chat.deliver_outbox(lambda _item: (True, None))
    operator_chat.add_message(dialog["id"], "client", "hello")
    with pytest.raises(PermissionError):
        operator_chat.list_messages(dialog["id"], other)
    reply = operator_chat.add_message(dialog["id"], "operator", "reply", user_id=owner)
    assert len(operator_chat.list_messages(dialog["id"], owner)) == 3
    with pytest.raises(PermissionError):
        operator_chat.close_dialog(dialog["id"], other)
    operator_chat.deliver_outbox(lambda _item: (True, None))
    assert operator_chat.close_dialog(dialog["id"], owner) == 6
    outbox = operator_chat.get_outbox_item_for_message(reply["id"])
    assert outbox["status"] == "delivered"
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
    normalized = operator_chat.normalize_jpeg(_jpeg_bytes() + b"<script>polyglot</script>", "image/jpeg")
    assert b"polyglot" not in normalized
    operator_chat.validate_jpeg(_jpeg_bytes(), "image/jpeg")
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
    monkeypatch.setattr(web, "_send_max_text", lambda *_: (True, None))
    web._flush_outbox()
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
    assert saved["chat_id"] == 0
    assert saved["client_ls"] is None
    assert count == 0


def test_outbox_failure_retry_and_idempotency(operator_db):
    _settings(max_active_dialogs=2)
    owner = _operator("delivery")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(40, profile=None, faq_context=None, ai_messages=[])
    operator_chat.deliver_outbox(lambda _item: (True, None))
    message = operator_chat.add_message(dialog["id"], "operator", "reply", user_id=owner)
    outbox = operator_chat.get_outbox_item_for_message(message["id"])
    calls = []

    def fail(item):
        calls.append(item["id"])
        return False, "max_http_429"

    result = operator_chat.deliver_outbox(fail, only_id=outbox["id"])
    assert result == [{
        "id": outbox["id"], "status": "failed", "error": "max_http_429", "retryable": True,
    }]
    assert len(calls) == 1
    assert operator_chat.deliver_outbox(fail, only_id=outbox["id"]) == []
    assert operator_chat.retry_message(message["id"], owner)
    result = operator_chat.deliver_outbox(lambda item: (True, None), only_id=outbox["id"])
    assert result[0]["status"] == "delivered"
    assert operator_chat.deliver_outbox(lambda item: pytest.fail("duplicate"), only_id=outbox["id"]) == []
    saved = operator_chat.list_messages(dialog["id"], owner)
    assert next(row for row in saved if row["id"] == message["id"])["delivery_status"] == "delivered"


def test_waiting_timeout_is_bounded_and_notified(operator_db):
    _settings(max_active_dialogs=1, inactivity_timeout_min=30)
    owner = _operator("wait-ttl")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    operator_chat.request_dialog(41, profile=None, faq_context=None, ai_messages=[], now=base)
    waiting = operator_chat.request_dialog(42, profile=None, faq_context=None, ai_messages=[], now=base)
    events = operator_chat.process_timeouts(now=base + timedelta(minutes=31))
    assert [row["id"] for row in events["waiting_closed"]] == [waiting["id"]]
    assert operator_chat.get_open_dialog_for_chat(42) is None


def test_cleanup_retries_unlink_and_sweeps_old_orphan(operator_db, monkeypatch):
    _settings(retention_days=1)
    owner = _operator("cleanup")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=old)
    dialog = operator_chat.request_dialog(43, profile=None, faq_context=None, ai_messages=[], now=old)
    image_root = Path(f".operator-images-{uuid.uuid4().hex}")
    image_root.mkdir()
    image = image_root / "kept.jpg"
    image.write_bytes(_jpeg_bytes())
    orphan = image_root / "orphan.jpg"
    orphan.write_bytes(_jpeg_bytes())
    os.utime(orphan, (old.timestamp(), old.timestamp()))
    message = operator_chat.add_message(dialog["id"], "client", "private", image_path=image.name, now=old)
    operator_chat.close_dialog(dialog["id"], owner, now=old)
    original = Path.unlink
    failed = {"done": False}

    def fail_once(path, *args, **kwargs):
        if path.name == image.name and not failed["done"]:
            failed["done"] = True
            raise OSError("busy")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    operator_chat.cleanup_history(image_root, now=old + timedelta(days=2))
    assert not orphan.exists()
    conn = db.get_conn()
    assert conn.execute("SELECT cleanup_pending FROM operator_messages WHERE id=?", (message["id"],)).fetchone()[0] == 1
    conn.close()
    operator_chat.cleanup_history(image_root, now=old + timedelta(days=2))
    conn = db.get_conn()
    assert conn.execute("SELECT 1 FROM operator_messages WHERE id=?", (message["id"],)).fetchone() is None
    conn.close()
    image_root.rmdir()


@pytest.mark.parametrize("url,allowed", [
    ("https://iu.oneme.ru/file.jpg", True),
    ("http://iu.oneme.ru/file.jpg", False),
    ("https://iu.oneme.ru.evil.test/file.jpg", False),
    ("https://127.0.0.1/file.jpg", False),
    ("https://user@iu.oneme.ru/file.jpg", False),
])
def test_max_image_exact_allowlist(url, allowed):
    assert bot._is_allowed_max_image_url(url) is allowed


def test_operator_workspace_has_per_dialog_poll_guards():
    source = Path("templates/operator_chat.html").read_text(encoding="utf-8")
    for token in ("AbortController", "generation", "cursors", "seen", "current.id!==dialogId"):
        assert token in source


def test_history_api_is_admin_only_and_excludes_active(operator_db):
    _settings(max_active_dialogs=2)
    owner = _operator("history-owner")
    ok, _ = db.create_user("history-admin", "sufficient-password", "Admin", "admin")
    assert ok
    admin = db.get_user("history-admin")["id"]
    operator_chat.start_shift(owner)
    closed = operator_chat.request_dialog(50, profile=None, faq_context=None, ai_messages=[])
    operator_chat.close_dialog(closed["id"], owner)
    active = operator_chat.request_dialog(51, profile=None, faq_context=None, ai_messages=[])
    client = web.app.test_client()
    _login_session(client, owner, "history-owner", "operator")
    assert client.get("/operator-chat/api/history").status_code == 302
    assert client.get(f"/operator-chat/api/history/{closed['id']}").status_code == 302
    _login_session(client, admin, "history-admin", "admin")
    listing = client.get("/operator-chat/api/history")
    assert listing.status_code == 200
    assert [row["id"] for row in listing.json] == [closed["id"]]
    assert client.get(f"/operator-chat/api/history/{closed['id']}").status_code == 200
    assert client.get(f"/operator-chat/api/history/{active['id']}").status_code == 404


def test_migration_preserves_pre_delivery_messages(monkeypatch):
    path = os.path.abspath(f".operator-migration-{uuid.uuid4().hex}.sqlite")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE operator_messages(id INTEGER PRIMARY KEY, dialog_id INTEGER)")
    conn.execute("INSERT INTO operator_messages(id,dialog_id) VALUES(1,99)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    try:
        db.init_db()
        conn = sqlite3.connect(path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(operator_messages)")}
        assert {"delivery_status", "delivery_attempts", "last_error", "cleanup_pending"} <= columns
        outbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(operator_outbox)")}
        assert {"dialog_id", "upload_token"} <= outbox_columns
        settings_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(operator_chat_settings)")
        }
        assert {
            "heartbeat_timeout_sec", "reconnect_grace_sec", "report_threshold",
            "evidence_retention_days",
        } <= settings_columns
        dialog_columns = {row[1] for row in conn.execute("PRAGMA table_info(operator_dialogs)")}
        assert "reassignment_pending" in dialog_columns
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"operator_client_reports", "operator_chat_blocks"} <= tables
        assert conn.execute("SELECT dialog_id FROM operator_messages WHERE id=1").fetchone()[0] == 99
        conn.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass


def test_jpeg_delivery_retries_only_attachment_not_ready(monkeypatch):
    path = Path(f".operator-upload-{uuid.uuid4().hex}.jpg")
    path.write_bytes(_jpeg_bytes())

    class Response:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("POST", "https://example.test")
                raise httpx.HTTPStatusError("failed", request=request, response=httpx.Response(self.status_code, request=request))

    responses = iter([
        Response(200, {"url": "https://iu.oneme.ru/upload"}),
        Response(200, {"token": "token"}),
        Response(400, {"code": "attachment.not.ready"}),
        Response(200, {}),
    ])
    calls = []

    def post(*args, **kwargs):
        calls.append(args[0])
        return next(responses)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(web.time, "sleep", lambda _seconds: None)
    try:
        assert web._send_max_jpeg(1, str(path), "caption") == (True, None)
        assert len(calls) == 4
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("result,error", [
    (httpx.TimeoutException("timeout"), "max_timeout"),
    (httpx.ConnectError("connect"), "max_transport"),
])
def test_text_delivery_records_transport_failures(monkeypatch, result, error):
    def fail(*_args, **_kwargs):
        raise result

    monkeypatch.setattr(httpx, "post", fail)
    assert web._send_max_text(1, "text") == (False, error)


def test_official_max_attachment_contract_and_redirect_rejection(monkeypatch):
    url = "https://iu.oneme.ru/image.jpg"
    attachment = {"type": "image", "payload": {"url": url}}
    assert bot._attachment_url(attachment) == url

    class Stream:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"content-type": "image/jpeg", "content-length": "4"}
            self.request = httpx.Request("GET", url)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self, _size):
            yield _jpeg_bytes()

    send = []
    monkeypatch.setattr(bot, "send_message", lambda _chat, text: send.append(text))
    monkeypatch.setattr(bot.httpx, "stream", lambda *_args, **_kwargs: Stream(302))
    monkeypatch.setattr(bot.operator_chat, "add_message", lambda *_args, **_kwargs: pytest.fail("redirect followed"))
    bot._handle_operator_attachments(1, {"id": 2}, {"attachments": [attachment]})
    assert send and "Не удалось принять изображение" in send[-1]


def test_inbound_jpeg_db_failure_removes_saved_file(monkeypatch):
    root = Path(f".operator-inbound-{uuid.uuid4().hex}")
    root.mkdir()
    url = "https://iu.oneme.ru/image.jpg"

    class Stream:
        def __init__(self):
            self.status_code = 200
            self.headers = {"content-type": "image/jpeg", "content-length": str(len(_jpeg_bytes()))}
            self.request = httpx.Request("GET", url)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self, _size):
            yield _jpeg_bytes()

    monkeypatch.setattr(bot, "OPERATOR_CHAT_IMAGE_DIR", str(root))
    monkeypatch.setattr(bot.httpx, "stream", lambda *_args, **_kwargs: Stream())
    monkeypatch.setattr(bot.operator_chat, "add_message", lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.Error("db")))
    monkeypatch.setattr(bot, "send_message", lambda *_args: True)
    bot._handle_operator_attachments(
        1, {"id": 2}, {"attachments": [{"type": "image", "payload": {"url": url}}]},
    )
    assert not list(root.glob("*.jpg"))
    root.rmdir()


def test_failed_reply_blocks_manual_close_until_retry_succeeds(operator_db):
    _settings(max_active_dialogs=2)
    owner = _operator("close-order")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(60, profile=None, faq_context=None, ai_messages=[])
    operator_chat.deliver_outbox(lambda _item: (True, None))
    message = operator_chat.add_message(dialog["id"], "operator", "reply", user_id=owner)
    outbox = operator_chat.get_outbox_item_for_message(message["id"])
    failed = operator_chat.deliver_outbox(
        lambda _item: (False, "max_http_500"), only_id=outbox["id"],
    )
    assert failed[0]["retryable"] is True
    with pytest.raises(operator_chat.UndeliveredMessagesError) as blocked:
        operator_chat.close_dialog(dialog["id"], owner)
    assert blocked.value.total == 1
    assert operator_chat.retry_message(message["id"], owner)
    operator_chat.deliver_outbox(lambda _item: (True, None), only_id=outbox["id"])
    operator_chat.close_dialog(dialog["id"], owner)
    assert not operator_chat.retry_message(message["id"], owner)
    deliveries = []
    operator_chat.deliver_outbox(
        lambda item: deliveries.append(item["event_key"]) or (True, None),
    )
    assert deliveries == [f"dialog:{dialog['id']}:closed"]


def test_failed_requeue_blocks_later_assignment_notification(operator_db, monkeypatch):
    _settings(max_active_dialogs=1, heartbeat_timeout_min=5)
    first, second = _operator("stale-first"), _operator("fresh-second")
    base = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(first, now=base)
    monkeypatch.setattr(operator_chat.random, "choice", lambda values: min(values))
    dialog = operator_chat.request_dialog(61, profile=None, faq_context=None, ai_messages=[], now=base)
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    later = base + timedelta(minutes=6)
    operator_chat.start_shift(second, now=later)
    events = operator_chat.process_timeouts(now=later)
    assert events["assigned"][0]["operator_id"] == second
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM operator_outbox WHERE dialog_id=? ORDER BY id", (dialog["id"],),
    ).fetchall()
    conn.close()
    assigned = dict(rows[-1])
    assert "направили вас к другому оператору" in assigned["body"]
    delivered = operator_chat.deliver_outbox(
        lambda item: (item["id"] == assigned["id"], None), now=later,
        only_id=assigned["id"],
    )
    assert delivered[0]["status"] == "delivered"


def test_timeout_supersedes_pending_warning_before_close_notice(operator_db):
    _settings(
        max_active_dialogs=1, heartbeat_timeout_min=5,
        inactivity_timeout_min=30, warning_before_min=5,
    )
    owner = _operator("warning-order")
    base = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    dialog = operator_chat.request_dialog(
        67, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    warning_time = base + timedelta(minutes=26)
    operator_chat.heartbeat(owner, now=warning_time)
    assert operator_chat.process_timeouts(now=warning_time)["warned"]
    close_time = base + timedelta(minutes=31)
    operator_chat.heartbeat(owner, now=close_time)
    assert operator_chat.process_timeouts(now=close_time)["closed"]
    conn = db.get_conn()
    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM operator_outbox WHERE dialog_id=? ORDER BY id", (dialog["id"],),
    ).fetchall()]
    conn.close()
    warning, close_notice = rows[-2:]
    assert warning["last_error"] == "superseded_by_timeout"
    assert warning["next_retry_at"] is None
    sent = operator_chat.deliver_outbox(
        lambda item: (item["id"] == close_notice["id"], None),
        now=close_time, only_id=close_notice["id"],
    )
    assert sent[0]["status"] == "delivered"


def test_transport_failure_is_scheduled_then_delivered(operator_db, monkeypatch):
    _settings(max_active_dialogs=1)
    owner = _operator("transport-retry")
    base = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    dialog = operator_chat.request_dialog(
        68, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    conn = db.get_conn()
    outbox = dict(conn.execute(
        "SELECT * FROM operator_outbox WHERE dialog_id=?", (dialog["id"],),
    ).fetchone())
    conn.close()
    calls = 0

    class Response:
        status_code = 200

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError(
                "dns failure", request=httpx.Request("POST", "https://example.test"),
            )
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    failed = operator_chat.deliver_outbox(
        web._deliver_outbox_item, now=base, only_id=outbox["id"],
    )
    assert failed[0] == {
        "id": outbox["id"], "status": "failed",
        "error": "max_transport", "retryable": True,
    }
    assert operator_chat.deliver_outbox(
        web._deliver_outbox_item, now=base + timedelta(seconds=1),
        only_id=outbox["id"],
    ) == []
    delivered = operator_chat.deliver_outbox(
        web._deliver_outbox_item, now=base + timedelta(seconds=3),
        only_id=outbox["id"],
    )
    assert delivered[0]["status"] == "delivered"
    assert calls == 2


def test_outbox_orders_old_close_before_new_dialog_assignment(operator_db):
    _settings(max_active_dialogs=1)
    owner = _operator("recipient-order")
    base = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    old_dialog = operator_chat.request_dialog(
        69, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    operator_chat.close_dialog(old_dialog["id"], owner, now=base)
    conn = db.get_conn()
    old_close = dict(conn.execute(
        "SELECT * FROM operator_outbox WHERE dialog_id=? ORDER BY id DESC LIMIT 1",
        (old_dialog["id"],),
    ).fetchone())
    conn.close()
    failed = operator_chat.deliver_outbox(
        lambda _item: (False, "max_transport"), now=base,
        only_id=old_close["id"],
    )
    assert failed[0]["retryable"] is True
    new_dialog = operator_chat.request_dialog(
        69, profile=None, faq_context=None, ai_messages=[],
        now=base + timedelta(seconds=1),
    )
    conn = db.get_conn()
    new_assignment = dict(conn.execute(
        "SELECT * FROM operator_outbox WHERE dialog_id=? ORDER BY id LIMIT 1",
        (new_dialog["id"],),
    ).fetchone())
    conn.close()
    assert operator_chat.deliver_outbox(
        lambda _item: pytest.fail("new dialog overtook old close"),
        now=base + timedelta(seconds=1), only_id=new_assignment["id"],
    ) == []
    assert operator_chat.deliver_outbox(
        lambda _item: (True, None), now=base + timedelta(seconds=3),
        only_id=old_close["id"],
    )[0]["status"] == "delivered"
    assert operator_chat.deliver_outbox(
        lambda _item: (True, None), now=base + timedelta(seconds=3),
        only_id=new_assignment["id"],
    )[0]["status"] == "delivered"


def test_operator_composer_matches_server_message_limit():
    template = Path("templates/operator_chat.html").read_text(encoding="utf-8")
    assert 'name="body" type="text" maxlength="3990"' in template
    assert "function clearCurrent" in template
    assert "clearCurrent('Диалог завершён.')" in template
    assert "clearCurrent('Диалоги переданы.')" in template


def test_non_retryable_4xx_dead_letters_until_manual_retry(operator_db):
    _settings(max_active_dialogs=2)
    owner = _operator("dead-letter")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(62, profile=None, faq_context=None, ai_messages=[])
    operator_chat.deliver_outbox(lambda _item: (True, None))
    message = operator_chat.add_message(dialog["id"], "operator", "reply", user_id=owner)
    outbox = operator_chat.get_outbox_item_for_message(message["id"])
    result = operator_chat.deliver_outbox(
        lambda _item: (False, "max_http_400"), only_id=outbox["id"],
    )[0]
    assert result["retryable"] is False
    saved = operator_chat.get_outbox_item_for_message(message["id"])
    assert saved["attempts"] == operator_chat.MAX_OUTBOX_ATTEMPTS
    assert saved["next_retry_at"] is None
    assert operator_chat.deliver_outbox(lambda _item: pytest.fail("automatic retry")) == []
    assert operator_chat.retry_message(message["id"], owner)
    assert operator_chat.deliver_outbox(
        lambda _item: (True, None), only_id=outbox["id"],
    )[0]["status"] == "delivered"


def test_retryable_5xx_stops_at_max_attempts(operator_db):
    _settings(max_active_dialogs=2)
    owner = _operator("retry-limit")
    operator_chat.start_shift(owner)
    base = operator_chat.utc_now()
    dialog = operator_chat.request_dialog(63, profile=None, faq_context=None, ai_messages=[], now=base)
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    message = operator_chat.add_message(dialog["id"], "operator", "reply", user_id=owner, now=base)
    outbox = operator_chat.get_outbox_item_for_message(message["id"])
    for attempt in range(operator_chat.MAX_OUTBOX_ATTEMPTS):
        result = operator_chat.deliver_outbox(
            lambda _item: (False, "max_http_503"),
            now=base + timedelta(minutes=attempt + 1), only_id=outbox["id"],
        )
        assert result
    assert result[0]["retryable"] is False
    assert operator_chat.deliver_outbox(
        lambda _item: pytest.fail("sixth attempt"), now=base + timedelta(days=1),
        only_id=outbox["id"],
    ) == []


def test_operator_prefix_respects_max_message_size(operator_db):
    _settings(max_active_dialogs=2)
    owner = _operator("max-length")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(64, profile=None, faq_context=None, ai_messages=[])
    operator_chat.add_message(
        dialog["id"], "operator", "x" * operator_chat.MAX_OPERATOR_BODY, user_id=owner,
    )
    with pytest.raises(ValueError):
        operator_chat.add_message(
            dialog["id"], "operator", "x" * (operator_chat.MAX_OPERATOR_BODY + 1), user_id=owner,
        )


def test_jpeg_rejects_fake_and_oversized_dimensions():
    with pytest.raises(ValueError):
        operator_chat.normalize_jpeg(b"\xff\xd8\xffnot-a-jpeg", "image/jpeg")
    accepted = operator_chat.normalize_jpeg(
        _jpeg_bytes((operator_chat.MAX_IMAGE_DIMENSION, 1)), "image/jpeg",
    )
    with Image.open(BytesIO(accepted)) as decoded:
        assert decoded.size == (7680, 1)
    with pytest.raises(ValueError, match="размеры"):
        operator_chat.normalize_jpeg(_jpeg_bytes((operator_chat.MAX_IMAGE_DIMENSION + 1, 1)), "image/jpeg")


@pytest.mark.parametrize(("url", "allowed"), [
    ("https://iu.oneme.ru/upload?signature=secret", True),
    ("https://iu.oneme.ru:443/upload", True),
    ("http://iu.oneme.ru/upload", False),
    ("https://iu.oneme.ru:444/upload", False),
    ("https://iu.oneme.ru.evil.test/upload", False),
    ("https://user@iu.oneme.ru/upload", False),
    ("https://iu.oneme.ru/upload#fragment", False),
])
def test_max_image_upload_url_exact_allowlist(url, allowed):
    assert operator_chat.is_allowed_max_image_upload_url(url) is allowed


def test_history_image_denies_cleanup_pending_and_expired(operator_db):
    _settings(max_active_dialogs=2, retention_days=30)
    owner = _operator("image-history-owner")
    ok, _ = db.create_user("image-history-admin", "sufficient-password", "Admin", "admin")
    assert ok
    admin = db.get_user("image-history-admin")["id"]
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(65, profile=None, faq_context=None, ai_messages=[])
    message = operator_chat.add_message(
        dialog["id"], "client", "image", image_path="private.jpg",
    )
    operator_chat.close_dialog(dialog["id"], owner)
    client = web.app.test_client()
    _login_session(client, admin, "image-history-admin", "admin")
    conn = db.get_conn()
    conn.execute("UPDATE operator_messages SET cleanup_pending=1 WHERE id=?", (message["id"],))
    conn.commit()
    conn.close()
    assert client.get(f"/operator-chat/history/images/{message['id']}").status_code == 404
    conn = db.get_conn()
    conn.execute("UPDATE operator_messages SET cleanup_pending=0 WHERE id=?", (message["id"],))
    conn.execute(
        "UPDATE operator_dialogs SET closed_at=? WHERE id=?",
        ((operator_chat.utc_now() - timedelta(days=31)).isoformat(timespec="seconds"), dialog["id"]),
    )
    conn.commit()
    conn.close()
    assert client.get(f"/operator-chat/history/images/{message['id']}").status_code == 404


def test_image_outbox_reuses_persisted_upload_token(operator_db, monkeypatch):
    _settings(max_active_dialogs=2)
    owner = _operator("token-reuse")
    operator_chat.start_shift(owner)
    base = operator_chat.utc_now()
    dialog = operator_chat.request_dialog(66, profile=None, faq_context=None, ai_messages=[], now=base)
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    root = Path(f".operator-token-{uuid.uuid4().hex}")
    root.mkdir()
    image = root / "image.jpg"
    image.write_bytes(_jpeg_bytes())
    message = operator_chat.add_message(
        dialog["id"], "operator", "photo", user_id=owner, image_path=image.name, now=base,
    )
    outbox = operator_chat.get_outbox_item_for_message(message["id"])

    class Response:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                request = httpx.Request("POST", "https://example.test")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError("failed", request=request, response=response)

    responses = iter([
        Response(200, {"url": "https://iu.oneme.ru/upload"}),
        Response(200, {"token": "stable-token"}),
        Response(400, {"code": "attachment.not.ready"}),
        Response(400, {"code": "attachment.not.ready"}),
        Response(400, {"code": "attachment.not.ready"}),
        Response(200, {}),
    ])
    calls = []

    def post(url, **_kwargs):
        calls.append(url)
        return next(responses)

    monkeypatch.setattr(web, "OPERATOR_CHAT_IMAGE_DIR", str(root))
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(web.time, "sleep", lambda _seconds: None)
    try:
        first = operator_chat.deliver_outbox(
            web._deliver_outbox_item, now=base, only_id=outbox["id"],
        )
        assert first[0]["error"] == "attachment_not_ready"
        assert operator_chat.get_outbox_item_for_message(message["id"])["upload_token"] == "stable-token"
        second = operator_chat.deliver_outbox(
            web._deliver_outbox_item, now=base + timedelta(minutes=1), only_id=outbox["id"],
        )
        assert second[0]["status"] == "delivered"
        assert sum(url.endswith("/uploads") for url in calls) == 1
        assert calls.count("https://iu.oneme.ru/upload") == 1
    finally:
        image.unlink(missing_ok=True)
        root.rmdir()


@pytest.mark.parametrize("format_name", ["JPEG", "PNG", "WEBP"])
def test_supported_images_are_content_decoded_and_stored_as_jpeg(format_name):
    source = _jpeg_bytes() if format_name == "JPEG" else _image_bytes(format_name)
    normalized = operator_chat.normalize_image(source, "application/octet-stream")
    with Image.open(BytesIO(normalized)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (2, 2)


def test_max_image_upload_contract_accepts_nested_photo_token(monkeypatch):
    path = Path(f".operator-contract-{uuid.uuid4().hex}.jpg")
    path.write_bytes(_jpeg_bytes())

    class Response:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status

        def json(self):
            return self._payload

        def raise_for_status(self):
            assert self.status_code == 200

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/uploads"):
            return Response({"url": "https://iu.oneme.ru/uploadImage?unchanged=1"})
        if url.startswith("https://iu.oneme.ru"):
            assert set(kwargs["files"]) == {"data"}
            name, _stream, content_type = kwargs["files"]["data"]
            assert name == "image.jpg"
            assert content_type == "image/jpeg"
            assert kwargs["headers"] == {"Authorization": web.TOKEN}
            assert kwargs["follow_redirects"] is False
            return Response({"photos": {"photo-id": {"token": "nested-token"}}})
        assert kwargs["params"] == {"chat_id": 101}
        assert kwargs["json"]["attachments"] == [
            {"type": "image", "payload": {"token": "nested-token"}},
        ]
        return Response({})

    monkeypatch.setattr(httpx, "post", post)
    try:
        assert web._send_max_jpeg(101, str(path), "caption") == (True, None)
        assert calls[0][1]["params"] == {"type": "image"}
        assert calls[0][1]["headers"] == {"Authorization": web.TOKEN}
        assert calls[1][0] == "https://iu.oneme.ru/uploadImage?unchanged=1"
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("malicious_url", [
    "http://iu.oneme.ru/upload",
    "https://evil.test/upload",
    "https://iu.oneme.ru:444/upload",
    "https://user@iu.oneme.ru/upload",
    "https://iu.oneme.ru/upload#fragment",
])
@pytest.mark.parametrize("sender", ["web", "bot"])
def test_image_sender_rejects_untrusted_upload_url(monkeypatch, malicious_url, sender):
    path = Path(f".operator-untrusted-{uuid.uuid4().hex}.jpg")
    path.write_bytes(_jpeg_bytes())

    class Response:
        status_code = 200

        def json(self):
            return {"url": malicious_url}

        def raise_for_status(self):
            return None

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    try:
        if sender == "web":
            result = web._send_max_jpeg(103, str(path), "caption")
        else:
            result = bot._send_operator_jpeg(
                103, str(path), "caption", outbox_id=1, upload_token=None,
            )
        assert result == (False, "upload_contract")
        assert len(calls) == 1
        assert calls[0][1]["headers"] == {"Authorization": web.TOKEN}
        assert calls[0][1]["params"] == {"type": "image"}
    finally:
        path.unlink(missing_ok=True)


def test_bot_max_image_upload_contract_accepts_nested_photo_token(monkeypatch):
    path = Path(f".operator-bot-contract-{uuid.uuid4().hex}.jpg")
    path.write_bytes(_jpeg_bytes())

    class Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/uploads"):
            return Response({"url": "https://iu.oneme.ru/uploadImage?signature=kept"})
        if url.startswith("https://iu.oneme.ru"):
            assert set(kwargs["files"]) == {"data"}
            assert kwargs["headers"] == {"Authorization": bot.TOKEN}
            assert kwargs["follow_redirects"] is False
            return Response({"photos": {"photo-id": {"token": "bot-nested-token"}}})
        assert kwargs["json"]["attachments"] == [
            {"type": "image", "payload": {"token": "bot-nested-token"}},
        ]
        return Response({})

    monkeypatch.setattr(bot.httpx, "post", post)
    monkeypatch.setattr(
        operator_chat, "set_outbox_upload_token", lambda outbox_id, token: token,
    )
    try:
        assert bot._send_operator_jpeg(
            102, str(path), "caption", outbox_id=99, upload_token=None,
        ) == (True, None)
        assert calls[0][1]["params"] == {"type": "image"}
        assert calls[0][1]["headers"] == {"Authorization": bot.TOKEN}
        assert calls[1][0] == "https://iu.oneme.ru/uploadImage?signature=kept"
    finally:
        path.unlink(missing_ok=True)


def test_inbound_png_uses_allowed_candidate_and_is_saved_as_jpeg(operator_db, monkeypatch):
    _settings(require_auth=False)
    owner = _operator("inbound-png")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(201, profile=None, faq_context=None, ai_messages=[])
    source = _image_bytes("PNG")
    root = Path(f".operator-inbound-{uuid.uuid4().hex}")
    root.mkdir()
    allowed = "https://iu.oneme.ru/image"

    class Stream:
        def __init__(self):
            self.status_code = 200
            self.headers = {
                "content-type": "image/png", "content-length": str(len(source)),
            }
            self.request = httpx.Request("GET", allowed)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self, _size):
            yield source

    monkeypatch.setattr(bot, "OPERATOR_CHAT_IMAGE_DIR", str(root))
    monkeypatch.setattr(bot.httpx, "stream", lambda *_args, **kwargs: Stream())
    monkeypatch.setattr(bot, "send_message", lambda *_args: pytest.fail("valid image rejected"))
    try:
        bot._handle_operator_attachments(201, dialog, {"attachments": [{
            "type": "image", "payload": {"url": "https://evil.test/x", "photos": {
                "full": {"url": allowed},
            }},
        }]})
        saved = next(root.glob("*.jpg"))
        with Image.open(saved) as decoded:
            assert decoded.format == "JPEG"
        rows = operator_chat.list_messages(dialog["id"], owner)
        assert any(row["sender"] == "client" and row["image_path"] == saved.name for row in rows)
    finally:
        for item in root.glob("*.jpg"):
            item.unlink()
        root.rmdir()


def test_delete_undelivered_image_enforces_owner_state_and_unblocks_close(operator_db):
    _settings(max_active_dialogs=2)
    owner, other = _operator("delete-owner"), _operator("delete-other")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(202, profile=None, faq_context=None, ai_messages=[])
    operator_chat.deliver_outbox(lambda _item: (True, None))
    message = operator_chat.add_message(
        dialog["id"], "operator", "photo", user_id=owner, image_path="pending.jpg",
    )
    with pytest.raises(PermissionError):
        operator_chat.delete_undelivered_image(message["id"], other)
    assert operator_chat.delete_undelivered_image(message["id"], owner) == "pending.jpg"
    assert operator_chat.get_outbox_item_for_message(message["id"]) is None
    assert operator_chat.close_dialog(dialog["id"], owner) == 202


def test_delete_image_rejects_delivered_and_in_flight_messages(operator_db):
    _settings(max_active_dialogs=2)
    owner = _operator("delete-state-owner")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(211, profile=None, faq_context=None, ai_messages=[])
    operator_chat.deliver_outbox(lambda _item: (True, None))
    delivered = operator_chat.add_message(
        dialog["id"], "operator", "delivered", user_id=owner, image_path="done.jpg",
    )
    operator_chat.deliver_outbox(lambda _item: (True, None))
    with pytest.raises(ValueError, match="Доставленное"):
        operator_chat.delete_undelivered_image(delivered["id"], owner)

    sending = operator_chat.add_message(
        dialog["id"], "operator", "sending", user_id=owner, image_path="sending.jpg",
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE operator_outbox SET status='sending' WHERE message_id=?", (sending["id"],),
    )
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="сейчас отправляется"):
        operator_chat.delete_undelivered_image(sending["id"], owner)


def test_delete_image_web_route_csrf_and_idor(operator_db, monkeypatch):
    _settings(max_active_dialogs=2)
    owner, other = _operator("delete-web-owner"), _operator("delete-web-other")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(203, profile=None, faq_context=None, ai_messages=[])
    operator_chat.deliver_outbox(lambda _item: (True, None))
    root = Path(f".operator-delete-{uuid.uuid4().hex}")
    root.mkdir()
    image = root / "pending.jpg"
    image.write_bytes(_jpeg_bytes())
    message = operator_chat.add_message(
        dialog["id"], "operator", "photo", user_id=owner, image_path=image.name,
    )
    monkeypatch.setattr(web, "OPERATOR_CHAT_IMAGE_DIR", str(root))
    client = web.app.test_client()
    _login_session(client, owner, "delete-web-owner", "operator")
    blocked = client.post(
        f"/operator-chat/api/dialogs/{dialog['id']}/close",
        headers={"X-CSRF-Token": "csrf"},
    )
    assert blocked.status_code == 409
    assert blocked.json["undelivered_count"] == 1
    assert blocked.json["undelivered_images"] == 1
    assert client.post(f"/operator-chat/api/messages/{message['id']}/delete").status_code == 400
    _login_session(client, other, "delete-web-other", "operator")
    assert client.post(
        f"/operator-chat/api/messages/{message['id']}/delete",
        headers={"X-CSRF-Token": "csrf"},
    ).status_code == 404
    _login_session(client, owner, "delete-web-owner", "operator")
    assert client.post(
        f"/operator-chat/api/messages/{message['id']}/delete",
        headers={"X-CSRF-Token": "csrf"},
    ).status_code == 200
    assert not image.exists()
    assert client.post(
        f"/operator-chat/api/dialogs/{dialog['id']}/close",
        headers={"X-CSRF-Token": "csrf"},
    ).status_code == 200
    root.rmdir()


def test_reconnect_within_grace_preserves_owner_then_reassigns_after_grace(operator_db, monkeypatch):
    _settings(
        max_active_dialogs=1, heartbeat_timeout_sec=30, reconnect_grace_sec=120,
    )
    first, second = _operator("grace-first"), _operator("grace-second")
    base = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(first, now=base)
    dialog = operator_chat.request_dialog(204, profile=None, faq_context=None, ai_messages=[], now=base)
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    operator_chat.start_shift(second, now=base + timedelta(seconds=31))
    assert not operator_chat.process_timeouts(now=base + timedelta(seconds=149))["requeued"]
    assert operator_chat.get_open_dialog_for_chat(204)["operator_id"] == first
    operator_chat.heartbeat(first, now=base + timedelta(seconds=149))
    assert not operator_chat.process_timeouts(now=base + timedelta(seconds=151))["requeued"]
    assert operator_chat.get_open_dialog_for_chat(204)["operator_id"] == first
    operator_chat.heartbeat(second, now=base + timedelta(seconds=301))
    monkeypatch.setattr(operator_chat.random, "choice", lambda values: max(values))
    events = operator_chat.process_timeouts(now=base + timedelta(seconds=301))
    assert events["requeued"] and events["assigned"][0]["operator_id"] == second
    assert operator_chat.get_open_dialog_for_chat(204)["id"] == dialog["id"]


def test_transfer_all_ends_shift_and_reassigns_atomically(operator_db, monkeypatch):
    _settings(max_active_dialogs=3)
    first, second = _operator("transfer-first"), _operator("transfer-second")
    now = operator_chat.utc_now()
    operator_chat.start_shift(first, now=now)
    monkeypatch.setattr(operator_chat.random, "choice", lambda values: min(values))
    dialogs = [
        operator_chat.request_dialog(chat_id, profile=None, faq_context=None, ai_messages=[], now=now)
        for chat_id in (205, 206)
    ]
    assert all(dialog["operator_id"] == first for dialog in dialogs)
    operator_chat.start_shift(second, now=now)
    transferred = operator_chat.transfer_all_dialogs(first, now=now + timedelta(seconds=1))
    assert transferred == [dialog["id"] for dialog in dialogs]
    conn = db.get_conn()
    assert conn.execute("SELECT active FROM operator_shifts WHERE user_id=?", (first,)).fetchone()[0] == 0
    conn.close()
    assigned = operator_chat.assign_waiting(now=now + timedelta(seconds=1))
    assert {row["operator_id"] for row in assigned} == {second}


def test_reports_threshold_reject_idempotency_and_unblock(operator_db):
    _settings(max_active_dialogs=2, report_threshold=3)
    owner, other = _operator("report-owner"), _operator("report-other")
    ok, _ = db.create_user("report-admin", "sufficient-password", "Admin", "admin")
    assert ok
    admin = db.get_user("report-admin")["id"]
    operator_chat.start_shift(owner)
    report_ids = []
    for index in range(3):
        dialog = operator_chat.request_dialog(
            207, profile=None, faq_context=None, ai_messages=[],
        )
        operator_chat.add_message(dialog["id"], "client", f"spam {index}")
        with pytest.raises(PermissionError):
            operator_chat.create_client_report(dialog["id"], other, "spam", "")
        report = operator_chat.create_client_report(dialog["id"], owner, "spam", "")
        report_ids.append(report["id"])
        assert report["status"] == "pending"
        assert not operator_chat.is_client_blocked(207)
        result = operator_chat.decide_client_report(report["id"], admin, "confirmed")
        assert result["confirmed_count"] == index + 1
    assert operator_chat.is_client_blocked(207)
    repeated_while_blocked = operator_chat.decide_client_report(
        report_ids[-1], admin, "confirmed",
    )
    assert repeated_while_blocked["blocked"] is True
    assert operator_chat.request_dialog(
        207, profile=None, faq_context=None, ai_messages=[],
    )["status"] == "blocked"
    assert operator_chat.unblock_client(207, admin)
    assert not operator_chat.is_client_blocked(207)
    repeated = operator_chat.decide_client_report(report_ids[-1], admin, "confirmed")
    assert repeated["confirmed_count"] == 3
    assert repeated["blocked"] is False
    assert not operator_chat.is_client_blocked(207)
    with pytest.raises(operator_chat.ReportDecisionConflictError):
        operator_chat.decide_client_report(report_ids[-1], admin, "rejected")

    historical = operator_chat.request_dialog(
        207, profile=None, faq_context=None, ai_messages=[],
    )
    rejected_after_threshold = operator_chat.create_client_report(
        historical["id"], owner, "spam", "",
    )
    result = operator_chat.decide_client_report(
        rejected_after_threshold["id"], admin, "rejected",
    )
    assert result["confirmed_count"] == 3
    assert result["blocked"] is False
    assert not operator_chat.is_client_blocked(207)

    dialog = operator_chat.request_dialog(208, profile=None, faq_context=None, ai_messages=[])
    rejected = operator_chat.create_client_report(dialog["id"], owner, "other", "контекст")
    result = operator_chat.decide_client_report(rejected["id"], admin, "rejected")
    assert result["confirmed_count"] == 0
    assert not operator_chat.is_client_blocked(208)


def test_concurrent_report_decisions_have_one_cas_winner(operator_db):
    _settings(max_active_dialogs=2, report_threshold=100)
    owner = _operator("report-race-owner")
    ok, _ = db.create_user("report-race-admin", "sufficient-password", "Admin", "admin")
    assert ok
    admin = db.get_user("report-race-admin")["id"]
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(213, profile=None, faq_context=None, ai_messages=[])
    report = operator_chat.create_client_report(dialog["id"], owner, "spam", "")
    barrier = threading.Barrier(2)
    outcomes = []

    def decide(value):
        barrier.wait()
        try:
            result = operator_chat.decide_client_report(report["id"], admin, value)
            outcomes.append(("saved", result["status"]))
        except operator_chat.ReportDecisionConflictError:
            outcomes.append(("conflict", value))

    threads = [
        threading.Thread(target=decide, args=("confirmed",)),
        threading.Thread(target=decide, args=("rejected",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([item for item in outcomes if item[0] == "saved"]) == 1
    assert len([item for item in outcomes if item[0] == "conflict"]) == 1


def test_request_rechecks_block_inside_assignment_transaction(operator_db, monkeypatch):
    _settings(max_active_dialogs=2)
    owner = _operator("request-block-race-owner")
    operator_chat.start_shift(owner)
    outer_check = threading.Event()
    monkeypatch.setattr(
        operator_chat, "is_client_blocked",
        lambda _chat_id: outer_check.set() or False,
    )
    blocker = db.get_conn()
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        """INSERT INTO operator_chat_blocks(chat_id,active,blocked_at)
           VALUES(?,1,?)""",
        (214, operator_chat.utc_now().isoformat(timespec="seconds")),
    )
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(operator_chat.request_dialog(
        214, profile=None, faq_context=None, ai_messages=[],
    )))
    thread.start()
    assert outer_check.wait(timeout=2)
    blocker.commit()
    blocker.close()
    thread.join(timeout=5)
    assert outcome == [{"status": "blocked"}]
    assert operator_chat.get_open_dialog_for_chat(214) is None


def test_confirm_block_racing_request_leaves_no_open_dialog(operator_db):
    _settings(max_active_dialogs=2, report_threshold=1)
    owner = _operator("confirm-request-race-owner")
    ok, _ = db.create_user(
        "confirm-request-race-admin", "sufficient-password", "Admin", "admin",
    )
    assert ok
    admin = db.get_user("confirm-request-race-admin")["id"]
    operator_chat.start_shift(owner)
    old = operator_chat.request_dialog(217, profile=None, faq_context=None, ai_messages=[])
    report = operator_chat.create_client_report(old["id"], owner, "spam", "")
    barrier = threading.Barrier(2)
    failures = []

    def request_again():
        try:
            barrier.wait()
            operator_chat.request_dialog(217, profile=None, faq_context=None, ai_messages=[])
        except (sqlite3.Error, ValueError, PermissionError) as exc:
            failures.append(exc)

    def confirm():
        try:
            barrier.wait()
            operator_chat.decide_client_report(report["id"], admin, "confirmed")
        except (sqlite3.Error, ValueError, PermissionError) as exc:
            failures.append(exc)

    threads = [threading.Thread(target=request_again), threading.Thread(target=confirm)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not failures
    assert operator_chat.is_client_blocked(217)
    assert operator_chat.get_open_dialog_for_chat(217) is None


def test_report_conflicts_with_in_flight_operator_reply(operator_db):
    _settings(max_active_dialogs=2)
    owner = _operator("report-sending-owner")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(215, profile=None, faq_context=None, ai_messages=[])
    operator_chat.deliver_outbox(lambda _item: (True, None))
    message = operator_chat.add_message(
        dialog["id"], "operator", "in flight", user_id=owner,
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE operator_outbox SET status='sending',lease_at=? WHERE message_id=?",
        (operator_chat.utc_now().isoformat(timespec="seconds"), message["id"]),
    )
    conn.commit()
    conn.close()

    with pytest.raises(operator_chat.DeliveryInProgressError):
        operator_chat.create_client_report(dialog["id"], owner, "spam", "")
    assert operator_chat.get_open_dialog_for_chat(215)["status"] == "active"
    assert operator_chat.list_client_reports() == []
    assert operator_chat.get_outbox_item_for_message(message["id"])["status"] == "sending"
    client = web.app.test_client()
    _login_session(client, owner, "report-sending-owner", "operator")
    response = client.post(
        f"/operator-chat/api/dialogs/{dialog['id']}/report",
        data={"reason": "spam"}, headers={"X-CSRF-Token": "csrf"},
    )
    assert response.status_code == 409
    assert "сейчас отправляется" in response.json["error"]


@pytest.mark.parametrize("event_kind", ["operator", "assignment", "warning"])
def test_admin_block_preflights_every_live_delivery_and_retries_stale_lease(
    operator_db, event_kind,
):
    _settings(max_active_dialogs=2, report_threshold=1)
    owner = _operator(f"admin-block-{event_kind}")
    admin_name = f"admin-block-{event_kind}-admin"
    ok, _ = db.create_user(admin_name, "sufficient-password", "Admin", "admin")
    assert ok
    admin = db.get_user(admin_name)["id"]
    base = datetime(2026, 5, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    old = operator_chat.request_dialog(
        510, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    report = operator_chat.create_client_report(
        old["id"], owner, "spam", "", now=base + timedelta(seconds=1),
    )
    operator_chat.deliver_outbox(
        lambda _item: (True, None), now=base + timedelta(seconds=1),
    )
    active = operator_chat.request_dialog(
        510, profile=None, faq_context=None, ai_messages=[],
        now=base + timedelta(seconds=2),
    )
    conn = db.get_conn()
    assignment_id = conn.execute(
        "SELECT id FROM operator_outbox WHERE dialog_id=? AND event_key LIKE ?",
        (active["id"], f"dialog:{active['id']}:assigned:%"),
    ).fetchone()["id"]
    operator_chat._enqueue_outbox_locked(
        conn, event_key=f"dialog:{active['id']}:warning_correction:test",
        dialog_id=active["id"], chat_id=510, kind="text", body="obsolete",
        now=base + timedelta(seconds=3),
    )
    conn.commit()
    if event_kind == "operator":
        conn.execute(
            "UPDATE operator_outbox SET status='delivered' WHERE id=?",
            (assignment_id,),
        )
        conn.commit()
        conn.close()
        message = operator_chat.add_message(
            active["id"], "operator", "in flight", user_id=owner,
            now=base + timedelta(seconds=3),
        )
        conn = db.get_conn()
        selector, value = "message_id=?", message["id"]
    else:
        selector, value = "id=?", assignment_id
        if event_kind == "warning":
            conn.execute(
                "UPDATE operator_outbox SET event_key=? WHERE id=?",
                (f"dialog:{active['id']}:warning:{active['last_activity_at']}", assignment_id),
            )
    decision_at = base + timedelta(seconds=4)
    conn.execute(
        f"UPDATE operator_outbox SET status='sending',lease_at=? WHERE {selector}",
        (decision_at.isoformat(timespec="seconds"), value),
    )
    conn.commit()
    conn.close()

    with pytest.raises(operator_chat.DeliveryInProgressError):
        operator_chat.decide_client_report(
            report["id"], admin, "confirmed", now=decision_at,
        )
    assert operator_chat.get_client_report(report["id"])["status"] == "pending"
    assert not operator_chat.is_client_blocked(510)
    assert operator_chat.get_open_dialog_for_chat(510)["id"] == active["id"]

    conn = db.get_conn()
    conn.execute(
        f"UPDATE operator_outbox SET lease_at=? WHERE {selector}",
        ((decision_at - timedelta(minutes=3)).isoformat(timespec="seconds"), value),
    )
    conn.commit()
    conn.close()
    result = operator_chat.decide_client_report(
        report["id"], admin, "confirmed", now=decision_at,
    )
    assert result["blocked"] is True
    assert operator_chat.get_open_dialog_for_chat(510) is None
    correction = next(
        row for row in _outbox_rows(active["id"])
        if "warning_correction" in row["event_key"]
    )
    assert correction["last_error"] == "cancelled_by_block"


@pytest.mark.parametrize("transition", ["close", "report", "transfer", "requeue", "timeout"])
def test_pending_warning_correction_is_superseded_by_dialog_transition(
    operator_db, transition,
):
    _settings(max_active_dialogs=1, inactivity_timeout_min=30, warning_before_min=5)
    owner = _operator(f"correction-{transition}")
    base = datetime(2026, 7, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    dialog = operator_chat.request_dialog(
        540, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    with operator_chat._connection() as conn:
        operator_chat._enqueue_outbox_locked(
            conn, event_key=f"dialog:{dialog['id']}:warning_correction:test",
            dialog_id=dialog["id"], chat_id=540, kind="text", body="obsolete",
            now=base + timedelta(seconds=1),
        )
    if transition == "close":
        operator_chat.close_dialog(dialog["id"], owner, now=base + timedelta(seconds=2))
    elif transition == "report":
        operator_chat.create_client_report(
            dialog["id"], owner, "spam", "", now=base + timedelta(seconds=2),
        )
    elif transition == "transfer":
        operator_chat.transfer_all_dialogs(owner, now=base + timedelta(seconds=2))
    elif transition == "requeue":
        operator_chat.process_timeouts(now=base + timedelta(minutes=6))
    else:
        operator_chat.heartbeat(owner, now=base + timedelta(minutes=31))
        operator_chat.process_timeouts(now=base + timedelta(minutes=31))
    correction = next(
        row for row in _outbox_rows(dialog["id"])
        if "warning_correction" in row["event_key"]
    )
    assert correction["status"] == "failed"
    assert correction["next_retry_at"] is None
    assert correction["last_error"].startswith(("superseded_", "cancelled_"))


@pytest.mark.parametrize("delivery_result", [
    (True, None),
    (False, "max_http_500"),
])
def test_delivery_finalize_cas_does_not_overwrite_superseding_state(
    operator_db, delivery_result,
):
    _settings(max_active_dialogs=2)
    owner = _operator("delivery-cas-owner")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(216, profile=None, faq_context=None, ai_messages=[])
    operator_chat.deliver_outbox(lambda _item: (True, None))
    message = operator_chat.add_message(
        dialog["id"], "operator", "race", user_id=owner,
    )
    outbox = operator_chat.get_outbox_item_for_message(message["id"])

    def supersede(_item):
        conn = db.get_conn()
        conn.execute(
            """UPDATE operator_outbox SET status='failed',attempts=?,next_retry_at=NULL,
               last_error='superseded_in_test' WHERE id=?""",
            (operator_chat.MAX_OUTBOX_ATTEMPTS, outbox["id"]),
        )
        conn.execute(
            "UPDATE operator_dialogs SET status='closed',closed_at=? WHERE id=?",
            (operator_chat.utc_now().isoformat(timespec="seconds"), dialog["id"]),
        )
        conn.commit()
        conn.close()
        return delivery_result

    result = operator_chat.deliver_outbox(supersede, only_id=outbox["id"])
    assert result[0]["status"] == "failed"
    assert result[0]["error"] == "superseded_in_test"
    conn = db.get_conn()
    saved_message = conn.execute(
        "SELECT delivery_status,delivered_at FROM operator_messages WHERE id=?",
        (message["id"],),
    ).fetchone()
    conn.close()
    assert tuple(saved_message) == ("pending", None)


def test_report_requires_other_comment_and_valid_client_image(operator_db):
    _settings(max_active_dialogs=2)
    owner = _operator("report-validation")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(209, profile=None, faq_context=None, ai_messages=[])
    with pytest.raises(ValueError, match="обязателен"):
        operator_chat.create_client_report(dialog["id"], owner, "other", "")
    operator_image = operator_chat.add_message(
        dialog["id"], "operator", "x", user_id=owner, image_path="operator.jpg",
    )
    with pytest.raises(ValueError, match="не относится"):
        operator_chat.create_client_report(
            dialog["id"], owner, "unwanted_image", "", image_message_id=operator_image["id"],
        )


def test_report_admin_routes_and_expired_evidence_are_protected(operator_db, monkeypatch):
    _settings(max_active_dialogs=2, evidence_retention_days=30)
    owner, other = _operator("report-route-owner"), _operator("report-route-other")
    ok, _ = db.create_user("report-route-admin", "sufficient-password", "Admin", "admin")
    assert ok
    admin = db.get_user("report-route-admin")["id"]
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=old)
    dialog = operator_chat.request_dialog(210, profile=None, faq_context=None, ai_messages=[], now=old)
    root = Path(f".operator-evidence-{uuid.uuid4().hex}")
    root.mkdir()
    evidence = root / "report-proof.jpg"
    evidence.write_bytes(_jpeg_bytes())
    report = operator_chat.create_client_report(
        dialog["id"], owner, "spam", "", evidence_path=evidence.name, now=old,
    )
    monkeypatch.setattr(web, "OPERATOR_CHAT_IMAGE_DIR", str(root))
    client = web.app.test_client()
    _login_session(client, other, "report-route-other", "operator")
    assert client.get("/operator-chat/reports").status_code == 302
    _login_session(client, admin, "report-route-admin", "admin")
    assert client.get("/operator-chat/reports").status_code == 200
    assert client.get(f"/operator-chat/reports/{report['id']}").status_code == 200
    assert client.get(f"/operator-chat/reports/{report['id']}/evidence").status_code == 404
    assert client.post(
        f"/operator-chat/reports/{report['id']}/confirmed",
    ).status_code == 400
    assert client.post(
        f"/operator-chat/reports/{report['id']}/confirmed",
        headers={"X-CSRF-Token": "csrf"},
    ).status_code == 200
    assert client.post(
        f"/operator-chat/reports/{report['id']}/rejected",
        headers={"X-CSRF-Token": "csrf"},
    ).status_code == 409
    evidence.unlink()
    root.rmdir()


def test_blocked_client_stale_operator_flow_is_cleared(operator_db, monkeypatch):
    _settings(max_active_dialogs=2, report_threshold=1)
    owner = _operator("blocked-flow-owner")
    ok, _ = db.create_user("blocked-flow-admin", "sufficient-password", "Admin", "admin")
    assert ok
    admin = db.get_user("blocked-flow-admin")["id"]
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(212, profile=None, faq_context=None, ai_messages=[])
    report = operator_chat.create_client_report(dialog["id"], owner, "spam", "")
    operator_chat.decide_client_report(report["id"], admin, "confirmed")
    state = bot._get_state(212)
    state["state"] = bot.S.OPERATOR_CHAT
    messages = []
    monkeypatch.setattr(bot, "send_message", lambda chat_id, text, **_kwargs: messages.append((chat_id, text)))
    monkeypatch.setattr(bot, "send_main_menu", lambda chat_id, text=None: messages.append((chat_id, text)))
    bot._on_operator_text(212, state, "stale text")
    assert state.get("state") == bot.S.MENU
    assert messages[-1] == (212, "Связь с оператором временно недоступна.")


def _outbox_rows(dialog_id: int):
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM operator_outbox WHERE dialog_id=? ORDER BY id", (dialog_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def test_cancel_waiting_creates_one_durable_terminal_keyboard(operator_db):
    _settings(max_active_dialogs=1)
    owner = _operator("cancel-terminal-owner")
    operator_chat.start_shift(owner)
    operator_chat.request_dialog(301, profile=None, faq_context=None, ai_messages=[])
    waiting = operator_chat.request_dialog(302, profile=None, faq_context=None, ai_messages=[])
    assert waiting["status"] == "waiting"

    assert operator_chat.cancel_waiting(302)
    assert not operator_chat.cancel_waiting(302)
    terminal = [
        item for item in _outbox_rows(waiting["id"])
        if item["event_key"].endswith(":cancelled_by_client")
    ]
    assert len(terminal) == 1
    assert terminal[0]["kind"] == "buttons"
    assert json.loads(terminal[0]["buttons_json"])[0][0]["payload"] == "main_menu"


@pytest.mark.parametrize("terminal_kind", ["close", "report", "timeout", "queue_timeout"])
def test_every_terminal_path_has_durable_exit_keyboard(
    operator_db, terminal_kind,
):
    _settings(max_active_dialogs=2, inactivity_timeout_min=30, warning_before_min=5)
    owner = _operator(f"terminal-{terminal_kind}")
    base = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    dialog = operator_chat.request_dialog(
        310, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    if terminal_kind == "close":
        operator_chat.close_dialog(dialog["id"], owner, now=base)
    elif terminal_kind == "report":
        operator_chat.create_client_report(
            dialog["id"], owner, "spam", "", now=base,
        )
    elif terminal_kind == "timeout":
        operator_chat.heartbeat(owner, now=base + timedelta(minutes=31))
        operator_chat.process_timeouts(now=base + timedelta(minutes=31))
    else:
        second = operator_chat.request_dialog(
            311, profile=None, faq_context=None, ai_messages=[], now=base,
        )
        assert second["status"] == "active"
        waiting = operator_chat.request_dialog(
            312, profile=None, faq_context=None, ai_messages=[], now=base,
        )
        dialog = waiting
        operator_chat.process_timeouts(now=base + timedelta(minutes=31))
    terminal = [item for item in _outbox_rows(dialog["id"]) if item["buttons_json"]]
    assert terminal
    buttons = json.loads(terminal[-1]["buttons_json"])
    assert any(button.get("payload") == "main_menu" for row in buttons for button in row)


def test_timeout_scheduler_is_idempotent_for_terminal_notification(operator_db):
    _settings(max_active_dialogs=1, inactivity_timeout_min=30, warning_before_min=5)
    owner = _operator("timeout-idempotency")
    base = datetime(2026, 1, 3, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    dialog = operator_chat.request_dialog(
        313, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    operator_chat.heartbeat(owner, now=base + timedelta(minutes=31))
    operator_chat.process_timeouts(now=base + timedelta(minutes=31))
    operator_chat.process_timeouts(now=base + timedelta(minutes=32))
    assert sum(
        item["event_key"] == f"dialog:{dialog['id']}:timed_out"
        for item in _outbox_rows(dialog["id"])
    ) == 1


def test_attachment_only_update_reconciles_stale_operator_flow(
    operator_db, monkeypatch,
):
    state = bot._get_state(314)
    state["state"] = bot.S.OPERATOR_CHAT
    sent = []
    monkeypatch.setattr(
        bot, "send_main_menu",
        lambda chat_id, text=None: sent.append((chat_id, text)),
    )
    bot.handle_message({
        "recipient": {"chat_id": 314},
        "body": {"attachments": [{"type": "image", "payload": {}}]},
    })
    assert state["state"] == bot.S.MENU
    assert sent == [(314, "Диалог с оператором завершён.")]


def test_stale_text_after_close_does_not_reopen_or_append(operator_db, monkeypatch):
    _settings(max_active_dialogs=1)
    owner = _operator("stale-text-owner")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(
        315, profile=None, faq_context=None, ai_messages=[],
    )
    operator_chat.deliver_outbox(lambda _item: (True, None))
    operator_chat.close_dialog(dialog["id"], owner)
    operator_chat.deliver_outbox(lambda _item: (True, None))
    state = bot._get_state(315)
    state["state"] = bot.S.OPERATOR_CHAT
    sent = []
    monkeypatch.setattr(
        bot, "send_main_menu", lambda chat_id, text=None: sent.append((chat_id, text)),
    )
    bot.handle_message({"recipient": {"chat_id": 315}, "body": {"text": "поздний ответ"}})
    assert state["state"] == bot.S.MENU
    assert sent == []  # durable terminal outbox owns the visible notification
    conn = db.get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM operator_messages WHERE dialog_id=? AND sender='client'",
        (dialog["id"],),
    ).fetchone()[0]
    conn.close()
    assert count == 0


def test_transfer_reassignment_notifies_once_and_stale_cancel_preserves_active_dialog(
    operator_db, monkeypatch,
):
    _settings(max_active_dialogs=2)
    first, second = _operator("transfer-notify-first"), _operator("transfer-notify-second")
    now = operator_chat.utc_now()
    operator_chat.start_shift(first, now=now)
    monkeypatch.setattr(operator_chat.random, "choice", lambda values: min(values))
    dialog = operator_chat.request_dialog(
        316, profile=None, faq_context=None, ai_messages=[], now=now,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=now)
    operator_chat.start_shift(second, now=now)
    operator_chat.transfer_all_dialogs(first, now=now + timedelta(seconds=1))
    assert operator_chat.assign_waiting(now=now + timedelta(seconds=1))
    assert not operator_chat.assign_waiting(now=now + timedelta(seconds=1))
    rows = _outbox_rows(dialog["id"])
    reassigned = [row for row in rows if "направили вас к другому" in row["body"]]
    assert len(reassigned) == 1

    state = bot._get_state(316)
    state["state"] = bot.S.OPERATOR_CHAT
    messages = []
    monkeypatch.setattr(bot, "send_message", lambda chat_id, text: messages.append((chat_id, text)))
    bot._cancel_operator_wait(316, state)
    assert operator_chat.get_open_dialog_for_chat(316)["status"] == "active"
    assert state["state"] == bot.S.OPERATOR_CHAT
    assert messages == [(316, "Оператор уже подключён к диалогу.")]


def test_faq_link_columns_migrate_and_round_trip(operator_db):
    script_id = db.create_script("FAQ со ссылкой")
    node_id = db.add_script_node(
        script_id, "Инструкция", True,
        "https://example.test/help", "Открыть сайт",
    )
    tree = db.get_script_tree(script_id)
    node = next(item for item in tree["nodes"] if item["id"] == node_id)
    assert node["link_url"] == "https://example.test/help"
    assert node["link_text"] == "Открыть сайт"
    db.init_db()  # additive migration remains idempotent on an existing database
    assert db.get_script_nodes(script_id)[0]["link_url"] == "https://example.test/help"


def test_faq_button_labels_validate_on_every_write_and_legacy_data_survives_migration(
    operator_db,
):
    with pytest.raises(ValueError):
        db.create_script("X\n")
    script_id = db.create_script("Valid")
    with pytest.raises(ValueError):
        db.update_script(script_id, "X" * 129, 0, True)
    first = db.add_script_node(script_id, "First", False)
    second = db.add_script_node(script_id, "Second", True)
    with pytest.raises(ValueError):
        db.add_script_node(script_id, "Unsafe", True, "javascript:alert(1)", "Open")
    with pytest.raises(ValueError):
        db.update_script_node(first, "First", False, "https://example.test", "Bad\u200b")
    with pytest.raises(ValueError):
        db.add_script_edge(script_id, first, "Bad\u200b", second)
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO scripts(title,sort_order,is_active) VALUES('legacy' || char(10),99,1)",
    )
    conn.commit()
    conn.close()
    db.init_db()
    assert any("legacy" in row["title"] for row in db.get_all_scripts())


def test_script_link_validation_rejects_unsafe_url_and_defaults_label():
    with pytest.raises(ValueError):
        web._validate_script_link("javascript:alert(1)", "Плохая ссылка")
    assert web._validate_script_link("https://example.test/help", "") == (
        "https://example.test/help", "Открыть сайт",
    )
    with pytest.raises(ValueError, match="Укажите адрес"):
        web._validate_script_link("", "Лишний текст")
    with pytest.raises(ValueError):
        web._validate_script_link("https://example.test", "Сайт\n")
    with pytest.raises(ValueError):
        web._validate_script_link(" https://example.test", "Сайт")


@pytest.mark.parametrize("transition", ["report", "transfer", "timeout"])
def test_transition_supersedes_failed_reply_and_unblocks_successor(
    operator_db, monkeypatch, transition,
):
    _settings(max_active_dialogs=2, inactivity_timeout_min=30, warning_before_min=5)
    first, second = _operator(f"supersede-{transition}-1"), _operator(f"supersede-{transition}-2")
    base = datetime(2026, 2, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(first, now=base)
    monkeypatch.setattr(operator_chat.random, "choice", lambda values: min(values))
    dialog = operator_chat.request_dialog(
        400, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    message = operator_chat.add_message(
        dialog["id"], "operator", "не доставлено", user_id=first, now=base,
    )
    outbox = operator_chat.get_outbox_item_for_message(message["id"])
    operator_chat.deliver_outbox(
        lambda _item: (False, "max_http_500"), now=base,
        only_id=outbox["id"],
    )

    if transition == "report":
        operator_chat.create_client_report(
            dialog["id"], first, "spam", "", now=base + timedelta(seconds=1),
        )
    elif transition == "transfer":
        operator_chat.start_shift(second, now=base + timedelta(seconds=1))
        operator_chat.transfer_all_dialogs(first, now=base + timedelta(seconds=1))
        operator_chat.assign_waiting(now=base + timedelta(seconds=1))
    else:
        later = base + timedelta(minutes=31)
        operator_chat.heartbeat(first, now=later)
        operator_chat.process_timeouts(now=later)

    saved = operator_chat.get_outbox_item_for_message(message["id"])
    assert saved["status"] == "failed" and saved["next_retry_at"] is None
    assert not operator_chat.retry_message(message["id"], first)
    assert not operator_chat.retry_message(message["id"], second)
    delivered = []
    operator_chat.deliver_outbox(
        lambda item: delivered.append(item["event_key"]) or (True, None),
        now=base + timedelta(minutes=32),
    )
    expected_suffix = {
        "report": ":closed", "transfer": ":assigned:2", "timeout": ":timed_out",
    }[transition]
    assert len(delivered) == 1
    assert delivered[0].endswith(expected_suffix)


@pytest.mark.parametrize("transition", ["close", "report", "transfer", "timeout"])
def test_transition_never_races_live_send(operator_db, transition):
    _settings(max_active_dialogs=2, inactivity_timeout_min=30, warning_before_min=5)
    owner = _operator(f"sending-{transition}")
    base = datetime(2026, 2, 2, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    dialog = operator_chat.request_dialog(
        401, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    message = operator_chat.add_message(
        dialog["id"], "operator", "в полёте", user_id=owner, now=base,
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE operator_outbox SET status='sending',lease_at=? WHERE message_id=?",
        (base.isoformat(timespec="seconds"), message["id"]),
    )
    conn.commit()
    conn.close()

    if transition == "close":
        with pytest.raises(operator_chat.DeliveryInProgressError):
            operator_chat.close_dialog(dialog["id"], owner, now=base + timedelta(seconds=1))
    elif transition == "report":
        with pytest.raises(operator_chat.DeliveryInProgressError):
            operator_chat.create_client_report(
                dialog["id"], owner, "spam", "", now=base + timedelta(seconds=1),
            )
    elif transition == "transfer":
        with pytest.raises(operator_chat.DeliveryInProgressError):
            operator_chat.transfer_all_dialogs(owner, now=base + timedelta(seconds=1))
    else:
        later = base + timedelta(minutes=31)
        conn = db.get_conn()
        conn.execute(
            "UPDATE operator_outbox SET lease_at=? WHERE message_id=?",
            (later.isoformat(timespec="seconds"), message["id"]),
        )
        conn.commit()
        conn.close()
        operator_chat.heartbeat(owner, now=later)
        events = operator_chat.process_timeouts(now=later)
        assert not events["closed"]
    assert operator_chat.get_open_dialog_for_chat(401)["status"] == "active"


@pytest.mark.parametrize("transition", ["close", "report", "transfer", "requeue"])
def test_live_assignment_event_blocks_every_transition(operator_db, transition):
    _settings(max_active_dialogs=1, inactivity_timeout_min=30, warning_before_min=5)
    owner = _operator(f"assignment-live-{transition}")
    base = datetime(2026, 4, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    dialog = operator_chat.request_dialog(
        450, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    conn = db.get_conn()
    event = conn.execute(
        "SELECT id FROM operator_outbox WHERE dialog_id=? ORDER BY id DESC LIMIT 1",
        (dialog["id"],),
    ).fetchone()
    assert event is not None
    transition_at = base + (
        timedelta(minutes=6) if transition == "requeue" else timedelta(seconds=1)
    )
    conn.execute(
        "UPDATE operator_outbox SET status='sending',lease_at=? WHERE id=?",
        (transition_at.isoformat(timespec="seconds"), event["id"]),
    )
    conn.commit()
    conn.close()
    if transition == "close":
        with pytest.raises(operator_chat.DeliveryInProgressError):
            operator_chat.close_dialog(dialog["id"], owner, now=transition_at)
    elif transition == "report":
        with pytest.raises(operator_chat.DeliveryInProgressError):
            operator_chat.create_client_report(
                dialog["id"], owner, "spam", "", now=transition_at,
            )
    elif transition == "transfer":
        with pytest.raises(operator_chat.DeliveryInProgressError):
            operator_chat.transfer_all_dialogs(owner, now=transition_at)
    else:
        result = operator_chat.process_timeouts(now=transition_at)
        assert result["requeued"] == []
    assert operator_chat.get_open_dialog_for_chat(450)["status"] == "active"


def test_live_warning_event_defers_timeout_close(operator_db):
    _settings(max_active_dialogs=1, inactivity_timeout_min=30, warning_before_min=5)
    owner = _operator("warning-live-timeout")
    base = datetime(2026, 4, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    dialog = operator_chat.request_dialog(
        452, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    operator_chat.heartbeat(owner, now=base + timedelta(minutes=26))
    operator_chat.process_timeouts(now=base + timedelta(minutes=26))
    transition_at = base + timedelta(minutes=31)
    conn = db.get_conn()
    event = conn.execute(
        "SELECT id FROM operator_outbox WHERE event_key LIKE ?",
        (f"dialog:{dialog['id']}:warning:%",),
    ).fetchone()
    conn.execute(
        "UPDATE operator_outbox SET status='sending',lease_at=? WHERE id=?",
        (transition_at.isoformat(timespec="seconds"), event["id"]),
    )
    conn.commit()
    conn.close()
    operator_chat.heartbeat(owner, now=transition_at)
    assert operator_chat.process_timeouts(now=transition_at)["closed"] == []
    assert operator_chat.get_open_dialog_for_chat(452)["status"] == "active"


@pytest.mark.parametrize("warning_status", ["pending", "failed"])
def test_client_activity_supersedes_unsent_inactivity_warning(
    operator_db, warning_status,
):
    _settings(max_active_dialogs=1, inactivity_timeout_min=30, warning_before_min=5)
    owner = _operator(f"warning-activity-{warning_status}")
    base = datetime(2026, 6, 1, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    dialog = operator_chat.request_dialog(
        520, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    operator_chat.heartbeat(owner, now=base + timedelta(minutes=26))
    operator_chat.process_timeouts(now=base + timedelta(minutes=26))
    if warning_status == "failed":
        conn = db.get_conn()
        conn.execute(
            "UPDATE operator_outbox SET status='failed',next_retry_at=? WHERE event_key LIKE ?",
            ((base + timedelta(minutes=27)).isoformat(timespec="seconds"),
             f"dialog:{dialog['id']}:warning:%"),
        )
        conn.commit()
        conn.close()
    operator_chat.add_message(
        dialog["id"], "client", "Я здесь", now=base + timedelta(minutes=27),
    )
    rows = _outbox_rows(dialog["id"])
    warning = next(row for row in rows if ":warning:" in row["event_key"])
    assert warning["next_retry_at"] is None
    assert warning["last_error"] == "superseded_by_client_activity"
    sent = []
    operator_chat.deliver_outbox(
        lambda item: (sent.append(item["body"]) or True, None),
        now=base + timedelta(minutes=28),
    )
    assert all("будет закрыт" not in body for body in sent)


def test_client_activity_correction_follows_in_flight_warning(operator_db):
    _settings(max_active_dialogs=1, inactivity_timeout_min=30, warning_before_min=5)
    owner = _operator("warning-activity-sending")
    base = datetime(2026, 6, 2, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    dialog = operator_chat.request_dialog(
        521, profile=None, faq_context=None, ai_messages=[], now=base,
    )
    operator_chat.deliver_outbox(lambda _item: (True, None), now=base)
    operator_chat.heartbeat(owner, now=base + timedelta(minutes=26))
    operator_chat.process_timeouts(now=base + timedelta(minutes=26))
    delivered = []

    def client_responds_while_sending(item):
        delivered.append(item["body"])
        if ":warning:" in item["event_key"]:
            operator_chat.add_message(
                dialog["id"], "client", "Я здесь",
                now=base + timedelta(minutes=27),
            )
        return True, None

    operator_chat.deliver_outbox(
        client_responds_while_sending, now=base + timedelta(minutes=27),
    )
    assert delivered == [
        "Диалог будет закрыт через 5 мин. без новых сообщений.",
        "Активность получена. Диалог остаётся открытым.",
    ]


def test_assignment_send_cas_finishes_before_later_transition(operator_db):
    _settings(max_active_dialogs=1)
    owner = _operator("assignment-cas")
    base = datetime(2026, 4, 2, 10, tzinfo=timezone.utc)
    operator_chat.start_shift(owner, now=base)
    operator_chat.request_dialog(
        451, profile=None, faq_context=None, ai_messages=[], now=base,
    )

    def transition_during_send(_item):
        with pytest.raises(operator_chat.DeliveryInProgressError):
            operator_chat.transfer_all_dialogs(owner, now=base + timedelta(seconds=1))
        return False, "max_http_500"

    result = operator_chat.deliver_outbox(transition_during_send, now=base)
    assert result[0]["status"] == "failed"
    assert operator_chat.get_open_dialog_for_chat(451)["status"] == "active"
    operator_chat.transfer_all_dialogs(owner, now=base + timedelta(seconds=2))
    assert operator_chat.get_open_dialog_for_chat(451)["status"] == "waiting"


def test_terminal_outbox_dead_letter_rearms_and_recovers_exactly_once(
    operator_db, monkeypatch,
):
    _settings(max_active_dialogs=1)
    owner = _operator("terminal-owner")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(402, profile=None, faq_context=None, ai_messages=[])
    operator_chat.deliver_outbox(lambda _item: (True, None))
    operator_chat.close_dialog(dialog["id"], owner)
    state = bot._get_state(402)
    state["state"] = bot.S.OPERATOR_CHAT
    sent = []
    monkeypatch.setattr(
        bot, "_deliver_operator_outbox",
        lambda item: (sent.append(item["event_key"]) or False, "max_http_500"),
    )
    bot.handle_message({
        "recipient": {"chat_id": 402},
        "body": {"attachments": [{"type": "image", "payload": {}}]},
    })
    assert sent == [f"dialog:{dialog['id']}:closed"]
    assert state["state"] == bot.S.OPERATOR_CHAT

    conn = db.get_conn()
    conn.execute(
        """UPDATE operator_outbox SET status='failed',attempts=?,next_retry_at=NULL
           WHERE dialog_id=? AND event_key=?""",
        (operator_chat.MAX_OUTBOX_ATTEMPTS, dialog["id"], f"dialog:{dialog['id']}:closed"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        bot, "_deliver_operator_outbox",
        lambda item: (sent.append(item["event_key"]) or True, None),
    )
    bot.handle_message({"recipient": {"chat_id": 402}, "body": {"text": "ещё"}})
    assert sent == [f"dialog:{dialog['id']}:closed"] * 2
    assert state["state"] == bot.S.MENU
    bot.handle_message({"recipient": {"chat_id": 402}, "body": {"text": "после"}})
    assert sent == [f"dialog:{dialog['id']}:closed"] * 2


@pytest.mark.parametrize("payload", ["main_menu", "script:999"])
def test_stale_callback_rearms_terminal_notice_before_dispatch(
    operator_db, monkeypatch, payload,
):
    _settings(max_active_dialogs=1)
    owner = _operator(f"terminal-callback-{payload.split(':')[0]}")
    operator_chat.start_shift(owner)
    chat_id = 530 if payload == "main_menu" else 531
    dialog = operator_chat.request_dialog(
        chat_id, profile=None, faq_context=None, ai_messages=[],
    )
    operator_chat.deliver_outbox(lambda _item: (True, None))
    operator_chat.close_dialog(dialog["id"], owner)
    conn = db.get_conn()
    conn.execute(
        """UPDATE operator_outbox SET status='failed',attempts=?,next_retry_at=NULL
           WHERE dialog_id=? AND event_key=?""",
        (operator_chat.MAX_OUTBOX_ATTEMPTS, dialog["id"], f"dialog:{dialog['id']}:closed"),
    )
    conn.commit()
    conn.close()
    state = bot._get_state(chat_id)
    state["state"] = bot.S.OPERATOR_CHAT
    sent = []
    monkeypatch.setattr(bot, "_ack_callback", lambda _callback_id: None)
    monkeypatch.setattr(
        bot, "_deliver_operator_outbox",
        lambda item: (sent.append(item["event_key"]) or True, None),
    )
    bot.handle_callback({
        "message": {"recipient": {"chat_id": chat_id}},
        "callback": {"callback_id": "stale", "payload": payload},
    })
    assert sent == [f"dialog:{dialog['id']}:closed"]
    assert state["state"] == bot.S.MENU


def test_first_rating_click_dispatches_after_delivered_terminal(operator_db, monkeypatch):
    _settings(max_active_dialogs=1)
    owner = _operator("terminal-first-rating")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(
        532, profile=None, faq_context=None, ai_messages=[],
    )
    operator_chat.deliver_outbox(lambda _item: (True, None))
    operator_chat.close_dialog(dialog["id"], owner)
    operator_chat.deliver_outbox(lambda _item: (True, None))
    state = bot._get_state(532)
    state["state"] = bot.S.OPERATOR_CHAT
    menus = []
    monkeypatch.setattr(bot, "_ack_callback", lambda _callback_id: None)
    monkeypatch.setattr(
        bot, "send_main_menu",
        lambda chat_id, text=None: menus.append((chat_id, text)) or True,
    )
    bot.handle_callback({
        "message": {"recipient": {"chat_id": 532}},
        "callback": {
            "callback_id": "rating-first-click",
            "payload": f"operator_rate:{dialog['id']}-5",
        },
    })
    saved = operator_chat.get_history_dialog(dialog["id"])
    assert saved["rating"] == 5
    assert menus == [(532, "Спасибо за оценку!")]


@pytest.mark.parametrize(("status", "attempts", "next_retry", "expected_state"), [
    ("delivered", 0, None, bot.S.MENU),
    ("failed", 1, "2099-01-01T00:00:00+00:00", bot.S.OPERATOR_CHAT),
])
def test_delivered_or_retryable_terminal_outbox_owns_client_notice(
    operator_db, monkeypatch, status, attempts, next_retry, expected_state,
):
    _settings(max_active_dialogs=1)
    owner = _operator(f"terminal-notice-{status}")
    operator_chat.start_shift(owner)
    dialog = operator_chat.request_dialog(403, profile=None, faq_context=None, ai_messages=[])
    operator_chat.deliver_outbox(lambda _item: (True, None))
    operator_chat.close_dialog(dialog["id"], owner)
    conn = db.get_conn()
    conn.execute(
        """UPDATE operator_outbox SET status=?,attempts=?,next_retry_at=?
           WHERE dialog_id=? AND event_key=?""",
        (
            status, attempts, next_retry, dialog["id"],
            f"dialog:{dialog['id']}:closed",
        ),
    )
    conn.commit()
    conn.close()
    state = bot._get_state(403)
    state["state"] = bot.S.OPERATOR_CHAT
    sent = []
    monkeypatch.setattr(bot, "send_main_menu", lambda *args: sent.append(args))
    bot.handle_message({"recipient": {"chat_id": 403}, "body": {"text": "stale"}})
    assert state["state"] == expected_state
    assert sent == []


def test_faq_editor_all_mutations_require_admin_csrf(operator_db):
    ok, _ = db.create_user("faq-csrf-admin", "sufficient-password", "Admin", "admin")
    assert ok
    admin = db.get_user("faq-csrf-admin")["id"]
    operator = _operator("faq-csrf-operator")
    script_id = db.create_script("CSRF baseline")
    first = db.add_script_node(script_id, "First", False)
    second = db.add_script_node(script_id, "Second", True)
    edge_id, error = db.add_script_edge(script_id, first, "Next", second)
    assert error is None

    requests = [
        ("/scripts/create", {"title": "Forbidden create"}),
        (f"/scripts/{script_id}/update", {"title": "Forbidden rename"}),
        (f"/scripts/{script_id}/delete", {}),
        (f"/scripts/{script_id}/nodes/add", {"title": "Forbidden node"}),
        (f"/scripts/{script_id}/nodes/{first}/update", {"title": "Forbidden node update"}),
        (f"/scripts/{script_id}/nodes/{second}/delete", {}),
        (
            f"/scripts/{script_id}/edges/add",
            {"from_node_id": first, "to_node_id": second, "label": "Forbidden edge"},
        ),
        (f"/scripts/{script_id}/edges/{edge_id}/delete", {}),
    ]

    def snapshot():
        return (
            [dict(row) for row in db.get_all_scripts()],
            [dict(row) for row in db.get_script_nodes(script_id)],
            [dict(row) for row in db.get_script_edges(script_id)],
        )

    baseline = snapshot()
    client = web.app.test_client()
    _login_session(client, admin, "faq-csrf-admin", "admin")
    for url, data in requests:
        assert client.post(url, data=data).status_code == 400
        assert client.post(url, data={**data, "csrf_token": "wrong"}).status_code == 400
        assert snapshot() == baseline

    _login_session(client, operator, "faq-csrf-operator", "operator")
    for url, data in requests:
        assert client.post(url, data={**data, "csrf_token": "csrf"}).status_code == 302
        assert snapshot() == baseline

    _login_session(client, admin, "faq-csrf-admin", "admin")
    response = client.post(
        f"/scripts/{script_id}/nodes/add",
        data={"title": "Allowed node", "csrf_token": "csrf"},
    )
    assert response.status_code == 302
    assert any(row["title"] == "Allowed node" for row in db.get_script_nodes(script_id))


def test_login_rate_limit_is_shared_atomic_and_resettable(operator_db):
    ip = "2001:db8::1"
    base = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    # Sequential calls represent independent Gunicorn workers because no
    # process-local object carries the state between calls.
    for expected_left in (4, 3, 2, 1):
        assert db.record_login_failure(ip, now=base) == (False, expected_left)
    assert db.record_login_failure(ip, now=base) == (True, 0)
    assert db.get_login_block_seconds(ip, now=base) == 15 * 60
    db.reset_login_rate_limit(ip)
    assert db.get_login_block_seconds(ip, now=base) == 0
    assert db.record_login_failure(ip, now=base + timedelta(minutes=16)) == (False, 4)


def test_login_rate_limit_concurrent_workers_do_not_lose_failures(operator_db):
    ip = "192.0.2.25"
    base = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    barrier = threading.Barrier(5)
    results = []
    failures = []

    def fail_login():
        try:
            barrier.wait()
            results.append(db.record_login_failure(ip, now=base))
        except sqlite3.Error as exc:  # pragma: no cover - assertion reports detail
            failures.append(exc)

    threads = [threading.Thread(target=fail_login) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert failures == []
    assert len(results) == 5
    assert sum(blocked for blocked, _left in results) == 1
    assert db.get_login_block_seconds(ip, now=base) == 15 * 60


def test_login_rate_limit_migration_and_ip_normalization(operator_db):
    db.init_db()
    conn = db.get_conn()
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(login_rate_limits)")
    }
    conn.close()
    assert {
        "ip", "attempts", "window_started_at", "blocked_until", "updated_at",
    } <= columns
    assert web._normalize_login_ip("2001:0db8:0:0::1") == "2001:db8::1"
    assert web._normalize_login_ip("::ffff:192.0.2.1") == "192.0.2.1"
    assert web._normalize_login_ip("x" * 10000) == "unknown"


def test_login_ip_uses_exactly_one_hop_from_trusted_proxy(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.0/8,172.31.44.0/24,::1/128")
    assert web._client_login_ip("198.51.100.10", "203.0.113.1") == "198.51.100.10"
    assert web._client_login_ip("172.31.44.5", "203.0.113.1") == "203.0.113.1"
    assert web._client_login_ip("172.31.44.5", "203.0.113.1, 198.51.100.2") == "172.31.44.5"
    assert web._client_login_ip("::1", "2001:0db8::7") == "2001:db8::7"
    assert web._client_login_ip("172.31.44.5", "not-an-ip") == "172.31.44.5"


def test_get_login_is_read_only_and_keeps_clients_separate(operator_db, monkeypatch):
    traces = []
    original_get_conn = db.get_conn

    def traced_connection():
        conn = original_get_conn()
        conn.set_trace_callback(traces.append)
        return conn

    monkeypatch.setattr(db, "get_conn", traced_connection)
    client = web.app.test_client()
    assert client.get("/login", environ_base={"REMOTE_ADDR": "192.0.2.10"}).status_code == 200
    assert not any(
        statement.lstrip().upper().startswith(("DELETE", "INSERT", "UPDATE", "BEGIN"))
        for statement in traces
    )
    monkeypatch.setattr(db, "get_conn", original_get_conn)
    for _ in range(5):
        db.record_login_failure("192.0.2.10")
    assert db.get_login_block_seconds("192.0.2.10") > 0
    assert db.get_login_block_seconds("192.0.2.11") == 0


def test_successful_web_login_resets_shared_rate_limit(operator_db):
    ok, _ = db.create_user(
        "shared-login", "sufficient-password", "Shared Login", "operator",
    )
    assert ok
    limit_key = web._login_limit_key("127.0.0.1", "shared-login")
    for _ in range(4):
        blocked, _left = db.record_login_failure(limit_key)
        assert not blocked
    client = web.app.test_client()
    response = client.post(
        "/login",
        data={"username": "shared-login", "password": "sufficient-password"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )
    assert response.status_code == 302
    assert db.get_login_block_seconds(limit_key) == 0
    conn = db.get_conn()
    assert conn.execute(
        "SELECT 1 FROM login_rate_limits WHERE ip=?", (limit_key,),
    ).fetchone() is None
    conn.close()


def test_login_rate_limit_isolated_by_account_behind_same_proxy(operator_db):
    ip = "172.30.0.1"
    first = web._login_limit_key(ip, "first-user")
    second = web._login_limit_key(ip, "second-user")
    for _ in range(5):
        db.record_login_failure(first)
    assert db.get_login_block_seconds(first) > 0
    assert db.get_login_block_seconds(second) == 0
    assert first != second


def test_web_login_two_level_limiter_bounds_unknown_names_and_preserves_peer(operator_db):
    ok, _ = db.create_user("proxy-first", "sufficient-password", "First", "operator")
    assert ok
    ok, _ = db.create_user("proxy-second", "sufficient-password", "Second", "operator")
    assert ok
    ip = "172.30.0.9"
    client = web.app.test_client()
    for _ in range(5):
        client.post(
            "/login", data={"username": "proxy-first", "password": "wrong"},
            environ_base={"REMOTE_ADDR": ip},
        )
    response = client.post(
        "/login",
        data={"username": "proxy-second", "password": "sufficient-password"},
        environ_base={"REMOTE_ADDR": ip},
    )
    assert response.status_code == 302

    unknown_ip = "172.30.0.10"
    stranger = web.app.test_client()
    for index in range(20):
        stranger.post(
            "/login", data={"username": f"unknown-{index}", "password": "wrong"},
            environ_base={"REMOTE_ADDR": unknown_ip},
        )
    conn = db.get_conn()
    keys = conn.execute(
        "SELECT ip FROM login_rate_limits WHERE ip LIKE ?",
        (f"{unknown_ip}|%",),
    ).fetchall()
    conn.close()
    assert len(keys) == 2  # one IP sentinel and one shared unknown-account bucket
    assert db.get_login_block_seconds(web._login_ip_limit_key(unknown_ip)) == 0


def test_ip_spray_bucket_has_higher_bounded_threshold(operator_db):
    key = web._login_ip_limit_key("192.0.2.200")
    for _ in range(web._MAX_IP_LOGIN_ATTEMPTS - 1):
        blocked, _ = db.record_login_failure(
            key, max_attempts=web._MAX_IP_LOGIN_ATTEMPTS,
        )
        assert not blocked
    blocked, _ = db.record_login_failure(
        key, max_attempts=web._MAX_IP_LOGIN_ATTEMPTS,
    )
    assert blocked
