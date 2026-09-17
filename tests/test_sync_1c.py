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

    def test_upgrade_drains_multiple_orphans_with_original_ids_and_members(self) -> None:
        for meter in ("M-1", "M-2", "M-3"):
            db.add_pokazaniya(42, "100001", "Вода", meter, "10")
        conn = db.get_conn()
        conn.execute("UPDATE pokazaniya SET batch_id='orphan-1' WHERE meter_number IN ('M-1', 'M-2')")
        conn.execute("UPDATE pokazaniya SET sent_to_1c=1, status_1c='accepted' WHERE meter_number='M-1'")
        conn.execute("UPDATE pokazaniya SET batch_id='orphan-2' WHERE meter_number='M-3'")
        conn.commit()
        conn.close()
        # Even a recent success must not hide pre-upgrade orphan work.
        db.update_1c_sync_state(last_success_at=self.now.isoformat())

        def response(batch_id, readings):
            return ({"batch_id": batch_id, "meters_changes": [],
                     "readings_status": [{**row, "status": "accepted"} for row in readings]}, None)

        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True), \
             patch.object(self.sync.client_1c, "sync_readings", side_effect=response) as send, \
             patch.object(self.sync, "_notify_reading") as notify:
            first = self.sync.sync_1c_job(self.now)
            second = self.sync.sync_1c_job(self.now + timedelta(hours=1))

        self.assertEqual((first, second), ("success", "success"))
        self.assertEqual([call.args[0] for call in send.call_args_list], ["orphan-1", "orphan-2"])
        self.assertEqual([row["meter_number"] for row in send.call_args_list[0].args[1]], ["M-1", "M-2"])
        self.assertEqual(notify.call_count, 2)  # Already-final M-1 is not notified twice.
        self.assertFalse(db.has_unsent_1c_readings())

    def test_pending_legacy_baseline_keeps_payload_and_suppresses_notification(self) -> None:
        conn = db.get_conn()
        conn.execute("""INSERT INTO pokazaniya
            (chat_id, ls, resource_type, meter_number, value1, created_at, batch_id)
            VALUES (0, '100001', 'Вода', 'baseline', '10', '2000-01-01 00:00', 'legacy')""")
        conn.commit()
        conn.close()
        db.update_1c_sync_state(pending_batch_id="legacy")
        db.init_db()

        def response(batch_id, readings):
            return ({"batch_id": batch_id, "meters_changes": [],
                     "readings_status": [{**row, "status": "accepted"} for row in readings]}, None)

        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True), \
             patch.object(self.sync.client_1c, "sync_readings", side_effect=response) as send, \
             patch.object(self.sync, "notify_client") as notify:
            outcome = self.sync.sync_1c_job(self.now)

        self.assertEqual(outcome, "success")
        self.assertEqual(send.call_args.args[0], "legacy")
        self.assertEqual([row["meter_number"] for row in send.call_args.args[1]], ["baseline"])
        notify.assert_not_called()

    def test_scheduler_during_inflight_send_does_not_start_another_batch(self) -> None:
        db.add_pokazaniya(42, "100001", "Вода", "M-1", "10")
        nested_outcomes = []

        def response(batch_id, readings):
            nested_outcomes.append(self.sync.sync_1c_job(self.now))
            return None, "timeout"

        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True), \
             patch.object(self.sync.client_1c, "sync_readings", side_effect=response) as send:
            outcome = self.sync.sync_1c_job(self.now)

        self.assertEqual(outcome, "failed")
        self.assertEqual(nested_outcomes, ["not_due"])
        send.assert_called_once()

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

    def test_nested_non_object_and_non_string_values_are_invalid(self) -> None:
        reading = {
            "chat_id": 42,
            "ls": "100001",
            "meter_number": "M-1",
            "value1": "10",
            "value2": None,
            "submitted_at": "2026-08-19 12:00",
        }
        valid_status = {**reading, "status": "accepted"}
        malformed = (
            {"readings_status": [None], "meters_changes": []},
            {"readings_status": [[valid_status]], "meters_changes": []},
            {"readings_status": [{**valid_status, "ls": None}], "meters_changes": []},
            {"readings_status": [{**valid_status, "value1": ["10"]}], "meters_changes": []},
            {"readings_status": [valid_status], "meters_changes": [None]},
            {"readings_status": [valid_status], "meters_changes": [{
                "action": "added", "ls": "100001", "meter_number": "M-2",
                "resource_type": {"name": "Вода"}, "meter_type": "Однотарифный",
            }]},
        )

        for body in malformed:
            with self.subTest(body=body):
                response = {"batch_id": "batch-1", **body}
                self.assertFalse(self.sync._valid_result(response, "batch-1", [reading]))

    def test_partially_invalid_meter_delta_changes_nothing_and_can_retry(self) -> None:
        db.add_pokazaniya(42, "100001", "Электроэнергия", "M-1", "10")

        def response(batch_id, readings):
            status = {**readings[0], "status": "accepted"}
            return ({
                "batch_id": batch_id,
                "readings_status": [status],
                "meters_changes": [
                    {"action": "added", "ls": "100001", "meter_number": "M-2",
                     "resource_type": "Вода", "meter_type": "Однотарифный"},
                    {"action": "changed", "ls": "100001", "meter_number": "M-3",
                     "resource_type": "Вода", "meter_type": ["Двухтарифный"]},
                ],
            }, None)

        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True), \
             patch.object(self.sync, "_generate_batch_id", return_value="batch-atomic"), \
             patch.object(self.sync.client_1c, "sync_readings", side_effect=response):
            failed = self.sync.sync_1c_job(self.now)

        row = db.get_pokazaniya()[0]
        self.assertEqual(failed, "failed")
        self.assertEqual((row["sent_to_1c"], row["status_1c"]), (0, None))
        self.assertEqual(db.get_schetchiki("100001"), [])
        self.assertEqual(db.get_1c_sync_state()["pending_batch_id"], "batch-atomic")

        def corrected(batch_id, readings):
            return ({
                "batch_id": batch_id,
                "readings_status": [{**readings[0], "status": "accepted"}],
                "meters_changes": [],
            }, None)

        with patch.object(self.sync, "ENABLE_1C_INTEGRATION", True), \
             patch.object(self.sync.client_1c, "sync_readings", side_effect=corrected), \
             patch.object(self.sync, "_notify_reading"):
            retried = self.sync.sync_1c_job(self.now + timedelta(hours=1))

        self.assertEqual(retried, "success")
        self.assertEqual(db.get_pokazaniya()[0]["sent_to_1c"], 1)
        self.assertIsNone(db.get_1c_sync_state()["pending_batch_id"])

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
