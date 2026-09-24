"""Low-level transport helpers for the MAX Bot API.

The module deliberately receives configuration and collaborators from the
caller. This keeps it independent from the legacy ``bot.py`` entry point and
makes the network contract testable without performing real HTTP requests.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx

from rso_bot.content_validation import (
    MAX_BUTTON_TEXT_LENGTH,
    validate_button_text,
    validate_link_url,
)

SendRaw = Callable[[int, dict[str, Any]], bool]
MAX_LINK_TEXT_LENGTH = MAX_BUTTON_TEXT_LENGTH
MAX_CALLBACK_PAYLOAD_LENGTH = 1024
MAX_KEYBOARD_ROWS = 30
MAX_KEYBOARD_BUTTONS = 210
MAX_BUTTONS_PER_ROW = 7
MAX_RESTRICTED_BUTTONS_PER_ROW = 3
RESTRICTED_ROW_TYPES = frozenset({
    "link", "open_app", "request_contact", "request_geo_location",
})


def make_link_button(text: str, url: str) -> dict[str, str]:
    """Build the documented MAX inline-keyboard link button."""
    label = validate_button_text(text)
    return {"type": "link", "text": label, "url": validate_link_url(url)}


def make_callback_button(text: str, payload: str) -> dict[str, str]:
    """Build a validated callback button without interpreting its label."""
    label = validate_button_text(text)
    if (
        not isinstance(payload, str) or not payload
        or len(payload.encode("utf-8")) > MAX_CALLBACK_PAYLOAD_LENGTH
    ):
        raise ValueError("Callback-кнопка должна содержать payload")
    return {"type": "callback", "text": label, "payload": payload}


def validate_inline_keyboard(buttons: Any) -> list[list[dict[str, Any]]]:
    """Validate documented MAX keyboard limits without altering callbacks."""
    if not isinstance(buttons, list) or not buttons or len(buttons) > MAX_KEYBOARD_ROWS:
        raise ValueError("Клавиатура должна содержать от 1 до 30 рядов")
    total = 0
    for row in buttons:
        if not isinstance(row, list) or not row or len(row) > MAX_BUTTONS_PER_ROW:
            raise ValueError("Ряд клавиатуры должен содержать от 1 до 7 кнопок")
        total += len(row)
        if total > MAX_KEYBOARD_BUTTONS:
            raise ValueError("Клавиатура содержит больше 210 кнопок")
        restricted = any(
            isinstance(button, dict) and button.get("type") in RESTRICTED_ROW_TYPES
            for button in row
        )
        if restricted and len(row) > MAX_RESTRICTED_BUTTONS_PER_ROW:
            raise ValueError("Ряд со специальной кнопкой может содержать не более 3 кнопок")
        for button in row:
            if not isinstance(button, dict):
                raise ValueError("Некорректная кнопка")  # noqa: TRY004
            button_type = button.get("type")
            validate_button_text(button.get("text"))
            if button_type == "callback":
                payload = button.get("payload")
                if (
                    not isinstance(payload, str) or not payload
                    or len(payload.encode("utf-8")) > MAX_CALLBACK_PAYLOAD_LENGTH
                ):
                    raise ValueError("Callback-кнопка должна содержать payload")
            elif button_type == "link":
                validate_link_url(button.get("url"))
            elif button_type not in RESTRICTED_ROW_TYPES:
                raise ValueError("Неподдерживаемый тип кнопки")
    return buttons


def send_raw(
    api_url: str,
    headers: dict[str, str],
    chat_id: int,
    body: dict[str, Any],
    *,
    http_client: Any = httpx,
    logger: logging.Logger,
) -> bool:
    """Send a raw message payload to MAX and report whether it was accepted."""
    try:
        response = http_client.post(
            f"{api_url}/messages",
            headers=headers,
            params={"chat_id": chat_id},
            json=body,
            timeout=5,
        )
        if response.status_code != 200:
            logger.warning("MAX API %s для chat_id=%s", response.status_code, chat_id)
            return False
        return True
    # Preserve the entry point's fail-closed boundary: a bot reply must not
    # terminate update processing even if a custom HTTP client fails oddly.
    except Exception as exc:  # noqa: BLE001
        logger.error("_send_raw chat_id=%s: %s", chat_id, exc)
        return False


def send_message(chat_id: int, text: str, *, sender: SendRaw) -> bool:
    """Send a plain-text MAX message through the supplied raw sender."""
    return sender(chat_id, {"text": text})


def send_buttons(
    chat_id: int,
    text: str,
    buttons: list[list[dict[str, Any]]],
    *,
    sender: SendRaw,
) -> bool:
    """Send an inline keyboard preserving MAX's nested button row format."""
    validate_inline_keyboard(buttons)
    return sender(
        chat_id,
        {
            "text": text,
            "attachments": [
                {
                    "type": "inline_keyboard",
                    "payload": {"buttons": buttons},
                }
            ],
        },
    )


def ack_callback(
    api_url: str,
    headers: dict[str, str],
    callback_id: str,
    *,
    http_client: Any = httpx,
    logger: logging.Logger,
) -> None:
    """Acknowledge a MAX callback, swallowing transport errors as before."""
    try:
        http_client.post(
            f"{api_url}/answers",
            headers=headers,
            json={"callback_id": callback_id, "notification": ""},
            timeout=3,
        )
    # Callback acknowledgement is best-effort and must never abort handling.
    except Exception as exc:  # noqa: BLE001
        logger.debug("_ack_callback %s: %s", callback_id, exc)
