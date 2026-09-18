from __future__ import annotations

import logging

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
            data, error = client_api._post(path, {"ls": sentinel, "code": sentinel})

    assert data is None
    assert error == "HTTP 422"
    assert sentinel not in caplog.text


def test_sensitive_post_redacts_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "SECRET-CODE-EXCEPTION"

    def fail(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError(sentinel)

    transport = httpx.MockTransport(fail)
    with httpx.Client(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(client_api.httpx, "post", client.post)
        with caplog.at_level(logging.ERROR, logger=client_api.log.name):
            data, error = client_api._post(
                "/api/v1/integrations/1c/auth/verify-code",
                {"code": sentinel},
            )

    assert data is None
    assert error == "request_failed"
    assert sentinel not in caplog.text


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
