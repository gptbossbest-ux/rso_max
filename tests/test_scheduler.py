# ruff: noqa: I001

from __future__ import annotations

from unittest.mock import Mock, call

import bot
from apscheduler.schedulers.background import BackgroundScheduler
from rso_bot import scheduler as scheduler_module


def _dependencies(
    factory: Mock | None = None,
) -> scheduler_module.SchedulerDependencies:
    return scheduler_module.SchedulerDependencies(
        auto_resolve_pending=Mock(name="auto_resolve_pending"),
        cleanup_user_states=Mock(name="cleanup_user_states"),
        appointment_reminder_24h=Mock(name="appointment_reminder_24h"),
        appointment_reminder_day=Mock(name="appointment_reminder_day"),
        scheduler_factory=factory or Mock(),
        logger=Mock(),
    )


def test_create_scheduler_preserves_timezone_jobs_and_options():
    instance = Mock()
    factory = Mock(return_value=instance)
    deps = _dependencies(factory)

    result = scheduler_module.create_scheduler(deps)

    assert result is instance
    factory.assert_called_once_with(timezone="Europe/Moscow")
    assert instance.add_job.call_args_list == [
        call(
            deps.auto_resolve_pending,
            trigger="interval",
            hours=1,
            id="auto_resolve_pending",
            max_instances=1,
            misfire_grace_time=300,
        ),
        call(
            deps.cleanup_user_states,
            trigger="interval",
            hours=1,
            id="cleanup_user_states",
            max_instances=1,
            misfire_grace_time=300,
        ),
        call(
            deps.appointment_reminder_24h,
            trigger="interval",
            hours=1,
            id="appointment_reminder_24h",
            max_instances=1,
            misfire_grace_time=300,
        ),
        call(
            deps.appointment_reminder_day,
            trigger="cron",
            hour=9,
            minute=0,
            timezone="Europe/Moscow",
            id="appointment_reminder_day",
            max_instances=1,
            misfire_grace_time=1800,
        ),
    ]
    for job_call in instance.add_job.call_args_list:
        assert "replace_existing" not in job_call.kwargs
        assert "coalesce" not in job_call.kwargs


def test_start_and_shutdown_keep_lifecycle_and_logging():
    instance = Mock()
    jobs = [Mock(id="auto_resolve_pending"), Mock(id="appointment_reminder_day")]
    instance.get_jobs.return_value = jobs
    logger = Mock()

    scheduler_module.start_scheduler(instance, logger)
    scheduler_module.shutdown_scheduler(instance, logger)

    instance.start.assert_called_once_with()
    assert instance.get_jobs.call_count == 2
    logger.info.assert_has_calls(
        [
            call("APScheduler запущен: %d задач — %s", 2, [job.id for job in jobs]),
            call("APScheduler остановлен, бот завершён"),
        ]
    )
    instance.shutdown.assert_called_once_with(wait=False)


def test_bot_factory_keeps_runtime_callback_seams(monkeypatch):
    callbacks = {
        "_task_auto_resolve_pending": Mock(),
        "_task_cleanup_user_states": Mock(),
        "_task_appointment_reminder_24h": Mock(),
        "_task_appointment_reminder_day": Mock(),
        "_task_cleanup_ai_sessions": Mock(),
    }
    for name, callback in callbacks.items():
        monkeypatch.setattr(bot, name, callback)
    factory = Mock()
    logger = Mock()
    monkeypatch.setattr(bot, "BackgroundScheduler", factory)
    monkeypatch.setattr(bot, "log", logger)

    deps = bot._scheduler_dependencies()

    assert deps.auto_resolve_pending is callbacks["_task_auto_resolve_pending"]
    assert deps.cleanup_user_states is callbacks["_task_cleanup_user_states"]
    assert deps.appointment_reminder_24h is callbacks["_task_appointment_reminder_24h"]
    assert deps.appointment_reminder_day is callbacks["_task_appointment_reminder_day"]
    assert deps.cleanup_ai_sessions is callbacks["_task_cleanup_ai_sessions"]
    assert deps.scheduler_factory is factory
    assert deps.logger is logger


def test_ai_cleanup_runs_on_scheduler_creation_and_then_hourly():
    instance = Mock()
    factory = Mock(return_value=instance)
    cleanup = Mock()
    deps = _dependencies(factory)
    deps = scheduler_module.SchedulerDependencies(
        **{**deps.__dict__, "cleanup_ai_sessions": cleanup}
    )

    scheduler_module.create_scheduler(deps)

    cleanup.assert_called_once_with()
    assert instance.add_job.call_args_list[-1] == call(
        cleanup,
        trigger="interval",
        hours=1,
        id="cleanup_ai_sessions",
        max_instances=1,
        misfire_grace_time=300,
    )


def test_operator_maintenance_runs_every_ten_seconds():
    instance = Mock()
    factory = Mock(return_value=instance)
    maintenance = Mock()
    deps = _dependencies(factory)
    deps = scheduler_module.SchedulerDependencies(
        **{**deps.__dict__, "operator_chat_maintenance": maintenance}
    )

    scheduler_module.create_scheduler(deps)

    assert instance.add_job.call_args_list[-1] == call(
        maintenance,
        trigger="interval",
        seconds=10,
        id="operator_chat_maintenance",
        max_instances=1,
        misfire_grace_time=30,
    )


def test_real_scheduler_applies_default_coalesce_and_exact_triggers():
    deps = _dependencies(BackgroundScheduler)
    instance = scheduler_module.create_scheduler(deps)

    instance.start(paused=True)
    try:
        jobs = {job.id: job for job in instance.get_jobs()}
        assert set(jobs) == {
            "auto_resolve_pending",
            "cleanup_user_states",
            "appointment_reminder_24h",
            "appointment_reminder_day",
        }
        assert str(instance.timezone) == "Europe/Moscow"
        for job in jobs.values():
            assert job.max_instances == 1
            assert job.coalesce is True
        assert jobs["auto_resolve_pending"].trigger.interval.total_seconds() == 3600
        assert jobs["cleanup_user_states"].trigger.interval.total_seconds() == 3600
        assert jobs["appointment_reminder_24h"].trigger.interval.total_seconds() == 3600
        assert jobs["appointment_reminder_day"].misfire_grace_time == 1800
        assert str(jobs["appointment_reminder_day"].trigger.timezone) == "Europe/Moscow"
        assert str(jobs["appointment_reminder_day"].trigger.fields[5]) == "9"
        assert str(jobs["appointment_reminder_day"].trigger.fields[6]) == "0"
    finally:
        instance.shutdown(wait=False)
