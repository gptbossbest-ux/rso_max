"""Account binding and 1C authorization conversation flow.

The module deliberately receives all infrastructure and continuation callbacks
from the entry point.  This keeps it independent from :mod:`bot` and preserves
the runtime patch points used by existing integrations and tests.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from rso_bot.states import S

Session = dict[str, Any]
Continuation = Callable[[int, str], None]
AttemptRegistry = MutableMapping[int, dict[str, Any]]

# Default process-local storage.  ``bot._auth_attempts`` remains an alias for
# compatibility and may still be rebound by tests or embedding applications.
auth_attempts: dict[int, dict[str, Any]] = {}


class LsValidation(Enum):
    """Typed result that keeps backend failures distinct from invalid accounts."""

    VALID = "valid"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"

    def __bool__(self) -> bool:
        """Preserve the legacy truthiness contract for existing callers."""
        return self is LsValidation.VALID


@dataclass(frozen=True)
class AccountDependencies:
    """Infrastructure used to read and persist a bound account."""

    get_state: Callable[[int], Session]
    get_bot_user: Callable[[int], Any]
    upsert_bot_user: Callable[..., None]
    get_ls: Callable[[str], Any]
    integration_enabled: bool
    logger: logging.Logger


@dataclass(frozen=True)
class BruteForceDependencies:
    """Mutable attempt registry plus the policy controlling it."""

    attempts: AttemptRegistry
    now: Callable[[], datetime]
    max_attempts: int
    block_minutes: int


@dataclass(frozen=True)
class AuthFlowDependencies:
    """Runtime callbacks used by the account authorization state machine."""

    get_state: Callable[[int], Session]
    touch: Callable[[Session], Session]
    clear_flow: Callable[[Session], None]
    send_message: Callable[[int, str], Any]
    send_main_menu: Callable[..., None]
    validate_ls: Callable[[str], bool | LsValidation]
    check_ls_brute: Callable[[int], str | None]
    fail_ls: Callable[[int], str]
    reset_ls_brute: Callable[[int], None]
    save_ls: Callable[..., None]
    start_1c_auth: Callable[[int, str | None], None]
    request_1c_auth_code: Callable[[str, int], tuple[dict | None, str | None]]
    verify_1c_auth_code: Callable[[str, int, str], tuple[dict | None, str | None]]
    continuations: dict[str, Continuation]
    integration_enabled: bool
    logger: logging.Logger


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read dict/sqlite row values without assuming ``.get`` support."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def get_saved_ls(chat_id: int, deps: AccountDependencies) -> str | None:
    """Return a session or persisted account that is valid for current mode."""
    state = deps.get_state(chat_id)
    if state.get("ls") and (not deps.integration_enabled or state.get("authorized_1c")):
        return str(state["ls"])

    try:
        row = deps.get_bot_user(chat_id)
    except Exception:  # noqa: BLE001 - DB adapter boundary must fail closed
        deps.logger.error("Не удалось получить сохранённый лицевой счёт")
        return None

    ls = _row_value(row, "ls") if row is not None else None
    authorized = bool(_row_value(row, "authorized_1c", False))
    if not ls or (deps.integration_enabled and not authorized):
        return None

    state["ls"] = ls
    state["fio"] = _row_value(row, "fio")
    state["authorized_1c"] = authorized
    return str(ls)


def save_ls(
    chat_id: int,
    ls: str,
    fio: str | None,
    deps: AccountDependencies,
) -> None:
    """Save the account in the process session and persistent user table."""
    state = deps.get_state(chat_id)
    missing = object()
    previous = {
        "ls": state.get("ls", missing),
        "authorized_1c": state.get("authorized_1c", missing),
        "fio": state.get("fio", missing),
    }
    previous_ls = state.get("ls")
    if previous_ls is None:
        persisted = deps.get_bot_user(chat_id)
        previous_ls = _row_value(persisted, "ls") if persisted is not None else None
    account_changed = previous_ls is not None and str(previous_ls) != ls

    state["ls"] = ls
    state["authorized_1c"] = deps.integration_enabled
    if fio:
        state["fio"] = fio
    elif account_changed:
        state.pop("fio", None)
    try:
        deps.upsert_bot_user(
            chat_id,
            ls,
            fio or "",
            authorized_1c=deps.integration_enabled,
            clear_fio=account_changed and not fio,
        )
    except Exception:
        for key, value in previous.items():
            if value is missing:
                state.pop(key, None)
            else:
                state[key] = value
        raise


def validate_ls(ls_number: str, deps: AccountDependencies) -> LsValidation:
    """Return a typed result without treating backend failures as bad input."""
    try:
        if deps.get_ls(ls_number) is not None:
            return LsValidation.VALID
        return LsValidation.INVALID
    except Exception:  # noqa: BLE001 - DB adapter boundary must fail closed
        deps.logger.error("Не удалось проверить лицевой счёт")
        return LsValidation.UNAVAILABLE


def check_ls_brute(chat_id: int, deps: BruteForceDependencies) -> str | None:
    """Return a block message when manual account lookup is rate limited."""
    info = deps.attempts.get(chat_id, {"attempts": 0, "blocked_until": None})
    blocked_until = info.get("blocked_until")
    now = deps.now()
    if blocked_until and now < blocked_until:
        remaining = int((blocked_until - now).total_seconds() / 60) + 1
        return f"Слишком много неудачных попыток.\nПопробуйте через {remaining} мин."
    if blocked_until:
        deps.attempts.pop(chat_id, None)
    return None


def fail_ls(chat_id: int, deps: BruteForceDependencies) -> str:
    """Record an invalid manual account lookup and return its user message."""
    info = deps.attempts.setdefault(chat_id, {"attempts": 0, "blocked_until": None})
    info["attempts"] += 1
    left = deps.max_attempts - info["attempts"]
    if info["attempts"] >= deps.max_attempts:
        info["blocked_until"] = deps.now() + timedelta(minutes=deps.block_minutes)
        info["attempts"] = 0
        return f"Превышено число попыток. Введите ЛС через {deps.block_minutes} мин."
    return f"Лицевой счёт не найден. Осталось попыток: {left}"


def reset_ls_brute(chat_id: int, deps: BruteForceDependencies) -> None:
    deps.attempts.pop(chat_id, None)


def request_ls(chat_id: int, after: str, deps: AuthFlowDependencies) -> None:
    """Request an account and remember which flow should continue afterward."""
    if deps.integration_enabled:
        deps.start_1c_auth(chat_id, after)
        return
    state = deps.get_state(chat_id)
    state["state"] = S.AWAIT_LS
    state["after_ls"] = after
    deps.touch(state)
    deps.send_message(chat_id, "Введите номер вашего лицевого счёта:")


def start_1c_auth(
    chat_id: int,
    after: str | None,
    deps: AuthFlowDependencies,
) -> None:
    """Start two-step account authorization through the published 1C API."""
    state = deps.get_state(chat_id)
    if after:
        state.pop("pending_1c_ls", None)
        state.pop("after_ls", None)
    else:
        deps.clear_flow(state)
    state["state"] = S.AWAIT_LS_1C
    if after:
        state["after_1c_auth"] = after
    deps.touch(state)
    deps.send_message(chat_id, "Введите номер вашего лицевого счёта:")


def _service_failure(chat_id: int, state: Session, deps: AuthFlowDependencies) -> None:
    deps.send_message(
        chat_id,
        "⚠️ Сервис авторизации временно недоступен. Попробуйте позже.",
    )
    deps.clear_flow(state)
    deps.send_main_menu(chat_id)


def on_await_ls(
    chat_id: int,
    state: Session,
    text: str,
    deps: AuthFlowDependencies,
) -> None:
    """Validate a manually entered account and resume its deferred flow."""
    block_message = deps.check_ls_brute(chat_id)
    if block_message:
        deps.send_message(chat_id, block_message)
        return

    validation = deps.validate_ls(text)
    if validation is LsValidation.UNAVAILABLE:
        deps.send_message(
            chat_id,
            "⚠️ Сервис временно недоступен. Попробуйте позже.",
        )
        return
    if not validation:
        deps.send_message(chat_id, deps.fail_ls(chat_id))
        deps.send_message(chat_id, "Введите номер лицевого счёта повторно:")
        return

    deps.reset_ls_brute(chat_id)
    try:
        deps.save_ls(chat_id, text)
    except Exception:  # noqa: BLE001 - persistence callback is an app boundary
        deps.logger.error("Не удалось сохранить подтверждённый лицевой счёт")
        deps.send_message(
            chat_id, "⚠️ Не удалось сохранить лицевой счёт. Попробуйте позже."
        )
        return

    after = state.pop("after_ls", None)
    state["state"] = S.MENU
    deps.touch(state)
    action = deps.continuations.get(after) if isinstance(after, str) else None
    if action:
        action(chat_id, text)
        return

    deps.logger.warning(
        "after_ls не задан или неизвестен: %r chat_id=%s", after, chat_id
    )
    deps.send_message(chat_id, "✅ Лицевой счёт сохранён.")
    deps.send_main_menu(chat_id)


def on_await_ls_1c(
    chat_id: int,
    state: Session,
    text: str,
    deps: AuthFlowDependencies,
) -> None:
    """Request a one-time authorization code for the entered account."""
    ls = text.strip()
    if not ls:
        deps.send_message(chat_id, "Введите номер лицевого счёта.")
        return
    try:
        data, error = deps.request_1c_auth_code(ls, chat_id)
    except Exception:  # noqa: BLE001 - API callback is an app boundary
        deps.logger.error("Ошибка запроса к сервису авторизации 1С")
        _service_failure(chat_id, state, deps)
        return
    if error or not isinstance(data, dict):
        _service_failure(chat_id, state, deps)
        return

    status_value = data.get("status")
    status = status_value if isinstance(status_value, str) else None
    message_value = data.get("message")
    message = (
        message_value
        if isinstance(message_value, str) and message_value
        else "Не удалось запросить код."
    )
    if status == "ok":
        state["pending_1c_ls"] = ls
        state["state"] = S.AWAIT_CODE_1C
        deps.touch(state)
        deps.send_message(chat_id, message)
        return
    deps.send_message(chat_id, message)
    deps.clear_flow(state)
    deps.send_main_menu(chat_id)


def on_await_code_1c(
    chat_id: int,
    state: Session,
    text: str,
    deps: AuthFlowDependencies,
) -> None:
    """Verify a one-time code without retaining or logging the secret value."""
    ls = state.get("pending_1c_ls")
    if not ls:
        deps.clear_flow(state)
        deps.send_main_menu(chat_id, "Начнём авторизацию заново.")
        return
    try:
        data, error = deps.verify_1c_auth_code(str(ls), chat_id, text.strip())
    except Exception:  # noqa: BLE001 - API callback is an app boundary
        deps.logger.error("Ошибка проверки кода авторизации 1С")
        _service_failure(chat_id, state, deps)
        return
    if error or not isinstance(data, dict):
        _service_failure(chat_id, state, deps)
        return

    status_value = data.get("status")
    status = status_value if isinstance(status_value, str) else None
    message_value = data.get("message")
    message = (
        message_value
        if isinstance(message_value, str) and message_value
        else "Не удалось проверить код."
    )
    if status == "wrong_code":
        deps.send_message(chat_id, message)
        return
    if status == "ok":
        after = state.pop("after_1c_auth", None)
        try:
            deps.save_ls(chat_id, str(ls))
        except Exception:  # noqa: BLE001 - persistence callback is an app boundary
            deps.logger.error("Не удалось сохранить авторизованный лицевой счёт")
            deps.send_message(
                chat_id,
                "⚠️ Не удалось сохранить лицевой счёт. Попробуйте позже.",
            )
            deps.clear_flow(state)
            deps.send_main_menu(chat_id)
            return
        state.pop("pending_1c_ls", None)
        state["state"] = S.MENU
        deps.touch(state)
        deps.send_message(chat_id, message)
        action = deps.continuations.get(after) if isinstance(after, str) else None
        if action:
            action(chat_id, str(ls))
        else:
            deps.clear_flow(state)
            deps.send_main_menu(chat_id)
        return
    deps.send_message(chat_id, message)
    deps.clear_flow(state)
    deps.send_main_menu(chat_id)
