from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import Mock

import pytest

import database as db
from rso_bot.flows import house_chats


def _message(
    text: Any = "В доме ТЕЧЬ",
    *,
    chat_id: Any = 100,
    sender_id: Any = 200,
) -> dict[str, Any]:
    return {
        "recipient": {"chat_id": chat_id, "chat_type": "chat"},
        "sender": {"user_id": sender_id},
        "body": {"text": text},
    }


def _scenario(**overrides: Any) -> dict[str, Any]:
    result = {
        "id": 1,
        "title": "Протечка",
        "keywords": '["течь", "вода"]',
        "response_text": "Принято",
        "suggest_appeal": 0,
    }
    result.update(overrides)
    return result


def _deps(
    *,
    house_chat: Any = None,
    excluded: bool = False,
    scenarios: list[Any] | None = None,
    send_message: Callable[[int, str], Any] | None = None,
    logger: logging.Logger | None = None,
    json_loads: Callable[[str], Any] | None = None,
) -> house_chats.HouseChatDependencies:
    if house_chat is None:
        house_chat = {"id": 7}
    values = [_scenario()] if scenarios is None else scenarios
    return house_chats.HouseChatDependencies(
        get_house_chat_by_chat_id=Mock(return_value=house_chat),
        is_user_excluded=Mock(return_value=excluded),
        get_scenarios_for_chat=Mock(return_value=values),
        send_message=send_message or Mock(return_value=True),
        logger=logger or Mock(spec=logging.Logger),
        **({"json_loads": json_loads} if json_loads else {}),
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ({"recipient": {"chat_type": "dialog"}}, "dialog"),
        ({"recipient": {"chat_type": "chat"}}, "chat"),
        ({"recipient": {"chat_type": "channel"}}, "channel"),
        ({"recipient": {}, "chat_type": "chat"}, "chat"),
        (
            {"recipient": {"chat_type": "channel"}, "chat_type": "chat"},
            "channel",
        ),
    ],
)
def test_get_chat_type_supports_payload_variants(message, expected):
    logger = Mock(spec=logging.Logger)

    state = house_chats.UnknownChatTypeWarningState()
    assert house_chats.get_chat_type(message, logger, state) == expected

    logger.warning.assert_not_called()


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"recipient": None},
        {"recipient": "not-a-mapping"},
        {"recipient": {"chat_id": []}},
        {"recipient": {"chat_type": 42}},
        None,
    ],
)
def test_get_chat_type_defaults_malformed_payload_to_dialog(message):
    assert (
        house_chats.get_chat_type(
            message,
            Mock(spec=logging.Logger),
            house_chats.UnknownChatTypeWarningState(),
        )
        == "dialog"
    )


@pytest.mark.parametrize(
    "chat_ids",
    [
        (["TOP-SECRET"], {"TOP-SECRET": True}),
        (b"TOP-SECRET", "TOP-SECRET" * 10_000),
    ],
)
def test_get_chat_type_warns_once_without_logging_chat_id_or_recipient(chat_ids):
    logger = Mock(spec=logging.Logger)
    state = house_chats.UnknownChatTypeWarningState()

    for chat_id in chat_ids:
        message = {
            "recipient": {
                "chat_id": chat_id,
                "access_token": "TOP-SECRET",
                "display_name": "PERSONAL NAME",
            }
        }
        assert house_chats.get_chat_type(message, logger, state) == "dialog"

    logger.warning.assert_called_once()
    rendered = " ".join(str(value) for value in logger.warning.call_args.args)
    assert "TOP-SECRET" not in rendered
    assert "PERSONAL NAME" not in rendered
    assert "access_token" not in rendered


def test_unknown_chat_type_warning_state_is_thread_safe_and_bounded():
    state = house_chats.UnknownChatTypeWarningState()

    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(lambda _: state.claim(), range(100)))

    assert claims.count(True) == 1
    assert claims.count(False) == 99


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"recipient": None},
        {"recipient": "bad"},
        {"recipient": {}},
    ],
)
def test_group_handler_ignores_messages_without_chat_id(message):
    deps = _deps()

    house_chats.handle_group_message(message, deps)

    deps.get_house_chat_by_chat_id.assert_not_called()
    deps.send_message.assert_not_called()


def test_group_handler_ignores_unregistered_chat_before_other_lookups():
    deps = _deps(house_chat=False)

    house_chats.handle_group_message(_message(), deps)

    deps.get_house_chat_by_chat_id.assert_called_once_with("100")
    deps.is_user_excluded.assert_not_called()
    deps.get_scenarios_for_chat.assert_not_called()
    deps.send_message.assert_not_called()


def test_group_handler_ignores_excluded_sender():
    deps = _deps(excluded=True)

    house_chats.handle_group_message(_message(sender_id="u-1"), deps)

    deps.is_user_excluded.assert_called_once_with(7, "max", "u-1")
    deps.get_scenarios_for_chat.assert_not_called()
    deps.send_message.assert_not_called()


@pytest.mark.parametrize(
    "message",
    [
        _message(""),
        _message(None),
        _message(123),
        {**_message(), "body": None},
        {**_message(), "body": "not-a-mapping"},
    ],
)
def test_group_handler_ignores_empty_or_malformed_body(message):
    deps = _deps()

    house_chats.handle_group_message(message, deps)

    deps.get_scenarios_for_chat.assert_not_called()
    deps.send_message.assert_not_called()


def test_group_handler_rejects_malformed_sender_before_scenarios():
    deps = _deps()

    house_chats.handle_group_message({**_message(), "sender": "not-a-mapping"}, deps)

    deps.is_user_excluded.assert_not_called()
    deps.get_scenarios_for_chat.assert_not_called()
    deps.send_message.assert_not_called()


@pytest.mark.parametrize("sender", [None, pytest.param("absent", id="absent")])
def test_group_handler_preserves_missing_sender_legacy_behavior(sender):
    message = _message()
    if sender == "absent":
        message.pop("sender")
    else:
        message["sender"] = None
    deps = _deps()

    house_chats.handle_group_message(message, deps)

    deps.is_user_excluded.assert_not_called()
    deps.send_message.assert_called_once_with(100, "Принято")


def test_group_handler_preserves_case_insensitive_substring_matching():
    deps = _deps(scenarios=[_scenario(keywords='["течь"]')])

    # Legacy matching is substring based, not word-boundary based.
    house_chats.handle_group_message(_message("Не дать воде подтечь"), deps)

    deps.send_message.assert_called_once_with(100, "Принято")


def test_group_handler_uses_first_matching_scenario_deterministically():
    scenarios = [
        _scenario(id=10, response_text="Первый"),
        _scenario(id=20, response_text="Второй"),
    ]
    deps = _deps(scenarios=scenarios)

    house_chats.handle_group_message(_message(), deps)

    deps.send_message.assert_called_once_with(100, "Первый")


@pytest.fixture
def house_chat_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "house-chat.sqlite"))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()
    return db


def test_database_orders_active_scenarios_by_id_and_flow_uses_first(house_chat_db):
    house_chat_id = house_chat_db.add_house_chat("Дом 1", "max", "100")
    first = house_chat_db.create_scenario("Первый", ["течь"], "Ответ 1", False)
    disabled = house_chat_db.create_scenario(
        "Выключен", ["течь"], "Не отправлять", False
    )
    third = house_chat_db.create_scenario("Третий", ["течь"], "Ответ 3", False)
    for scenario_id in (third, disabled, first):
        house_chat_db.link_scenario_to_chat(scenario_id, house_chat_id)
    house_chat_db.update_scenario(
        disabled,
        "Выключен",
        ["течь"],
        "Не отправлять",
        False,
        False,
    )

    rows = house_chat_db.get_scenarios_for_chat(house_chat_id)
    assert [row["id"] for row in rows] == [first, third]

    sender = Mock(return_value=True)
    deps = house_chats.HouseChatDependencies(
        get_house_chat_by_chat_id=house_chat_db.get_house_chat_by_chat_id,
        is_user_excluded=house_chat_db.is_user_excluded,
        get_scenarios_for_chat=house_chat_db.get_scenarios_for_chat,
        send_message=sender,
        logger=Mock(spec=logging.Logger),
    )
    house_chats.handle_group_message(_message(), deps)
    sender.assert_called_once_with(100, "Ответ 1")


def test_group_handler_sends_appeal_suggestion_after_response():
    sender = Mock(return_value=True)
    deps = _deps(
        scenarios=[_scenario(suggest_appeal=1)],
        send_message=sender,
    )

    house_chats.handle_group_message(_message(), deps)

    assert [item.args for item in sender.call_args_list] == [
        (100, "Принято"),
        (100, house_chats.APPEAL_SUGGESTION),
    ]


def test_group_handler_stops_without_success_when_response_delivery_is_false():
    sender = Mock(return_value=False)
    deps = _deps(
        scenarios=[_scenario(suggest_appeal=1)],
        send_message=sender,
    )

    house_chats.handle_group_message(_message(), deps)

    sender.assert_called_once_with(100, "Принято")
    deps.logger.info.assert_not_called()
    deps.logger.warning.assert_called_once()


def test_group_handler_does_not_log_success_when_suggestion_delivery_is_false():
    sender = Mock(side_effect=[True, False])
    deps = _deps(
        scenarios=[_scenario(suggest_appeal=1)],
        send_message=sender,
    )

    house_chats.handle_group_message(_message(), deps)

    assert [item.args for item in sender.call_args_list] == [
        (100, "Принято"),
        (100, house_chats.APPEAL_SUGGESTION),
    ]
    deps.logger.info.assert_not_called()
    deps.logger.warning.assert_called_once()


def test_group_handler_is_silent_for_empty_or_nonmatching_scenarios():
    deps = _deps(scenarios=[])
    house_chats.handle_group_message(_message(), deps)
    deps.send_message.assert_not_called()

    deps = _deps(scenarios=[_scenario(keywords='["электричество"]')])
    house_chats.handle_group_message(_message(), deps)
    deps.send_message.assert_not_called()


@pytest.mark.parametrize(
    "bad_scenario",
    [
        _scenario(keywords="not-json"),
        _scenario(keywords="null"),
        _scenario(keywords='{"течь": true}'),
        _scenario(keywords='["течь", 7]'),
        _scenario(keywords=None),
        {},
    ],
)
def test_group_handler_skips_malformed_keywords_and_continues(bad_scenario):
    deps = _deps(scenarios=[bad_scenario, _scenario(id=2, response_text="Резервный")])

    house_chats.handle_group_message(_message(), deps)

    deps.send_message.assert_called_once_with(100, "Резервный")
    deps.logger.warning.assert_called()


@pytest.mark.parametrize("response", [None, "", 42])
def test_group_handler_skips_malformed_response_and_continues(response):
    deps = _deps(
        scenarios=[
            _scenario(response_text=response),
            _scenario(id=2, response_text="Резервный"),
        ]
    )

    house_chats.handle_group_message(_message(), deps)

    deps.send_message.assert_called_once_with(100, "Резервный")


def test_group_handler_uses_injected_json_loader():
    loader = Mock(return_value=["течь"])
    deps = _deps(json_loads=loader)

    house_chats.handle_group_message(_message(), deps)

    loader.assert_called_once_with('["течь", "вода"]')
    deps.send_message.assert_called_once_with(100, "Принято")


@pytest.mark.parametrize(
    "dependency_name",
    [
        "get_house_chat_by_chat_id",
        "is_user_excluded",
        "get_scenarios_for_chat",
    ],
)
def test_group_handler_propagates_database_errors(dependency_name):
    deps = _deps()
    object.__setattr__(
        deps,
        dependency_name,
        Mock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        house_chats.handle_group_message(_message(), deps)

    deps.send_message.assert_not_called()


def test_group_handler_propagates_send_error_without_sending_suggestion():
    sender = Mock(side_effect=RuntimeError("MAX unavailable"))
    deps = _deps(
        scenarios=[_scenario(suggest_appeal=1)],
        send_message=sender,
    )

    with pytest.raises(RuntimeError, match="MAX unavailable"):
        house_chats.handle_group_message(_message(), deps)

    assert sender.call_count == 1


def test_group_handler_never_logs_raw_message_or_response():
    logger = Mock(spec=logging.Logger)
    secret_text = "SECRET USER MESSAGE"
    secret_response = "SECRET RESPONSE"
    secret_title = "SECRET TITLE"
    deps = _deps(
        scenarios=[
            _scenario(
                title=secret_title,
                keywords='["secret"]',
                response_text=secret_response,
            )
        ],
        logger=logger,
    )

    house_chats.handle_group_message(_message(secret_text), deps)

    logged = " ".join(
        str(value)
        for call in logger.method_calls
        for value in (call.args if hasattr(call, "args") else ())
    )
    assert secret_text not in logged
    assert secret_response not in logged
    assert secret_title not in logged


def test_bot_wrappers_resolve_runtime_dependencies(monkeypatch):
    import bot

    get_chat = Mock(return_value={"id": 99})
    excluded = Mock(return_value=False)
    get_scenarios = Mock(return_value=[])
    sender = Mock(return_value=True)
    delegated = Mock()
    monkeypatch.setattr(bot.db, "get_house_chat_by_chat_id", get_chat)
    monkeypatch.setattr(bot.db, "is_user_excluded", excluded)
    monkeypatch.setattr(bot.db, "get_scenarios_for_chat", get_scenarios)
    monkeypatch.setattr(bot, "send_message", sender)
    monkeypatch.setattr(bot.house_chats, "handle_group_message", delegated)

    message = _message()
    bot._handle_group_message(message)

    delegated.assert_called_once()
    passed_message, deps = delegated.call_args.args
    assert passed_message is message
    assert deps.get_house_chat_by_chat_id is get_chat
    assert deps.is_user_excluded is excluded
    assert deps.get_scenarios_for_chat is get_scenarios
    assert deps.send_message is sender


def test_bot_chat_type_wrapper_preserves_compatibility_registry(monkeypatch):
    import bot

    delegated = Mock(return_value="channel")
    monkeypatch.setattr(bot.house_chats, "get_chat_type", delegated)
    message = {"recipient": {"chat_type": "channel"}}

    assert bot._get_chat_type(message) == "channel"
    delegated.assert_called_once_with(message, bot.log, bot._UNKNOWN_CHAT_TYPE_WARNED)
