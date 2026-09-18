"""Receipt delivery flow for the MAX bot.

All filesystem, transport, account and session collaborators are supplied by
the entry point.  The module therefore remains independent from ``bot.py`` and
can be exercised without reading real customer documents or calling MAX.

The upload URL returned by MAX is intentionally treated as a trusted,
provider-controlled pre-signed endpoint.  If the configured MAX API itself is
compromised it could still redirect an upload to an unintended host; enforcing
a local host allow-list would break the provider's dynamic upload contract.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


@dataclass(frozen=True)
class ReceiptUploadDependencies:
    """Filesystem and MAX API dependencies for uploading one PDF receipt."""

    open_receipt: Callable[[str], AbstractContextManager[BinaryIO]]
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


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def validate_receipt_account(account: str) -> str:
    """Validate an account as one safe, portable filename stem.

    The billing API accepts identifiers such as ``TEST-LS-001``.  Keep those
    identifiers compatible while rejecting Unicode confusables and every
    character with path or alternate-data-stream semantics.
    """
    if (
        re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account, flags=re.ASCII) is None
        or account.upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise FileNotFoundError
    return account


def _open_posix_receipt(root: Path, filename: str) -> BinaryIO:
    """Atomically open a regular file relative to a non-symlink directory."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC

    root_fd = -1
    file_fd = -1
    try:
        root_fd = os.open(root, directory_flags)
        file_fd = os.open(filename, file_flags, dir_fd=root_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise FileNotFoundError
        document = os.fdopen(file_fd, "rb")
        file_fd = -1  # ownership transferred to the Python file object
        return document
    except (OSError, ValueError):
        raise FileNotFoundError from None
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _open_windows_receipt(root: Path, filename: str) -> BinaryIO:
    """Open a non-reparse regular file while preventing root/file replacement."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    get_information.restype = wintypes.BOOL
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_final_path.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    share_read = 0x00000001
    share_write = 0x00000002
    open_existing = 3
    attribute_directory = 0x00000010
    attribute_reparse_point = 0x00000400
    flag_backup_semantics = 0x02000000
    flag_open_reparse_point = 0x00200000
    flag_sequential_scan = 0x08000000
    invalid_handle = wintypes.HANDLE(-1).value

    def final_path(handle: int) -> str:
        length = get_final_path(handle, None, 0, 0)
        if not length:
            raise FileNotFoundError
        buffer = ctypes.create_unicode_buffer(length + 1)
        written = get_final_path(handle, buffer, len(buffer), 0)
        if not written or written >= len(buffer):
            raise FileNotFoundError
        return os.path.normcase(os.path.normpath(buffer.value))

    root_path = Path(os.path.abspath(root))
    root_handle = create_file(
        str(root_path),
        0,
        share_read | share_write,  # deliberately omit FILE_SHARE_DELETE
        None,
        open_existing,
        flag_backup_semantics | flag_open_reparse_point,
        None,
    )
    file_handle = invalid_handle
    try:
        root_info = FileInformation()
        if root_handle == invalid_handle or not get_information(
            root_handle, ctypes.byref(root_info)
        ):
            raise FileNotFoundError
        if not root_info.dwFileAttributes & attribute_directory:
            raise FileNotFoundError
        if root_info.dwFileAttributes & attribute_reparse_point:
            raise FileNotFoundError

        # Holding root_handle without FILE_SHARE_DELETE prevents the directory
        # from being replaced between its validation and this file open.
        file_handle = create_file(
            str(root_path / filename),
            generic_read,
            share_read,  # prevent rename/delete until the upload closes it
            None,
            open_existing,
            flag_open_reparse_point | flag_sequential_scan,
            None,
        )
        file_info = FileInformation()
        if file_handle == invalid_handle or not get_information(
            file_handle, ctypes.byref(file_info)
        ):
            raise FileNotFoundError
        if file_info.dwFileAttributes & (attribute_directory | attribute_reparse_point):
            raise FileNotFoundError
        # Confirm the handle actually belongs to the directory handle we kept
        # locked, including when an ancestor contains a junction/reparse point.
        if os.path.dirname(final_path(file_handle)) != final_path(root_handle):
            raise FileNotFoundError

        descriptor = msvcrt.open_osfhandle(
            int(file_handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
        file_handle = invalid_handle  # ownership transferred to descriptor
        return os.fdopen(descriptor, "rb")
    except (OSError, ValueError):
        raise FileNotFoundError from None
    finally:
        if file_handle != invalid_handle:
            close_handle(file_handle)
        if root_handle != invalid_handle:
            close_handle(root_handle)


def open_local_receipt(root: Path, account: str) -> BinaryIO:
    """Validate and securely open one receipt without a check/open race."""
    filename = f"{validate_receipt_account(account)}.pdf"
    if os.name == "nt":
        return _open_windows_receipt(root, filename)
    return _open_posix_receipt(root, filename)


def send_pdf(chat_id: int, account: str, deps: ReceiptUploadDependencies) -> None:
    """Upload a receipt PDF and send its attachment token to the MAX chat."""
    try:
        with deps.open_receipt(account) as document:
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

            # httpx streams file objects instead of buffering the full
            # receipt in application memory.
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
        sent = deps.send_raw(
            chat_id,
            {
                "text": f"Квитанция по ЛС {account}:",
                "attachments": [{"type": "file", "payload": {"token": token}}],
            },
        )
        if not sent:
            deps.logger.error("PDF: MAX не подтвердил отправку вложения")
            deps.send_message(
                chat_id, "Не удалось отправить квитанцию. Попробуйте позже."
            )
            return
        deps.logger.info("PDF-квитанция отправлена")
    except FileNotFoundError:
        deps.send_message(chat_id, f"Квитанция для ЛС {account} не найдена.")
    except Exception as exc:  # noqa: BLE001 - injected adapters may be non-standard
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
