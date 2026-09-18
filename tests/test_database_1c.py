from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import MagicMock, patch

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

    def test_bot_user_fio_account_transition_is_null_safe(self) -> None:
        db.upsert_bot_user(42, None, "Старое ФИО")

        db.upsert_bot_user(42, "new-account", None)
        self.assertIsNone(db.get_bot_user(42)["fio"])

        db.upsert_bot_user(42, "new-account", "Новое ФИО")
        db.upsert_bot_user(42, None, None)
        self.assertIsNone(db.get_bot_user(42)["fio"])

    def test_bot_user_fio_is_preserved_for_same_or_null_account(self) -> None:
        db.upsert_bot_user(42, None, "ФИО без ЛС")
        db.upsert_bot_user(42, None, None)
        self.assertEqual(db.get_bot_user(42)["fio"], "ФИО без ЛС")

        db.upsert_bot_user(43, "same-account", "ФИО с ЛС")
        db.upsert_bot_user(43, "same-account", None)
        self.assertEqual(db.get_bot_user(43)["fio"], "ФИО с ЛС")

    def test_bot_user_explicit_fio_clear_wins_without_account_change(self) -> None:
        db.upsert_bot_user(42, None, "ФИО без ЛС")
        db.upsert_bot_user(42, None, None, clear_fio=True)
        self.assertIsNone(db.get_bot_user(42)["fio"])

        db.upsert_bot_user(42, "same-account", "ФИО с ЛС")
        db.upsert_bot_user(42, "same-account", "Новое ФИО", clear_fio=True)
        self.assertIsNone(db.get_bot_user(42)["fio"])

        db.upsert_bot_user(43, "first-account", "Новое ФИО", clear_fio=True)
        self.assertIsNone(db.get_bot_user(43)["fio"])

    def test_bot_user_fio_update_priority_matrix(self) -> None:
        """Lock down clear/new-FIO/account-change precedence for all NULL cases."""
        chat_id = 1000
        for old_ls in (None, "account-a"):
            for new_ls in (None, "account-a", "account-b"):
                for new_fio in (None, "", "Новое ФИО"):
                    for clear_fio in (False, True):
                        chat_id += 1
                        db.upsert_bot_user(chat_id, old_ls, "Старое ФИО")

                        db.upsert_bot_user(
                            chat_id,
                            new_ls,
                            new_fio,
                            clear_fio=clear_fio,
                        )

                        if clear_fio:
                            expected_fio = None
                        elif new_fio:
                            expected_fio = new_fio
                        elif old_ls != new_ls:
                            expected_fio = None
                        else:
                            expected_fio = "Старое ФИО"
                        with self.subTest(
                            old_ls=old_ls,
                            new_ls=new_ls,
                            new_fio=new_fio,
                            clear_fio=clear_fio,
                        ):
                            row = db.get_bot_user(chat_id)
                            self.assertIsNotNone(row)
                            self.assertEqual(row["ls"], new_ls)
                            self.assertEqual(row["fio"], expected_fio)

    def claim(self, batch_id="new", now=None):
        return db.claim_1c_sync_batch(
            now or datetime(2099, 1, 1, tzinfo=timezone.utc), batch_id, 500, 1, 24,
        )

    def test_state_write_failure_rolls_back_batch_membership(self) -> None:
        db.add_pokazaniya(11, "100001", "Вода", "M-1", "10")
        conn = db.get_conn()
        conn.execute("""CREATE TRIGGER fail_claim BEFORE UPDATE ON sync_1c_state
                        BEGIN SELECT RAISE(ABORT, 'simulated crash'); END""")
        conn.commit()
        conn.close()

        with self.assertRaises(sqlite3.IntegrityError):
            self.claim()

        self.assertIsNone(db.get_1c_sync_state()["pending_batch_id"])
        self.assertEqual(len(db.get_unsent_1c_readings(500)), 1)
        self.assertEqual(db.get_1c_readings_by_batch("new"), [])

    def test_concurrent_scheduler_claims_have_one_winner(self) -> None:
        db.add_pokazaniya(11, "100001", "Вода", "M-1", "10")
        barrier = Barrier(2)

        def claim(batch_id):
            barrier.wait(timeout=5)
            return self.claim(batch_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["worker-1", "worker-2"]))

        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        batch_id = winners[0]["batch_id"]
        self.assertEqual(db.get_1c_sync_state()["pending_batch_id"], batch_id)
        self.assertEqual([row["batch_id"] for row in winners[0]["rows"]], [batch_id])

    def test_stale_completion_cannot_clear_newer_attempt(self) -> None:
        first = self.claim()
        now = datetime(2099, 1, 1, 1, tzinfo=timezone.utc)
        second = self.claim("unused", now=now)
        self.assertFalse(db.complete_1c_sync_batch(first["batch_id"], first["attempt_at"]))
        self.assertEqual(db.get_1c_sync_state()["last_attempt_at"], second["attempt_at"])
        self.assertEqual(db.get_1c_sync_state()["pending_batch_id"], "new")

    def test_legacy_migration_excludes_only_exact_unassigned_baseline(self) -> None:
        conn = db.get_conn()
        conn.execute("DROP TABLE pokazaniya")
        conn.execute("""CREATE TABLE pokazaniya (
            id INTEGER PRIMARY KEY, chat_id INTEGER, ls TEXT, resource_type TEXT,
            meter_number TEXT, value1 TEXT, value2 TEXT, created_at TEXT)""")
        samples = [(0, "2000-01-01 00:00"), (11, "2000-01-01 00:00"),
                   (0, "2026-08-19 11:59"), (None, None)]
        conn.executemany("INSERT INTO pokazaniya (chat_id, created_at) VALUES (?, ?)", samples)
        conn.commit()
        conn.close()

        db.init_db()
        db.init_db()

        rows = list(reversed(db.get_pokazaniya()))
        self.assertEqual([row["sent_to_1c"] for row in rows], [1, 0, 0, 0])
        self.assertEqual(len(db.get_unsent_1c_readings(500)), 3)

    def test_imported_baseline_is_history_but_never_new_queue_work(self) -> None:
        workbook = MagicMock()
        workbook.sheetnames = ["Счетчики"]
        workbook.__getitem__.return_value.iter_rows.return_value = [
            ["ls", "resource", "number", "type", "initial1", "initial2"],
            ["100001", "Вода", "M-1", "Однотарифный", 10, 0],
        ]
        excel = MagicMock()
        excel.load_workbook.return_value = workbook
        with patch.dict("sys.modules", {"openpyxl": excel}):
            db.import_from_excel("meters.xlsx")

        self.assertEqual(len(db.get_pokazaniya()), 1)
        self.assertEqual(db.get_pokazaniya()[0]["sent_to_1c"], 1)
        self.assertFalse(db.has_unsent_1c_readings())
        self.assertEqual(self.claim()["rows"], [])

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

    def test_atomic_sync_result_rolls_back_status_and_earlier_meter_change(self) -> None:
        db.add_pokazaniya(11, "100001", "Электроэнергия", "M-1", "10")
        claim = self.claim("batch-atomic")
        reading = claim["rows"][0]
        statuses = [{
            "ls": reading["ls"],
            "meter_number": reading["meter_number"],
            "value1": reading["value1"],
            "value2": reading["value2"],
            "submitted_at": reading["created_at"],
            "status": "accepted",
        }]
        changes = [
            {"action": "added", "ls": "100001", "meter_number": "M-2",
             "resource_type": "Вода", "meter_type": "Однотарифный"},
            {"action": "added", "ls": "100001", "meter_number": "M-3",
             "resource_type": "Вода", "meter_type": "invalid"},
        ]

        with self.assertRaises(ValueError):
            db.apply_1c_sync_result(
                claim["batch_id"], claim["attempt_at"], statuses, changes
            )

        stored = db.get_1c_readings_by_batch("batch-atomic")[0]
        self.assertEqual((stored["sent_to_1c"], stored["status_1c"]), (0, None))
        self.assertEqual(db.get_schetchiki("100001"), [])
        self.assertEqual(
            db.get_1c_sync_state()["pending_batch_id"], "batch-atomic"
        )

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
