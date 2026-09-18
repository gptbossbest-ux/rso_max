from __future__ import annotations

import logging
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
        "receipt_path": MagicMock(return_value=Path("KV/100001.pdf")),
        "read_bytes": MagicMock(return_value=b"%PDF-1.7\x00binary"),
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
    deps = make_upload_deps(read_bytes=MagicMock(side_effect=FileNotFoundError))

    receipts.send_pdf(7, "100001", deps)

    deps.send_message.assert_called_once_with(7, "Квитанция для ЛС 100001 не найдена.")
    deps.http_client.post.assert_not_called()


def test_read_error_reports_failure_without_calling_max() -> None:
    deps = make_upload_deps(read_bytes=MagicMock(side_effect=OSError("private path")))

    receipts.send_pdf(7, "100001", deps)

    deps.send_message.assert_called_once_with(
        7, "Не удалось отправить квитанцию. Попробуйте позже."
    )
    deps.http_client.post.assert_not_called()


def test_valid_pdf_preserves_two_step_upload_and_attachment_contract() -> None:
    document = b"%PDF-1.7\x00\xffbinary"
    deps = make_upload_deps(read_bytes=MagicMock(return_value=document))

    receipts.send_pdf(7, "100001", deps)

    assert deps.http_client.post.call_args_list == [
        call(
            "https://max.test/uploads",
            headers={"Authorization": "secret"},
            params={"type": "file"},
            timeout=10,
        ),
        call(
            "https://upload.test/file",
            headers={"Authorization": "secret"},
            files={"data": ("100001.pdf", document, "application/pdf")},
            timeout=30,
        ),
    ]
    deps.sleep.assert_called_once_with(2)
    deps.send_raw.assert_called_once_with(
        7,
        {
            "text": "Квитанция по ЛС 100001:",
            "attachments": [{"type": "file", "payload": {"token": "attachment-token"}}],
        },
    )
    deps.send_message.assert_not_called()


def test_large_binary_document_is_forwarded_without_text_decoding() -> None:
    document = bytes(range(256)) * 16_384
    deps = make_upload_deps(read_bytes=MagicMock(return_value=document))

    receipts.send_pdf(7, "100001", deps)

    uploaded = deps.http_client.post.call_args_list[1].kwargs["files"]["data"][1]
    assert uploaded is document
    assert len(uploaded) == 4_194_304


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
        receipt_path=MagicMock(return_value=Path(path_secret)),
        read_bytes=MagicMock(return_value=bytes_secret),
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
    fake_read = MagicMock(return_value=b"pdf")
    fake_path = MagicMock(return_value=Path("receipt.pdf"))
    with (
        patch.object(bot.receipts, "send_pdf", delegated),
        patch.object(bot, "_receipt_path", fake_path),
        patch.object(bot, "_read_receipt_bytes", fake_read),
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
    assert deps.receipt_path is fake_path
    assert deps.read_bytes is fake_read
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


def test_default_receipt_path_preserves_runtime_contract() -> None:
    assert bot._receipt_path("100001") == Path("KV/100001.pdf")
