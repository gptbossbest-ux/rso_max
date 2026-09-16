from __future__ import annotations

import unittest

from flask import render_template

from web import app


class Portal1CTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = {
            "id": 1,
            "ls": "100001",
            "resource_type": "Электроэнергия",
            "meter_number": "M-1",
            "value1": "10",
            "value2": None,
            "prev_value1": "9",
            "prev_value2": None,
            "diff1": 1,
            "diff2": None,
            "created_at": "2026-08-19 12:00",
            "status_1c": "accepted",
        }

    def _render(self, enabled: bool) -> str:
        with app.test_request_context("/pokazaniya"):
            return render_template(
                "pokazaniya.html",
                rows=[self.row],
                user={"name": "Тест"},
                integration_1c_enabled=enabled,
            )

    def test_integration_mode_shows_1c_status_without_previous_columns(self) -> None:
        html = self._render(True)
        self.assertIn("Статус 1С", html)
        self.assertIn("Принято", html)
        self.assertNotIn(">Предыдущие<", html)
        self.assertNotIn(">Разница<", html)

    def test_legacy_mode_keeps_previous_and_difference_columns(self) -> None:
        html = self._render(False)
        self.assertIn(">Предыдущие<", html)
        self.assertIn(">Разница<", html)
        self.assertNotIn("Статус 1С", html)


if __name__ == "__main__":
    unittest.main()
