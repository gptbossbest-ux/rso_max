"""APScheduler construction and lifecycle for the RSO MAX bot."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

JobCallback = Callable[[], None]
SchedulerFactory = Callable[..., Any]


@dataclass(frozen=True)
class SchedulerDependencies:
    """Callbacks and runtime objects needed to configure the scheduler."""

    auto_resolve_pending: JobCallback
    cleanup_user_states: JobCallback
    appointment_reminder_24h: JobCallback
    appointment_reminder_day: JobCallback
    scheduler_factory: SchedulerFactory
    logger: logging.Logger
    cleanup_ai_sessions: JobCallback | None = None
    operator_chat_maintenance: JobCallback | None = None


def register_jobs(scheduler: Any, deps: SchedulerDependencies) -> None:
    """Register the four existing jobs without changing their schedules."""
    scheduler.add_job(
        deps.auto_resolve_pending,
        trigger="interval",
        hours=1,
        id="auto_resolve_pending",
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        deps.cleanup_user_states,
        trigger="interval",
        hours=1,
        id="cleanup_user_states",
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        deps.appointment_reminder_24h,
        trigger="interval",
        hours=1,
        id="appointment_reminder_24h",
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        deps.appointment_reminder_day,
        trigger="cron",
        hour=9,
        minute=0,
        timezone="Europe/Moscow",
        id="appointment_reminder_day",
        max_instances=1,
        misfire_grace_time=1800,
    )
    if deps.cleanup_ai_sessions is not None:
        scheduler.add_job(
            deps.cleanup_ai_sessions,
            trigger="interval",
            hours=1,
            id="cleanup_ai_sessions",
            max_instances=1,
            misfire_grace_time=300,
        )
    if deps.operator_chat_maintenance is not None:
        scheduler.add_job(
            deps.operator_chat_maintenance,
            trigger="interval",
            minutes=1,
            id="operator_chat_maintenance",
            max_instances=1,
            misfire_grace_time=60,
        )


def create_scheduler(deps: SchedulerDependencies) -> Any:
    """Create a Moscow-time scheduler and register all current bot jobs."""
    if deps.cleanup_ai_sessions is not None:
        deps.cleanup_ai_sessions()
    scheduler = deps.scheduler_factory(timezone="Europe/Moscow")
    register_jobs(scheduler, deps)
    return scheduler


def start_scheduler(scheduler: Any, logger: logging.Logger) -> None:
    """Start the scheduler and retain the existing startup log."""
    scheduler.start()
    logger.info(
        "APScheduler запущен: %d задач — %s",
        len(scheduler.get_jobs()),
        [job.id for job in scheduler.get_jobs()],
    )


def shutdown_scheduler(scheduler: Any, logger: logging.Logger) -> None:
    """Stop the scheduler without waiting for running jobs."""
    scheduler.shutdown(wait=False)
    logger.info("APScheduler остановлен, бот завершён")
