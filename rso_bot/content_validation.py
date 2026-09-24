"""Neutral validation for user-configured MAX button content."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import idna

MAX_LINK_URL_LENGTH = 2048
MAX_BUTTON_TEXT_LENGTH = 128


def validate_button_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Текст кнопки должен быть строкой")  # noqa: TRY004
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise ValueError("Некорректный текст кнопки")
    label = value.strip()
    if not label or len(label) > MAX_BUTTON_TEXT_LENGTH:
        raise ValueError("Некорректный текст кнопки")
    return label


def validate_link_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Некорректная ссылка")  # noqa: TRY004
    unsafe = "\\" in value or any(
        character.isspace() or unicodedata.category(character) in {"Cc", "Cf"}
        for character in value
    )
    if (
        not value or value != value.strip() or unsafe
        or len(value) > MAX_LINK_URL_LENGTH
        or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.IGNORECASE)
    ):
        raise ValueError("Некорректная ссылка")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Некорректная ссылка") from exc
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
    ):
        raise ValueError("Разрешены только обычные ссылки http/https без логина и пароля")
    expected_port = 80 if parsed.scheme == "http" else 443
    if port not in {None, expected_port}:
        raise ValueError("Для ссылки разрешён только стандартный порт")
    raw_host = parsed.hostname
    if "%" in raw_host or raw_host.endswith(".") or "_" in raw_host:
        raise ValueError("Некорректное имя сайта")
    try:
        normalized_host = ipaddress.ip_address(raw_host).compressed
    except ValueError:
        try:
            normalized_host = idna.encode(raw_host, uts46=True, std3_rules=True).decode("ascii").lower()
        except idna.IDNAError as exc:
            raise ValueError("Некорректное имя сайта") from exc
    if not normalized_host or len(normalized_host) > 253:
        raise ValueError("Некорректное имя сайта")
    host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    return urlunsplit((parsed.scheme, host + (f":{port}" if port is not None else ""), parsed.path, parsed.query, parsed.fragment))


def validate_optional_link(url: Any, text: Any) -> tuple[str | None, str | None]:
    if url in {None, ""}:
        if text not in {None, ""}:
            raise ValueError("Укажите адрес ссылки или очистите текст кнопки")
        return None, None
    return validate_link_url(url), validate_button_text(text or "Открыть сайт")
