from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from starlette.exceptions import StarletteDeprecationWarning

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning)

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
