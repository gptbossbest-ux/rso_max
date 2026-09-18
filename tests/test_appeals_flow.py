from __future__ import annotations

from unittest.mock import Mock

import bot
from rso_bot.flows import appeals, auth


def _dependencies(
    *, saved_ls: str | None = None
) -> tuple[appeals.AppealDependencies, dict]:
    state: dict = {"state": "menu"}
    deps = appeals.AppealDependencies(
        create_appeal=Mock(return_value=({"ticket_no": "A-42"}, None)),
        list_appeals_by_ls=Mock(return_value=({"appeals": []}, None)),
        confirm_appeal=Mock(return_value=({"ticket_no": "A-42"}, None)),
        reopen_appeal=Mock(return_value=({"ticket_no": "A-42"}, None)),
        get_state=Mock(return_value=state),
        touch=Mock(side_effect=lambda value: value),
        get_saved_ls=Mock(return_value=saved_ls),
        request_ls=Mock(),
        check_ls_brute=Mock(return_value=None),
        validate_ls=Mock(return_value=True),
        fail_ls=Mock(return_value="Лицевой счёт не найден. Осталось попыток: 2"),
        reset_ls_brute=Mock(),
        save_ls=Mock(),
        submit_appeal=Mock(),
        make_callback=lambda label, payload: {
            "type": "callback",
            "text": label,
            "payload": payload,
        },
        send_message=Mock(),
        send_buttons=Mock(),
        send_main_menu=Mock(),
        logger=Mock(),
        categories={"Авария": "авария", "Прочее": "прочее"},
        category_state="appeal_category",
        body_state="appeal_body",
        reopen_comment_state="reopen_comment",
        menu_state="menu",
    )
    return deps, state


def test_start_and_category_preserve_state_buttons_and_text():
    deps, state = _dependencies()

    appeals.start_appeal(42, deps)

    assert state["state"] == "appeal_category"
    assert "appeal" not in state
    deps.send_buttons.assert_called_once_with(
        42,
        "Выберите категорию обращения:",
        [
            [{"type": "callback", "text": "Авария", "payload": "cat:авария"}],
            [{"type": "callback", "text": "Прочее", "payload": "cat:прочее"}],
            [{"type": "callback", "text": "❌ Отмена", "payload": "cancel"}],
        ],
    )

    appeals.set_category(42, "авария", deps)

    assert state == {
        "state": "appeal_body",
        "appeal": {"category": "авария"},
    }
    deps.send_message.assert_called_once_with(42, "Опишите вашу проблему или вопрос:")


def test_body_submits_with_saved_ls_or_requests_authorization_without_it():
    saved, state = _dependencies(saved_ls="100001")
    state["appeal"] = {"category": "авария"}
    appeals.got_body(42, "Нет воды", saved)
    assert state["appeal"]["body"] == "Нет воды"
    saved.submit_appeal.assert_called_once_with(42, "100001")
    saved.request_ls.assert_not_called()

    missing, state = _dependencies()
    state["appeal"] = {"category": "авария"}
    appeals.got_body(43, "Нет воды", missing)
    missing.request_ls.assert_called_once_with(43, "appeal")
    assert state["appeal"] == {"category": "авария", "body": "Нет воды"}


def test_got_ls_preserves_block_invalid_and_valid_paths():
    blocked, _ = _dependencies()
    blocked.check_ls_brute.return_value = "Подождите"
    appeals.got_ls(42, "bad", blocked)
    blocked.send_message.assert_called_once_with(42, "Подождите")
    blocked.validate_ls.assert_not_called()

    invalid, _ = _dependencies()
    invalid.validate_ls.return_value = False
    appeals.got_ls(42, "bad", invalid)
    assert invalid.send_message.call_args_list[0].args == (
        42,
        "Лицевой счёт не найден. Осталось попыток: 2",
    )
    assert invalid.send_message.call_args_list[1].args == (
        42,
        "Введите номер лицевого счёта повторно:",
    )
    invalid.save_ls.assert_not_called()

    valid, _ = _dependencies()
    appeals.got_ls(42, "100001", valid)
    valid.reset_ls_brute.assert_called_once_with(42)
    valid.save_ls.assert_called_once_with(42, "100001")
    valid.submit_appeal.assert_called_once_with(42, "100001")


def test_got_ls_does_not_spend_attempt_when_database_is_unavailable():
    unavailable, state = _dependencies()
    unavailable.validate_ls.return_value = auth.LsValidation.UNAVAILABLE

    appeals.got_ls(42, "100001", unavailable)

    unavailable.fail_ls.assert_not_called()
    unavailable.save_ls.assert_not_called()
    unavailable.send_message.assert_called_once_with(
        42, "⚠️ Сервис временно недоступен. Попробуйте позже."
    )
    assert state == {"state": "menu"}


def test_submit_success_preserves_api_contract_and_clears_flow():
    deps, state = _dependencies()
    state.update(
        {"state": "appeal_body", "appeal": {"category": "авария", "body": "Нет воды"}}
    )

    appeals.submit_appeal(42, "100001", deps)

    deps.create_appeal.assert_called_once_with(
        ls="100001",
        channel="max",
        category="авария",
        body="Нет воды",
        chat_id=42,
    )
    assert state == {"state": "menu"}
    deps.send_message.assert_called_once_with(
        42,
        "✅ Обращение принято!\nНомер: A-42\n\nМы свяжемся с вами в ближайшее время.",
    )
    deps.send_main_menu.assert_called_once_with(42)


def test_submit_error_preserves_cleanup_logging_and_user_response():
    deps, state = _dependencies()
    state["appeal"] = {"body": "Вопрос"}
    deps.create_appeal.return_value = (None, "timeout")

    appeals.submit_appeal(42, "100001", deps)

    assert state == {"state": "menu"}
    deps.logger.error.assert_called_once_with(
        "create_appeal chat_id=%s err=%s", 42, "timeout"
    )
    deps.send_message.assert_called_once_with(
        42, "⚠️ Сервис временно недоступен. Попробуйте позже."
    )
    deps.send_main_menu.assert_called_once_with(42)


def test_my_appeals_requests_ls_and_handles_service_error():
    missing, _ = _dependencies()
    appeals.show_my_appeals(42, missing)
    missing.request_ls.assert_called_once_with(42, "my_appeals")

    failed, _ = _dependencies(saved_ls="100001")
    failed.list_appeals_by_ls.return_value = (None, "timeout")
    appeals.show_my_appeals(42, failed)
    failed.send_message.assert_called_once_with(
        42, "⚠️ Сервис временно недоступен. Попробуйте позже."
    )
    failed.send_main_menu.assert_called_once_with(42)


def test_my_appeals_filters_closed_items_and_formats_statuses():
    deps, _ = _dependencies(saved_ls="100001")
    deps.list_appeals_by_ls.return_value = (
        {
            "appeals": [
                {
                    "ticket_no": "N-1",
                    "status": "new",
                    "body": "Коротко",
                    "created_at": "2026-09-17T10:11:12",
                },
                {
                    "ticket_no": "N-2",
                    "status": "in_work",
                    "body": "x" * 61,
                    "created_at": None,
                },
                {"ticket_no": "N-3", "status": "resolved", "body": "Скрыто"},
            ]
        },
        None,
    )

    appeals.show_my_appeals(42, deps)

    message = deps.send_message.call_args.args[1]
    assert "№ N-1 — 🆕 Новое" in message
    assert "№ N-2 — ⚙️ В работе" in message
    assert "x" * 60 + "..." in message
    assert "N-3" not in message
    deps.send_main_menu.assert_called_once_with(42)


def test_my_appeals_reports_no_active_items():
    deps, _ = _dependencies(saved_ls="100001")
    deps.list_appeals_by_ls.return_value = ({"appeals": [{"status": "closed"}]}, None)
    appeals.show_my_appeals(42, deps)
    deps.send_message.assert_called_once_with(42, "✅ У вас нет активных обращений.")


def test_confirm_success_and_error_preserve_responses():
    success, state = _dependencies()
    appeals.confirm_appeal(42, state, "7", success)
    success.confirm_appeal.assert_called_once_with(7, "max", 42)
    success.send_message.assert_called_once_with(
        42, "✅ Обращение №A-42 закрыто.\nСпасибо!"
    )
    success.send_main_menu.assert_called_once_with(42)

    failed, state = _dependencies()
    failed.confirm_appeal.return_value = (None, "conflict")
    appeals.confirm_appeal(42, state, "7", failed)
    failed.send_message.assert_called_once_with(
        42, "⚠️ Не удалось подтвердить закрытие: conflict"
    )


def test_begin_reopen_and_comment_success_preserve_state_and_text():
    deps, state = _dependencies()
    appeals.begin_reopen(42, state, "7", deps)
    assert state["state"] == "reopen_comment"
    assert state["reopen_appeal_id"] == 7
    deps.send_message.assert_called_once_with(
        42,
        "Опишите, пожалуйста, причину возврата обращения в работу:",
    )

    deps.send_message.reset_mock()
    appeals.on_reopen_comment(42, state, "Проблема осталась", deps)
    assert state == {"state": "menu"}
    deps.reopen_appeal.assert_called_once_with(7, "Проблема осталась")
    deps.send_message.assert_called_once_with(
        42,
        "↩️ Обращение №A-42 возвращено в работу.\nВаш комментарий: Проблема осталась",
    )
    deps.send_main_menu.assert_called_once_with(42)


def test_reopen_error_and_missing_context_still_return_to_menu():
    failed, state = _dependencies()
    state["reopen_appeal_id"] = 7
    failed.reopen_appeal.return_value = (None, "timeout")
    appeals.on_reopen_comment(42, state, "Причина", failed)
    failed.send_message.assert_called_once_with(
        42, "⚠️ Не удалось вернуть обращение: timeout"
    )
    assert state == {"state": "menu"}

    missing, state = _dependencies()
    appeals.on_reopen_comment(43, state, "Причина", missing)
    missing.reopen_appeal.assert_not_called()
    missing.send_message.assert_not_called()
    missing.send_main_menu.assert_called_once_with(43)


def test_bot_wrappers_delegate_and_keep_runtime_patch_points(monkeypatch):
    delegated = Mock()
    monkeypatch.setattr(bot.appeals, "start_appeal", delegated)
    patched_sender = Mock()
    patched_submit = Mock()
    monkeypatch.setattr(bot, "send_message", patched_sender)
    monkeypatch.setattr(bot, "_submit_appeal", patched_submit)

    bot._start_appeal(42)

    chat_id, deps = delegated.call_args.args
    assert chat_id == 42
    assert deps.send_message is patched_sender
    assert deps.submit_appeal is patched_submit
    assert deps.create_appeal is bot.client_api.create_appeal


def test_all_bot_appeal_wrappers_delegate(monkeypatch):
    names_and_calls = [
        (
            "set_category",
            lambda: bot._appeal_set_category(42, "авария"),
            (42, "авария"),
        ),
        ("got_body", lambda: bot._appeal_got_body(42, "Текст"), (42, "Текст")),
        ("got_ls", lambda: bot._appeal_got_ls(42, "100001"), (42, "100001")),
        ("submit_appeal", lambda: bot._submit_appeal(42, "100001"), (42, "100001")),
        ("show_my_appeals", lambda: bot._show_my_appeals(42), (42,)),
        ("confirm_appeal", lambda: bot._cb_confirm_appeal(42, {}, "7"), (42, {}, "7")),
        ("begin_reopen", lambda: bot._cb_reopen_appeal(42, {}, "7"), (42, {}, "7")),
        (
            "on_reopen_comment",
            lambda: bot._on_reopen_comment(42, {}, "Причина"),
            (42, {}, "Причина"),
        ),
    ]
    for name, call, expected in names_and_calls:
        delegated = Mock()
        monkeypatch.setattr(bot.appeals, name, delegated)
        call()
        assert delegated.call_args.args[:-1] == expected
