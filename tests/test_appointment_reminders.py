from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock, call

import pytest

import bot
import database as db
from rso_bot.jobs import appointment_reminders


def _appointment(appointment_id: int = 7, *, theme: str | None = "Перерасчёт") -> dict:
    return {
        "id": appointment_id,
        "chat_id": 42 + appointment_id,
        "branch_name": "Центральный офис",
        "branch_address": "ул. Ленина, 1",
        "slot_date": "2026-09-21",
        "slot_time": "10:30",
        "theme": theme,
    }


def _dependencies(
    appointments: list[dict] | None = None,
) -> appointment_reminders.AppointmentReminderDependencies:
    records = appointments if appointments is not None else [_appointment()]
    return appointment_reminders.AppointmentReminderDependencies(
        get_appointments_for_reminder_24h=Mock(return_value=records),
        get_appointments_for_reminder_day=Mock(return_value=records),
        send_message=Mock(return_value=True),
        mark_reminded=Mock(),
        logger=Mock(),
    )


def test_format_reminder_preserves_text_with_and_without_theme():
    assert appointment_reminders.format_appointment_reminder(
        _appointment(), "завтра"
    ) == (
        "⏰ Напоминаем: завтра у вас запись на приём.\n\n"
        "📍 Центральный офис\n"
        "🏠 ул. Ленина, 1\n"
        "📅 2026-09-21  🕐 10:30\n"
        "Тема: Перерасчёт"
    )
    assert appointment_reminders.format_appointment_reminder(
        _appointment(theme=None), "сегодня"
    ).endswith("📅 2026-09-21  🕐 10:30")


@pytest.mark.parametrize(
    ("task", "query_name", "when_label", "reminder_type", "log_label"),
    [
        (
            appointment_reminders.task_appointment_reminder_24h,
            "get_appointments_for_reminder_24h",
            "завтра",
            "24h",
            "appointment_reminder_24h",
        ),
        (
            appointment_reminders.task_appointment_reminder_day,
            "get_appointments_for_reminder_day",
            "сегодня",
            "day",
            "appointment_reminder_day",
        ),
    ],
)
def test_reminder_tasks_send_mark_and_log_each_due_record(
    task, query_name, when_label, reminder_type, log_label
):
    records = [_appointment(7), _appointment(8, theme=None)]
    deps = _dependencies(records)

    task(deps)

    getattr(deps, query_name).assert_called_once_with()
    assert deps.send_message.call_args_list == [
        call(
            record["chat_id"],
            appointment_reminders.format_appointment_reminder(record, when_label),
        )
        for record in records
    ]
    assert deps.mark_reminded.call_args_list == [
        call(7, reminder_type),
        call(8, reminder_type),
    ]
    deps.logger.info.assert_called_once_with(
        f"APScheduler {log_label}: обработано %d записей", 2
    )


@pytest.mark.parametrize(
    "task",
    [
        appointment_reminders.task_appointment_reminder_24h,
        appointment_reminders.task_appointment_reminder_day,
    ],
)
def test_no_due_records_performs_no_delivery_or_summary_log(task):
    deps = _dependencies([])

    task(deps)

    deps.send_message.assert_not_called()
    deps.mark_reminded.assert_not_called()
    deps.logger.info.assert_not_called()


@pytest.mark.parametrize(
    ("task", "query_name", "log_label"),
    [
        (
            appointment_reminders.task_appointment_reminder_24h,
            "get_appointments_for_reminder_24h",
            "appointment_reminder_24h",
        ),
        (
            appointment_reminders.task_appointment_reminder_day,
            "get_appointments_for_reminder_day",
            "appointment_reminder_day",
        ),
    ],
)
def test_database_read_error_is_contained_and_logged(task, query_name, log_label):
    deps = _dependencies()
    error = RuntimeError("database unavailable")
    getattr(deps, query_name).side_effect = error

    task(deps)

    deps.logger.error.assert_called_once_with(
        f"APScheduler {log_label}: ошибка чтения БД: %s", error
    )
    deps.send_message.assert_not_called()
    deps.mark_reminded.assert_not_called()


def test_failed_delivery_is_marked_and_does_not_block_later_records():
    """Characterize the legacy no-retry behavior documented in bot.py."""
    records = [_appointment(7), _appointment(8)]
    deps = _dependencies(records)
    deps.send_message.side_effect = [False, True]

    appointment_reminders.task_appointment_reminder_24h(deps)

    assert deps.send_message.call_count == 2
    assert deps.mark_reminded.call_args_list == [call(7, "24h"), call(8, "24h")]
    deps.logger.warning.assert_called_once_with(
        "Напоминание за 24ч не доставлено: appointment_id=%s  chat_id=%s",
        7,
        49,
    )


def test_unexpected_send_or_mark_exception_preserves_legacy_propagation():
    send_failure = _dependencies([_appointment(7), _appointment(8)])
    send_failure.send_message.side_effect = RuntimeError("transport contract broken")
    with pytest.raises(RuntimeError, match="transport contract broken"):
        appointment_reminders.task_appointment_reminder_day(send_failure)
    send_failure.mark_reminded.assert_not_called()

    mark_failure = _dependencies([_appointment(7), _appointment(8)])
    mark_failure.mark_reminded.side_effect = RuntimeError("write failed")
    with pytest.raises(RuntimeError, match="write failed"):
        appointment_reminders.task_appointment_reminder_day(mark_failure)
    mark_failure.send_message.assert_called_once()


def test_legacy_wrappers_resolve_runtime_dependencies(monkeypatch):
    records = [_appointment()]
    query = Mock(return_value=records)
    sender = Mock(return_value=True)
    marker = Mock()
    monkeypatch.setattr(bot.db, "get_appointments_for_reminder_24h", query)
    monkeypatch.setattr(bot, "send_message", sender)
    monkeypatch.setattr(bot.db, "mark_reminded", marker)

    bot._task_appointment_reminder_24h()

    query.assert_called_once_with()
    sender.assert_called_once()
    marker.assert_called_once_with(7, "24h")
    assert bot._format_appointment_reminder(records[0], "завтра") == (
        appointment_reminders.format_appointment_reminder(records[0], "завтра")
    )


def test_database_24h_window_keeps_inclusive_23_to_25_hour_boundaries(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is None
            return cls(2026, 9, 17, 12, 30)

    cursor = Mock()
    cursor.fetchall.return_value = []
    connection = Mock()
    connection.execute.return_value = cursor
    monkeypatch.setattr(db, "datetime", FixedDateTime)
    monkeypatch.setattr(db, "get_conn", Mock(return_value=connection))

    assert db.get_appointments_for_reminder_24h() == []

    query, parameters = connection.execute.call_args.args
    assert "BETWEEN ? AND ?" in query
    assert parameters == ("2026-09-18 11:30", "2026-09-18 13:30")
    connection.close.assert_called_once_with()


def test_database_day_query_uses_configured_timezone_date(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is not None
            assert tz.utcoffset(None) == timedelta(hours=db.TIMEZONE_OFFSET)
            return cls(2026, 9, 18, 0, 5, tzinfo=tz)

    cursor = Mock()
    cursor.fetchall.return_value = []
    connection = Mock()
    connection.execute.return_value = cursor
    monkeypatch.setattr(db, "datetime", FixedDateTime)
    monkeypatch.setattr(db, "get_conn", Mock(return_value=connection))

    assert db.get_appointments_for_reminder_day() == []

    assert connection.execute.call_args.args[1] == ("2026-09-18",)
    connection.close.assert_called_once_with()
