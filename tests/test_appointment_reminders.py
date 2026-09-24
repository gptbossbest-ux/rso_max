from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, call
from zoneinfo import ZoneInfo

import pytest

import bot
import database as db
from rso_bot.jobs import appointment_reminders

MOSCOW = ZoneInfo("Europe/Moscow")
FIXED_NOW = datetime(2026, 9, 17, 12, 30, tzinfo=MOSCOW)


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
        retry_backoff=Mock(side_effect=lambda failed_attempt: float(failed_attempt)),
        sleep=Mock(),
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


def test_false_delivery_is_retried_then_marked_once_after_success():
    deps = _dependencies()
    deps.send_message.side_effect = [False, True]
    appointment_reminders.task_appointment_reminder_24h(deps)
    assert deps.send_message.call_count == 2
    deps.mark_reminded.assert_called_once_with(7, "24h")
    deps.retry_backoff.assert_called_once_with(1)
    deps.sleep.assert_called_once_with(1.0)


def test_all_false_stops_at_max_attempts_and_does_not_mark():
    deps = _dependencies()
    deps.send_message.return_value = False
    appointment_reminders.task_appointment_reminder_day(deps)
    assert deps.send_message.call_count == 3
    deps.mark_reminded.assert_not_called()
    assert deps.retry_backoff.call_args_list == [call(1), call(2)]
    assert deps.sleep.call_args_list == [call(1.0), call(2.0)]


def test_custom_attempt_limit_and_negative_backoff_are_honoured():
    deps = _dependencies()
    deps.send_message.return_value = False
    object.__setattr__(deps, "max_delivery_attempts", 2)
    deps.retry_backoff.side_effect = None
    deps.retry_backoff.return_value = -10
    appointment_reminders.task_appointment_reminder_day(deps)
    assert deps.send_message.call_count == 2
    deps.sleep.assert_called_once_with(0.0)
    deps.mark_reminded.assert_not_called()


def test_send_exception_is_retried_then_success_is_marked():
    deps = _dependencies()
    deps.send_message.side_effect = [RuntimeError("temporary failure"), True]
    appointment_reminders.task_appointment_reminder_day(deps)
    assert deps.send_message.call_count == 2
    deps.mark_reminded.assert_called_once_with(7, "day")
    deps.sleep.assert_called_once_with(1.0)
    assert "appointment_id=%s chat_id=%s" in deps.logger.error.call_args.args[0]
    assert deps.logger.error.call_args.args[2:4] == (7, 49)


def test_delivery_failure_for_one_record_does_not_block_later_record():
    deps = _dependencies([_appointment(7), _appointment(8)])
    deps.send_message.side_effect = [False, False, False, True]
    appointment_reminders.task_appointment_reminder_24h(deps)
    assert deps.send_message.call_count == 4
    assert deps.mark_reminded.call_args_list == [call(8, "24h")]


def test_mark_exception_is_not_retried_and_does_not_block_later_record():
    deps = _dependencies([_appointment(7), _appointment(8)])
    deps.mark_reminded.side_effect = [RuntimeError("uncertain commit"), None]
    appointment_reminders.task_appointment_reminder_day(deps)
    assert deps.send_message.call_count == 2
    assert deps.mark_reminded.call_args_list == [call(7, "day"), call(8, "day")]
    assert "возможен дубль" in deps.logger.error.call_args_list[0].args[0]
    assert deps.logger.error.call_args_list[0].args[2:4] == (7, 49)


def test_legacy_bot_wrapper_still_accepts_no_arguments(monkeypatch):
    records = [_appointment()]
    query = Mock(return_value=records)
    sender = Mock(return_value=True)
    marker = Mock()
    monkeypatch.setattr(bot.db, "get_appointments_for_reminder_24h", query)
    monkeypatch.setattr(bot, "send_message", sender)
    monkeypatch.setattr(bot.db, "mark_reminded", marker)
    bot._task_appointment_reminder_24h()
    assert query.call_count == 1
    assert query.call_args.args[0].tzinfo == MOSCOW
    sender.assert_called_once()
    marker.assert_called_once_with(7, "24h")
    assert bot._format_appointment_reminder(records[0], "завтра") == (
        appointment_reminders.format_appointment_reminder(records[0], "завтра")
    )


def test_bot_adapter_injects_explicit_moscow_now_into_database(monkeypatch):
    query = Mock(return_value=[])
    monkeypatch.setattr(bot.db, "get_appointments_for_reminder_24h", query)
    deps = bot._appointment_reminder_dependencies(now=lambda: FIXED_NOW)
    appointment_reminders.task_appointment_reminder_24h(deps)
    query.assert_called_once_with(FIXED_NOW)


def test_real_sqlite_row_is_processed_and_marked():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE due (
            id INTEGER, chat_id INTEGER, branch_name TEXT, branch_address TEXT,
            slot_date TEXT, slot_time TEXT, theme TEXT
        )"""
    )
    connection.execute(
        "INSERT INTO due VALUES (7, 49, 'Офис', 'Адрес', '2026-09-18', '12:30', NULL)"
    )
    row = connection.execute("SELECT * FROM due").fetchone()
    deps = _dependencies([])
    deps.get_appointments_for_reminder_24h.return_value = [row]
    appointment_reminders.task_appointment_reminder_24h(deps)
    deps.send_message.assert_called_once()
    deps.mark_reminded.assert_called_once_with(7, "24h")
    connection.close()


def test_malformed_row_does_not_block_later_sqlite_row():
    class BrokenRow:
        def __getitem__(self, key):
            raise RuntimeError(f"broken {key}")

    valid = _appointment(8)
    deps = _dependencies([])
    deps.get_appointments_for_reminder_day.return_value = [BrokenRow(), valid]
    appointment_reminders.task_appointment_reminder_day(deps)
    deps.send_message.assert_called_once()
    deps.mark_reminded.assert_called_once_with(8, "day")
    assert deps.logger.error.call_args_list[0].args[2:4] == ("unknown", "unknown")


def test_database_24h_window_converts_explicit_now_to_moscow(monkeypatch):
    cursor = Mock()
    cursor.fetchall.return_value = []
    connection = Mock()
    connection.execute.return_value = cursor
    monkeypatch.setattr(db, "get_conn", Mock(return_value=connection))
    non_moscow_now = datetime(2026, 9, 17, 4, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert db.get_appointments_for_reminder_24h(non_moscow_now) == []
    query, parameters = connection.execute.call_args.args
    assert "BETWEEN ? AND ?" in query
    assert parameters == ("2026-09-18 11:30", "2026-09-18 13:30")
    connection.close.assert_called_once_with()


def test_naive_database_now_is_interpreted_as_moscow_wall_time():
    naive = datetime.fromisoformat("2026-09-17 12:30")
    converted = db._as_moscow_time(naive)
    assert converted.replace(tzinfo=None) == naive
    assert converted.tzinfo == MOSCOW


def test_database_24h_window_includes_boundaries_and_applies_created_cutoff(
    monkeypatch,
):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE branches (id INTEGER PRIMARY KEY, name TEXT, address TEXT);
        CREATE TABLE appointments (
            id INTEGER PRIMARY KEY, branch_id INTEGER, chat_id INTEGER,
            status TEXT, reminded_24h INTEGER, slot_date TEXT, slot_time TEXT,
            theme TEXT, created_at TEXT
        );
        INSERT INTO branches VALUES (1, 'Офис', 'Адрес');
        INSERT INTO appointments VALUES
            (1, 1, 101, 'active', 0, '2026-09-18', '11:30', NULL, '2026-09-17 11:30'),
            (2, 1, 102, 'active', 0, '2026-09-18', '13:30', NULL, '2026-09-17 13:30'),
            (3, 1, 103, 'active', 0, '2026-09-18', '11:29', NULL, '2026-09-17 10:00'),
            (4, 1, 104, 'active', 0, '2026-09-18', '13:31', NULL, '2026-09-17 10:00'),
            (5, 1, 105, 'active', 0, '2026-09-18', '12:30', NULL, '2026-09-17 12:31'),
            (6, 1, 106, 'cancelled', 0, '2026-09-18', '12:30', NULL, '2026-09-17 10:00'),
            (7, 1, 107, 'active', 1, '2026-09-18', '12:30', NULL, '2026-09-17 10:00');
        """
    )
    connection.commit()

    class ConnectionWithoutClose:
        def execute(self, *args, **kwargs):
            return connection.execute(*args, **kwargs)

        def close(self):
            pass

    monkeypatch.setattr(db, "get_conn", ConnectionWithoutClose)
    rows = db.get_appointments_for_reminder_24h(FIXED_NOW)
    assert [row["id"] for row in rows] == [1, 2]
    connection.close()


def test_database_day_query_excludes_past_and_equal_slots(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE branches (id INTEGER PRIMARY KEY, name TEXT, address TEXT);
        CREATE TABLE appointments (
            id INTEGER PRIMARY KEY, branch_id INTEGER, chat_id INTEGER,
            status TEXT, reminded_day INTEGER, slot_date TEXT, slot_time TEXT,
            theme TEXT
        );
        INSERT INTO branches VALUES (1, 'Офис', 'Адрес');
        INSERT INTO appointments VALUES
            (1, 1, 101, 'active', 0, '2026-09-17', '11:59', NULL),
            (2, 1, 102, 'active', 0, '2026-09-17', '12:00', NULL),
            (3, 1, 103, 'active', 0, '2026-09-17', '12:01', NULL),
            (4, 1, 104, 'active', 0, '2026-09-18', '10:00', NULL);
        """
    )
    connection.commit()

    class ConnectionWithoutClose:
        def execute(self, *args, **kwargs):
            return connection.execute(*args, **kwargs)

        def close(self):
            pass

    monkeypatch.setattr(db, "get_conn", ConnectionWithoutClose)
    rows = db.get_appointments_for_reminder_day(FIXED_NOW.replace(minute=0))
    assert [row["id"] for row in rows] == [3]
    connection.close()


def test_moscow_zone_has_fixed_utc_plus_three_across_seasons():
    winter = datetime(2026, 1, 15, 12, tzinfo=MOSCOW)
    summer = datetime(2026, 7, 15, 12, tzinfo=MOSCOW)
    assert winter.utcoffset() == timedelta(hours=3)
    assert summer.utcoffset() == timedelta(hours=3)


def test_database_reminder_queries_keep_legacy_no_argument_calls(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert getattr(tz, "key", None) == "Europe/Moscow"
            return cls(2026, 9, 17, 12, 30, tzinfo=tz)

    cursor = Mock()
    cursor.fetchall.return_value = []
    connection = Mock()
    connection.execute.return_value = cursor
    monkeypatch.setattr(db, "datetime", FixedDateTime)
    monkeypatch.setattr(db, "get_conn", Mock(return_value=connection))
    assert db.get_appointments_for_reminder_24h() == []
    assert db.get_appointments_for_reminder_day() == []
