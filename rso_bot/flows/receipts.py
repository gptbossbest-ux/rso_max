"""Receipt delivery flow for the MAX bot.

All filesystem, transport, account and session collaborators are supplied by
the entry point.  The module therefore remains independent from ``bot.py`` and
can be exercised without reading real customer documents or calling MAX.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReceiptUploadDependencies:
    """Filesystem and MAX API dependencies for uploading one PDF receipt."""

    receipt_path: Callable[[str], Path]
    read_bytes: Callable[[Path], bytes]
    http_client: Any
    api_base: str
    headers: Mapping[str, str]
    send_raw: Callable[[int, dict[str, Any]], bool]
    send_message: Callable[[int, str], Any]
    sleep: Callable[[float], None]
    logger: logging.Logger


@dataclass(frozen=True)
class ReceiptFlowDependencies:
    """Account, session and presentation dependencies for the receipt flow."""

    get_saved_ls: Callable[[int], str | None]
    request_ls: Callable[[int, str], None]
    clear_flow: Callable[[dict[str, Any]], None]
    send_message: Callable[[int, str], Any]
    send_main_menu: Callable[[int], None]
    send_pdf: Callable[[int, str], None]
    deliver_receipt: Callable[[int, str], None]


def send_pdf(chat_id: int, account: str, deps: ReceiptUploadDependencies) -> None:
    """Upload a receipt PDF and send its attachment token to the MAX chat."""
    try:
        document_path = deps.receipt_path(account)
        document = deps.read_bytes(document_path)
    except FileNotFoundError:
        deps.send_message(chat_id, f"Квитанция для ЛС {account} не найдена.")
        return
    except OSError:
        deps.logger.error("PDF: не удалось прочитать файл квитанции")
        deps.send_message(chat_id, "Не удалось отправить квитанцию. Попробуйте позже.")
        return

    try:
        metadata_response = deps.http_client.post(
            f"{deps.api_base}/uploads",
            headers=dict(deps.headers),
            params={"type": "file"},
            timeout=10,
        )
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        if not isinstance(metadata, dict):
            raise TypeError("invalid upload metadata")
        upload_url = metadata.get("url")
        if not isinstance(upload_url, str) or not upload_url:
            raise ValueError("missing upload URL")

        upload_response = deps.http_client.post(
            upload_url,
            headers=dict(deps.headers),
            files={"data": (f"{account}.pdf", document, "application/pdf")},
            timeout=30,
        )
        upload_response.raise_for_status()
        upload_payload = upload_response.json()
        if not isinstance(upload_payload, dict):
            raise TypeError("invalid upload payload")
        token = upload_payload.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("missing attachment token")

        # MAX needs a short processing interval before the token can be used.
        deps.sleep(2)
        deps.send_raw(
            chat_id,
            {
                "text": f"Квитанция по ЛС {account}:",
                "attachments": [{"type": "file", "payload": {"token": token}}],
            },
        )
        deps.logger.info("PDF-квитанция отправлена")
    except Exception as exc:  # noqa: BLE001 - HTTP adapters may raise custom errors
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code is not None:
            deps.logger.error("PDF: MAX API вернул статус %s", status_code)
        else:
            deps.logger.error("PDF: ошибка загрузки или ответа MAX API")
        deps.send_message(chat_id, "Не удалось отправить квитанцию. Попробуйте позже.")


def deliver(chat_id: int, account: str, deps: ReceiptFlowDependencies) -> None:
    """Deliver a known account's receipt and return to the main menu."""
    deps.send_message(chat_id, "Ищу квитанцию, подождите...")
    deps.send_pdf(chat_id, account)
    deps.send_main_menu(chat_id)


def start(
    chat_id: int,
    state: dict[str, Any],
    deps: ReceiptFlowDependencies,
) -> None:
    """Start receipt delivery or request the account needed to continue it."""
    deps.clear_flow(state)
    account = deps.get_saved_ls(chat_id)
    if not account:
        deps.request_ls(chat_id, "kvitanciya")
        return
    deps.deliver_receipt(chat_id, account)
