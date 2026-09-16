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
    if not isinstance(result, dict) or result.get("batch_id") != batch_id:
        return False
    statuses = result.get("readings_status")
    changes = result.get("meters_changes")
    if not isinstance(statuses, list) or not isinstance(changes, list):
        return False
    if any(item.get("status") not in {"accepted", "rejected"} for item in statuses):
        return False
    if Counter(_reading_key(item) for item in statuses) != Counter(
        _reading_key(item) for item in readings
    ):
        return False
    for change in changes:
        if change.get("action") not in {"added", "changed", "removed"}:
            return False
        if not change.get("ls") or not change.get("meter_number"):
            return False
        if change["action"] != "removed" and change.get("meter_type") not in {
            "Однотарифный", "Двухтарифный"
        }:
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

    updated = db.apply_1c_reading_statuses(batch_id, result["readings_status"])
    db.apply_1c_meter_changes(result["meters_changes"])
    db.complete_1c_sync_batch(batch_id, claim["attempt_at"])
    for reading in updated:
        _notify_reading(reading)
    log.info(
        "Синхронизация 1С завершена: batch_id=%s, показаний=%d, изменений=%d",
        batch_id,
        len(readings),
        len(result["meters_changes"]),
    )
    return "success"
