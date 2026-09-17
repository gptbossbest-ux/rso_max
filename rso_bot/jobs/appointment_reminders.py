"""Appointment reminder jobs, independent from the bot entry point."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

Appointment = Mapping[str, Any]


@dataclass(frozen=True)
class AppointmentReminderDependencies:
    """Database and delivery operations required by reminder jobs."""

    get_appointments_for_reminder_24h: Callable[[], Sequence[Appointment]]
    get_appointments_for_reminder_day: Callable[[], Sequence[Appointment]]
    send_message: Callable[[int, str], bool]
    mark_reminded: Callable[[int, str], None]
    logger: logging.Logger


def format_appointment_reminder(appointment: Appointment, when_label: str) -> str:
    """Build the common text used by both appointment reminder jobs."""
    theme_line = f"\nТема: {appointment['theme']}" if appointment["theme"] else ""
    return (
        f"⏰ Напоминаем: {when_label} у вас запись на приём.\n\n"
        f"📍 {appointment['branch_name']}\n"
        f"🏠 {appointment['branch_address']}\n"
        f"📅 {appointment['slot_date']}  🕐 {appointment['slot_time']}"
        f"{theme_line}"
    )


def task_appointment_reminder_24h(deps: AppointmentReminderDependencies) -> None:
    """Send reminders for appointments in the existing 23–25 hour DB window."""
    try:
        appointments = deps.get_appointments_for_reminder_24h()
    except Exception as exc:
        deps.logger.error(
            "APScheduler appointment_reminder_24h: ошибка чтения БД: %s", exc
        )
        return

    for appointment in appointments:
        ok = deps.send_message(
            appointment["chat_id"],
            format_appointment_reminder(appointment, "завтра"),
        )
        if not ok:
            deps.logger.warning(
                "Напоминание за 24ч не доставлено: appointment_id=%s  chat_id=%s",
                appointment["id"],
                appointment["chat_id"],
            )
        # Keep the established no-retry policy: even a failed delivery is marked.
        deps.mark_reminded(appointment["id"], "24h")

    if appointments:
        deps.logger.info(
            "APScheduler appointment_reminder_24h: обработано %d записей",
            len(appointments),
        )


def task_appointment_reminder_day(deps: AppointmentReminderDependencies) -> None:
    """Send reminders for appointments due today."""
    try:
        appointments = deps.get_appointments_for_reminder_day()
    except Exception as exc:
        deps.logger.error(
            "APScheduler appointment_reminder_day: ошибка чтения БД: %s", exc
        )
        return

    for appointment in appointments:
        ok = deps.send_message(
            appointment["chat_id"],
            format_appointment_reminder(appointment, "сегодня"),
        )
        if not ok:
            deps.logger.warning(
                "Напоминание в день приёма не доставлено: appointment_id=%s  chat_id=%s",
                appointment["id"],
                appointment["chat_id"],
            )
        # Keep the established no-retry policy: even a failed delivery is marked.
        deps.mark_reminded(appointment["id"], "day")

    if appointments:
        deps.logger.info(
            "APScheduler appointment_reminder_day: обработано %d записей",
            len(appointments),
        )
