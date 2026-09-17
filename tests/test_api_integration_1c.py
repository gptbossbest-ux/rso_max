from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from starlette.exceptions import StarletteDeprecationWarning

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

import httpx
from fastapi.testclient import TestClient

import api.deps as deps
import client_1c
import database as db
from api.main import app


class Integration1CApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        self.old_token = deps.INTERNAL_API_TOKEN
        db.DB_PATH = str(Path(self.tmp.name) / "test.sqlite")
        deps.INTERNAL_API_TOKEN = "internal-test-token"
        db.init_db()
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer internal-test-token"}

    def tearDown(self) -> None:
        db.DB_PATH = self.old_path
        deps.INTERNAL_API_TOKEN = self.old_token
        self.tmp.cleanup()

    def test_request_code_requires_internal_bearer(self) -> None:
        response = self.client.post(
            "/api/v1/integrations/1c/auth/request-code",
            json={"ls": "100001", "chat_id": 42},
        )
        self.assertEqual(response.status_code, 401)

    def test_request_code_forwards_business_status(self) -> None:
        with patch.object(
            client_1c,
            "request_auth_code",
            return_value=({"status": "ok", "message": "Код отправлен"}, None),
        ) as request:
            response = self.client.post(
                "/api/v1/integrations/1c/auth/request-code",
                headers=self.headers,
                json={"ls": "100001", "chat_id": 42},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        request.assert_called_once_with("100001", 42)

    def test_synchronous_1c_calls_run_outside_event_loop_thread(self) -> None:
        async def check_route(path: str, method: str, payload: dict, result: dict) -> None:
            loop_thread = threading.get_ident()
            call_threads = []

            def call_1c(*args):
                call_threads.append(threading.get_ident())
                return result, None

            with patch.object(client_1c, method, side_effect=call_1c):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    response = await client.post(path, headers=self.headers, json=payload)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), result)
            self.assertEqual(len(call_threads), 1)
            self.assertNotEqual(call_threads[0], loop_thread)

        cases = (
            ("request-code", "request_auth_code", {"ls": "100001", "chat_id": 42},
             {"status": "ok", "message": "Код отправлен"}),
            ("verify-code", "verify_auth_code", {"ls": "100001", "chat_id": 42, "code": "123456"},
             {"status": "wrong_code", "message": "Код неверный"}),
        )
        for route, method, payload, result in cases:
            with self.subTest(route=route):
                asyncio.run(check_route(
                    f"/api/v1/integrations/1c/auth/{route}", method, payload, result,
                ))

    def test_verify_success_persists_binding_and_meters(self) -> None:
        result = {
            "status": "ok",
            "message": "Авторизация выполнена",
            "meters": [{
                "meter_number": "M-1",
                "resource_type": "Электроэнергия",
                "meter_type": "Однотарифный",
            }],
        }
        with patch.object(client_1c, "verify_auth_code", return_value=(result, None)):
            response = self.client.post(
                "/api/v1/integrations/1c/auth/verify-code",
                headers=self.headers,
                json={"ls": "100001", "chat_id": 42, "code": "123456"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(db.get_bot_user(42)["ls"], "100001")
        self.assertEqual(db.get_schetchiki("100001")[0]["meter_number"], "M-1")

    def test_verify_success_preserves_existing_fio(self) -> None:
        db.upsert_bot_user(42, "old-ls", "Иванов Иван", authorized_1c=False)
        result = {"status": "ok", "message": "Готово", "meters": []}

        with patch.object(client_1c, "verify_auth_code", return_value=(result, None)):
            response = self.client.post(
                "/api/v1/integrations/1c/auth/verify-code",
                headers=self.headers,
                json={"ls": "100001", "chat_id": 42, "code": "123456"},
            )

        self.assertEqual(response.status_code, 200)
        user = db.get_bot_user(42)
        self.assertEqual(user["ls"], "100001")
        self.assertEqual(user["fio"], "Иванов Иван")
        self.assertEqual(user["authorized_1c"], 1)

    def test_malformed_auth_payload_is_rejected_before_any_write(self) -> None:
        invalid_meters = (
            None,
            [None],
            [{"meter_number": ["M-1"], "resource_type": "Электроэнергия",
              "meter_type": "Однотарифный"}],
            [{"meter_number": "M-1", "resource_type": {"name": "Электроэнергия"},
              "meter_type": "Однотарифный"}],
            [
                {"meter_number": "M-1", "resource_type": "Электроэнергия",
                 "meter_type": "Однотарифный"},
                {"meter_number": "M-2", "resource_type": "Электроэнергия",
                 "meter_type": ["Двухтарифный"]},
            ],
        )
        for meters in invalid_meters:
            with self.subTest(meters=meters):
                result = {"status": "ok", "message": "Готово", "meters": meters}
                with patch.object(
                    client_1c, "verify_auth_code", return_value=(result, None)
                ):
                    response = self.client.post(
                        "/api/v1/integrations/1c/auth/verify-code",
                        headers=self.headers,
                        json={"ls": "100001", "chat_id": 42, "code": "123456"},
                    )

                self.assertEqual(response.status_code, 502)
                self.assertIsNone(db.get_bot_user(42))
                self.assertEqual(db.get_schetchiki("100001"), [])

    def test_internal_token_uses_constant_time_comparison(self) -> None:
        with patch.object(
            deps.secrets,
            "compare_digest",
            wraps=deps.secrets.compare_digest,
        ) as compare:
            response = self.client.post(
                "/api/v1/integrations/1c/auth/request-code",
                headers={"Authorization": "Bearer wrong-token"},
                json={"ls": "100001", "chat_id": 42},
            )

        self.assertEqual(response.status_code, 401)
        compare.assert_called_once_with("wrong-token", "internal-test-token")

    def test_timeout_maps_to_gateway_timeout(self) -> None:
        with patch.object(client_1c, "verify_auth_code", return_value=(None, "timeout")):
            response = self.client.post(
                "/api/v1/integrations/1c/auth/verify-code",
                headers=self.headers,
                json={"ls": "100001", "chat_id": 42, "code": "123456"},
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["detail"], "Сервис 1С временно недоступен")

    def test_empty_code_is_rejected_before_calling_1c(self) -> None:
        with patch.object(client_1c, "verify_auth_code") as verify:
            response = self.client.post(
                "/api/v1/integrations/1c/auth/verify-code",
                headers=self.headers,
                json={"ls": "100001", "chat_id": 42, "code": ""},
            )

        self.assertEqual(response.status_code, 422)
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
