"""Low-level transport helpers for the MAX Bot API.

The module deliberately receives configuration and collaborators from the
caller. This keeps it independent from the legacy ``bot.py`` entry point and
makes the network contract testable without performing real HTTP requests.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import unicodedata
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import idna

SendRaw = Callable[[int, dict[str, Any]], bool]
MAX_LINK_URL_LENGTH = 2048
MAX_LINK_TEXT_LENGTH = 128
MAX_KEYBOARD_ROWS = 30
MAX_KEYBOARD_BUTTONS = 210
MAX_BUTTONS_PER_ROW = 7
MAX_RESTRICTED_BUTTONS_PER_ROW = 3
RESTRICTED_ROW_TYPES = frozenset({
    "link", "open_app", "request_contact", "request_geo_location",
})


def _has_unsafe_text_characters(value: str) -> bool:
    return bool(
        "\\" in value
        or any(
            character.isspace()
            or unicodedata.category(character) in {"Cc", "Cf"}
            for character in value
        )
    )


def _validate_button_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Текст кнопки должен быть строкой")  # noqa: TRY004
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise ValueError("Некорректный текст кнопки")
    label = value.strip()
    if not label or len(label) > MAX_LINK_TEXT_LENGTH:
        raise ValueError("Некорректный текст кнопки")
    return label


def validate_link_url(value: str) -> str:
    """Return a safe absolute HTTP(S) URL accepted by a MAX link button."""
    if not isinstance(value, str):
        raise ValueError("Некорректная ссылка")  # noqa: TRY004
    if value != value.strip() or _has_unsafe_text_characters(value):
        raise ValueError("Некорректная ссылка")
    if (
        not value
        or len(value) > MAX_LINK_URL_LENGTH
        or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.IGNORECASE)
    ):
        raise ValueError("Ссылка должна содержать не более 2048 символов")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Некорректная ссылка") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Разрешены только обычные ссылки http/https без логина и пароля")
    expected_port = 80 if parsed.scheme == "http" else 443
    if port not in {None, expected_port}:
        raise ValueError("Для ссылки разрешён только стандартный порт")
    raw_host = parsed.hostname
    if "%" in raw_host or raw_host.endswith(".") or "_" in raw_host:
        raise ValueError("Некорректное имя сайта")
    try:
        address = ipaddress.ip_address(raw_host)
    except ValueError:
        try:
            normalized_host = idna.encode(
                raw_host, uts46=True, std3_rules=True,
            ).decode("ascii").lower()
        except idna.IDNAError as exc:
            raise ValueError("Некорректное имя сайта") from exc
    else:
        normalized_host = address.compressed
    if not normalized_host or len(normalized_host) > 253:
        raise ValueError("Некорректное имя сайта")
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    netloc = normalized_host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def make_link_button(text: str, url: str) -> dict[str, str]:
    """Build the documented MAX inline-keyboard link button."""
    label = _validate_button_text(text)
    return {"type": "link", "text": label, "url": validate_link_url(url)}


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
            _validate_button_text(button.get("text"))
            if button_type == "callback":
                if not isinstance(button.get("payload"), str) or not button["payload"]:
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
