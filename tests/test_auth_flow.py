from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call

import pytest

import bot
import database as db
from rso_bot.flows import auth
from rso_bot.states import S

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _account_deps(**overrides: object) -> auth.AccountDependencies:
    values = {
        "get_state": MagicMock(return_value={}),
        "get_bot_user": MagicMock(return_value=None),
        "upsert_bot_user": MagicMock(),
        "get_ls": MagicMock(return_value=None),
        "integration_enabled": False,
        "logger": logging.getLogger("test.auth.account"),
    }
    values.update(overrides)
    return auth.AccountDependencies(**values)  # type: ignore[arg-type]


def _brute_deps(**overrides: object) -> auth.BruteForceDependencies:
    values = {
        "attempts": {},
        "now": lambda: NOW,
        "max_attempts": 3,
        "block_minutes": 30,
    }
    values.update(overrides)
    return auth.BruteForceDependencies(**values)  # type: ignore[arg-type]


def _flow_deps(**overrides: object) -> auth.AuthFlowDependencies:
    def clear_flow(state: dict) -> None:
        state.clear()
        state["state"] = S.MENU

    values = {
        "get_state": MagicMock(return_value={"state": S.MENU}),
        "touch": MagicMock(side_effect=lambda state: state),
        "clear_flow": MagicMock(side_effect=clear_flow),
        "send_message": MagicMock(),
        "send_main_menu": MagicMock(),
        "validate_ls": MagicMock(return_value=True),
        "check_ls_brute": MagicMock(return_value=None),
        "fail_ls": MagicMock(return_value="bad account"),
        "reset_ls_brute": MagicMock(),
        "save_ls": MagicMock(),
        "start_1c_auth": MagicMock(),
        "request_1c_auth_code": MagicMock(
            return_value=({"status": "ok", "message": "Код отправлен"}, None)
        ),
        "verify_1c_auth_code": MagicMock(
            return_value=({"status": "ok", "message": "Успешно"}, None)
        ),
        "continuations": {},
        "integration_enabled": False,
        "logger": logging.getLogger("test.auth.flow"),
    }
    values.update(overrides)
    return auth.AuthFlowDependencies(**values)  # type: ignore[arg-type]


def test_get_saved_ls_uses_session_and_does_not_hit_database() -> None:
    get_user = MagicMock()
    deps = _account_deps(
        get_state=MagicMock(return_value={"ls": "100001"}),
        get_bot_user=get_user,
    )

    assert auth.get_saved_ls(42, deps) == "100001"
    get_user.assert_not_called()


def test_get_saved_ls_hydrates_fio_from_real_sqlite_row() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT '100001' AS ls, 'Иванов И.И.' AS fio, 1 AS authorized_1c"
    ).fetchone()
    state: dict = {}
    deps = _account_deps(
        get_state=MagicMock(return_value=state),
        get_bot_user=MagicMock(return_value=row),
        integration_enabled=True,
    )

    assert auth.get_saved_ls(42, deps) == "100001"
    assert state == {
        "ls": "100001",
        "fio": "Иванов И.И.",
        "authorized_1c": True,
    }


def test_get_saved_ls_rejects_non_authorized_persisted_1c_account() -> None:
    state: dict = {}
    deps = _account_deps(
        get_state=MagicMock(return_value=state),
        get_bot_user=MagicMock(
            return_value={"ls": "100001", "fio": "Иванов", "authorized_1c": 0}
        ),
        integration_enabled=True,
    )

    assert auth.get_saved_ls(42, deps) is None
    assert state == {}


def test_get_saved_ls_database_failure_does_not_log_account_or_fio(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.auth.saved.database")
    deps = _account_deps(
        get_bot_user=MagicMock(side_effect=sqlite3.Error("SECRET-LS Иванов Секретный")),
        logger=logger,
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        assert auth.get_saved_ls(42, deps) is None

    assert "SECRET-LS" not in caplog.text
    assert "Иванов Секретный" not in caplog.text


def test_save_ls_binds_first_account_without_requesting_fio_clear() -> None:
    state: dict = {}
    upsert = MagicMock()
    deps = _account_deps(
        get_state=MagicMock(return_value=state),
        upsert_bot_user=upsert,
        integration_enabled=True,
    )

    auth.save_ls(42, "100001", None, deps)

    assert state == {"ls": "100001", "authorized_1c": True}
    upsert.assert_called_once_with(
        42,
        "100001",
        "",
        authorized_1c=True,
        clear_fio=False,
    )


def test_save_ls_clears_stale_fio_for_null_to_account_transition(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "auth.sqlite"))
    db.init_db()
    db.upsert_bot_user(42, None, "Устаревшее ФИО", authorized_1c=True)
    state = {"fio": "Устаревшее ФИО", "authorized_1c": True}
    deps = _account_deps(
        get_state=MagicMock(return_value=state),
        get_bot_user=db.get_bot_user,
        upsert_bot_user=db.upsert_bot_user,
        integration_enabled=True,
    )

    auth.save_ls(42, "new-account", None, deps)

    assert state == {"ls": "new-account", "authorized_1c": True}
    row = db.get_bot_user(42)
    assert row is not None
    assert row["ls"] == "new-account"
    assert row["fio"] is None


def test_save_ls_explicit_fio_wins_during_account_transition(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "auth.sqlite"))
    db.init_db()
    db.upsert_bot_user(42, "old-account", "Старое ФИО", authorized_1c=True)
    state = {"ls": "old-account", "fio": "Старое ФИО"}
    deps = _account_deps(
        get_state=MagicMock(return_value=state),
        get_bot_user=db.get_bot_user,
        upsert_bot_user=db.upsert_bot_user,
        integration_enabled=True,
    )

    auth.save_ls(42, "new-account", "Новое ФИО", deps)

    assert state == {
        "ls": "new-account",
        "fio": "Новое ФИО",
        "authorized_1c": True,
    }
    row = db.get_bot_user(42)
    assert row is not None
    assert row["ls"] == "new-account"
    assert row["fio"] == "Новое ФИО"


def test_save_ls_clears_fio_on_account_change_in_session_and_database(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "auth.sqlite"))
    db.init_db()
    db.upsert_bot_user(42, "old-account", "Старое ФИО", authorized_1c=True)
    state = {
        "ls": "old-account",
        "fio": "Старое ФИО",
        "authorized_1c": True,
    }
    deps = _account_deps(
        get_state=MagicMock(return_value=state),
        get_bot_user=db.get_bot_user,
        upsert_bot_user=db.upsert_bot_user,
        integration_enabled=True,
    )

    auth.save_ls(42, "new-account", None, deps)

    assert state == {"ls": "new-account", "authorized_1c": True}
    assert db.get_bot_user(42)["fio"] is None
    assert db.get_all_bot_users()[0]["fio"] is None


def test_save_ls_preserves_fio_for_same_account_in_real_database(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "auth.sqlite"))
    db.init_db()
    db.upsert_bot_user(42, "same-account", "Сохранённое ФИО", authorized_1c=True)
    state: dict = {}
    deps = _account_deps(
        get_state=MagicMock(return_value=state),
        get_bot_user=db.get_bot_user,
        upsert_bot_user=db.upsert_bot_user,
        integration_enabled=True,
    )

    auth.save_ls(42, "same-account", None, deps)

    assert db.get_bot_user(42)["fio"] == "Сохранённое ФИО"


def test_save_ls_rolls_back_session_when_persistence_fails() -> None:
    state = {
        "ls": "old-account",
        "fio": "Иванов Иван",
        "authorized_1c": False,
    }
    deps = _account_deps(
        get_state=MagicMock(return_value=state),
        upsert_bot_user=MagicMock(side_effect=sqlite3.Error("disk failed")),
        integration_enabled=True,
    )

    with pytest.raises(sqlite3.Error, match="disk failed"):
        auth.save_ls(42, "new-account", "Новое имя", deps)

    assert state == {
        "ls": "old-account",
        "fio": "Иванов Иван",
        "authorized_1c": False,
    }


def test_save_ls_rolls_back_stale_fio_clear_when_persistence_fails() -> None:
    state = {"fio": "Устаревшее ФИО", "authorized_1c": False}
    deps = _account_deps(
        get_state=MagicMock(return_value=state),
        get_bot_user=MagicMock(return_value={"ls": None, "fio": "Устаревшее ФИО"}),
        upsert_bot_user=MagicMock(side_effect=sqlite3.Error("disk failed")),
        integration_enabled=True,
    )

    with pytest.raises(sqlite3.Error, match="disk failed"):
        auth.save_ls(42, "new-account", None, deps)

    assert state == {"fio": "Устаревшее ФИО", "authorized_1c": False}


@pytest.mark.parametrize("value", ["100001", " 100001 ", "", "abc", "１２３"])
def test_validate_ls_passes_input_without_normalization(value: str) -> None:
    get_ls = MagicMock(return_value={"ls": value})
    deps = _account_deps(get_ls=get_ls)

    assert auth.validate_ls(value, deps)
    get_ls.assert_called_once_with(value)


def test_validate_ls_database_failure_is_safe_and_does_not_log_account(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.auth.validate.database")
    deps = _account_deps(
        get_ls=MagicMock(side_effect=sqlite3.Error("SECRET-LS")),
        logger=logger,
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        assert not auth.validate_ls("SECRET-LS", deps)

    assert "SECRET-LS" not in caplog.text


def test_database_outage_does_not_consume_attempts_or_lose_deferred_flow() -> None:
    state = {"state": S.AWAIT_LS, "after_ls": "appeal", "appeal": {"body": "x"}}
    attempts: dict = {}
    account_deps = _account_deps(
        get_ls=MagicMock(
            side_effect=[
                sqlite3.Error("offline 1"),
                sqlite3.Error("offline 2"),
                sqlite3.Error("offline 3"),
                {"ls": "100001"},
            ]
        )
    )
    brute_deps = _brute_deps(attempts=attempts)
    continuation = MagicMock()
    deps = _flow_deps(
        validate_ls=lambda value: auth.validate_ls(value, account_deps),
        check_ls_brute=lambda chat_id: auth.check_ls_brute(chat_id, brute_deps),
        fail_ls=lambda chat_id: auth.fail_ls(chat_id, brute_deps),
        reset_ls_brute=lambda chat_id: auth.reset_ls_brute(chat_id, brute_deps),
        continuations={"appeal": continuation},
    )

    for _ in range(3):
        auth.on_await_ls(42, state, "100001", deps)
        assert attempts == {}
        assert state == {
            "state": S.AWAIT_LS,
            "after_ls": "appeal",
            "appeal": {"body": "x"},
        }

    auth.on_await_ls(42, state, "100001", deps)

    continuation.assert_called_once_with(42, "100001")
    assert attempts == {}
    assert state == {"state": S.MENU, "appeal": {"body": "x"}}


def test_brute_force_blocks_on_exact_limit_and_expires() -> None:
    attempts: dict = {}
    deps = _brute_deps(attempts=attempts)

    assert auth.fail_ls(42, deps) == "Лицевой счёт не найден. Осталось попыток: 2"
    assert auth.fail_ls(42, deps) == "Лицевой счёт не найден. Осталось попыток: 1"
    assert auth.check_ls_brute(42, deps) is None
    assert auth.fail_ls(42, deps) == "Превышено число попыток. Введите ЛС через 30 мин."
    assert auth.check_ls_brute(42, deps) == (
        "Слишком много неудачных попыток.\nПопробуйте через 31 мин."
    )

    expired = _brute_deps(attempts=attempts, now=lambda: NOW + timedelta(minutes=30))
    assert auth.check_ls_brute(42, expired) is None
    assert 42 not in attempts
    assert auth.fail_ls(42, expired) == "Лицевой счёт не найден. Осталось попыток: 2"
    assert attempts[42]["attempts"] == 1


def test_bot_brute_wrappers_keep_runtime_registry_clock_and_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: dict = {}
    monkeypatch.setattr(bot, "_auth_attempts", attempts)
    monkeypatch.setattr(bot, "_now", lambda: NOW)
    monkeypatch.setattr(bot, "MAX_AUTH_ATTEMPTS", 1)
    monkeypatch.setattr(bot, "AUTH_BLOCK_MINUTES", 7)

    assert bot._fail_ls(42) == "Превышено число попыток. Введите ЛС через 7 мин."
    assert attempts[42]["blocked_until"] == NOW + timedelta(minutes=7)
    assert bot._check_ls_brute(42) == (
        "Слишком много неудачных попыток.\nПопробуйте через 8 мин."
    )
    bot._reset_ls_brute(42)
    assert attempts == {}


def test_bot_default_attempt_registry_is_owned_by_auth_module() -> None:
    assert bot._auth_attempts is auth.auth_attempts


@pytest.mark.parametrize(
    "message_state",
    [S.AWAIT_LS, S.AWAIT_LS_1C, S.AWAIT_CODE_1C, S.APPEAL_BODY, S.MENU],
)
def test_handle_message_redacts_all_user_input_from_debug_log(
    message_state: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "SECRET-LS-OR-CODE-991122"
    state = {"state": message_state}
    handler = MagicMock()
    monkeypatch.setattr(bot, "_get_state", MagicMock(return_value=state))
    monkeypatch.setattr(bot, "_touch", MagicMock())
    handlers = {} if message_state == S.MENU else {message_state: handler}
    monkeypatch.setattr(bot, "_MESSAGE_HANDLERS", handlers)
    monkeypatch.setattr(bot, "send_main_menu", MagicMock())

    with caplog.at_level(logging.DEBUG, logger=bot.log.name):
        bot.handle_message({"recipient": {"chat_id": 42}, "body": {"text": sentinel}})

    if message_state == S.MENU:
        handler.assert_not_called()
    else:
        handler.assert_called_once_with(42, state, sentinel)
    assert sentinel not in caplog.text
    assert "<скрыто>" in caplog.text


def test_request_ls_preserves_context_and_deferred_action() -> None:
    state = {"appeal": {"body": "Нет воды"}}
    deps = _flow_deps(get_state=MagicMock(return_value=state))

    auth.request_ls(42, "appeal", deps)

    assert state["appeal"] == {"body": "Нет воды"}
    assert state["state"] == S.AWAIT_LS
    assert state["after_ls"] == "appeal"


def test_request_ls_delegates_to_1c_auth_when_enabled() -> None:
    start = MagicMock()
    deps = _flow_deps(integration_enabled=True, start_1c_auth=start)

    auth.request_ls(42, "pokazaniya", deps)

    start.assert_called_once_with(42, "pokazaniya")


def test_manual_account_block_stops_before_validation() -> None:
    state = {"state": S.AWAIT_LS, "after_ls": "appeal"}
    validate = MagicMock()
    deps = _flow_deps(
        check_ls_brute=MagicMock(return_value="Подождите"),
        validate_ls=validate,
    )

    auth.on_await_ls(42, state, "100001", deps)

    validate.assert_not_called()
    deps.send_message.assert_called_once_with(42, "Подождите")  # type: ignore[attr-defined]
    assert state == {"state": S.AWAIT_LS, "after_ls": "appeal"}


def test_invalid_manual_account_records_failure_and_reprompts() -> None:
    state = {"state": S.AWAIT_LS, "after_ls": "appeal"}
    fail = MagicMock(return_value="ЛС не найден")
    deps = _flow_deps(validate_ls=MagicMock(return_value=False), fail_ls=fail)

    auth.on_await_ls(42, state, "bad-account", deps)

    fail.assert_called_once_with(42)
    assert deps.send_message.call_args_list == [  # type: ignore[attr-defined]
        call(42, "ЛС не найден"),
        call(42, "Введите номер лицевого счёта повторно:"),
    ]
    assert state == {"state": S.AWAIT_LS, "after_ls": "appeal"}


def test_valid_manual_account_saves_and_resumes_context() -> None:
    continuation = MagicMock()
    state = {
        "state": S.AWAIT_LS,
        "after_ls": "appeal",
        "appeal": {"body": "Нет воды"},
    }
    deps = _flow_deps(continuations={"appeal": continuation})

    auth.on_await_ls(42, state, "100001", deps)

    deps.reset_ls_brute.assert_called_once_with(42)  # type: ignore[attr-defined]
    deps.save_ls.assert_called_once_with(42, "100001")  # type: ignore[attr-defined]
    continuation.assert_called_once_with(42, "100001")
    assert state == {
        "state": S.MENU,
        "appeal": {"body": "Нет воды"},
    }


def test_manual_account_without_known_continuation_confirms_and_opens_menu() -> None:
    state = {"state": S.AWAIT_LS, "after_ls": "unknown"}
    deps = _flow_deps()

    auth.on_await_ls(42, state, "100001", deps)

    deps.send_message.assert_called_once_with(  # type: ignore[attr-defined]
        42, "✅ Лицевой счёт сохранён."
    )
    deps.send_main_menu.assert_called_once_with(42)  # type: ignore[attr-defined]
    assert state == {"state": S.MENU}


def test_start_deferred_auth_preserves_flow_but_clears_stale_account() -> None:
    state = {
        "appeal": {"body": "Нет воды"},
        "pending_1c_ls": "old",
        "after_ls": "appeal",
    }
    deps = _flow_deps(get_state=MagicMock(return_value=state))

    auth.start_1c_auth(42, "appeal", deps)

    assert state == {
        "appeal": {"body": "Нет воды"},
        "state": S.AWAIT_LS_1C,
        "after_1c_auth": "appeal",
    }


def test_start_standalone_auth_clears_previous_flow() -> None:
    state = {"appeal": {"body": "Нет воды"}}
    deps = _flow_deps(get_state=MagicMock(return_value=state))

    auth.start_1c_auth(42, None, deps)

    assert state == {"state": S.AWAIT_LS_1C}


def test_request_code_strips_account_and_waits_for_code() -> None:
    state = {"state": S.AWAIT_LS_1C}
    request = MagicMock(
        return_value=({"status": "ok", "message": "Код отправлен"}, None)
    )
    deps = _flow_deps(request_1c_auth_code=request)

    auth.on_await_ls_1c(42, state, " 100001 ", deps)

    request.assert_called_once_with("100001", 42)
    assert state["pending_1c_ls"] == "100001"
    assert state["state"] == S.AWAIT_CODE_1C


@pytest.mark.parametrize("response", [(None, "offline"), (["ok"], None)])
def test_request_code_handles_error_and_malformed_payload(response: tuple) -> None:
    state = {"state": S.AWAIT_LS_1C, "appeal": {}}
    deps = _flow_deps(request_1c_auth_code=MagicMock(return_value=response))

    auth.on_await_ls_1c(42, state, "100001", deps)

    assert state == {"state": S.MENU}
    deps.send_main_menu.assert_called_once_with(42)  # type: ignore[attr-defined]


def test_request_code_rejects_wrong_status_and_message_types() -> None:
    state = {"state": S.AWAIT_LS_1C}
    deps = _flow_deps(
        request_1c_auth_code=MagicMock(
            return_value=({"status": ["ok"], "message": {"secret": "value"}}, None)
        )
    )

    auth.on_await_ls_1c(42, state, "100001", deps)

    deps.send_message.assert_called_once_with(  # type: ignore[attr-defined]
        42, "⚠️ Сервис авторизации временно недоступен. Попробуйте позже."
    )
    assert state == {"state": S.MENU}


@pytest.mark.parametrize("bad_message", [None, "", "   ", {"secret": "SECRET-LS"}])
def test_request_success_with_malformed_message_fails_closed(
    bad_message: object,
) -> None:
    state = {"state": S.AWAIT_LS_1C, "appeal": {"body": "context"}}
    deps = _flow_deps(
        request_1c_auth_code=MagicMock(
            return_value=({"status": "ok", "message": bad_message}, None)
        )
    )

    auth.on_await_ls_1c(42, state, "100001", deps)

    assert state == {"state": S.MENU}
    assert "pending_1c_ls" not in state
    deps.send_main_menu.assert_called_once_with(42)  # type: ignore[attr-defined]


def test_request_code_rejects_empty_account_without_calling_api() -> None:
    state = {"state": S.AWAIT_LS_1C}
    request = MagicMock()
    deps = _flow_deps(request_1c_auth_code=request)

    auth.on_await_ls_1c(42, state, "   ", deps)

    request.assert_not_called()
    deps.send_message.assert_called_once_with(  # type: ignore[attr-defined]
        42, "Введите номер лицевого счёта."
    )
    assert state == {"state": S.AWAIT_LS_1C}


def test_request_code_api_exception_uses_fallback_without_logging_account(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = {"state": S.AWAIT_LS_1C}
    logger = logging.getLogger("test.auth.request.exception")
    deps = _flow_deps(
        request_1c_auth_code=MagicMock(side_effect=RuntimeError("upstream failed")),
        logger=logger,
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        auth.on_await_ls_1c(42, state, "SECRET-LS-100001", deps)

    assert "SECRET-LS-100001" not in caplog.text
    assert state == {"state": S.MENU}


def test_wrong_code_keeps_pending_authorization() -> None:
    state = {"state": S.AWAIT_CODE_1C, "pending_1c_ls": "100001"}
    deps = _flow_deps(
        verify_1c_auth_code=MagicMock(
            return_value=({"status": "wrong_code", "message": "Неверный код"}, None)
        )
    )

    auth.on_await_code_1c(42, state, "000000", deps)

    assert state == {"state": S.AWAIT_CODE_1C, "pending_1c_ls": "100001"}


@pytest.mark.parametrize("response", [(None, "offline"), (["ok"], None)])
def test_verify_code_handles_error_and_malformed_payload(response: tuple) -> None:
    state = {"state": S.AWAIT_CODE_1C, "pending_1c_ls": "100001"}
    deps = _flow_deps(verify_1c_auth_code=MagicMock(return_value=response))

    auth.on_await_code_1c(42, state, "123456", deps)

    assert state == {"state": S.MENU}
    deps.send_main_menu.assert_called_once_with(42)  # type: ignore[attr-defined]


def test_verify_code_rejects_wrong_status_and_message_types() -> None:
    state = {"state": S.AWAIT_CODE_1C, "pending_1c_ls": "100001"}
    deps = _flow_deps(
        verify_1c_auth_code=MagicMock(
            return_value=({"status": {"ok": True}, "message": ["secret"]}, None)
        )
    )

    auth.on_await_code_1c(42, state, "123456", deps)

    deps.send_message.assert_called_once_with(  # type: ignore[attr-defined]
        42, "⚠️ Сервис авторизации временно недоступен. Попробуйте позже."
    )
    assert state == {"state": S.MENU}


@pytest.mark.parametrize("bad_message", [None, "", "   ", ["SECRET-CODE"]])
def test_verify_success_with_malformed_message_fails_closed(
    bad_message: object,
) -> None:
    continuation = MagicMock()
    state = {
        "state": S.AWAIT_CODE_1C,
        "pending_1c_ls": "100001",
        "after_1c_auth": "appeal",
    }
    deps = _flow_deps(
        verify_1c_auth_code=MagicMock(
            return_value=({"status": "ok", "message": bad_message}, None)
        ),
        continuations={"appeal": continuation},
    )

    auth.on_await_code_1c(42, state, "123456", deps)

    deps.save_ls.assert_not_called()  # type: ignore[attr-defined]
    continuation.assert_not_called()
    assert state == {"state": S.MENU}
    deps.send_main_menu.assert_called_once_with(42)  # type: ignore[attr-defined]


def test_missing_pending_account_restarts_authorization_without_api_call() -> None:
    state = {"state": S.AWAIT_CODE_1C}
    verify = MagicMock()
    deps = _flow_deps(verify_1c_auth_code=verify)

    auth.on_await_code_1c(42, state, "123456", deps)

    verify.assert_not_called()
    deps.send_main_menu.assert_called_once_with(  # type: ignore[attr-defined]
        42, "Начнём авторизацию заново."
    )
    assert state == {"state": S.MENU}


@pytest.mark.parametrize("status", ["expired_code", "rejected", None])
def test_non_success_code_status_clears_flow(status: str | None) -> None:
    state = {
        "state": S.AWAIT_CODE_1C,
        "pending_1c_ls": "100001",
        "appeal": {},
    }
    deps = _flow_deps(
        verify_1c_auth_code=MagicMock(
            return_value=({"status": status, "message": "Не принято"}, None)
        )
    )

    auth.on_await_code_1c(42, state, "000000", deps)

    assert state == {"state": S.MENU}


@pytest.mark.parametrize(
    "after",
    ["appeal", "pokazaniya", "appointment", "kvitanciya", "my_appeals"],
)
def test_success_saves_account_and_resumes_every_supported_flow(after: str) -> None:
    continuation = MagicMock()
    state = {
        "state": S.AWAIT_CODE_1C,
        "pending_1c_ls": "100001",
        "after_1c_auth": after,
        "appeal": {"body": "context"},
    }
    deps = _flow_deps(continuations={after: continuation})

    auth.on_await_code_1c(42, state, "123456", deps)

    deps.save_ls.assert_called_once_with(42, "100001")  # type: ignore[attr-defined]
    continuation.assert_called_once_with(42, "100001")
    assert state["appeal"] == {"body": "context"}
    assert state["state"] == S.MENU
    assert "pending_1c_ls" not in state
    assert "after_1c_auth" not in state


def test_verify_api_exception_clears_flow_without_logging_code_or_account(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = {"state": S.AWAIT_CODE_1C, "pending_1c_ls": "SECRET-LS"}
    logger = logging.getLogger("test.auth.verify.exception")
    deps = _flow_deps(
        verify_1c_auth_code=MagicMock(side_effect=RuntimeError("upstream failed")),
        logger=logger,
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        auth.on_await_code_1c(42, state, "SECRET-CODE", deps)

    assert "SECRET-LS" not in caplog.text
    assert "SECRET-CODE" not in caplog.text
    assert state == {"state": S.MENU}


def test_save_failure_returns_safe_fallback_and_does_not_continue() -> None:
    continuation = MagicMock()
    state = {
        "state": S.AWAIT_CODE_1C,
        "pending_1c_ls": "100001",
        "after_1c_auth": "appeal",
    }
    deps = _flow_deps(
        save_ls=MagicMock(side_effect=sqlite3.Error("disk failed")),
        continuations={"appeal": continuation},
    )

    auth.on_await_code_1c(42, state, "123456", deps)

    continuation.assert_not_called()
    assert state == {"state": S.MENU}
