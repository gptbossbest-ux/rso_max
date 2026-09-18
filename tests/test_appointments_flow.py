from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

import bot
from rso_bot.flows import appointments


def _button(label: str, payload: str) -> dict:
    return {"type": "callback", "text": label, "payload": payload}


def _dependencies(
    *,
    saved_ls: str | None = None,
    state: dict | None = None,
) -> tuple[appointments.AppointmentDependencies, dict]:
    active_state = state if state is not None else {"state": "menu"}
    deps = appointments.AppointmentDependencies(
        get_active_appointment=Mock(return_value=None),
        get_branches=Mock(return_value=[{"id": 2, "name": "Центральный офис"}]),
        get_available_dates=Mock(return_value=["2026-09-21"]),
        get_branch=Mock(
            return_value={
                "id": 2,
                "name": "Центральный офис",
                "address": "ул. Ленина, 1",
            }
        ),
        get_available_slots=Mock(return_value=["09:00", "09:30"]),
        create_appointment=Mock(return_value=(17, None)),
        get_appointment=Mock(
            return_value={"id": 17, "status": "active", "chat_id": 42}
        ),
        cancel_appointment=Mock(),
        get_state=Mock(return_value=active_state),
        touch=Mock(side_effect=lambda value: value),
        get_saved_ls=Mock(return_value=saved_ls),
        request_ls=Mock(),
        clear_flow=Mock(
            side_effect=lambda value: (
                value.clear(),
                value.update({"state": "menu"}),
            )
        ),
        show_active_appointment=Mock(),
        show_branch_select=Mock(),
        show_date_select=Mock(),
        show_time_select=Mock(),
        show_appointment_confirm=Mock(),
        finalize_appointment=Mock(),
        make_callback=_button,
        send_message=Mock(),
        send_buttons=Mock(),
        send_main_menu=Mock(),
        parse_date=datetime.strptime,
        database_errors=(RuntimeError,),
        logger=Mock(),
        menu_state="menu",
        branch_state="appointment_branch",
        date_state="appointment_date",
        time_state="appointment_time",
        theme_state="appointment_theme",
        confirm_state="appointment_confirm",
    )
    return deps, active_state


def test_start_uses_explicit_or_saved_ls_and_requests_missing_ls():
    explicit, _ = _dependencies(saved_ls="saved")
    appointments.start_appointment_flow(42, "explicit", explicit)
    explicit.get_saved_ls.assert_not_called()
    explicit.get_active_appointment.assert_called_once_with("explicit")
    explicit.show_branch_select.assert_called_once_with(42, "explicit")

    saved, _ = _dependencies(saved_ls="100001")
    appointments.start_appointment_flow(42, None, saved)
    saved.show_branch_select.assert_called_once_with(42, "100001")

    missing, _ = _dependencies()
    appointments.start_appointment_flow(42, None, missing)
    missing.request_ls.assert_called_once_with(42, "appointment")
    missing.get_active_appointment.assert_not_called()


def test_start_shows_existing_appointment_instead_of_branches():
    deps, _ = _dependencies(saved_ls="100001")
    existing = {"id": 7, "status": "active"}
    deps.get_active_appointment.return_value = existing

    appointments.start_appointment_flow(42, None, deps)

    deps.show_active_appointment.assert_called_once_with(42, existing)
    deps.show_branch_select.assert_not_called()


def test_show_active_appointment_preserves_text_state_and_callbacks():
    deps, state = _dependencies(state={"state": "appointment_date"})
    appointment = {
        "id": 7,
        "theme": "Перерасчёт",
        "branch_name": "Офис",
        "branch_address": "Адрес",
        "slot_date": "2026-09-21",
        "slot_time": "10:30",
    }

    appointments.show_active_appointment(42, appointment, deps)

    assert state["state"] == "menu"
    deps.send_buttons.assert_called_once_with(
        42,
        "У вас уже есть активная запись на приём:\n\n"
        "📍 Офис\n🏠 Адрес\n📅 2026-09-21  🕐 10:30\nТема: Перерасчёт",
        [
            [_button("❌ Отменить запись", "appt_cancel:7")],
            [_button("🏠 Главное меню", "main_menu")],
        ],
    )


def test_branch_selection_and_unavailable_branch_path():
    deps, state = _dependencies()
    appointments.show_branch_select(42, "100001", deps)

    assert state == {
        "state": "appointment_branch",
        "ls": "100001",
    }
    deps.send_buttons.assert_called_once_with(
        42,
        "Выберите филиал:",
        [
            [_button("📍 Центральный офис", "appt_branch:2")],
            [_button("❌ Отмена", "main_menu")],
        ],
    )

    unavailable, _ = _dependencies()
    unavailable.get_branches.return_value = []
    appointments.show_branch_select(43, "100002", unavailable)
    unavailable.send_message.assert_called_once_with(
        43, "На данный момент запись на приём недоступна."
    )
    unavailable.send_main_menu.assert_called_once_with(43)


def test_date_selection_limits_buttons_and_handles_no_dates():
    deps, state = _dependencies()
    deps.get_available_dates.return_value = [
        f"2026-10-{day:02d}" for day in range(1, 13)
    ]

    appointments.show_date_select(42, 2, deps)

    assert state["state"] == "appointment_date"
    assert state["appt_branch_id"] == 2
    rows = deps.send_buttons.call_args.args[2]
    assert len(rows) == 11
    assert rows[0] == [_button("01.10 (Чт)", "appt_date:2026-10-01")]
    assert rows[-2] == [_button("10.10 (Сб)", "appt_date:2026-10-10")]
    assert rows[-1] == [_button("❌ Отмена", "main_menu")]

    empty, _ = _dependencies()
    empty.get_available_dates.return_value = []
    appointments.show_date_select(43, 2, empty)
    empty.send_message.assert_called_once_with(
        43, "На выбранный филиал сейчас нет свободных дат для записи."
    )
    empty.send_main_menu.assert_called_once_with(43)


@pytest.mark.parametrize(
    ("date", "label"),
    [
        ("2024-02-29", "29.02 (Чт)"),
        ("2026-09-20", "20.09 (Вс)"),
        ("2026-09-21", "21.09 (Пн)"),
    ],
)
def test_format_date_label_is_stable_at_calendar_boundaries(date, label):
    deps, _ = _dependencies()
    assert appointments.format_date_label(date, deps) == label


def test_format_date_uses_injected_time_parser():
    deps, _ = _dependencies()
    parser = Mock(return_value=datetime(2026, 9, 21, tzinfo=timezone.utc))
    deps = replace(deps, parse_date=parser)

    assert appointments.format_date_label("boundary", deps) == "21.09 (Пн)"
    parser.assert_called_once_with("boundary", "%Y-%m-%d")


def test_time_selection_groups_slots_and_handles_slot_race():
    deps, state = _dependencies()
    deps.get_available_slots.return_value = ["09:00", "09:30", "10:00", "10:30"]

    appointments.show_time_select(42, 2, "2026-09-21", deps)

    assert state["state"] == "appointment_time"
    assert state["appt_date"] == "2026-09-21"
    assert deps.send_buttons.call_args.args[2] == [
        [
            _button("09:00", "appt_time:09:00"),
            _button("09:30", "appt_time:09:30"),
            _button("10:00", "appt_time:10:00"),
        ],
        [_button("10:30", "appt_time:10:30")],
        [_button("❌ Отмена", "main_menu")],
    ]

    empty, _ = _dependencies()
    empty.get_available_slots.return_value = []
    appointments.show_time_select(43, 2, "2026-09-21", empty)
    empty.send_message.assert_called_once_with(
        43, "На эту дату свободных слотов не осталось. Выберите другую дату."
    )
    empty.show_date_select.assert_called_once_with(43, 2)


def test_theme_text_skip_and_confirmation_preserve_payloads():
    deps, state = _dependencies(
        state={
            "state": "appointment_time",
            "appt_branch_id": 2,
            "appt_date": "2026-09-21",
        }
    )
    appointments.ask_theme(42, "10:30", deps)
    assert state["state"] == "appointment_theme"
    assert state["appt_time"] == "10:30"
    assert deps.send_buttons.call_args.args[2] == [
        [_button("⏭ Пропустить", "appt_skip_theme")]
    ]

    appointments.on_theme(42, state, "т" * 250, deps)
    assert state["appt_theme"] == "т" * 200
    deps.show_appointment_confirm.assert_called_once_with(42)

    deps.show_appointment_confirm.reset_mock()
    appointments.skip_theme(42, state, deps)
    assert state["appt_theme"] is None
    deps.show_appointment_confirm.assert_called_once_with(42)

    state["appt_theme"] = "Перерасчёт"
    appointments.show_appointment_confirm(42, deps)
    assert state["state"] == "appointment_confirm"
    assert deps.send_buttons.call_args.args[1] == (
        "Проверьте данные записи:\n\n"
        "📍 Центральный офис\n"
        "🏠 ул. Ленина, 1\n"
        "📅 21.09 (Пн)\n"
        "🕐 10:30\n"
        "Тема: Перерасчёт"
    )
    assert deps.send_buttons.call_args.args[2] == [
        [_button("✅ Подтвердить запись", "appt_confirm")],
        [_button("❌ Отмена", "main_menu")],
    ]


def _confirmation_state() -> dict:
    return {
        "state": "appointment_confirm",
        "ls": "100001",
        "appt_branch_id": 2,
        "appt_date": "2026-09-21",
        "appt_time": "10:30",
        "appt_theme": "Перерасчёт",
    }


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            (None, "Этот слот уже занят, выберите другое время"),
            "⚠️ Этот слот уже занят, выберите другое время",
        ),
        ((None, "Ошибка: database unavailable"), "⚠️ Ошибка: database unavailable"),
    ],
)
def test_finalize_preserves_conflict_and_database_errors(result, message):
    deps, state = _dependencies(state=_confirmation_state())
    deps.create_appointment.return_value = result

    appointments.finalize_appointment(42, deps)

    deps.send_message.assert_called_once_with(42, message)
    deps.logger.warning.assert_called_once()
    assert state == {"state": "menu"}
    deps.send_main_menu.assert_called_once_with(42)


def test_finalize_success_uses_complete_contract_and_clears_flow():
    deps, state = _dependencies(state=_confirmation_state())

    appointments.finalize_appointment(42, deps)

    deps.create_appointment.assert_called_once_with(
        ls="100001",
        branch_id=2,
        slot_date="2026-09-21",
        slot_time="10:30",
        channel="max",
        chat_id=42,
        theme="Перерасчёт",
    )
    deps.send_message.assert_called_once_with(
        42, "✅ Вы успешно записаны на приём! Напомним о визите заранее."
    )
    assert state == {"state": "menu"}
    deps.send_main_menu.assert_called_once_with(42)


def test_confirm_only_finalizes_from_confirmation_state():
    deps, state = _dependencies(state={"state": "appointment_theme"})
    appointments.confirm(42, state, deps)
    deps.finalize_appointment.assert_not_called()

    state["state"] = "appointment_confirm"
    appointments.confirm(42, state, deps)
    deps.finalize_appointment.assert_called_once_with(42)


def test_date_callback_requires_selected_branch():
    deps, state = _dependencies(state={"state": "appointment_date"})
    appointments.select_date(42, state, "2026-09-21", deps)
    deps.send_main_menu.assert_called_once_with(42)
    deps.show_time_select.assert_not_called()

    state["appt_branch_id"] = 2
    appointments.select_date(42, state, "2026-09-21", deps)
    deps.show_time_select.assert_called_once_with(42, 2, "2026-09-21")


def test_cancel_own_rejects_missing_inactive_and_foreign_appointments():
    for appointment in (
        None,
        {"id": 17, "status": "cancelled", "chat_id": 42},
        {"id": 17, "status": "active", "chat_id": 999},
    ):
        deps, _ = _dependencies()
        deps.get_appointment.return_value = appointment
        appointments.cancel_own_appointment(42, 17, deps)
        deps.cancel_appointment.assert_not_called()
        deps.send_message.assert_called_once_with(
            42, "Запись не найдена или уже отменена."
        )
        deps.send_main_menu.assert_called_once_with(42)


def test_cancel_own_success_and_database_error_are_controlled():
    deps, _ = _dependencies()
    appointments.cancel_own_appointment(42, 17, deps)
    deps.cancel_appointment.assert_called_once_with(
        17, "client", "Отменено клиентом через бот"
    )
    deps.send_message.assert_called_once_with(42, "Запись отменена.")

    failed, _ = _dependencies()
    failed.cancel_appointment.side_effect = RuntimeError("database unavailable")
    appointments.cancel_own_appointment(42, 17, failed)
    failed.send_message.assert_called_once_with(
        42, "⚠️ Не удалось отменить запись. Попробуйте позже."
    )
    failed.logger.error.assert_called_once()
    failed.send_main_menu.assert_called_once_with(42)


def test_bot_wrappers_delegate_and_keep_runtime_patch_dependencies(monkeypatch):
    start = Mock()
    select_date = Mock()
    on_theme = Mock()
    monkeypatch.setattr(bot.appointments, "start_appointment_flow", start)
    monkeypatch.setattr(bot.appointments, "select_date", select_date)
    monkeypatch.setattr(bot.appointments, "on_theme", on_theme)

    bot._start_appointment_flow(42, "100001")
    state = {"state": bot.S.APPOINTMENT_DATE, "appt_branch_id": 2}
    bot._cb_select_date(42, state, "2026-09-21")
    bot._on_appointment_theme(42, state, "Тема")

    assert start.call_args.args[:2] == (42, "100001")
    assert select_date.call_args.args[:3] == (42, state, "2026-09-21")
    assert on_theme.call_args.args[:3] == (42, state, "Тема")
    for call in (start.call_args, select_date.call_args, on_theme.call_args):
        assert isinstance(call.args[-1], appointments.AppointmentDependencies)


def test_bot_dependency_builder_resolves_wrappers_at_call_time(monkeypatch):
    replacement = Mock()
    monkeypatch.setattr(bot, "_show_date_select", replacement)

    deps = bot._appointment_dependencies()

    assert deps.show_date_select is replacement
