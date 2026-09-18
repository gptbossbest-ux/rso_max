"""Monitoring flow for registered MAX house chats.

The module is deliberately independent from :mod:`bot`: persistence, delivery,
JSON decoding and logging are injected by the entry point.  This keeps polling
routing thin and makes the group-chat behaviour testable without a real
database or MAX connection.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, MutableSet, Sequence
from dataclasses import dataclass
from typing import Any

APPEAL_SUGGESTION = (
    "Если вопрос не решён — напишите мне в личные сообщения, "
    "оформим обращение с отслеживанием статуса."
)

# Process-local deduplication of warnings about payloads whose type is unknown.
unknown_chat_type_warned: set[Any] = set()


@dataclass(frozen=True)
class HouseChatDependencies:
    """Persistence and delivery collaborators used by house-chat monitoring."""

    get_house_chat_by_chat_id: Callable[[str], Any]
    is_user_excluded: Callable[[int, str, str], bool]
    get_scenarios_for_chat: Callable[[int], Sequence[Any]]
    send_message: Callable[[int, str], Any]
    logger: logging.Logger
    json_loads: Callable[[str], Any] = json.loads


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _field(record: Any, key: str, default: Any = None) -> Any:
    """Read mappings as well as ``sqlite3.Row`` without importing sqlite."""
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return default


def _warning_key(chat_id: Any) -> Any:
    try:
        hash(chat_id)
    except TypeError:
        return ("unhashable", type(chat_id).__name__)
    return chat_id


def get_chat_type(
    message: Mapping[str, Any] | Any,
    logger: logging.Logger,
    warned: MutableSet[Any] | None = None,
) -> str:
    """Return MAX chat type, defaulting unknown/malformed payloads to dialog.

    Defaulting to a private dialog is fail-safe: an uncertain update continues
    through the established client flow instead of triggering a house-chat
    scenario.  The warning intentionally reports field names, not the raw
    recipient payload, so user-supplied content and tokens cannot reach logs.
    """
    message_data = _mapping(message)
    recipient = _mapping(message_data.get("recipient"))
    chat_type = recipient.get("chat_type") or message_data.get("chat_type")
    if isinstance(chat_type, str) and chat_type:
        return chat_type

    registry = unknown_chat_type_warned if warned is None else warned
    chat_id = recipient.get("chat_id")
    warning_key = _warning_key(chat_id)
    if warning_key not in registry:
        registry.add(warning_key)
        logger.warning(
            "Не удалось определить chat_type для chat_id=%s. "
            "Уточни точное имя поля в реальном payload и поправь get_chat_type(). "
            "По умолчанию считаем 'dialog' (личный чат), чтобы не сломать "
            "существующий клиентский флоу.",
            chat_id,
        )
    return "dialog"


def _scenario_keywords(scenario: Any, deps: HouseChatDependencies) -> list[str] | None:
    scenario_id = _field(scenario, "id")
    try:
        decoded = deps.json_loads(_field(scenario, "keywords"))
    except (TypeError, ValueError, json.JSONDecodeError):
        deps.logger.warning(
            "Некорректный JSON в keywords сценария id=%s — пропускаем",
            scenario_id,
        )
        return None

    if not isinstance(decoded, list) or not all(
        isinstance(keyword, str) for keyword in decoded
    ):
        deps.logger.warning(
            "Некорректный формат keywords сценария id=%s — пропускаем",
            scenario_id,
        )
        return None
    return decoded


def handle_group_message(
    message: Mapping[str, Any] | Any, deps: HouseChatDependencies
) -> None:
    """Apply the first matching active scenario for a registered group chat.

    Database and delivery exceptions deliberately propagate to the polling
    update boundary, which already logs and isolates a failed update.  This is
    the legacy failure contract and prevents later messages from being sent
    after an uncertain persistence or delivery result.
    """
    message_data = _mapping(message)
    recipient = _mapping(message_data.get("recipient"))
    chat_id = recipient.get("chat_id")
    if chat_id is None:
        return

    house_chat = deps.get_house_chat_by_chat_id(str(chat_id))
    if not house_chat:
        deps.logger.debug(
            "Сообщение из незарегистрированного группового чата chat_id=%s — игнорируем",
            chat_id,
        )
        return

    house_chat_id = _field(house_chat, "id")
    if house_chat_id is None:
        deps.logger.warning("У записи домового чата отсутствует id — пропускаем")
        return

    sender = _mapping(message_data.get("sender"))
    sender_id = sender.get("user_id")
    if sender_id is not None and deps.is_user_excluded(
        house_chat_id, "max", str(sender_id)
    ):
        deps.logger.debug(
            "Отправитель user_id=%s в списке исключений чата id=%s — игнорируем",
            sender_id,
            house_chat_id,
        )
        return

    body = _mapping(message_data.get("body"))
    raw_text = body.get("text")
    if not isinstance(raw_text, str) or not raw_text:
        return
    text = raw_text.lower()

    for scenario in deps.get_scenarios_for_chat(house_chat_id):
        keywords = _scenario_keywords(scenario, deps)
        if keywords is None:
            continue

        if not any(keyword.lower() in text for keyword in keywords if keyword):
            continue

        response_text = _field(scenario, "response_text")
        if not isinstance(response_text, str) or not response_text:
            deps.logger.warning(
                "У сценария id=%s отсутствует текст ответа — пропускаем",
                _field(scenario, "id"),
            )
            continue

        deps.send_message(chat_id, response_text)
        deps.logger.info(
            "Сценарий id=%s сработал в чате id=%s (house_chat) по ключевому слову",
            _field(scenario, "id"),
            house_chat_id,
        )
        if _field(scenario, "suggest_appeal", False):
            deps.send_message(chat_id, APPEAL_SUGGESTION)
        return
