from __future__ import annotations

import logging
import os
import threading
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

import bot
from rso_bot.flows import receipts


def response(payload: object, *, status: int = 200, url: str = "https://max.test"):
    result = MagicMock()
    result.status_code = status
    result.json.return_value = payload
    if status >= 400:
        result.raise_for_status.side_effect = httpx.HTTPStatusError(
            "request failed",
            request=httpx.Request("POST", url),
            response=httpx.Response(status, request=httpx.Request("POST", url)),
        )
    return result


def make_upload_deps(**overrides) -> receipts.ReceiptUploadDependencies:
    defaults = {
        "open_receipt": MagicMock(
            side_effect=lambda _account: BytesIO(b"%PDF-1.7\x00binary")
        ),
        "http_client": MagicMock(),
        "api_base": "https://max.test",
        "headers": {"Authorization": "secret"},
        "send_raw": MagicMock(return_value=True),
        "send_message": MagicMock(),
        "sleep": MagicMock(),
        "logger": logging.getLogger("test.receipts"),
    }
    defaults.update(overrides)
    deps = receipts.ReceiptUploadDependencies(**defaults)
    deps.http_client.post.side_effect = [
        response({"url": "https://upload.test/file"}),
        response({"token": "attachment-token"}),
    ]
    return deps


def make_flow_deps(**overrides) -> receipts.ReceiptFlowDependencies:
    defaults = {
        "get_saved_ls": MagicMock(return_value=None),
        "request_ls": MagicMock(),
        "clear_flow": MagicMock(),
        "send_message": MagicMock(),
        "send_main_menu": MagicMock(),
        "send_pdf": MagicMock(),
        "deliver_receipt": MagicMock(),
    }
    defaults.update(overrides)
    return receipts.ReceiptFlowDependencies(**defaults)


def test_start_clears_old_flow_and_requests_missing_account() -> None:
    deps = make_flow_deps()
    state = {"state": "old-flow", "appeal_body": "private"}

    receipts.start(7, state, deps)

    deps.clear_flow.assert_called_once_with(state)
    deps.get_saved_ls.assert_called_once_with(7)
    deps.request_ls.assert_called_once_with(7, "kvitanciya")
    deps.deliver_receipt.assert_not_called()


def test_start_delivers_for_saved_account() -> None:
    deps = make_flow_deps(get_saved_ls=MagicMock(return_value="100001"))
    state = {"state": "menu"}

    receipts.start(7, state, deps)

    deps.clear_flow.assert_called_once_with(state)
    deps.deliver_receipt.assert_called_once_with(7, "100001")
    deps.request_ls.assert_not_called()


def test_deliver_preserves_messages_order_and_menu() -> None:
    events: list[object] = []
    deps = make_flow_deps(
        send_message=MagicMock(side_effect=lambda *args: events.append(args)),
        send_pdf=MagicMock(side_effect=lambda *args: events.append(args)),
        send_main_menu=MagicMock(side_effect=lambda *args: events.append(args)),
    )

    receipts.deliver(7, "100001", deps)

    assert events == [
        (7, "Ищу квитанцию, подождите..."),
        (7, "100001"),
        (7,),
    ]


def test_missing_receipt_returns_exact_not_found_message() -> None:
    deps = make_upload_deps(open_receipt=MagicMock(side_effect=FileNotFoundError))

    receipts.send_pdf(7, "100001", deps)

    deps.send_message.assert_called_once_with(7, "Квитанция для ЛС 100001 не найдена.")
    deps.http_client.post.assert_not_called()


def test_read_error_reports_failure_without_calling_max() -> None:
    deps = make_upload_deps(open_receipt=MagicMock(side_effect=OSError("private path")))

    receipts.send_pdf(7, "100001", deps)

    deps.send_message.assert_called_once_with(
        7, "Не удалось отправить квитанцию. Попробуйте позже."
    )
    deps.http_client.post.assert_not_called()


def test_nonstandard_storage_exception_is_contained_without_sensitive_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "PRIVATE-STORAGE-DETAIL"
    logger = logging.getLogger("test.receipts.storage-error")
    deps = make_upload_deps(
        open_receipt=MagicMock(side_effect=RuntimeError(secret)), logger=logger
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        receipts.send_pdf(7, "100001", deps)

    deps.send_message.assert_called_once_with(
        7, "Не удалось отправить квитанцию. Попробуйте позже."
    )
    deps.http_client.post.assert_not_called()
    assert secret not in caplog.text


def test_valid_pdf_preserves_two_step_upload_and_attachment_contract() -> None:
    document = b"%PDF-1.7\x00\xffbinary"
    stream = BytesIO(document)
    deps = make_upload_deps(open_receipt=MagicMock(return_value=stream))
    uploaded_bytes = b""

    def post(url, **kwargs):
        nonlocal uploaded_bytes
        if "files" not in kwargs:
            return response({"url": "https://upload.test/file"})
        uploaded_bytes = kwargs["files"]["data"][1].read()
        return response({"token": "attachment-token"})

    deps.http_client.post.side_effect = post

    receipts.send_pdf(7, "100001", deps)

    assert deps.http_client.post.call_args_list[0] == call(
        "https://max.test/uploads",
        headers={"Authorization": "secret"},
        params={"type": "file"},
        timeout=10,
    )
    upload = deps.http_client.post.call_args_list[1]
    assert upload.args == ("https://upload.test/file",)
    assert upload.kwargs["headers"] == {"Authorization": "secret"}
    assert upload.kwargs["timeout"] == 30
    name, uploaded_stream, content_type = upload.kwargs["files"]["data"]
    assert (name, content_type) == ("100001.pdf", "application/pdf")
    assert uploaded_stream is stream
    assert uploaded_bytes == document
    assert stream.closed
    deps.sleep.assert_called_once_with(2)
    deps.send_raw.assert_called_once_with(
        7,
        {
            "text": "Квитанция по ЛС 100001:",
            "attachments": [{"type": "file", "payload": {"token": "attachment-token"}}],
        },
    )
    deps.send_message.assert_not_called()


def test_failed_attachment_send_is_not_reported_as_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.receipts.send-failure")
    deps = make_upload_deps(send_raw=MagicMock(return_value=False), logger=logger)

    with caplog.at_level(logging.INFO, logger=logger.name):
        receipts.send_pdf(7, "100001", deps)

    deps.send_message.assert_called_once_with(
        7, "Не удалось отправить квитанцию. Попробуйте позже."
    )
    assert "не подтвердил отправку" in caplog.text
    assert "PDF-квитанция отправлена" not in caplog.text


def test_large_binary_document_is_streamed_in_bounded_reads() -> None:
    class GuardedStream(BytesIO):
        def __init__(self, payload: bytes) -> None:
            super().__init__(payload)
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            assert 0 <= size <= 64 * 1024
            self.read_sizes.append(size)
            return super().read(size)

    document = bytes(range(256)) * 16_384
    stream = GuardedStream(document)
    captured = bytearray()
    deps = make_upload_deps(open_receipt=MagicMock(return_value=stream))

    def post(url, **kwargs):
        if "files" not in kwargs:
            return response({"url": "https://upload.test/file"})
        uploaded = kwargs["files"]["data"][1]
        while chunk := uploaded.read(64 * 1024):
            captured.extend(chunk)
        return response({"token": "attachment-token"})

    deps.http_client.post.side_effect = post

    receipts.send_pdf(7, "100001", deps)

    assert captured == document
    assert len(stream.read_sizes) > 1
    assert stream.closed


@pytest.mark.parametrize("failed_step", [0, 1])
def test_non_200_from_either_http_step_reports_failure(failed_step: int) -> None:
    deps = make_upload_deps()
    replies = [
        response({"url": "https://upload.test/file"}),
        response({"token": "attachment-token"}),
    ]
    replies[failed_step] = response({}, status=503)
    deps.http_client.post.side_effect = replies

    receipts.send_pdf(7, "100001", deps)

    deps.send_message.assert_called_once_with(
        7, "Не удалось отправить квитанцию. Попробуйте позже."
    )
    deps.send_raw.assert_not_called()


@pytest.mark.parametrize(
    "replies",
    [
        [response({}, status=503)],
        [response({"url": "https://upload.test/file"}), response({}, status=503)],
        [response({"url": "https://upload.test/file"}), RuntimeError("network")],
        [response({"url": "https://upload.test/file"}), response([])],
    ],
    ids=["metadata-non-200", "upload-non-200", "upload-exception", "bad-payload"],
)
def test_document_stream_is_closed_on_every_http_failure(replies: list[object]) -> None:
    stream = BytesIO(b"%PDF-1.7")
    deps = make_upload_deps(open_receipt=MagicMock(return_value=stream))
    deps.http_client.post.side_effect = replies

    receipts.send_pdf(7, "100001", deps)

    assert stream.closed
    deps.send_message.assert_called_once_with(
        7, "Не удалось отправить квитанцию. Попробуйте позже."
    )


@pytest.mark.parametrize(
    ("metadata", "payload"),
    [
        ([], {"token": "attachment-token"}),
        ({}, {"token": "attachment-token"}),
        ({"url": 123}, {"token": "attachment-token"}),
        ({"url": "https://upload.test/file"}, []),
        ({"url": "https://upload.test/file"}, {}),
        ({"url": "https://upload.test/file"}, {"token": 123}),
    ],
)
def test_malformed_max_response_is_rejected(metadata, payload) -> None:
    deps = make_upload_deps()
    deps.http_client.post.side_effect = [response(metadata), response(payload)]

    receipts.send_pdf(7, "100001", deps)

    deps.send_message.assert_called_once_with(
        7, "Не удалось отправить квитанцию. Попробуйте позже."
    )
    deps.send_raw.assert_not_called()


def test_http_exception_is_contained() -> None:
    deps = make_upload_deps()
    deps.http_client.post.side_effect = RuntimeError("network secret")

    receipts.send_pdf(7, "100001", deps)

    deps.send_message.assert_called_once_with(
        7, "Не удалось отправить квитанцию. Попробуйте позже."
    )
    deps.send_raw.assert_not_called()


def test_errors_do_not_log_account_path_token_body_or_document_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    account = "PRIVATE-LS-918273"
    path_secret = "C:/private/customer/receipt.pdf"
    token_secret = "TOKEN-564738"
    body_secret = "BODY-192837"
    bytes_secret = b"BYTES-SECRET-675849"
    logger = logging.getLogger("test.receipts.private")
    deps = make_upload_deps(
        open_receipt=MagicMock(return_value=BytesIO(bytes_secret)),
        logger=logger,
    )
    deps.http_client.post.side_effect = RuntimeError(
        f"{account} {path_secret} {token_secret} {body_secret} {bytes_secret!r}"
    )

    with caplog.at_level(logging.ERROR, logger=logger.name):
        receipts.send_pdf(7, account, deps)

    output = caplog.text
    assert account not in output
    assert path_secret not in output
    assert token_secret not in output
    assert body_secret not in output
    assert "BYTES-SECRET" not in output
    assert all(record.exc_info is None for record in caplog.records)


def test_bot_upload_wrapper_resolves_runtime_patch_points() -> None:
    delegated = MagicMock()
    fake_client = object()
    fake_logger = object()
    fake_open = MagicMock(return_value=BytesIO(b"pdf"))
    with (
        patch.object(bot.receipts, "send_pdf", delegated),
        patch.object(bot, "_open_receipt_binary", fake_open),
        patch.object(bot, "httpx", fake_client),
        patch.object(bot, "API", "https://patched.test"),
        patch.object(bot, "_MAX_HEADERS", {"Authorization": "patched"}),
        patch.object(bot, "_send_raw") as send_raw,
        patch.object(bot, "send_message") as send_message,
        patch.object(bot.time, "sleep") as sleep,
        patch.object(bot, "log", fake_logger),
    ):
        bot._send_pdf(7, "100001")

    deps = delegated.call_args.args[2]
    assert delegated.call_args.args[:2] == (7, "100001")
    assert deps.open_receipt is fake_open
    assert deps.http_client is fake_client
    assert deps.api_base == "https://patched.test"
    assert deps.headers == {"Authorization": "patched"}
    assert deps.send_raw is send_raw
    assert deps.send_message is send_message
    assert deps.sleep is sleep
    assert deps.logger is fake_logger


def test_bot_delivery_wrapper_keeps_send_pdf_patch_point() -> None:
    with (
        patch.object(bot, "send_message") as send_message,
        patch.object(bot, "_send_pdf") as send_pdf,
        patch.object(bot, "send_main_menu") as send_main_menu,
    ):
        bot._deliver_kvitanciya(7, "100001")

    send_message.assert_called_once_with(7, "Ищу квитанцию, подождите...")
    send_pdf.assert_called_once_with(7, "100001")
    send_main_menu.assert_called_once_with(7)


def test_bot_callback_wrapper_clears_flow_and_keeps_hooks() -> None:
    state = {"state": "old"}
    with (
        patch.object(bot, "_clear_flow") as clear_flow,
        patch.object(bot, "_get_saved_ls", return_value=None),
        patch.object(bot, "_request_ls") as request_ls,
    ):
        bot._cb_kvitanciya(7, state)

    clear_flow.assert_called_once_with(state)
    request_ls.assert_called_once_with(7, "kvitanciya")


def test_after_ls_receipt_continuation_keeps_legacy_wrapper() -> None:
    assert bot._AFTER_LS_ACTIONS["kvitanciya"] is bot._deliver_kvitanciya


def test_default_receipt_open_preserves_runtime_adapter() -> None:
    expected = BytesIO(b"pdf")
    with patch.object(
        bot.receipts, "open_local_receipt", return_value=expected
    ) as open_receipt:
        assert bot._open_receipt_binary("TEST-LS-001") is expected
    open_receipt.assert_called_once_with(Path("KV"), "TEST-LS-001")


@pytest.mark.parametrize(
    "account",
    [
        "",
        ".",
        "..",
        "../secret",
        "..\\secret",
        "dir/receipt",
        "/absolute",
        "C:",
        "D:",
        "C:/secret",
        "foo:bar",
        "CON",
        "con",
        "LPT1",
        "name.",
        "name ",
        "name.ext",
        "１２３４５６",
        "1" * 65,
    ],
)
def test_receipt_account_rejects_unsafe_basename(account: str) -> None:
    with pytest.raises(FileNotFoundError):
        receipts.validate_receipt_account(account)


@pytest.mark.parametrize("account", ["100001", "TEST-LS-001", "test_account-2"])
def test_receipt_open_accepts_supported_safe_accounts(
    tmp_path: Path, account: str
) -> None:
    root = tmp_path / "KV"
    root.mkdir()
    document = root / f"{account}.pdf"
    document.write_bytes(b"exact-pdf")

    with receipts.open_local_receipt(root, account) as opened:
        assert opened.read() == b"exact-pdf"


def test_receipt_open_rejects_missing_file_and_sibling_prefix(tmp_path: Path) -> None:
    root = tmp_path / "KV"
    sibling = tmp_path / "KV_evil"
    root.mkdir()
    sibling.mkdir()
    (sibling / "100001.pdf").write_bytes(b"private")

    with pytest.raises(FileNotFoundError):
        receipts.open_local_receipt(root, "100001")


def test_receipt_path_rejects_symlink_file_to_outside(tmp_path: Path) -> None:
    root = tmp_path / "KV"
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"private")
    link = root / "100001.pdf"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(FileNotFoundError):
        receipts.open_local_receipt(root, "100001")


def test_receipt_path_rejects_symlink_root_to_outside(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "100001.pdf").write_bytes(b"private")
    root = tmp_path / "KV"
    try:
        root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(FileNotFoundError):
        receipts.open_local_receipt(root, "100001")


@pytest.mark.skipif(os.name == "nt", reason="POSIX openat/O_NOFOLLOW race test")
def test_posix_open_rejects_file_swapped_to_outside_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "KV"
    root.mkdir()
    document = root / "TEST-LS-001.pdf"
    document.write_bytes(b"expected")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"must-not-upload")
    original_open = os.open

    def racing_open(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            document.unlink()
            document.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(receipts.os, "open", racing_open)
    deps = make_upload_deps(
        open_receipt=lambda account: receipts.open_local_receipt(root, account)
    )

    receipts.send_pdf(7, "TEST-LS-001", deps)

    deps.http_client.post.assert_not_called()
    deps.send_message.assert_called_once_with(
        7, "Квитанция для ЛС TEST-LS-001 не найдена."
    )
    assert outside.read_bytes() == b"must-not-upload"


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO behavior")
def test_posix_fifo_is_rejected_without_blocking_or_calling_http(
    tmp_path: Path,
) -> None:
    root = tmp_path / "KV"
    root.mkdir()
    os.mkfifo(root / "TEST-LS-001.pdf")
    deps = make_upload_deps(
        open_receipt=lambda account: receipts.open_local_receipt(root, account)
    )

    worker = threading.Thread(
        target=receipts.send_pdf,
        args=(7, "TEST-LS-001", deps),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=1)

    assert not worker.is_alive(), (
        "opening a FIFO must fail without waiting for a writer"
    )
    deps.http_client.post.assert_not_called()
    deps.send_message.assert_called_once_with(
        7, "Квитанция для ЛС TEST-LS-001 не найдена."
    )
