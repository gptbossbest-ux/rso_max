"""Low-level transport helpers for the MAX Bot API.

The module deliberately receives configuration and collaborators from the
caller. This keeps it independent from the legacy ``bot.py`` entry point and
makes the network contract testable without performing real HTTP requests.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

SendRaw = Callable[[int, dict[str, Any]], bool]
MAX_LINK_URL_LENGTH = 2048
MAX_LINK_TEXT_LENGTH = 128


def validate_link_url(value: str) -> str:
    """Return a safe absolute HTTP(S) URL accepted by a MAX link button."""
    value = (value or "").strip()
    if (
        not value
        or len(value) > MAX_LINK_URL_LENGTH
        or any(ord(character) <= 32 for character in value)
    ):
        raise ValueError("Ссылка должна содержать не более 2048 символов")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Некорректная ссылка") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        raise ValueError("Разрешены только обычные ссылки http/https без логина и пароля")
    return value


def make_link_button(text: str, url: str) -> dict[str, str]:
    """Build the documented MAX inline-keyboard link button."""
    label = (text or "").strip()
    if not label or len(label) > MAX_LINK_TEXT_LENGTH:
        raise ValueError("Текст кнопки должен содержать от 1 до 128 символов")
    return {"type": "link", "text": label, "url": validate_link_url(url)}


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
