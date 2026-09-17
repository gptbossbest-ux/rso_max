"""Защищённые внутренние маршруты авторизации через 1С."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

import client_1c
import database as db
from api.deps import verify_token
from api.schemas import Integration1CAuthRequest, Integration1CVerifyRequest

log = logging.getLogger("rso.api.integration_1c")

router = APIRouter(
    prefix="/integrations/1c",
    tags=["integration-1c"],
    dependencies=[Depends(verify_token)],
)

_REQUEST_STATUSES = {"ok", "ls_not_found", "no_contact"}
_VERIFY_STATUSES = {"ok", "wrong_code", "expired_code", "attempts_exceeded"}


def _raise_transport_error(error: str | None) -> None:
    if error == "timeout":
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Сервис 1С временно недоступен",
        )
    if error == "configuration_error":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Интеграция с 1С не настроена",
        )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Некорректный ответ сервиса 1С",
    )


def _validated_result(data: dict | None, error: str | None, allowed: set[str]) -> dict:
    if error or data is None:
        _raise_transport_error(error)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("status"), str)
        or data["status"] not in allowed
        or not isinstance(data.get("message"), str)
    ):
        log.error("1С вернула неизвестный бизнес-статус")
        _raise_transport_error("invalid_response")
    return data


def _validated_meters(result: dict) -> list[dict]:
    """Validate the complete nested success payload before any database write."""
    meters = result.get("meters")
    if not isinstance(meters, list):
        _raise_transport_error("invalid_response")
    for meter in meters:
        if (
            not isinstance(meter, dict)
            or not isinstance(meter.get("meter_number"), str)
            or not meter["meter_number"]
            or not isinstance(meter.get("resource_type"), str)
            or not meter["resource_type"]
            or not isinstance(meter.get("meter_type"), str)
            or meter["meter_type"] not in {"Однотарифный", "Двухтарифный"}
        ):
            log.error("1С вернула некорректный список счётчиков")
            _raise_transport_error("invalid_response")
    return meters


@router.post("/auth/request-code")
def request_code(payload: Integration1CAuthRequest) -> dict:
    data, error = client_1c.request_auth_code(payload.ls, payload.chat_id)
    result = _validated_result(data, error, _REQUEST_STATUSES)
    log.info("Запрошен код авторизации в 1С: chat_id=%s", payload.chat_id)
    return result


@router.post("/auth/verify-code")
def verify_code(payload: Integration1CVerifyRequest) -> dict:
    data, error = client_1c.verify_auth_code(payload.ls, payload.chat_id, payload.code)
    result = _validated_result(data, error, _VERIFY_STATUSES)
    if result["status"] == "ok":
        meters = _validated_meters(result)
        try:
            db.upsert_1c_meters(payload.ls, meters)
        except ValueError:
            log.error("1С вернула некорректный список счётчиков")
            _raise_transport_error("invalid_response")
        db.upsert_bot_user(payload.chat_id, payload.ls, "", authorized_1c=True)
        log.info("Авторизация через 1С успешна: chat_id=%s", payload.chat_id)
    else:
        log.info("Проверка кода 1С завершена: status=%s chat_id=%s", result["status"], payload.chat_id)
    return result
