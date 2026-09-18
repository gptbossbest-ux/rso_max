from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

import bot
from rso_bot.session import SessionManager
from rso_bot.states import FLOW_KEYS, METER_INPUT_KEYS, S

STATE_VALUES = {
    "MENU": "menu",
    "APPEAL_CATEGORY": "appeal_category",
    "APPEAL_BODY": "appeal_body",
    "AWAIT_LS": "await_ls",
    "AWAIT_LS_1C": "await_ls_1c",
    "AWAIT_CODE_1C": "await_code_1c",
    "REOPEN_COMMENT": "reopen_comment",
    "SCRIPT_LIST": "script_list",
    "SCRIPT_NODE": "script_node",
    "METER_SELECT": "meter_select",
    "WAITING_VALUE1": "waiting_value1",
    "WAITING_VALUE2": "waiting_value2",
    "CONFIRM_POKAZANIYA": "confirm_pokazaniya",
    "APPOINTMENT_BRANCH": "appointment_branch",
    "APPOINTMENT_DATE": "appointment_date",
    "APPOINTMENT_TIME": "appointment_time",
    "APPOINTMENT_THEME": "appointment_theme",
    "APPOINTMENT_CONFIRM": "appointment_confirm",
}


@pytest.fixture(autouse=True)
def clear_bot_sessions():
    bot.user_states.clear()
    yield
    bot.user_states.clear()


def test_all_state_values_are_stable_unique_and_reexported():
    actual = {name: getattr(S, name) for name in STATE_VALUES}

    assert actual == STATE_VALUES
    assert len(set(actual.values())) == len(actual)
    assert bot.S is S


def test_get_state_creates_touches_and_reuses_session(monkeypatch):
    now = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(bot, "_now", lambda: now)

    created = bot._get_state(42)
    reused = bot._get_state(42)

    assert created is reused
    assert created == {"state": S.MENU, "last_active": now}
    assert bot.user_states[42] is created
    assert bot._session_manager.states is bot.user_states


def test_touch_uses_runtime_patched_bot_clock(monkeypatch):
    first = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    second = first + timedelta(minutes=1)
    state = {}

    monkeypatch.setattr(bot, "_now", lambda: first)
    assert bot._touch(state) is state
    assert state["last_active"] == first

    monkeypatch.setattr(bot, "_now", lambda: second)
    bot._touch(state)
    assert state["last_active"] == second


def test_cleanup_uses_strict_ttl_boundary_and_preserves_missing_or_non_dict(
    monkeypatch,
):
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(bot, "_now", lambda: now)
    states = {
        1: {"last_active": now - timedelta(minutes=31)},
        2: {"last_active": now - timedelta(minutes=30)},
        3: {"last_active": now - timedelta(minutes=29)},
        4: {"state": S.MENU},
        5: "invalid",
    }

    removed = bot.cleanup_user_states(states, 30)

    assert removed == 1
    assert set(states) == {2, 3, 4, 5}


def test_cleanup_preserves_existing_malformed_timestamp_failure(monkeypatch):
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(bot, "_now", lambda: now)

    with pytest.raises(TypeError):
        bot.cleanup_user_states({1: {"last_active": "not-a-datetime"}}, 30)


def test_clear_flow_removes_exact_flow_keys_and_preserves_session_data():
    state = {key: f"value-{index}" for index, key in enumerate(FLOW_KEYS)}
    state.update(
        {
            "state": S.APPOINTMENT_CONFIRM,
            "ls": "100001",
            "fio": "Иванов Иван",
            "authorized_1c": True,
            "last_active": "untouched",
            "custom": 7,
        }
    )

    bot._clear_flow(state)

    assert all(key not in state for key in FLOW_KEYS)
    assert state == {
        "state": S.MENU,
        "ls": "100001",
        "fio": "Иванов Иван",
        "authorized_1c": True,
        "last_active": "untouched",
        "custom": 7,
    }
    assert bot._FLOW_KEYS is FLOW_KEYS


def test_reset_meter_input_removes_only_values_and_waits_for_first_tariff():
    state = {
        "state": S.WAITING_VALUE2,
        "new_value1": 123.4,
        "new_value2": 234.5,
        "meter_idx": 1,
        "meters": ["meter"],
    }

    bot._reset_meter_input(state)

    assert state == {
        "state": S.WAITING_VALUE1,
        "meter_idx": 1,
        "meters": ["meter"],
    }
    assert bot._METER_INPUT_KEYS is METER_INPUT_KEYS


def test_cleanup_scheduler_wrapper_keeps_runtime_callback_seam(monkeypatch):
    cleanup = Mock(return_value=0)
    monkeypatch.setattr(bot, "cleanup_user_states", cleanup)

    bot._task_cleanup_user_states()

    cleanup.assert_called_once_with(bot.user_states, bot.SESSION_TTL_MINUTES)


def test_session_manager_get_touch_cleanup_are_safe_under_concurrent_calls():
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    states: dict = {}
    manager = SessionManager(states, clock=lambda: now)

    def exercise(index: int) -> None:
        state = manager.get_state(index % 10)
        manager.touch(state)
        manager.cleanup(ttl_minutes=30)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(exercise, range(500)))

    assert set(states) == set(range(10))
    assert all(state["last_active"] == now for state in states.values())
