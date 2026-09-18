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

SendRaw = Callable[[int, dict[str, Any]], bool]


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
