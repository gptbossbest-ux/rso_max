"""
api/deps.py — FastAPI-зависимости.

verify_token   — проверяет Bearer-токен для всех операторских эндпоинтов.
                 Токен задаётся в .env: INTERNAL_API_TOKEN=<секрет>.
                 Flask-портал и bot.py передают его в заголовке:
                   Authorization: Bearer <INTERNAL_API_TOKEN>

get_operator_id — извлекает operator_id из тела запроса (необязательно).
                  При отсутствии возвращает 0 (системный вызов).
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import ALLOW_INSECURE_DEV_API, API_HOST, APP_ENV, INTERNAL_API_TOKEN

log = logging.getLogger("rso.api.deps")

_bearer_scheme = HTTPBearer(auto_error=False)


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> None:
    """
    Dependency для защищённых эндпоинтов.
    Использование: добавь `Depends(verify_token)` в параметры маршрута
    или в APIRouter(dependencies=[Depends(verify_token)]).

    Без токена доступ закрыт. Единственное исключение — явно включённый
    development/test bypass на loopback-интерфейсе.
    """
    if not INTERNAL_API_TOKEN:
        if (
            ALLOW_INSECURE_DEV_API
            and APP_ENV in {"development", "test"}
            and API_HOST in {"127.0.0.1", "localhost", "::1"}
        ):
            log.warning("Включён изолированный development bypass API-аутентификации")
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Внутренняя API-аутентификация не настроена",
        )

    if credentials is None or credentials.credentials != INTERNAL_API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный или отсутствующий Bearer-токен",
            headers={"WWW-Authenticate": "Bearer"},
        )
