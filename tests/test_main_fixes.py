from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import database as db


@pytest.fixture()
def clean_db(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()
    return path


def _future_slot(days: int = 2):
    date = (datetime.now() + timedelta(days=days)).date()
    return date, "10:00"


def _branch_with_schedule(date, capacity=1, start="09:00", end="12:00"):
    branch_id = db.create_branch("Центр", "ул. Тестовая")
    db.create_schedule(branch_id, date.weekday(), start, end, 30, capacity, 14)
    return branch_id


def test_bootstrap_admin_requires_explicit_secret_and_forces_change(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "bootstrap.sqlite"))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()
    assert db.get_user("admin") is None

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "with-admin.sqlite"))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "one-time-password-123")
    db.init_db()
    admin = db.get_user("admin")
    assert admin["must_change_password"] == 1
    assert check_password_hash(admin["password"], "one-time-password-123")
    assert not check_password_hash(admin["password"], "admin123")


def test_weak_bootstrap_secret_fails_before_database_changes(tmp_path, monkeypatch):
    path = tmp_path / "weak.sqlite"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "  short  ")
    with pytest.raises(RuntimeError, match="12"):
        db.init_db()
    assert not path.exists()


def test_legacy_known_admin_password_is_disabled(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, "
        "password TEXT NOT NULL, name TEXT NOT NULL, role TEXT DEFAULT 'operator')"
    )
    conn.execute(
        "INSERT INTO users VALUES (1, 'admin', ?, 'Admin', 'admin')",
        (generate_password_hash("admin123"),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    with pytest.raises(RuntimeError, match="BOOTSTRAP_ADMIN_PASSWORD"):
        db.init_db()
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "recover-admin-123")
    db.init_db()
    admin = db.get_user("admin")
    assert not check_password_hash(admin["password"], "admin123")
    assert check_password_hash(admin["password"], "recover-admin-123")
    assert admin["must_change_password"] == 1


@pytest.mark.parametrize("secret", ["", "change-me-in-production", "too-short"])
def test_production_configuration_fails_closed(secret):
    env = os.environ.copy()
    env.update(APP_ENV="production", SECRET_KEY=secret, INTERNAL_API_TOKEN="valid-api-token")
    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=os.getcwd(), env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_production_configuration_accepts_strong_secret():
    env = os.environ.copy()
    env.update(
        APP_ENV="production",
        SECRET_KEY="a-unique-production-secret-value-123456789",
        INTERNAL_API_TOKEN="valid-api-token",
    )
    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=os.getcwd(), env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_internal_api_missing_token_is_closed(monkeypatch):
    import api.deps as deps

    monkeypatch.setattr(deps, "INTERNAL_API_TOKEN", "")
    monkeypatch.setattr(deps, "ALLOW_INSECURE_DEV_API", False)
    with pytest.raises(HTTPException) as exc:
        deps.verify_token(None)
    assert exc.value.status_code == 503

    monkeypatch.setattr(deps, "ALLOW_INSECURE_DEV_API", True)
    monkeypatch.setattr(deps, "APP_ENV", "development")
    monkeypatch.setattr(deps, "API_HOST", "127.0.0.1")
    deps.verify_token(None)


def test_password_and_role_changes_revoke_existing_session(clean_db):
    ok, _ = db.create_user("operator", "initial-password", "Operator", "admin")
    assert ok
    user = db.get_user("operator")
    original_version = user["session_version"]
    db.change_role(user["id"], "operator")
    assert db.get_user("operator")["session_version"] == original_version + 1
    db.change_password(user["id"], "replacement-password")
    assert db.get_user("operator")["session_version"] == original_version + 2
    db.delete_user(user["id"])
    assert db.get_user_by_id(user["id"]) is None


def test_web_session_is_rejected_after_role_change(clean_db):
    import web

    ok, _ = db.create_user("admin2", "initial-password", "Admin", "admin")
    assert ok
    user = db.get_user("admin2")
    client = web.app.test_client()
    response = client.post(
        "/login",
        data={"username": "admin2", "password": "initial-password"},
    )
    assert response.status_code == 302
    assert client.get("/users").status_code == 200
    db.change_role(user["id"], "operator")
    response = client.get("/users")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


@pytest.mark.parametrize(
    "duration,capacity",
    [(0, 1), (-1, 1), (30, 0), (30, -1)],
)
def test_schedule_rejects_non_positive_values(clean_db, duration, capacity):
    branch = db.create_branch("Филиал", "Адрес")
    with pytest.raises(ValueError):
        db.create_schedule(branch, 1, "09:00", "10:00", duration, capacity, 14)


def test_invalid_legacy_schedule_is_skipped_without_loop(clean_db):
    date, _ = _future_slot()
    branch = db.create_branch("Филиал", "Адрес")
    conn = db.get_conn()
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        "INSERT INTO branch_schedules "
        "(branch_id,weekday,time_from,time_to,slot_duration_min,capacity,booking_horizon_days) "
        "VALUES (?,?,?,?,?,?,?)",
        (branch, date.weekday(), "09:00", "10:00", 0, 1, 14),
    )
    conn.commit()
    conn.close()
    assert db.get_available_slots(branch, date.isoformat()) == []


def test_concurrent_booking_never_exceeds_capacity(clean_db):
    date, slot = _future_slot()
    branch = _branch_with_schedule(date, capacity=1)
    barrier = threading.Barrier(2)
    results = []

    def book(ls):
        barrier.wait()
        results.append(db.create_appointment(ls, branch, date.isoformat(), slot, "max", 1))

    threads = [threading.Thread(target=book, args=(f"LS-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(row_id is not None for row_id, _ in results) == 1


def test_booking_uses_exact_day_interval_capacity(clean_db):
    date, _ = _future_slot()
    branch = _branch_with_schedule(date, capacity=1, start="09:00", end="10:00")
    db.create_schedule(branch, date.weekday(), "14:00", "16:00", 30, 2, 14)
    assert db.create_appointment("A", branch, date.isoformat(), "14:00", "max", 1)[0]
    assert db.create_appointment("B", branch, date.isoformat(), "14:00", "max", 2)[0]
    assert db.create_appointment("C", branch, date.isoformat(), "14:00", "max", 3)[0] is None


def test_overlapping_schedules_use_same_deterministic_capacity(clean_db):
    date, _ = _future_slot()
    branch = _branch_with_schedule(date, capacity=1, start="09:00", end="12:00")
    db.create_schedule(branch, date.weekday(), "10:00", "11:00", 30, 2, 14)
    assert "10:00" in db.get_available_slots(branch, date.isoformat())
    assert db.create_appointment("A", branch, date.isoformat(), "10:00", "max", 1)[0]
    assert db.create_appointment("B", branch, date.isoformat(), "10:00", "max", 2)[0]
    assert "10:00" not in db.get_available_slots(branch, date.isoformat())


def test_overlapping_schedule_horizon_is_consistent_for_listing_and_booking(clean_db):
    date, _ = _future_slot(days=14)
    branch = db.create_branch("Филиал", "Адрес")
    db.create_schedule(branch, date.weekday(), "09:00", "12:00", 30, 1, 30)
    db.create_schedule(branch, date.weekday(), "10:00", "11:00", 30, 1, 7)

    slots = db.get_available_slots(branch, date.isoformat())
    assert "09:00" in slots
    assert "10:00" not in slots
    appointment_id, error = db.create_appointment(
        "A", branch, date.isoformat(), "10:00", "max", 1
    )
    assert appointment_id is None
    assert "горизонта" in error
    assert date.isoformat() in db.get_available_dates(branch)


def test_booking_revalidates_exception_and_slot_alignment(clean_db):
    date, _ = _future_slot()
    branch = _branch_with_schedule(date)
    assert db.create_appointment("A", branch, date.isoformat(), "09:15", "max", 1)[0] is None
    db.add_exception(branch, date.isoformat(), "Закрыто")
    assert db.create_appointment("B", branch, date.isoformat(), "10:00", "max", 2)[0] is None

    far_date, _ = _future_slot(days=30)
    far_branch = _branch_with_schedule(far_date)
    assert db.create_appointment(
        "C", far_branch, far_date.isoformat(), "10:00", "max", 3
    )[0] is None


def test_house_chat_router_contract_and_keywords(clean_db, monkeypatch):
    import api.deps as deps
    import api.main as api_main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(deps, "INTERNAL_API_TOKEN", "test-token")
    client = TestClient(api_main.app)
    headers = {"Authorization": "Bearer test-token"}
    assert client.get("/api/v1/house-chats").status_code == 401
    assert client.get(
        "/api/v1/house-chats", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    scenario = client.post(
        "/api/v1/scenarios",
        headers=headers,
        json={"title": "Течь", "keywords": [" течь ", "вода"], "response_text": "Принято"},
    )
    assert scenario.status_code == 201
    chat = client.post(
        "/api/v1/house-chats",
        headers=headers,
        json={"address": "Дом 1", "messenger": "max", "chat_id": "100"},
    )
    assert chat.status_code == 201
    linked = client.post(
        f"/api/v1/house-chats/{chat.json()['id']}/scenarios/{scenario.json()['id']}",
        headers=headers,
    )
    assert linked.status_code == 201
    row = db.get_scenario(scenario.json()["id"])
    assert row["keywords"] == '["течь", "вода"]'

    for invalid in (
        {"title": " ", "keywords": ["течь"], "response_text": "Ответ"},
        {"title": "Течь", "keywords": ["   "], "response_text": "Ответ"},
        {"title": "Течь", "keywords": ["течь"], "response_text": "   "},
    ):
        assert client.post("/api/v1/scenarios", headers=headers, json=invalid).status_code == 422

    assert client.put(
        f"/api/v1/scenarios/{scenario.json()['id']}",
        headers=headers,
        json={
            "title": "Течь",
            "keywords": ["  "],
            "response_text": "Ответ",
            "is_active": True,
        },
    ).status_code == 422


def test_reopen_reason_and_latest_response_are_persisted(clean_db):
    ok, _ = db.create_user("op", "password", "Оператор", "operator")
    assert ok
    user = db.get_user("op")
    ticket = db.create_appeal("1", "max", "прочее", "Текст", 11)
    appeal = db.get_appeal_by_ticket(ticket)
    db.update_appeal_status(appeal["id"], "in_work", reason="Проблема осталась")
    assert db.get_appeal_by_id(appeal["id"])["reopen_reason"] == "Проблема осталась"
    first = db.add_appeal_response(appeal["id"], user["id"], "Первый")
    second = db.add_appeal_response(appeal["id"], user["id"], "Второй")
    assert second > first
    assert db.get_last_appeal_response(appeal["id"])["body"] == "Второй"


def test_reopen_client_sends_reason(monkeypatch):
    import client_api

    captured = {}

    def fake_patch(path, body):
        captured.update(path=path, body=body)
        return {"ok": True}, None

    monkeypatch.setattr(client_api, "_patch", fake_patch)
    client_api.reopen_appeal(42, "Вода всё ещё течёт")
    assert captured == {
        "path": "/api/v1/appeals/42/status",
        "body": {
            "status": "in_work",
            "operator_id": None,
            "reason": "Вода всё ещё течёт",
        },
    }


def test_auto_resolve_does_not_overwrite_reopened_candidate(clean_db, monkeypatch):
    ticket = db.create_appeal("1", "max", "прочее", "Текст", 11)
    appeal = db.get_appeal_by_ticket(ticket)
    old = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    conn = db.get_conn()
    conn.execute(
        "UPDATE appeals SET status='pending_confirmation', updated_at=? WHERE id=?",
        (old, appeal["id"]),
    )
    conn.commit()
    conn.close()

    real_get_conn = db.get_conn

    class CursorProxy:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            other = real_get_conn()
            other.execute("UPDATE appeals SET status='in_work' WHERE id=?", (appeal["id"],))
            other.commit()
            other.close()
            return rows

    class ConnProxy:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=()):
            cursor = self.conn.execute(sql, params)
            if sql.lstrip().startswith("SELECT id FROM appeals"):
                return CursorProxy(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self.conn, name)

    monkeypatch.setattr(db, "get_conn", lambda: ConnProxy(real_get_conn()))
    assert db.auto_resolve_pending(hours=24) == 0
    monkeypatch.setattr(db, "get_conn", real_get_conn)
    assert db.get_appeal_by_id(appeal["id"])["status"] == "in_work"
