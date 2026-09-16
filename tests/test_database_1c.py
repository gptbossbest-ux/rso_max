from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import database as db


class Database1CTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / "test.sqlite")
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_init_db_adds_integration_columns_and_state(self) -> None:
        conn = db.get_conn()
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pokazaniya)")}
            bot_user_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(bot_users)")
            }
            state = conn.execute("SELECT * FROM sync_1c_state WHERE id=1").fetchone()
        finally:
            conn.close()

        self.assertTrue({"sent_to_1c", "status_1c", "batch_id"} <= columns)
        self.assertIn("authorized_1c", bot_user_columns)
        self.assertIsNotNone(state)

    def test_1c_authorization_flag_is_persisted(self) -> None:
        db.upsert_bot_user(42, "100001", "", authorized_1c=True)
        self.assertEqual(db.get_bot_user(42)["authorized_1c"], 1)

    def test_assigning_batch_keeps_same_members_on_retry(self) -> None:
        db.add_pokazaniya(11, "100001", "Электроэнергия", "M-1", "10")
        db.add_pokazaniya(12, "100002", "Холодная вода", "M-2", "20")
        rows = db.get_unsent_1c_readings(limit=500)
        db.assign_readings_to_1c_batch([row["id"] for row in rows], "batch-1")
        db.add_pokazaniya(13, "100003", "Горячая вода", "M-3", "30")

        retry = db.get_1c_readings_by_batch("batch-1")

        self.assertEqual([row["meter_number"] for row in retry], ["M-1", "M-2"])

    def test_apply_status_marks_reading_final_and_returns_owner(self) -> None:
        db.add_pokazaniya(11, "100001", "Электроэнергия", "M-1", "10")
        row = db.get_unsent_1c_readings(limit=1)[0]
        db.assign_readings_to_1c_batch([row["id"]], "batch-1")

        updated = db.apply_1c_reading_statuses("batch-1", [{
            "ls": "100001",
            "meter_number": "M-1",
            "value1": "10",
            "value2": None,
            "submitted_at": row["created_at"],
            "status": "rejected",
        }])

        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0]["chat_id"], 11)
        stored = db.get_1c_readings_by_batch("batch-1")[0]
        self.assertEqual(stored["sent_to_1c"], 1)
        self.assertEqual(stored["status_1c"], "rejected")

    def test_duplicate_natural_keys_finalize_distinct_rows(self) -> None:
        db.add_pokazaniya(11, "100001", "Электроэнергия", "M-1", "10")
        db.add_pokazaniya(11, "100001", "Электроэнергия", "M-1", "10")
        rows = db.get_unsent_1c_readings(limit=2)
        db.assign_readings_to_1c_batch([row["id"] for row in rows], "batch-1")
        status = {
            "ls": "100001",
            "meter_number": "M-1",
            "value1": "10",
            "value2": None,
            "submitted_at": rows[0]["created_at"],
            "status": "accepted",
        }

        updated = db.apply_1c_reading_statuses("batch-1", [status, status])

        self.assertEqual(len(updated), 2)
        self.assertEqual(
            [row["sent_to_1c"] for row in db.get_1c_readings_by_batch("batch-1")],
            [1, 1],
        )

    def test_meter_upsert_and_delta_actions(self) -> None:
        db.upsert_1c_meters("100001", [{
            "meter_number": "M-1",
            "resource_type": "Электроэнергия",
            "meter_type": "Однотарифный",
        }])
        db.apply_1c_meter_changes([
            {
                "ls": "100001",
                "action": "changed",
                "meter_number": "M-1",
                "resource_type": "Холодная вода",
                "meter_type": "Двухтарифный",
            },
            {
                "ls": "100001",
                "action": "added",
                "meter_number": "M-2",
                "resource_type": "Горячая вода",
                "meter_type": "Однотарифный",
            },
        ])
        db.apply_1c_meter_changes([{
            "ls": "100001",
            "action": "removed",
            "meter_number": "M-2",
        }])

        meters = [dict(row) for row in db.get_schetchiki("100001")]
        self.assertEqual(len(meters), 1)
        self.assertEqual(meters[0]["resource_type"], "Холодная вода")
        self.assertEqual(meters[0]["meter_type"], "Двухтарифный")

    def test_sync_state_persists_pending_batch(self) -> None:
        db.update_1c_sync_state(
            pending_batch_id="batch-1",
            last_attempt_at="2026-08-19 12:00",
        )

        state = db.get_1c_sync_state()

        self.assertEqual(state["pending_batch_id"], "batch-1")
        self.assertEqual(state["last_attempt_at"], "2026-08-19 12:00")


if __name__ == "__main__":
    unittest.main()
