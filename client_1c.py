"""Синхронный HTTPS-клиент FastAPI → 1С с локальным mock-режимом."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx

from config import (
    INTEGRATION_1C_AUTH_TIMEOUT_SECONDS,
    INTEGRATION_1C_AUTH_TOKEN,
    INTEGRATION_1C_BASE_URL,
    INTEGRATION_1C_CODE_MAX_ATTEMPTS,
    INTEGRATION_1C_CODE_TTL_MINUTES,
    INTEGRATION_1C_MOCK,
    INTEGRATION_1C_MOCK_CODE,
    INTEGRATION_1C_SYNC_TIMEOUT_SECONDS,
)

log = logging.getLogger("rso.client_1c")


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


class Mock1CBackend:
    """Детерминированная имитация 1С для разработки без опубликованных методов."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        code: str = "000000",
        ttl_minutes: int = 10,
        max_attempts: int = 5,
    ) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._code = code
        self._ttl = timedelta(minutes=ttl_minutes)
        self._max_attempts = max_attempts
        self._pending: dict[tuple[str, int], dict] = {}
        self._sync_results: dict[str, dict] = {}
        self._lock = threading.Lock()

    def request_auth_code(self, ls: str, chat_id: int) -> dict:
        with self._lock:
            self._pending[(ls, chat_id)] = {
                "created_at": self._now(),
                "attempts": 0,
                "blocked": False,
            }
        return {"status": "ok", "message": "Код отправлен на электронную почту"}

    def verify_auth_code(self, ls: str, chat_id: int, code: str) -> dict:
        key = (ls, chat_id)
        with self._lock:
            pending = self._pending.get(key)
            if pending is None or self._now() - pending["created_at"] > self._ttl:
                return {"status": "expired_code", "message": "Срок действия кода истёк"}
            if pending["blocked"]:
                return {"status": "attempts_exceeded", "message": "Число попыток исчерпано"}
            if code != self._code:
                pending["attempts"] += 1
                if pending["attempts"] >= self._max_attempts:
                    pending["blocked"] = True
                    return {"status": "attempts_exceeded", "message": "Число попыток исчерпано"}
                return {"status": "wrong_code", "message": "Код неверный"}

            return {
                "status": "ok",
                "message": "Авторизация выполнена",
                "meters": [
                    {
                        "meter_number": "MOCK-001",
                        "resource_type": "Электроэнергия",
                        "meter_type": "Однотарифный",
                    },
                    {
                        "meter_number": "MOCK-002",
                        "resource_type": "Электроэнергия",
                        "meter_type": "Двухтарифный",
                    },
                ],
            }

    def sync_readings(self, batch_id: str, readings: list[dict]) -> dict:
        with self._lock:
            cached = self._sync_results.get(batch_id)
            if cached is not None:
                return cached
            statuses = []
            for reading in readings:
                statuses.append({
                    "ls": reading["ls"],
                    "meter_number": reading["meter_number"],
                    "value1": reading["value1"],
                    "value2": reading.get("value2"),
                    "submitted_at": reading["submitted_at"],
                    "status": "accepted",
                })
            result = {
                "batch_id": batch_id,
                "readings_status": statuses,
                "meters_changes": [],
            }
            self._sync_results[batch_id] = result
            return result


_mock_backend = Mock1CBackend(
    code=INTEGRATION_1C_MOCK_CODE,
    ttl_minutes=INTEGRATION_1C_CODE_TTL_MINUTES,
    max_attempts=INTEGRATION_1C_CODE_MAX_ATTEMPTS,
)


def _post_json(path: str, body: dict, timeout: int) -> tuple[dict | None, str | None]:
    if not INTEGRATION_1C_BASE_URL or not INTEGRATION_1C_AUTH_TOKEN:
        log.error("1С-запрос %s не выполнен: отсутствует обязательная конфигурация", path)
        return None, "configuration_error"

    try:
        response = httpx.post(
            _join_url(INTEGRATION_1C_BASE_URL, path),
            headers={"Authorization": f"Bearer {INTEGRATION_1C_AUTH_TOKEN}"},
            json=body,
            timeout=timeout,
        )
    except httpx.TimeoutException:
        log.warning("1С-запрос %s: timeout", path)
        return None, "timeout"
    except Exception as exc:
        # Текст исключения может содержать URL или секреты из стороннего кода.
        log.error("1С-запрос %s: ошибка соединения (%s)", path, type(exc).__name__)
        return None, "connection_error"

    if response.status_code == 401:
        log.error("1С-запрос %s: авторизация отклонена", path)
        return None, "unauthorized"
    if response.status_code in (408, 504):
        log.warning("1С-запрос %s: timeout HTTP %s", path, response.status_code)
        return None, "timeout"
    if response.status_code != 200:
        log.error("1С-запрос %s: HTTP %s", path, response.status_code)
        return None, "http_error"

    try:
        data = response.json()
    except ValueError:
        log.error("1С-запрос %s: ответ не является JSON", path)
        return None, "invalid_response"
    if not isinstance(data, dict):
        log.error("1С-запрос %s: JSON верхнего уровня не объект", path)
        return None, "invalid_response"
    return data, None


def request_auth_code(ls: str, chat_id: int) -> tuple[dict | None, str | None]:
    if INTEGRATION_1C_MOCK:
        return _mock_backend.request_auth_code(ls, chat_id), None
    return _post_json(
        "/auth/request-code",
        {"ls": ls, "chat_id": chat_id},
        INTEGRATION_1C_AUTH_TIMEOUT_SECONDS,
    )


def verify_auth_code(ls: str, chat_id: int, code: str) -> tuple[dict | None, str | None]:
    if INTEGRATION_1C_MOCK:
        return _mock_backend.verify_auth_code(ls, chat_id, code), None
    return _post_json(
        "/auth/verify-code",
        {"ls": ls, "chat_id": chat_id, "code": code},
        INTEGRATION_1C_AUTH_TIMEOUT_SECONDS,
    )


def sync_readings(batch_id: str, readings: list[dict]) -> tuple[dict | None, str | None]:
    if INTEGRATION_1C_MOCK:
        return _mock_backend.sync_readings(batch_id, readings), None
    return _post_json(
        "/readings/sync",
        {"batch_id": batch_id, "readings": readings},
        INTEGRATION_1C_SYNC_TIMEOUT_SECONDS,
    )
