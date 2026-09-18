from __future__ import annotations

import re
from unittest.mock import Mock

import pytest

import bot
import database as db
import web
from rso_bot.flows import ai_assistant


@pytest.fixture()
def ai_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ai.sqlite"))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()


def _deps(**overrides):
    state = {"state": "menu"}
    session = {"history": [], "faq_context": None, "question_count": 0}
    values = dict(  # noqa: C408 - names map directly to dependency fields
        get_settings=Mock(
            return_value={
                "enabled": True,
                "model": "yandexgpt/latest",
                "system_prompt": "Только ЖКХ",
                "daily_limit": 5,
                "temperature": 0.3,
                "max_output_tokens": 800,
            }
        ),
        get_session=Mock(return_value=session),
        set_context=Mock(),
        reserve_question=Mock(return_value=True),
        release_question=Mock(),
        append_exchange=Mock(),
        clear_history=Mock(),
        get_sensitive_values=Mock(
            return_value=["123456", "Иванов Иван Иванович", "ул. Ленина, д. 1"]
        ),
        complete=Mock(return_value="Оформите заявку"),
        get_state=Mock(return_value=state),
        touch=Mock(side_effect=lambda item: item),
        start_appeal_draft=Mock(),
        make_callback=lambda label, payload: {"type": "callback", "text": label, "payload": payload},
        send_buttons=Mock(),
        send_main_menu=Mock(),
        is_configured=Mock(return_value=True),
        logger=Mock(),
        question_state="ai_question",
    )
    values.update(overrides)
    return ai_assistant.AIDependencies(**values), state, session


def test_sanitizer_removes_known_and_obvious_personal_data():
    source = (
        "ФИО: Петров Петр Петрович; лицевой счет 12345678; "
        "тел. +7 (999) 123-45-67; test@example.ru; адрес: улица Ленина, дом 1; SECRET-LS"
    )
    cleaned = ai_assistant.sanitize_personal_data(source, ["SECRET-LS"])
    for secret in (
        "Петров",
        "12345678",
        "999",
        "test@example.ru",
        "Ленина",
        "SECRET-LS",
    ):
        assert secret not in cleaned


def test_yandex_client_builds_bounded_request_without_secret_in_body():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "result": {"alternatives": [{"message": {"text": "Ответ"}}]}
    }
    http = Mock()
    http.post.return_value = response
    client = ai_assistant.YandexGPTClient(
        api_key="top-secret",
        folder_id="folder",
        api_url="https://example.invalid/completion",
        timeout_seconds=7,
        http_client=http,
    )
    answer = client.complete(
        settings={
            "model": "yandexgpt/latest",
            "system_prompt": "Только ЖКХ",
            "temperature": 0.2,
            "max_output_tokens": 500,
        },
        history=[{"role": "assistant", "text": "Ранее"}],
        question="Вопрос",
        faq_context="FAQ → Вода",
    )
    assert answer == "Ответ"
    call = http.post.call_args
    assert call.kwargs["timeout"] == 7
    assert call.kwargs["headers"]["Authorization"] == "Api-Key top-secret"
    assert "top-secret" not in str(call.kwargs["json"])
    assert call.kwargs["json"]["modelUri"] == "gpt://folder/yandexgpt/latest"
    assert call.kwargs["json"]["messages"][-1] == {"role": "user", "text": "Вопрос"}


def test_database_settings_migration_and_daily_session_reset_preserve_data(ai_db):
    assert db.create_lschet("KEEP-1", "Клиент", "Дом 1")
    settings = db.get_ai_settings()
    assert settings["daily_limit"] == 5
    assert settings["enabled"] is False
    db.update_ai_settings(
        enabled=True,
        model="yandexgpt/latest",
        system_prompt="Только ЖКХ",
        daily_limit=2,
        temperature=0.4,
        max_output_tokens=700,
    )
    assert db.get_ai_settings()["enabled"] is True
    assert db.reserve_ai_question(10, 2, session_date="2026-09-18")
    assert db.reserve_ai_question(10, 2, session_date="2026-09-18")
    assert not db.reserve_ai_question(10, 2, session_date="2026-09-18")
    db.append_ai_exchange(10, "q", "a", session_date="2026-09-18")
    db.clear_ai_history(10, session_date="2026-09-18")
    same_day = db.get_ai_session(10, session_date="2026-09-18")
    assert same_day["question_count"] == 2
    assert same_day["history"] == []
    next_day = db.get_ai_session(10, session_date="2026-09-19")
    assert next_day["question_count"] == 0
    assert next_day["history"] == []
    assert db.get_ls("KEEP-1")["fio"] == "Клиент"


def test_flow_sanitizes_question_context_and_answer_before_storage():
    deps, state, session = _deps()
    session["faq_context"] = "Квартира по адресу ул. Ленина, д. 1"
    deps.complete.return_value = "Для 123456 нужно оформить заявку"

    ai_assistant.ask(42, "Мой ЛС 123456, нет воды", deps)

    kwargs = deps.complete.call_args.kwargs
    assert "123456" not in kwargs["question"]
    assert "Ленина" not in kwargs["faq_context"]
    stored = deps.append_exchange.call_args.args
    assert "123456" not in stored[1]
    assert "123456" not in stored[2]
    assert state["state"] == "ai_question"
    payloads = [row[0]["payload"] for row in deps.send_buttons.call_args.args[2]]
    assert payloads == ["ai_appeal", "ai_more", "ai_new", "main_menu"]


def test_limit_and_provider_failure_have_safe_appeal_fallback():
    limited, limited_state, _ = _deps(reserve_question=Mock(return_value=False))
    ai_assistant.ask(42, "Вопрос", limited)
    limited.complete.assert_not_called()
    assert limited_state["ai_last_exchange"]["question"] == "Вопрос"
    assert limited.send_buttons.call_args.args[2][0][0]["payload"] == "ai_appeal"

    failed, failed_state, _ = _deps(complete=Mock(side_effect=ai_assistant.AIServiceError()))
    ai_assistant.ask(43, "Вопрос", failed)
    failed.release_question.assert_called_once_with(43)
    assert failed_state["ai_last_exchange"]["question"] == "Вопрос"
    assert "временно недоступен" in failed.send_buttons.call_args.args[1]


def test_new_dialog_keeps_counter_and_ai_appeal_uses_sanitized_exchange():
    deps, state, _ = _deps()
    state["ai_last_exchange"] = {"question": "Где вода?", "answer": "Оформите заявку"}
    ai_assistant.create_appeal_draft(42, deps)
    draft = deps.start_appeal_draft.call_args.args[1]
    assert "Где вода?" in draft and "Ответ ИИ" in draft

    ai_assistant.new_dialog(42, deps)
    deps.clear_history.assert_called_once_with(42)
    assert "ai_last_exchange" not in state
    deps.reserve_question.assert_not_called()


def _login_admin(client):
    ok, _ = db.create_user("ai-admin", "initial-password", "Admin", "admin")
    assert ok
    assert client.post(
        "/login", data={"username": "ai-admin", "password": "initial-password"}
    ).status_code == 302


def test_ai_admin_page_updates_non_secret_settings_only(ai_db):
    client = web.app.test_client()
    _login_admin(client)
    page = client.get("/ai-settings")
    assert page.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/ai-settings",
        data={
            "csrf_token": token,
            "enabled": "on",
            "model": "yandexgpt/latest",
            "system_prompt": "Только ЖКХ, не выдумывай",
            "daily_limit": "5",
            "temperature": "0.2",
            "max_output_tokens": "900",
            "api_key": "must-not-be-saved",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "сохранены" in response.text
    settings = db.get_ai_settings()
    assert settings["enabled"] is True
    assert settings["daily_limit"] == 5
    columns = {row[1] for row in db.get_conn().execute("PRAGMA table_info(ai_settings)")}
    assert "api_key" not in columns


def test_operator_cannot_open_ai_admin_page(ai_db):
    ok, _ = db.create_user("ai-op", "initial-password", "Operator", "operator")
    assert ok
    client = web.app.test_client()
    client.post("/login", data={"username": "ai-op", "password": "initial-password"})
    assert client.get("/ai-settings").status_code == 302


def test_main_menu_and_router_expose_ai_entry(monkeypatch):
    sender = Mock()
    monkeypatch.setattr(bot, "send_buttons", sender)
    bot.send_main_menu(42)
    payloads = [row[0]["payload"] for row in sender.call_args.args[2]]
    assert "ai_start" in payloads
    assert bot._CALLBACK_STATIC["ai_from_faq"] is bot._start_ai_from_faq
    assert bot._MESSAGE_HANDLERS[bot.S.AI_QUESTION] is bot._on_ai_question
