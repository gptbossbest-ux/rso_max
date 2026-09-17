from __future__ import annotations

import importlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


class FrozenClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


class Client1CTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "ENABLE_1C_INTEGRATION": "true",
            "INTEGRATION_1C_MOCK": "true",
            "INTEGRATION_1C_BASE_URL": "https://example.invalid/publication/hs/max/",
            "INTEGRATION_1C_AUTH_TOKEN": "test-secret-token",
            "INTEGRATION_1C_AUTH_TIMEOUT_SECONDS": "5",
            "INTEGRATION_1C_SYNC_TIMEOUT_SECONDS": "60",
            "INTEGRATION_1C_MOCK_CODE": "000000",
        }, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def _module(self):
        import config
        import client_1c
        importlib.reload(config)
        return importlib.reload(client_1c)

    def test_join_url_normalizes_slashes(self) -> None:
        client = self._module()
        self.assertEqual(
            client._join_url("https://host/base/", "/auth/request-code"),
            "https://host/base/auth/request-code",
        )

    def test_live_request_adds_bearer_header_and_auth_timeout(self) -> None:
        client = self._module()
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {"status": "ok"}

        with patch.object(client, "INTEGRATION_1C_MOCK", False), patch.object(
            client.httpx, "post", return_value=response
        ) as post:
            data, error = client.request_auth_code("100001", 42)

        self.assertIsNone(error)
        self.assertEqual(data, {"status": "ok"})
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-secret-token")
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["json"], {"ls": "100001", "chat_id": 42})

    def test_transport_error_does_not_return_or_log_token(self) -> None:
        client = self._module()
        with patch.object(client, "INTEGRATION_1C_MOCK", False), patch.object(
            client.httpx, "post", side_effect=RuntimeError("test-secret-token leaked")
        ), self.assertLogs("rso.client_1c", level="ERROR") as logs:
            data, error = client.request_auth_code("100001", 42)

        self.assertIsNone(data)
        self.assertEqual(error, "connection_error")
        self.assertNotIn("test-secret-token", "\n".join(logs.output))

    def test_mock_code_expires_after_ten_minutes(self) -> None:
        client = self._module()
        clock = FrozenClock()
        backend = client.Mock1CBackend(now=clock.now, code="000000", ttl_minutes=10, max_attempts=5)

        backend.request_auth_code("100001", 42)
        clock.value += timedelta(minutes=11)
        result = backend.verify_auth_code("100001", 42, "000000")

        self.assertEqual(result["status"], "expired_code")

    def test_mock_locks_after_five_wrong_attempts(self) -> None:
        client = self._module()
        clock = FrozenClock()
        backend = client.Mock1CBackend(now=clock.now, code="000000", ttl_minutes=10, max_attempts=5)
        backend.request_auth_code("100001", 42)

        statuses = [
            backend.verify_auth_code("100001", 42, "111111")["status"]
            for _ in range(5)
        ]

        self.assertEqual(statuses[:4], ["wrong_code"] * 4)
        self.assertEqual(statuses[4], "attempts_exceeded")

    def test_mock_success_returns_meter_list(self) -> None:
        client = self._module()
        clock = FrozenClock()
        backend = client.Mock1CBackend(now=clock.now, code="000000", ttl_minutes=10, max_attempts=5)
        backend.request_auth_code("100001", 42)

        result = backend.verify_auth_code("100001", 42, "000000")

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["meters"])
        self.assertEqual(result["meters"][0]["meter_type"], "Однотарифный")


if __name__ == "__main__":
    unittest.main()
