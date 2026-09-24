"""Appointment reminder jobs, independent from the bot entry point."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

Appointment = Mapping[str, Any]
MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


def moscow_now() -> datetime:
    """Return the current timezone-aware Moscow time."""
    return datetime.now(MOSCOW_TIMEZONE)


def _default_retry_backoff(failed_attempt: int) -> float:
    """Return a short bounded linear delay after a failed delivery attempt."""
    return float(failed_attempt)


@dataclass(frozen=True)
class AppointmentReminderDependencies:
    """Database and delivery operations required by reminder jobs."""

    get_appointments_for_reminder_24h: Callable[[], Sequence[Appointment]]
    get_appointments_for_reminder_day: Callable[[], Sequence[Appointment]]
    send_message: Callable[[int, str], bool]
    mark_reminded: Callable[[int, str], None]
    logger: logging.Logger
    max_delivery_attempts: int = 3
    retry_backoff: Callable[[int], float] = _default_retry_backoff
    sleep: Callable[[float], None] = time.sleep


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


def _send_with_retry(
    deps: AppointmentReminderDependencies,
    appointment: Appointment,
    message: str,
    reminder_label: str,
) -> bool:
    appointment_id = appointment["id"]
    chat_id = appointment["chat_id"]
    attempts = max(1, deps.max_delivery_attempts)

    for attempt in range(1, attempts + 1):
        try:
            if deps.send_message(chat_id, message):
                return True
            deps.logger.warning(
                "%s не доставлено: appointment_id=%s chat_id=%s attempt=%s/%s",
                reminder_label,
                appointment_id,
                chat_id,
                attempt,
                attempts,
            )
        except Exception as exc:  # noqa: BLE001 - isolate transport per record.
            deps.logger.error(
                "%s: ошибка доставки: appointment_id=%s chat_id=%s attempt=%s/%s: %s",
                reminder_label,
                appointment_id,
                chat_id,
                attempt,
                attempts,
                exc,
            )

        if attempt < attempts:
            deps.sleep(max(0.0, deps.retry_backoff(attempt)))

    return False


def _process_appointment(
    deps: AppointmentReminderDependencies,
    appointment: Appointment,
    *,
    when_label: str,
    reminder_type: str,
    reminder_label: str,
) -> None:
    try:
        # sqlite3.Row deliberately implements subscription, but not Mapping.get().
        # Read all required identifiers inside the per-record boundary so a broken
        # row can never abort the rest of the batch.
        appointment_id = appointment["id"]
        chat_id = appointment["chat_id"]
        message = format_appointment_reminder(appointment, when_label)
        delivered = _send_with_retry(deps, appointment, message, reminder_label)
        if not delivered:
            return
        try:
            deps.mark_reminded(appointment_id, reminder_type)
        except Exception as exc:  # noqa: BLE001 - isolate DB write per record.
            # Delivery has already happened. Retrying this write in-process risks
            # hiding an uncertain commit. Leaving it unmarked can duplicate the
            # reminder on the next job run; the error log makes that risk visible.
            deps.logger.error(
                "%s: доставка подтверждена, но отметка не сохранена; возможен "
                "дубль при следующем запуске: appointment_id=%s chat_id=%s: %s",
                reminder_label,
                appointment_id,
                chat_id,
                exc,
            )
    except Exception as exc:  # noqa: BLE001 - malformed record must not stop batch.
        appointment_id = _safe_record_value(appointment, "id")
        chat_id = _safe_record_value(appointment, "chat_id")
        deps.logger.error(
            "%s: ошибка обработки записи: appointment_id=%s chat_id=%s: %s",
            reminder_label,
            appointment_id,
            chat_id,
            exc,
        )


def _safe_record_value(appointment: object, key: str) -> object:
    """Read a diagnostic field without trusting a malformed DB record."""
    try:
        return appointment[key]  # type: ignore[index]
    except Exception:  # noqa: BLE001 - diagnostics must never break batch isolation.
        return "unknown"


def task_appointment_reminder_24h(deps: AppointmentReminderDependencies) -> None:
    """Send reminders for appointments in the existing 23–25 hour DB window."""
    try:
        appointments = deps.get_appointments_for_reminder_24h()
    except Exception as exc:  # noqa: BLE001 - scheduler boundary must contain DB failures.
        deps.logger.error(
            "APScheduler appointment_reminder_24h: ошибка чтения БД: %s", exc
        )
        return

    for appointment in appointments:
        _process_appointment(
            deps,
            appointment,
            when_label="завтра",
            reminder_type="24h",
            reminder_label="Напоминание за 24ч",
        )

    if appointments:
        deps.logger.info(
            "APScheduler appointment_reminder_24h: обработано %d записей",
            len(appointments),
        )


def task_appointment_reminder_day(deps: AppointmentReminderDependencies) -> None:
    """Send reminders for appointments due today."""
    try:
        appointments = deps.get_appointments_for_reminder_day()
    except Exception as exc:  # noqa: BLE001 - scheduler boundary must contain DB failures.
        deps.logger.error(
            "APScheduler appointment_reminder_day: ошибка чтения БД: %s", exc
        )
        return

    for appointment in appointments:
        _process_appointment(
            deps,
            appointment,
            when_label="сегодня",
            reminder_type="day",
            reminder_label="Напоминание в день приёма",
        )

    if appointments:
        deps.logger.info(
            "APScheduler appointment_reminder_day: обработано %d записей",
            len(appointments),
        )
