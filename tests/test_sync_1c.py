from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import database as db


class Sync1CTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = str(Path(self.tmp.name) / "test.sqlite")
        self.now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        self.db_clock = patch.object(db, "msk_now", return_value="2026-08-19 11:59")
        self.db_clock.start()
        db.init_db()
        import sync_1c
        self.sync = sync_1c

    def tearDown(self) -> None:
        self.db_clock.stop()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_first_run_sends_empty_batch_to_receive_meter_delta(self) -> None:
        result = {"batch_id": "fixed-batch", "readings_status": [], "meters_changes": []}
        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True, create=True), \
             patch.object(self.sync, "_generate_batch_id", return_value="fixed-batch"), \
             patch.object(self.sync.client_1c, "sync_readings", return_value=(result, None)) as send:
            outcome = self.sync.sync_1c_job(now=self.now)

        self.assertEqual(outcome, "success")
        send.assert_called_once_with("fixed-batch", [])
        self.assertIsNone(db.get_1c_sync_state()["pending_batch_id"])

    def test_success_marks_reading_and_notifies_owner(self) -> None:
        db.add_pokazaniya(42, "100001", "Электроэнергия", "M-1", "10")

        def response(batch_id, readings):
            return ({
                "batch_id": batch_id,
                "readings_status": [{
                    "ls": readings[0]["ls"],
                    "meter_number": readings[0]["meter_number"],
                    "value1": readings[0]["value1"],
                    "value2": readings[0]["value2"],
                    "submitted_at": readings[0]["submitted_at"],
                    "status": "accepted",
                }],
                "meters_changes": [],
            }, None)

        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True, create=True), \
             patch.object(self.sync.client_1c, "sync_readings", side_effect=response), \
             patch.object(self.sync, "_notify_reading") as notify:
            outcome = self.sync.sync_1c_job(now=self.now)

        self.assertEqual(outcome, "success")
        row = db.get_pokazaniya()[0]
        self.assertEqual(row["status_1c"], "accepted")
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0]["chat_id"], 42)

    def test_failed_batch_retries_after_hour_with_same_members(self) -> None:
        db.add_pokazaniya(42, "100001", "Электроэнергия", "M-1", "10")
        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True, create=True), \
             patch.object(self.sync, "_generate_batch_id", return_value="batch-1"), \
             patch.object(self.sync.client_1c, "sync_readings", return_value=(None, "timeout")):
            first = self.sync.sync_1c_job(now=self.now)

        db.add_pokazaniya(43, "100002", "Холодная вода", "M-2", "20")
        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True, create=True), \
             patch.object(self.sync.client_1c, "sync_readings", return_value=(None, "timeout")) as send:
            too_soon = self.sync.sync_1c_job(now=self.now + timedelta(minutes=30))
            retry = self.sync.sync_1c_job(now=self.now + timedelta(hours=1))

        self.assertEqual(first, "failed")
        self.assertEqual(too_soon, "not_due")
        self.assertEqual(retry, "failed")
        batch_id, readings = send.call_args.args
        self.assertEqual(batch_id, "batch-1")
        self.assertEqual([item["meter_number"] for item in readings], ["M-1"])

    def test_mismatched_response_batch_keeps_pending_state(self) -> None:
        result = {"batch_id": "other", "readings_status": [], "meters_changes": []}
        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True, create=True), \
             patch.object(self.sync, "_generate_batch_id", return_value="batch-1"), \
             patch.object(self.sync.client_1c, "sync_readings", return_value=(result, None)):
            outcome = self.sync.sync_1c_job(now=self.now)

        self.assertEqual(outcome, "failed")
        self.assertEqual(db.get_1c_sync_state()["pending_batch_id"], "batch-1")

    def test_duplicate_readings_require_one_status_per_row(self) -> None:
        reading = {
            "chat_id": 42,
            "ls": "100001",
            "meter_number": "M-1",
            "value1": "10",
            "value2": None,
            "submitted_at": "2026-08-19 12:00",
        }
        response = {
            "batch_id": "batch-1",
            "readings_status": [{**reading, "status": "accepted"}],
            "meters_changes": [],
        }

        valid = self.sync._valid_result(response, "batch-1", [reading, reading])

        self.assertFalse(valid)

    def test_queue_larger_than_batch_continues_on_next_hour(self) -> None:
        db.add_pokazaniya(42, "100001", "Электроэнергия", "M-1", "10")
        db.add_pokazaniya(43, "100002", "Холодная вода", "M-2", "20")

        def response(batch_id, readings):
            statuses = [{
                "ls": item["ls"],
                "meter_number": item["meter_number"],
                "value1": item["value1"],
                "value2": item["value2"],
                "submitted_at": item["submitted_at"],
                "status": "accepted",
            } for item in readings]
            return ({
                "batch_id": batch_id,
                "readings_status": statuses,
                "meters_changes": [],
            }, None)

        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True), \
             patch.object(self.sync, "INTEGRATION_1C_BATCH_SIZE", 1), \
             patch.object(self.sync.client_1c, "sync_readings", side_effect=response), \
             patch.object(self.sync, "_notify_reading"):
            first = self.sync.sync_1c_job(now=self.now)
            second = self.sync.sync_1c_job(now=self.now + timedelta(hours=1))

        self.assertEqual((first, second), ("success", "success"))
        self.assertEqual([row["sent_to_1c"] for row in db.get_pokazaniya()], [1, 1])


if __name__ == "__main__":
    unittest.main()
