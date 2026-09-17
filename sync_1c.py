"""Идемпотентная фоновая синхронизация очереди показаний с 1С."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import client_1c
import database as db
from api.notifier import notify_client
from config import (
    ENABLE_1C_INTEGRATION,
    INTEGRATION_1C_BATCH_SIZE,
    INTEGRATION_1C_SYNC_PERIOD_HOURS,
    INTEGRATION_1C_SYNC_RETRY_HOURS,
    TIMEZONE_OFFSET,
)

log = logging.getLogger("rso.sync_1c")


def _now() -> datetime:
    return datetime.now(timezone(timedelta(hours=TIMEZONE_OFFSET)))


def _generate_batch_id(now: datetime) -> str:
    return f"{now:%Y%m%d-%H%M}-{uuid.uuid4().hex[:8]}"


def _reading_payload(row) -> dict:
    return {
        "chat_id": row["chat_id"],
        "ls": row["ls"],
        "meter_number": row["meter_number"],
        "value1": row["value1"],
        "value2": row["value2"],
        "submitted_at": row["created_at"],
    }


def _reading_key(item: dict) -> tuple:
    return (
        str(item.get("ls")),
        str(item.get("meter_number")),
        str(item.get("value1")),
        None if item.get("value2") is None else str(item.get("value2")),
        str(item.get("submitted_at")),
    )


def _valid_result(result: dict | None, batch_id: str, readings: list[dict]) -> bool:
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("batch_id"), str)
        or result["batch_id"] != batch_id
    ):
        return False
    statuses = result.get("readings_status")
    changes = result.get("meters_changes")
    if not isinstance(statuses, list) or not isinstance(changes, list):
        return False
    for item in statuses:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("ls"), str)
            or not item["ls"]
            or not isinstance(item.get("meter_number"), str)
            or not item["meter_number"]
            or not isinstance(item.get("value1"), str)
            or not isinstance(item.get("value2"), (str, type(None)))
            or not isinstance(item.get("submitted_at"), str)
            or not item["submitted_at"]
            or not isinstance(item.get("status"), str)
            or item["status"] not in {"accepted", "rejected"}
        ):
            return False
    if Counter(_reading_key(item) for item in statuses) != Counter(
        _reading_key(item) for item in readings
    ):
        return False
    for change in changes:
        if not isinstance(change, dict):
            return False
        action = change.get("action")
        if not isinstance(action, str) or action not in {"added", "changed", "removed"}:
            return False
        if (
            not isinstance(change.get("ls"), str)
            or not change["ls"]
            or not isinstance(change.get("meter_number"), str)
            or not change["meter_number"]
        ):
            return False
        if action != "removed" and (
            not isinstance(change.get("resource_type"), str)
            or not change["resource_type"]
            or not isinstance(change.get("meter_type"), str)
            or change["meter_type"] not in {"Однотарифный", "Двухтарифный"}
        ):
            return False
    return True


def _notify_reading(reading: dict) -> None:
    if reading.get("chat_id") == 0 and reading.get("created_at") == "2000-01-01 00:00":
        return  # Historical import baselines have no customer to notify.
    status = reading["status_1c"]
    if status == "accepted":
        text = (
            f"✅ Ваши показания по счётчику {reading['resource_type']} "
            f"№{reading['meter_number']} приняты."
        )
        buttons = None
    else:
        text = (
            f"❌ Ваши показания по счётчику {reading['resource_type']} "
            f"№{reading['meter_number']} не приняты."
        )
        buttons = [("🏠 Главное меню", "main_menu")]
    try:
        asyncio.run(notify_client("max", reading.get("chat_id"), text, buttons))
    except Exception as exc:
        log.error(
            "Не удалось уведомить MAX о статусе показания (%s)",
            type(exc).__name__,
        )


def sync_1c_job(now: datetime | None = None) -> str:
    """Выполняет суточный обмен или почасовой повтор зависшего пакета."""
    if not ENABLE_1C_INTEGRATION:
        return "disabled"

    now = now or _now()
    claim = db.claim_1c_sync_batch(
        now, _generate_batch_id(now), INTEGRATION_1C_BATCH_SIZE,
        INTEGRATION_1C_SYNC_RETRY_HOURS, INTEGRATION_1C_SYNC_PERIOD_HOURS,
    )
    if claim is None:
        return "not_due"
    batch_id = claim["batch_id"]
    rows = claim["rows"]
    first_attempt = claim["first_attempt"]

    readings = [_reading_payload(row) for row in rows]
    result, error = client_1c.sync_readings(batch_id, readings)
    if error or not _valid_result(result, batch_id, readings):
        if first_attempt:
            log.error("1С недоступна при первой синхронизации: batch_id=%s", batch_id)
        else:
            log.warning("Повтор синхронизации 1С неуспешен: batch_id=%s", batch_id)
        return "failed"

    try:
        updated = db.apply_1c_sync_result(
            batch_id,
            claim["attempt_at"],
            result["readings_status"],
            result["meters_changes"],
        )
    except ValueError:
        log.error("Ответ 1С не удалось атомарно применить: batch_id=%s", batch_id)
        return "failed"
    if updated is None:
        log.warning("Устаревший ответ 1С не применён: batch_id=%s", batch_id)
        return "failed"
    for reading in updated:
        _notify_reading(reading)
    log.info(
        "Синхронизация 1С завершена: batch_id=%s, показаний=%d, изменений=%d",
        batch_id,
        len(readings),
        len(result["meters_changes"]),
    )
    return "success"
