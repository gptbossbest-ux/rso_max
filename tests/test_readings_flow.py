from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

import bot
from rso_bot.flows import readings


def meter(*, two_tariff: bool = False) -> dict:
    return {
        "meter_number": "E-1",
        "resource_type": "Электроэнергия",
        "meter_type": "Двухтарифный" if two_tariff else "Однотарифный",
        "initial1": "10",
        "initial2": "20",
    }


def make_deps(**overrides) -> readings.ReadingDependencies:
    states: dict[int, dict] = {}

    def get_state(chat_id: int) -> dict:
        return states.setdefault(chat_id, {})

    defaults = {
        "get_meters": MagicMock(return_value=[]),
        "get_last_reading": MagicMock(return_value=None),
        "add_reading": MagicMock(),
        "get_state": get_state,
        "touch": MagicMock(side_effect=lambda state: state),
        "reset_meter_input": MagicMock(),
        "clear_flow": MagicMock(),
        "get_saved_ls": MagicMock(return_value=None),
        "request_ls": MagicMock(),
        "parse_input": MagicMock(),
        "get_current_value": MagicMock(return_value=0.0),
        "show_meter_select": MagicMock(),
        "ask_meter_value": MagicMock(),
        "confirm_meter_reading": MagicMock(),
        "make_callback": lambda label, payload: {"text": label, "payload": payload},
        "send_message": MagicMock(),
        "send_buttons": MagicMock(),
        "send_main_menu": MagicMock(),
        "logger": logging.getLogger("test.readings"),
        "integration_enabled": False,
        "meter_select_state": "meter_select",
        "waiting_value1_state": "waiting_value1",
        "waiting_value2_state": "waiting_value2",
        "confirm_state": "confirm",
    }
    defaults.update(overrides)
    return readings.ReadingDependencies(**defaults)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12.5", 12.5), ("12,5", 12.5), (" 12.5 ", 12.5), ("0", 0.0)],
)
def test_parse_reading_accepts_finite_non_negative_values(
    raw: str, expected: float
) -> None:
    deps = make_deps()
    assert readings.parse_reading(7, raw, deps) == expected
    deps.send_message.assert_not_called()


@pytest.mark.parametrize("raw", ["-1", "nan", "inf", "+inf", "-inf"])
def test_parse_reading_rejects_negative_and_non_finite_values(raw: str) -> None:
    deps = make_deps()
    assert readings.parse_reading(7, raw, deps) is None
    deps.send_message.assert_called_once_with(
        7, "Введите неотрицательное числовое значение."
    )


def test_parse_reading_reports_non_number() -> None:
    deps = make_deps()
    assert readings.parse_reading(7, "нет", deps) is None
    deps.send_message.assert_called_once_with(7, "Введите числовое значение.")


def test_current_reading_prefers_latest_and_falls_back_for_empty_value() -> None:
    latest = {"value1": "17.5"}
    deps = make_deps(get_last_reading=MagicMock(return_value=latest))
    assert readings.current_reading("LS", meter(), "value1", "initial1", deps) == 17.5

    deps.get_last_reading.return_value = {"value1": 0}
    assert readings.current_reading("LS", meter(), "value1", "initial1", deps) == 10.0


@pytest.mark.parametrize("latest", [0, None])
def test_current_reading_supports_sqlite_row_and_falls_back(latest) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("CREATE TABLE latest (value1 REAL)")
        connection.execute("CREATE TABLE meters (meter_number TEXT, initial1 REAL)")
        connection.execute("INSERT INTO latest VALUES (?)", (latest,))
        connection.execute("INSERT INTO meters VALUES ('E-1', 10)")
        row = connection.execute("SELECT value1 FROM latest").fetchone()
        meter_row = connection.execute("SELECT * FROM meters").fetchone()
        deps = make_deps(get_last_reading=MagicMock(return_value=row))
        assert (
            readings.current_reading("LS", meter_row, "value1", "initial1", deps)
            == 10.0
        )
    finally:
        connection.close()


def test_start_uses_saved_account_or_requests_it() -> None:
    missing = make_deps()
    readings.start(7, missing)
    missing.request_ls.assert_called_once_with(7, "pokazaniya")

    saved = replace(missing, get_saved_ls=MagicMock(return_value="LS-1"))
    readings.start(8, saved)
    saved.show_meter_select.assert_called_once_with(8, "LS-1")


def test_empty_meter_list_returns_to_menu() -> None:
    deps = make_deps()
    readings.show_meter_select(7, "LS", deps)
    deps.send_message.assert_called_once_with(7, "По вашему счёту счётчики не найдены.")
    deps.send_main_menu.assert_called_once_with(7)


def test_meter_list_preserves_payloads_and_two_tariff_label() -> None:
    deps = make_deps(
        get_meters=MagicMock(return_value=[meter(), meter(two_tariff=True)])
    )
    readings.show_meter_select(7, "LS", deps)
    state = deps.get_state(7)
    assert state["state"] == "meter_select"
    assert state["meters"] == [meter(), meter(two_tariff=True)]
    rows = deps.send_buttons.call_args.args[2]
    assert [row[0]["payload"] for row in rows] == ["meter:0", "meter:1", "main_menu"]
    assert rows[1][0]["text"].endswith(" (2Т)")


def test_single_meter_selection_uses_initial_value_in_prompt() -> None:
    deps = make_deps()
    deps.get_state(7).update(
        {"state": "waiting_value1", "ls": "LS", "meters": [meter()], "meter_idx": 0}
    )
    readings.ask_meter_value(7, deps)
    deps.send_message.assert_called_once_with(
        7, "Электроэнергия №E-1\nНачальное: 10\nВведите показание:"
    )


def test_two_tariff_t2_prompt_shows_both_latest_values() -> None:
    last = {"value1": "15", "value2": "25", "created_at": "2026-09-18 10:00:00"}
    deps = make_deps(get_last_reading=MagicMock(return_value=last))
    deps.get_state(7).update(
        {
            "state": "waiting_value2",
            "ls": "LS",
            "meters": [meter(two_tariff=True)],
            "meter_idx": 0,
        }
    )
    readings.ask_meter_value(7, deps)
    deps.send_message.assert_called_once_with(
        7,
        "Электроэнергия №E-1\nТекущие: Т1=15, Т2=25 (от 2026-09-18)\n"
        "Введите Т2 (ночь):",
    )


@pytest.mark.parametrize("argument", ["x", "1", "-1"])
def test_invalid_meter_selection_does_not_mutate_state(argument) -> None:
    deps = make_deps(
        reset_meter_input=MagicMock(
            side_effect=lambda state: state.update(state="waiting_value1")
        )
    )
    state = {"meters": [meter()]}
    before = state.copy()
    with pytest.raises(ValueError):
        readings.select_meter(7, state, argument, deps)
    assert state == before
    deps.reset_meter_input.assert_not_called()
    deps.ask_meter_value.assert_not_called()


@pytest.mark.parametrize("argument", ["x", "1", "-1"])
def test_invalid_meter_callback_returns_to_menu_without_invalid_state(argument) -> None:
    chat_id = 777
    bot.user_states[chat_id] = {"state": bot.S.METER_SELECT, "meters": [meter()]}
    update = {
        "callback": {
            "callback_id": "callback-invalid-meter",
            "payload": f"meter:{argument}",
        },
        "message": {"recipient": {"chat_id": chat_id}},
    }
    with (
        patch.object(bot, "_ack_callback"),
        patch.object(bot, "send_main_menu") as menu,
    ):
        bot.handle_callback(update)
    menu.assert_called_once_with(chat_id)
    assert "meter_idx" not in bot.user_states[chat_id]
    assert bot.user_states[chat_id]["state"] == bot.S.METER_SELECT


def test_single_tariff_value_advances_to_confirmation() -> None:
    deps = make_deps(
        parse_input=MagicMock(return_value=15.0),
        get_current_value=MagicMock(return_value=10.0),
    )
    state = {"ls": "LS", "meters": [meter()], "meter_idx": 0}
    readings.on_value1(7, state, "15", deps)
    assert state["new_value1"] == "15.0"
    assert state["new_value2"] is None
    deps.confirm_meter_reading.assert_called_once_with(7)


def test_two_tariff_t1_and_t2_transitions() -> None:
    deps = make_deps(
        parse_input=MagicMock(side_effect=[15.0, 25.0]),
        get_current_value=MagicMock(side_effect=[10.0, 20.0]),
    )
    state = {
        "ls": "LS",
        "state": "waiting_value1",
        "meters": [meter(two_tariff=True)],
        "meter_idx": 0,
    }
    readings.on_value1(7, state, "15", deps)
    assert state["state"] == "waiting_value2"
    deps.ask_meter_value.assert_called_once_with(7)

    readings.on_value2(7, state, "25", deps)
    assert state["new_value2"] == "25.0"
    deps.confirm_meter_reading.assert_called_once_with(7)


def test_confirmation_preserves_exact_text_and_payloads() -> None:
    deps = make_deps()
    deps.get_state(7).update(
        {
            "meters": [meter(two_tariff=True)],
            "meter_idx": 0,
            "new_value1": "15.0",
            "new_value2": "25.0",
        }
    )
    readings.confirm_meter_reading(7, deps)
    text, rows = deps.send_buttons.call_args.args[1:]
    assert text == "Проверьте показания:\nЭлектроэнергия №E-1: Т1=15.0, Т2=25.0"
    assert [row[0]["payload"] for row in rows] == ["meter_confirm", "meter_retry"]


@pytest.mark.parametrize(
    ("handler", "label"),
    [(readings.on_value1, "Показание 5.0"), (readings.on_value2, "Показание Т2 5.0")],
)
def test_lower_value_is_rejected_and_can_be_retried(handler, label: str) -> None:
    deps = make_deps(
        parse_input=MagicMock(return_value=5.0),
        get_current_value=MagicMock(return_value=10.0),
    )
    state = {"ls": "LS", "meters": [meter(two_tariff=True)], "meter_idx": 0}
    handler(7, state, "5", deps)
    assert label in deps.send_message.call_args.args[1]
    assert "new_value1" not in state and "new_value2" not in state


def test_integration_mode_accepts_lower_value() -> None:
    deps = make_deps(
        integration_enabled=True,
        parse_input=MagicMock(return_value=5.0),
        get_current_value=MagicMock(return_value=10.0),
    )
    state = {"ls": "LS", "meters": [meter()], "meter_idx": 0}
    readings.on_value1(7, state, "5", deps)
    assert state["new_value1"] == "5.0"


@pytest.mark.parametrize("invalid", [-1.0, float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("handler", [readings.on_value1, readings.on_value2])
@pytest.mark.parametrize("integration_enabled", [False, True])
def test_invalid_values_never_mutate_or_confirm(
    invalid, handler, integration_enabled: bool
) -> None:
    deps = make_deps(
        integration_enabled=integration_enabled,
        parse_input=MagicMock(return_value=invalid),
    )
    state = {
        "ls": "LS",
        "state": "waiting_value1",
        "meters": [meter(two_tariff=True)],
        "meter_idx": 0,
    }
    before = state.copy()
    handler(7, state, str(invalid), deps)
    assert state == before
    deps.touch.assert_not_called()
    deps.confirm_meter_reading.assert_not_called()
    deps.add_reading.assert_not_called()


def test_confirm_persists_exact_arguments_and_reopens_meter_list() -> None:
    deps = make_deps()
    state = {
        "state": "confirm",
        "ls": "LS",
        "meters": [meter(two_tariff=True)],
        "meter_idx": 0,
        "new_value1": "15.0",
        "new_value2": "25.0",
    }
    readings.confirm(7, state, deps)
    deps.add_reading.assert_called_once_with(
        7, "LS", "Электроэнергия", "E-1", "15.0", "25.0"
    )
    deps.clear_flow.assert_called_once_with(state)
    deps.show_meter_select.assert_called_once_with(7, "LS")
    assert "приняты!" in deps.send_message.call_args.args[1]


def test_confirm_database_error_preserves_state_and_allows_retry() -> None:
    add_reading = MagicMock(side_effect=[RuntimeError("database unavailable"), None])
    deps = make_deps(add_reading=add_reading)
    state = {
        "state": "confirm",
        "ls": "LS",
        "meters": [meter()],
        "meter_idx": 0,
        "new_value1": "15.0",
        "new_value2": None,
    }
    before = state.copy()
    readings.confirm(7, state, deps)
    assert state == before
    deps.clear_flow.assert_not_called()
    deps.show_meter_select.assert_not_called()
    assert "Попробуйте подтвердить" in deps.send_message.call_args.args[1]

    deps.send_message.reset_mock()
    readings.confirm(7, state, deps)
    assert add_reading.call_count == 2
    deps.clear_flow.assert_called_once_with(state)
    deps.show_meter_select.assert_called_once_with(7, "LS")
    assert "приняты!" in deps.send_message.call_args.args[1]


def test_retry_only_resets_confirmation_state() -> None:
    deps = make_deps()
    readings.retry(7, {"state": "waiting_value1"}, deps)
    deps.reset_meter_input.assert_not_called()

    state = {"state": "confirm"}
    readings.retry(7, state, deps)
    deps.reset_meter_input.assert_called_once_with(state)
    deps.ask_meter_value.assert_called_once_with(7)


def test_bot_wrappers_resolve_runtime_patch_points() -> None:
    state = {"ls": "LS", "meters": [meter()], "meter_idx": 0}
    with (
        patch.object(bot, "_parse_reading", return_value=15.0) as parse,
        patch.object(bot, "_current_reading", return_value=10.0) as current,
        patch.object(bot, "_confirm_meter_reading") as confirm,
    ):
        bot._on_value1(7, state, "15")
    parse.assert_called_once_with(7, "15")
    current.assert_called_once_with("LS", state["meters"][0], "value1", "initial1")
    confirm.assert_called_once_with(7)


def test_bot_start_wrapper_resolves_saved_ls_and_request_hooks() -> None:
    with (
        patch.object(bot, "_get_saved_ls", return_value=None),
        patch.object(bot, "_request_ls") as request,
    ):
        bot._start_pokazaniya(7)
    request.assert_called_once_with(7, "pokazaniya")


def test_1c_auth_continues_deferred_readings_flow() -> None:
    chat_id = 778
    state = bot._get_state(chat_id)
    state.update(
        {
            "state": bot.S.AWAIT_CODE_1C,
            "pending_1c_ls": "100001",
            "after_1c_auth": "pokazaniya",
        }
    )
    result = {"status": "ok", "message": "Авторизация выполнена"}
    with (
        patch.object(
            bot.client_api, "verify_1c_auth_code", return_value=(result, None)
        ),
        patch.object(bot, "_save_ls"),
        patch.object(bot, "send_message"),
        patch.object(bot, "_show_meter_select") as show_meter_select,
    ):
        bot._on_await_code_1c(chat_id, state, "123456")
    show_meter_select.assert_called_once_with(chat_id, "100001")
    assert state["state"] == bot.S.MENU
    assert "after_1c_auth" not in state
    assert "pending_1c_ls" not in state
