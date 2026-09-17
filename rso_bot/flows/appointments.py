"""Appointment booking flow for the MAX bot.

The entry point keeps update parsing and routing.  This module owns the
appointment-specific state transitions and presentation rules; every external
operation is supplied explicitly so importing it never initializes ``bot.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

State = dict[str, Any]
Button = dict[str, Any]
Record = Mapping[str, Any]


@dataclass(frozen=True)
class AppointmentDependencies:
    """Database, state, transport and continuation dependencies for the flow."""

    get_active_appointment: Callable[[str], Record | None]
    get_branches: Callable[[], Sequence[Record]]
    get_available_dates: Callable[[int], Sequence[str]]
    get_branch: Callable[[int], Record | None]
    get_available_slots: Callable[[int, str], Sequence[str]]
    create_appointment: Callable[..., tuple[int | None, str | None]]
    get_appointment: Callable[[int], Record | None]
    cancel_appointment: Callable[[int, str, str | None], None]
    get_state: Callable[[int], State]
    touch: Callable[[State], State]
    get_saved_ls: Callable[[int], str | None]
    request_ls: Callable[[int, str], None]
    clear_flow: Callable[[State], None]
    # Calls back through entry-point wrappers to retain runtime monkeypatch seams.
    show_active_appointment: Callable[[int, Record], None]
    show_branch_select: Callable[[int, str], None]
    show_date_select: Callable[[int, int], None]
    show_time_select: Callable[[int, int, str], None]
    show_appointment_confirm: Callable[[int], None]
    finalize_appointment: Callable[[int], None]
    make_callback: Callable[[str, str], Button]
    send_message: Callable[[int, str], Any]
    send_buttons: Callable[[int, str, list[list[Button]]], Any]
    send_main_menu: Callable[..., None]
    parse_date: Callable[[str, str], datetime]
    database_errors: tuple[type[Exception], ...]
    logger: logging.Logger
    menu_state: str
    branch_state: str
    date_state: str
    time_state: str
    theme_state: str
    confirm_state: str


_WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def start_appointment_flow(
    chat_id: int,
    ls: str | None,
    deps: AppointmentDependencies,
) -> None:
    """Start booking, or show the account's existing active appointment."""
    account = ls or deps.get_saved_ls(chat_id)
    if not account:
        deps.request_ls(chat_id, "appointment")
        return

    existing = deps.get_active_appointment(account)
    if existing:
        deps.show_active_appointment(chat_id, existing)
        return

    deps.show_branch_select(chat_id, account)


def show_active_appointment(
    chat_id: int,
    appointment: Record,
    deps: AppointmentDependencies,
) -> None:
    """Display the current appointment and offer cancellation."""
    state = deps.get_state(chat_id)
    state["state"] = deps.menu_state
    deps.touch(state)

    theme_line = f"\nТема: {appointment['theme']}" if appointment["theme"] else ""
    deps.send_buttons(
        chat_id,
        f"У вас уже есть активная запись на приём:\n\n"
        f"📍 {appointment['branch_name']}\n"
        f"🏠 {appointment['branch_address']}\n"
        f"📅 {appointment['slot_date']}  🕐 {appointment['slot_time']}"
        f"{theme_line}",
        [
            [
                deps.make_callback(
                    "❌ Отменить запись", f"appt_cancel:{appointment['id']}"
                )
            ],
            [deps.make_callback("🏠 Главное меню", "main_menu")],
        ],
    )


def show_branch_select(
    chat_id: int,
    ls: str,
    deps: AppointmentDependencies,
) -> None:
    """Show active branches as the first booking step."""
    branches = deps.get_branches()
    if not branches:
        deps.send_message(chat_id, "На данный момент запись на приём недоступна.")
        deps.send_main_menu(chat_id)
        return

    state = deps.get_state(chat_id)
    state["state"] = deps.branch_state
    state["ls"] = ls
    deps.touch(state)

    rows = [
        [deps.make_callback(f"📍 {branch['name']}", f"appt_branch:{branch['id']}")]
        for branch in branches
    ]
    rows.append([deps.make_callback("❌ Отмена", "main_menu")])
    deps.send_buttons(chat_id, "Выберите филиал:", rows)


def show_date_select(
    chat_id: int,
    branch_id: int,
    deps: AppointmentDependencies,
) -> None:
    """Show up to ten nearest available dates for a branch."""
    dates = deps.get_available_dates(branch_id)
    if not dates:
        deps.send_message(
            chat_id, "На выбранный филиал сейчас нет свободных дат для записи."
        )
        deps.send_main_menu(chat_id)
        return

    branch = deps.get_branch(branch_id)
    state = deps.get_state(chat_id)
    state["state"] = deps.date_state
    state["appt_branch_id"] = branch_id
    deps.touch(state)

    rows = [
        [deps.make_callback(format_date_label(date, deps), f"appt_date:{date}")]
        for date in dates[:10]
    ]
    rows.append([deps.make_callback("❌ Отмена", "main_menu")])
    deps.send_buttons(chat_id, f"Филиал: {branch['name']}\nВыберите дату:", rows)


def format_date_label(date_str: str, deps: AppointmentDependencies) -> str:
    """Format ``YYYY-MM-DD`` with the same locale-independent weekday labels."""
    parsed = deps.parse_date(date_str, "%Y-%m-%d")
    return f"{parsed.strftime('%d.%m')} ({_WEEKDAYS_SHORT[parsed.weekday()]})"


def show_time_select(
    chat_id: int,
    branch_id: int,
    slot_date: str,
    deps: AppointmentDependencies,
) -> None:
    """Show available time slots, keeping three buttons per row."""
    slots = deps.get_available_slots(branch_id, slot_date)
    if not slots:
        deps.send_message(
            chat_id,
            "На эту дату свободных слотов не осталось. Выберите другую дату.",
        )
        deps.show_date_select(chat_id, branch_id)
        return

    state = deps.get_state(chat_id)
    state["state"] = deps.time_state
    state["appt_date"] = slot_date
    deps.touch(state)

    rows: list[list[Button]] = []
    row: list[Button] = []
    for index, slot in enumerate(slots, 1):
        row.append(deps.make_callback(slot, f"appt_time:{slot}"))
        if index % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([deps.make_callback("❌ Отмена", "main_menu")])
    deps.send_buttons(
        chat_id,
        f"Дата: {format_date_label(slot_date, deps)}\nВыберите время:",
        rows,
    )


def ask_theme(
    chat_id: int,
    slot_time: str,
    deps: AppointmentDependencies,
) -> None:
    """Store the selected slot and ask for an optional appointment topic."""
    state = deps.get_state(chat_id)
    state["state"] = deps.theme_state
    state["appt_time"] = slot_time
    deps.touch(state)

    deps.send_buttons(
        chat_id,
        "Укажите тему обращения (необязательно) — так сотрудник сможет заранее подготовиться.\n\n"
        "Напишите тему текстом или нажмите «Пропустить».",
        [[deps.make_callback("⏭ Пропустить", "appt_skip_theme")]],
    )


def show_appointment_confirm(
    chat_id: int,
    deps: AppointmentDependencies,
) -> None:
    """Render the complete booking before it is persisted."""
    state = deps.get_state(chat_id)
    branch = deps.get_branch(state["appt_branch_id"])
    theme = state.get("appt_theme")
    theme_line = f"\nТема: {theme}" if theme else ""

    state["state"] = deps.confirm_state
    deps.touch(state)

    deps.send_buttons(
        chat_id,
        f"Проверьте данные записи:\n\n"
        f"📍 {branch['name']}\n"
        f"🏠 {branch['address']}\n"
        f"📅 {format_date_label(state['appt_date'], deps)}\n"
        f"🕐 {state['appt_time']}"
        f"{theme_line}",
        [
            [deps.make_callback("✅ Подтвердить запись", "appt_confirm")],
            [deps.make_callback("❌ Отмена", "main_menu")],
        ],
    )


def finalize_appointment(chat_id: int, deps: AppointmentDependencies) -> None:
    """Persist the selected appointment and finish the flow."""
    state = deps.get_state(chat_id)
    appointment_id, error = deps.create_appointment(
        ls=state["ls"],
        branch_id=state["appt_branch_id"],
        slot_date=state["appt_date"],
        slot_time=state["appt_time"],
        channel="max",
        chat_id=chat_id,
        theme=state.get("appt_theme"),
    )

    account = state.get("ls")
    deps.clear_flow(state)
    deps.touch(state)

    if error:
        deps.send_message(chat_id, f"⚠️ {error}")
        deps.logger.warning("Запись не создана: ls=%s  ошибка=%s", account, error)
    else:
        deps.send_message(
            chat_id, "✅ Вы успешно записаны на приём! Напомним о визите заранее."
        )
        deps.logger.info("Запись создана: id=%s  chat_id=%s", appointment_id, chat_id)

    deps.send_main_menu(chat_id)


def cancel_own_appointment(
    chat_id: int,
    appointment_id: int,
    deps: AppointmentDependencies,
) -> None:
    """Cancel an active appointment only when it belongs to this MAX chat."""
    try:
        appointment = deps.get_appointment(appointment_id)
        if (
            not appointment
            or appointment["status"] != "active"
            or appointment["chat_id"] != chat_id
        ):
            deps.send_message(chat_id, "Запись не найдена или уже отменена.")
            deps.send_main_menu(chat_id)
            return

        deps.cancel_appointment(appointment_id, "client", "Отменено клиентом через бот")
    except deps.database_errors as exc:
        deps.logger.error(
            "Не удалось отменить запись id=%s chat_id=%s: %s",
            appointment_id,
            chat_id,
            exc,
        )
        deps.send_message(chat_id, "⚠️ Не удалось отменить запись. Попробуйте позже.")
        deps.send_main_menu(chat_id)
        return

    deps.send_message(chat_id, "Запись отменена.")
    deps.send_main_menu(chat_id)


def select_date(
    chat_id: int,
    state: State,
    date: str,
    deps: AppointmentDependencies,
) -> None:
    """Handle an appointment date callback."""
    branch_id = state.get("appt_branch_id")
    if not branch_id:
        deps.send_main_menu(chat_id)
        return
    deps.show_time_select(chat_id, branch_id, date)


def skip_theme(
    chat_id: int,
    state: State,
    deps: AppointmentDependencies,
) -> None:
    """Handle the optional-theme skip callback."""
    state["appt_theme"] = None
    deps.touch(state)
    deps.show_appointment_confirm(chat_id)


def confirm(
    chat_id: int,
    state: State,
    deps: AppointmentDependencies,
) -> None:
    """Finalize only callbacks belonging to the confirmation step."""
    if state.get("state") == deps.confirm_state:
        deps.finalize_appointment(chat_id)


def on_theme(
    chat_id: int,
    state: State,
    text: str,
    deps: AppointmentDependencies,
) -> None:
    """Store a typed topic, applying the existing 200-character limit."""
    state["appt_theme"] = text[:200]
    deps.touch(state)
    deps.show_appointment_confirm(chat_id)
