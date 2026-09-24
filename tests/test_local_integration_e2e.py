from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

import bot
import client_1c
import database as db
import sync_1c
from api import notifier

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class LocalIntegrationE2ETests(unittest.TestCase):
    """SQLite -> sync job -> HTTP 1C -> SQLite -> HTTP MAX, without a network."""

    def setUp(self) -> None:
        self.original_bot_user_states = deepcopy(bot.user_states)
        bot.user_states.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / "integration.sqlite")
        self.now = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)
        self.db_clock = patch.object(db, "msk_now", return_value="2026-09-17 08:59")
        self.db_clock.start()
        self.addCleanup(self.db_clock.stop)
        db.init_db()
        self.one_c_requests: list[dict] = []
        self.max_requests: list[dict] = []

    def tearDown(self) -> None:
        try:
            db.DB_PATH = self.old_db_path
            self.tmp.cleanup()
        finally:
            bot.user_states.clear()
            bot.user_states.update(deepcopy(self.original_bot_user_states))

    @staticmethod
    def _status_result(request_body: dict, status: str) -> dict:
        return {
            "batch_id": request_body["batch_id"],
            "readings_status": [
                {
                    "ls": item["ls"],
                    "meter_number": item["meter_number"],
                    "value1": item["value1"],
                    "value2": item.get("value2"),
                    "submitted_at": item["submitted_at"],
                    "status": status,
                }
                for item in request_body["readings"]
            ],
            "meters_changes": [],
        }

    def _transport_context(self, one_c_handler):
        def record_one_c(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.one_c_requests.append(
                {
                    "url": str(request.url),
                    "authorization": request.headers.get("authorization"),
                    "body": body,
                }
            )
            return one_c_handler(request, body)

        one_c_transport = httpx.MockTransport(record_one_c)

        def post(url, **kwargs):
            with httpx.Client(transport=one_c_transport) as client:
                return client.post(url, **kwargs)

        def record_max(request: httpx.Request) -> httpx.Response:
            self.max_requests.append(
                {
                    "url": str(request.url),
                    "authorization": request.headers.get("authorization"),
                    "body": json.loads(request.content),
                }
            )
            return httpx.Response(200, json={"ok": True})

        max_transport = httpx.MockTransport(record_max)

        def async_client(*args, **kwargs):
            kwargs["transport"] = max_transport
            return _REAL_ASYNC_CLIENT(*args, **kwargs)

        stack = ExitStack()
        stack.enter_context(patch.object(sync_1c, "ENABLE_1C_INTEGRATION", True))
        stack.enter_context(patch.object(client_1c, "INTEGRATION_1C_MOCK", False))
        stack.enter_context(
            patch.object(
                client_1c, "INTEGRATION_1C_BASE_URL", "https://one-c.invalid/test"
            )
        )
        stack.enter_context(
            patch.object(client_1c, "INTEGRATION_1C_AUTH_TOKEN", "audit-1c-secret")
        )
        stack.enter_context(patch.object(client_1c.httpx, "post", side_effect=post))
        stack.enter_context(patch.object(notifier, "TOKEN", "audit-max-secret"))
        stack.enter_context(patch.object(notifier, "API", "https://max.invalid"))
        stack.enter_context(
            patch.object(notifier.httpx, "AsyncClient", side_effect=async_client)
        )
        return stack

    def _add_reading(self) -> None:
        db.add_pokazaniya(70001, "TEST-LS-001", "Вода", "TEST-METER-1", "12.5")

    def _add_reading_through_bot(self) -> None:
        """Exercise the real callback/message flow, including saved-LS authorization."""
        chat_id = 70001
        ls = "TEST-LS-001"
        db.upsert_bot_user(chat_id, ls, "", authorized_1c=True)
        db.upsert_1c_meters(
            ls,
            [
                {
                    "meter_number": "TEST-METER-1",
                    "resource_type": "Вода",
                    "meter_type": "Однотарифный",
                }
            ],
        )

        def callback(payload: str) -> dict:
            return {
                "callback": {"callback_id": f"callback-{payload}", "payload": payload},
                "message": {"recipient": {"chat_id": chat_id}},
            }

        with (
            patch.object(bot, "ENABLE_1C_INTEGRATION", True),
            patch.object(bot, "_ack_callback"),
            patch.object(bot, "_send_raw", return_value=True),
        ):
            bot.handle_callback(callback("pokazaniya"))
            bot.handle_callback(callback("meter:0"))
            bot.handle_message(
                {"recipient": {"chat_id": chat_id}, "body": {"text": "12.5"}}
            )
            bot.handle_callback(callback("meter_confirm"))

    def test_success_sends_batch_persists_status_and_notifies_max(self) -> None:
        self._add_reading_through_bot()
        queued_row = db.get_pokazaniya()[0]

        def accepted(request, body):
            result = self._status_result(body, "accepted")
            result["meters_changes"] = [
                {
                    "action": "added",
                    "ls": "TEST-LS-001",
                    "meter_number": "TEST-METER-2",
                    "resource_type": "Вода",
                    "meter_type": "Однотарифный",
                }
            ]
            return httpx.Response(200, json=result, request=request)

        with (
            self._transport_context(accepted),
            patch.object(sync_1c, "_generate_batch_id", return_value="batch-success"),
        ):
            outcome = sync_1c.sync_1c_job(self.now)

        row = db.get_pokazaniya()[0]
        self.assertEqual(outcome, "success")
        self.assertEqual((row["sent_to_1c"], row["status_1c"]), (1, "accepted"))
        self.assertIsNone(db.get_1c_sync_state()["pending_batch_id"])
        self.assertIn(
            "TEST-METER-2",
            {row["meter_number"] for row in db.get_schetchiki("TEST-LS-001")},
        )
        self.assertEqual(
            self.one_c_requests[0]["authorization"], "Bearer audit-1c-secret"
        )
        self.assertTrue(self.one_c_requests[0]["url"].endswith("/readings/sync"))
        self.assertEqual(
            self.one_c_requests[0]["body"]["readings"],
            [
                {
                    "chat_id": 70001,
                    "ls": "TEST-LS-001",
                    "meter_number": "TEST-METER-1",
                    "value1": "12.5",
                    "value2": None,
                    "submitted_at": queued_row["created_at"],
                }
            ],
        )
        self.assertIsInstance(queued_row["created_at"], str)
        self.assertRegex(queued_row["created_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertEqual(self.max_requests[0]["authorization"], "audit-max-secret")
        self.assertIn("приняты", self.max_requests[0]["body"]["text"])

    def test_local_auth_two_tariff_reading_and_sync_chain(self) -> None:
        chat_id = 70002
        ls = "TEST-LS-002"
        db.create_lschet(ls, "Тестовый пользователь", "Тестовый адрес")
        db.upsert_1c_meters(
            ls,
            [
                {
                    "meter_number": "ELECTRICITY-2T",
                    "resource_type": "Электроэнергия",
                    "meter_type": "Двухтарифный",
                }
            ],
        )

        def one_c(request, body):
            return httpx.Response(
                200,
                json=self._status_result(body, "accepted"),
                request=request,
            )

        def callback(payload: str) -> dict:
            return {
                "callback": {"callback_id": f"callback-{payload}", "payload": payload},
                "message": {"recipient": {"chat_id": chat_id}},
            }

        with (
            self._transport_context(one_c),
            patch.object(bot, "ENABLE_1C_INTEGRATION", True),
            patch.object(bot.client_api, "request_1c_auth_code") as request_code_api,
            patch.object(bot.client_api, "verify_1c_auth_code") as verify_code_api,
            patch.object(bot, "_ack_callback"),
            patch.object(bot, "_send_raw", return_value=True),
            patch.object(sync_1c, "_generate_batch_id", return_value="batch-full-e2e"),
        ):
            bot._start_1c_auth(chat_id)
            bot.handle_message(
                {"recipient": {"chat_id": chat_id}, "body": {"text": ls}}
            )
            bot.handle_callback(callback("pokazaniya"))
            bot.handle_callback(callback("meter:0"))
            bot.handle_message(
                {"recipient": {"chat_id": chat_id}, "body": {"text": "120.5"}}
            )
            bot.handle_message(
                {"recipient": {"chat_id": chat_id}, "body": {"text": "48.25"}}
            )
            bot.handle_callback(callback("meter_confirm"))
            outcome = sync_1c.sync_1c_job(self.now)

        self.assertEqual(outcome, "success")
        request_code_api.assert_not_called()
        verify_code_api.assert_not_called()
        self.assertEqual(
            [request["url"].rsplit("/", 2)[-2:] for request in self.one_c_requests],
            [["readings", "sync"]],
        )
        user = db.get_bot_user(chat_id)
        self.assertEqual((user["ls"], user["authorized_1c"]), (ls, 1))
        reading = db.get_pokazaniya()[0]
        self.assertEqual(
            (reading["meter_number"], reading["value1"], reading["value2"]),
            ("ELECTRICITY-2T", "120.5", "48.25"),
        )
        self.assertEqual((reading["sent_to_1c"], reading["status_1c"]), (1, "accepted"))
        self.assertIsNone(db.get_1c_sync_state()["pending_batch_id"])
        self.assertIn("приняты", self.max_requests[0]["body"]["text"])

    def test_rejected_status_is_persisted_and_notified(self) -> None:
        self._add_reading()

        def rejected(request, body):
            return httpx.Response(
                200, json=self._status_result(body, "rejected"), request=request
            )

        with (
            self._transport_context(rejected),
            patch.object(sync_1c, "_generate_batch_id", return_value="batch-rejected"),
        ):
            outcome = sync_1c.sync_1c_job(self.now)

        row = db.get_pokazaniya()[0]
        self.assertEqual(outcome, "success")
        self.assertEqual((row["sent_to_1c"], row["status_1c"]), (1, "rejected"))
        self.assertIn("не приняты", self.max_requests[0]["body"]["text"])
        self.assertEqual(
            self.max_requests[0]["body"]["attachments"][0]["type"], "inline_keyboard"
        )

    def test_timeout_retries_same_batch_without_duplicate_rows(self) -> None:
        self._add_reading()
        logical_process_count = 0
        cached_results: dict[str, dict] = {}

        def timeout_then_success(request, body):
            nonlocal logical_process_count
            batch_id = body["batch_id"]
            if batch_id not in cached_results:
                logical_process_count += 1
                cached_results[batch_id] = self._status_result(body, "accepted")
                # 1C committed and cached the idempotent result, but its response was lost.
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(200, json=cached_results[batch_id], request=request)

        with (
            self._transport_context(timeout_then_success),
            patch.object(sync_1c, "_generate_batch_id", return_value="batch-retry"),
        ):
            first = sync_1c.sync_1c_job(self.now)
            pending = dict(db.get_1c_sync_state())
            retry = sync_1c.sync_1c_job(self.now + timedelta(hours=1))

        self.assertEqual((first, retry), ("failed", "success"))
        self.assertEqual(len(self.one_c_requests), 2)
        self.assertEqual(logical_process_count, 1)
        self.assertEqual(pending["pending_batch_id"], "batch-retry")
        self.assertEqual(
            [call["body"]["batch_id"] for call in self.one_c_requests],
            [
                "batch-retry",
                "batch-retry",
            ],
        )
        self.assertEqual(
            self.one_c_requests[0]["body"]["readings"],
            self.one_c_requests[1]["body"]["readings"],
        )
        rows = db.get_pokazaniya()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["sent_to_1c"], rows[0]["status_1c"]), (1, "accepted"))
        self.assertEqual(len(self.max_requests), 1)

    def test_invalid_response_keeps_batch_pending_for_retry(self) -> None:
        self._add_reading()

        def invalid(request, body):
            return httpx.Response(
                200,
                json={"batch_id": body["batch_id"], "readings_status": []},
                request=request,
            )

        with (
            self._transport_context(invalid),
            patch.object(sync_1c, "_generate_batch_id", return_value="batch-invalid"),
        ):
            outcome = sync_1c.sync_1c_job(self.now)

        row = db.get_pokazaniya()[0]
        self.assertEqual(outcome, "failed")
        self.assertEqual(db.get_1c_sync_state()["pending_batch_id"], "batch-invalid")
        self.assertEqual((row["sent_to_1c"], row["status_1c"]), (0, None))
        self.assertEqual(self.max_requests, [])

    def test_transport_failure_does_not_leak_1c_secret_to_logs(self) -> None:
        self._add_reading()

        def leaking_exception(request, body):
            raise RuntimeError("transport included audit-1c-secret")

        with (
            self._transport_context(leaking_exception),
            patch.object(sync_1c, "_generate_batch_id", return_value="batch-secret"),
            self.assertLogs(level="ERROR") as logs,
        ):
            outcome = sync_1c.sync_1c_job(self.now)

        self.assertEqual(outcome, "failed")
        self.assertNotIn("audit-1c-secret", "\n".join(logs.output))
        self.assertEqual(db.get_1c_sync_state()["pending_batch_id"], "batch-secret")

    def test_max_transport_failure_does_not_leak_secret_to_logs(self) -> None:
        def leaking_max_transport(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("transport included audit-max-secret")

        transport = httpx.MockTransport(leaking_max_transport)

        def async_client(*args, **kwargs):
            kwargs["transport"] = transport
            return _REAL_ASYNC_CLIENT(*args, **kwargs)

        with (
            patch.object(notifier, "TOKEN", "audit-max-secret"),
            patch.object(notifier, "API", "https://max.invalid"),
            patch.object(notifier.httpx, "AsyncClient", side_effect=async_client),
            self.assertLogs("rso.api.notifier", level="ERROR") as logs,
        ):
            result = asyncio.run(notifier.notify_client("max", 70001, "test"))

        self.assertFalse(result)
        joined_logs = "\n".join(logs.output)
        self.assertNotIn("audit-max-secret", joined_logs)
        self.assertIn("RuntimeError", joined_logs)

    def test_max_http_error_body_does_not_leak_secret_to_logs(self) -> None:
        def rejected_max_transport(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                500,
                text="upstream echoed audit-max-secret",
                request=request,
            )

        transport = httpx.MockTransport(rejected_max_transport)

        def async_client(*args, **kwargs):
            kwargs["transport"] = transport
            return _REAL_ASYNC_CLIENT(*args, **kwargs)

        with (
            patch.object(notifier, "TOKEN", "audit-max-secret"),
            patch.object(notifier, "API", "https://max.invalid"),
            patch.object(notifier.httpx, "AsyncClient", side_effect=async_client),
            self.assertLogs("rso.api.notifier", level="WARNING") as logs,
        ):
            result = asyncio.run(notifier.notify_client("max", 70001, "test"))

        self.assertFalse(result)
        joined_logs = "\n".join(logs.output)
        self.assertNotIn("audit-max-secret", joined_logs)
        self.assertIn("500", joined_logs)


if __name__ == "__main__":
    unittest.main()
