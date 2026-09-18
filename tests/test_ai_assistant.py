from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
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
    monkeypatch.setattr(db, "_server_date", lambda: "2026-09-18")
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
        get_operation_date=Mock(return_value="2026-09-18"),
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


@pytest.mark.parametrize(
    ("source", "secret"),
    [
        ("Меня зовут Иванов Иван", "Иванов Иван"),
        ("Собственник Иванов И. И.", "Иванов И. И."),
        ("Живу Ленина 10, нет воды", "Ленина 10"),
        ("ЛС: 7, нет воды", "ЛС: 7"),
        ("ЛС № AB/12-3, нет воды", "AB/12-3"),
    ],
)
def test_sanitizer_covers_names_initials_unmarked_addresses_and_short_accounts(
    source, secret
):
    assert secret not in ai_assistant.sanitize_personal_data(source)


def test_known_account_is_redacted_even_with_different_separators():
    cleaned = ai_assistant.sanitize_personal_data(
        "По счёту 12 34 нужен перерасчёт",
        ["12-34"],
    )
    assert "12 34" not in cleaned


@pytest.mark.parametrize("source", ["меня зовут иван иванов", "иванов и.и."])
def test_lowercase_name_forms_are_redacted(source):
    cleaned = ai_assistant.sanitize_personal_data(source)
    assert "иванов" not in cleaned.lower()


@pytest.mark.parametrize(
    "source",
    [
        "1234",
        "ЛС 12 34, нет воды",
        "AB-12",
    ],
)
def test_ambiguous_personal_data_is_never_sent_to_provider(source):
    deps, _, _ = _deps()
    ai_assistant.ask(42, source, deps)
    deps.reserve_question.assert_not_called()
    deps.complete.assert_not_called()
    assert "переформулируйте" in deps.send_buttons.call_args.args[1].lower()


@pytest.mark.parametrize(
    "source",
    [
        "кучма леонид жалуется на отопление",
        "кучма леонид, нет отопления",
        "живу ленина 10, нет воды",
        "на ленина 10 нет воды",
        "по ленина 10 нет воды",
        "на садовой 10 нет воды",
        "по центральной 15 нет света",
        "на рабочей 7 нет отопления",
        "AB123",
    ],
)
def test_contextual_name_address_and_short_account_are_never_sent(source):
    deps, _, _ = _deps()
    ai_assistant.ask(42, source, deps)
    deps.complete.assert_not_called()
    deps.reserve_question.assert_not_called()


@pytest.mark.parametrize(
    ("source", "required_parts"),
    [
        ("Как получить перерасчёт за 2024 год?", ("2024", "перерасчёт")),
        ("из-за чего отключили воду?", ("из-за", "воду")),
        ("Почему в доме 25 нет воды?", ("Почему", "доме", "нет воды")),
        ("вывоз отходов", ("вывоз отходов",)),
        ("Когда будет ремонт домов?", ("ремонт домов",)),
        ("живу без горячей воды уже неделю", ("живу без горячей воды уже неделю",)),
        ("весь подъезд жалуется на холод", ("весь подъезд жалуется на холод",)),
        ("Почему начисляют по тарифу 10 рублей?", ("по тарифу 10",)),
        ("Расчёт по нормативу 15 кубов", ("по нормативу 15",)),
        ("На ремонт нужно 10 дней", ("На ремонт нужно 10",)),
    ],
)
def test_normal_utility_questions_keep_meaning_and_reach_provider(
    source, required_parts
):
    deps, _, _ = _deps()
    ai_assistant.ask(42, source, deps)
    sent = deps.complete.call_args.kwargs["question"]
    for part in required_parts:
        assert part in sent
    if source in {
        "вывоз отходов",
        "Когда будет ремонт домов?",
        "живу без горячей воды уже неделю",
        "весь подъезд жалуется на холод",
        "Почему начисляют по тарифу 10 рублей?",
        "Расчёт по нормативу 15 кубов",
        "На ремонт нужно 10 дней",
    }:
        assert sent == source


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


@pytest.mark.parametrize(
    "payload",
    [
        {"result": None},
        {"result": {"alternatives": [None]}},
        {"result": {"alternatives": [{"message": None}]}},
        {"result": {"alternatives": [{"message": {"text": None}}]}},
    ],
)
def test_yandex_client_rejects_malformed_provider_json(payload):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    client = ai_assistant.YandexGPTClient(
        api_key="secret",
        folder_id="folder",
        api_url="https://example.invalid",
        http_client=Mock(post=Mock(return_value=response)),
    )
    with pytest.raises(ai_assistant.AIServiceError):
        client.complete(
            settings={
                "model": "yandexgpt/latest",
                "system_prompt": "ЖКХ",
                "temperature": 0.2,
                "max_output_tokens": 100,
            },
            history=[],
            question="Вопрос",
            faq_context=None,
        )


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


def test_parallel_first_reservations_cannot_reset_daily_counter(ai_db):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: db.reserve_ai_question(
                    77, 1, session_date="2026-09-18"
                ),
                range(2),
            )
        )
    assert sorted(results) == [False, True]
    assert db.get_ai_session(77, session_date="2026-09-18")["question_count"] == 1


def test_parallel_history_appends_do_not_lose_an_exchange(ai_db):
    assert db.reserve_ai_question(88, 5, session_date="2026-09-18")
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                lambda index: db.append_ai_exchange(
                    88,
                    f"q{index}",
                    f"a{index}",
                    session_date="2026-09-18",
                ),
                range(2),
            )
        )
    history = db.get_ai_session(88, session_date="2026-09-18")["history"]
    assert len(history) == 4
    assert {item["text"] for item in history} == {"q0", "a0", "q1", "a1"}


def test_expired_history_is_removed_after_missed_midnight_cleanup(ai_db):
    assert db.reserve_ai_question(99, 5, session_date="2026-09-18")
    db.append_ai_exchange(99, "old question", "old answer", session_date="2026-09-18")
    assert db.cleanup_expired_ai_sessions(session_date="2026-09-20") == 1
    conn = db.get_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_daily_sessions WHERE chat_id=99"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_late_result_after_midnight_cannot_change_new_day_session(
    ai_db, monkeypatch
):
    assert db.reserve_ai_question(101, 5, session_date="2026-09-18")
    monkeypatch.setattr(db, "_server_date", lambda: "2026-09-19")
    assert db.reserve_ai_question(101, 5, session_date="2026-09-19")

    db.append_ai_exchange(
        101, "late old question", "late old answer", session_date="2026-09-18"
    )
    db.release_ai_question(101, session_date="2026-09-18")

    current = db.get_ai_session(101, session_date="2026-09-19")
    assert current["question_count"] == 1
    assert current["history"] == []


def test_late_reserve_cannot_roll_current_session_backwards(ai_db, monkeypatch):
    assert db.reserve_ai_question(103, 5, session_date="2026-09-18")
    monkeypatch.setattr(db, "_server_date", lambda: "2026-09-19")
    assert db.reserve_ai_question(103, 5, session_date="2026-09-19")

    assert not db.reserve_ai_question(103, 5, session_date="2026-09-18")

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT session_date, question_count, history_json "
            "FROM ai_daily_sessions WHERE chat_id=103"
        ).fetchone()
        assert tuple(row) == ("2026-09-19", 1, "[]")
    finally:
        conn.close()


def test_late_result_after_cleanup_does_not_recreate_expired_session(
    ai_db, monkeypatch
):
    assert db.reserve_ai_question(102, 5, session_date="2026-09-18")
    monkeypatch.setattr(db, "_server_date", lambda: "2026-09-19")
    assert db.cleanup_expired_ai_sessions(session_date="2026-09-19") == 1

    db.append_ai_exchange(
        102, "late old question", "late old answer", session_date="2026-09-18"
    )
    db.release_ai_question(102, session_date="2026-09-18")

    conn = db.get_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_daily_sessions WHERE chat_id=102"
        ).fetchone()[0] == 0
    finally:
        conn.close()


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
    failed.release_question.assert_called_once_with(43, session_date="2026-09-18")
    assert failed_state["ai_last_exchange"]["question"] == "Вопрос"
    assert "временно недоступен" in failed.send_buttons.call_args.args[1]


def test_empty_answer_after_sanitizing_is_safe_failure():
    failed, _, _ = _deps(complete=Mock(return_value="   "))
    ai_assistant.ask(43, "Как подать заявку?", failed)
    failed.release_question.assert_called_once_with(43, session_date="2026-09-18")
    failed.append_exchange.assert_not_called()


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


def test_question_operation_uses_one_fixed_server_date_for_all_storage_calls():
    deps, _, _ = _deps()
    ai_assistant.ask(42, "Как подать заявку?", deps)

    deps.get_operation_date.assert_called_once_with()
    deps.reserve_question.assert_called_once_with(42, 5, session_date="2026-09-18")
    deps.get_session.assert_called_once_with(
        42, session_date="2026-09-18", create_if_missing=False
    )
    deps.append_exchange.assert_called_once_with(
        42,
        "Как подать заявку?",
        "Оформите заявку",
        session_date="2026-09-18",
    )
