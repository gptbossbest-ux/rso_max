from __future__ import annotations

import logging
from unittest.mock import MagicMock

import httpx
import pytest

import client_api


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/integrations/1c/auth/request-code",
        "/api/v1/integrations/1c/auth/verify-code",
    ],
)
def test_sensitive_post_redacts_error_response_body(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "SECRET-LS-CODE-991122"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            422,
            json={"detail": sentinel},
            request=request,
        )
    )
    with httpx.Client(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(client_api.httpx, "post", client.post)
        with caplog.at_level(logging.WARNING, logger=client_api.log.name):
            data, error = client_api._post(
                path,
                {"ls": sentinel, "code": sentinel},
                sensitive=True,
            )

    assert data is None
    assert error == "HTTP 422"
    assert sentinel not in caplog.text


def test_sensitive_post_redacts_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "SECRET-CODE-EXCEPTION"
    query_secret = "SECRET-QUERY-EXCEPTION"

    def fail(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError(sentinel)

    transport = httpx.MockTransport(fail)
    with httpx.Client(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(client_api.httpx, "post", client.post)
        with caplog.at_level(logging.ERROR, logger=client_api.log.name):
            data, error = client_api._post(
                "/api/v1/integrations/1c/auth/verify-code"
                f"?trace={query_secret}#SECRET-FRAGMENT-EXCEPTION",
                {"code": sentinel},
                sensitive=True,
            )

    assert data is None
    assert error == "request_failed"
    assert sentinel not in caplog.text
    assert query_secret not in caplog.text
    assert "SECRET-FRAGMENT-EXCEPTION" not in caplog.text


def test_non_sensitive_post_keeps_diagnostic_detail(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    detail = "ordinary validation detail"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, json={"detail": detail}, request=request)
    )
    with httpx.Client(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(client_api.httpx, "post", client.post)
        with caplog.at_level(logging.WARNING, logger=client_api.log.name):
            data, error = client_api._post("/api/v1/appeals", {"text": "ordinary"})

    assert data is None
    assert error == f"HTTP 400: {detail}"
    assert detail in caplog.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/integrations/1c/auth/request-code?ls=SECRET-QUERY",
        "/api/v1/integrations/1c/auth/verify-code#SECRET-FRAGMENT",
        "/api/v1/integrations/1c/auth/request-code/",
        "/prefix/api/v1/integrations/1c/auth/request-code",
        "/api/v1/integrations/1c/auth/verify-code/suffix",
    ],
)
def test_explicit_sensitive_post_never_logs_raw_path_or_payload(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload_secret = "SECRET-PAYLOAD"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            422,
            json={"detail": payload_secret},
            request=request,
        )
    )
    with httpx.Client(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(client_api.httpx, "post", client.post)
        with caplog.at_level(logging.WARNING, logger=client_api.log.name):
            data, error = client_api._post(
                path,
                {"code": payload_secret},
                sensitive=True,
            )

    assert data is None
    assert error == "HTTP 422"
    assert path not in caplog.text
    assert "SECRET-QUERY" not in caplog.text
    assert "SECRET-FRAGMENT" not in caplog.text
    assert payload_secret not in caplog.text


def test_auth_shaped_path_is_not_implicitly_sensitive(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    detail = "diagnostic detail"
    path = "/api/v1/integrations/1c/auth/verify-code?mode=diagnostic"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, json={"detail": detail}, request=request)
    )
    with httpx.Client(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(client_api.httpx, "post", client.post)
        with caplog.at_level(logging.WARNING, logger=client_api.log.name):
            data, error = client_api._post(path, {"text": "ordinary"})

    assert data is None
    assert error == f"HTTP 400: {detail}"
    assert path in caplog.text
    assert detail in caplog.text


def test_auth_wrappers_always_mark_post_as_sensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = MagicMock(return_value=({"status": "ok"}, None))
    monkeypatch.setattr(client_api, "_post", post)

    client_api.request_1c_auth_code("100001", 42)
    client_api.verify_1c_auth_code("100001", 42, "123456")

    assert post.call_count == 2
    assert all(call.kwargs == {"sensitive": True} for call in post.call_args_list)
