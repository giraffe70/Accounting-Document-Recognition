"""Offline migration and recognition regressions using synthetic records only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.db import session as db_session
from app.db.manual_entry_retirement import retire_manual_entry
from app.db.models import ReviewStatus, VoucherRecord
from app.models.schemas import Voucher
from app.services import review


class ManualEntryRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.db"
        self.engine = create_engine(f"sqlite:///{self.path}")
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def enable_fks(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

    def legacy(self, flags=(0, "0", None), data=None):
        raw = json.dumps(data if data is not None else {"fields": {"total_amount": 123}}, ensure_ascii=False)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("""CREATE TABLE voucher_records (
                id INTEGER PRIMARY KEY, current_json TEXT, ai_json TEXT,
                manual_entry INTEGER, status TEXT DEFAULT 'confirmed',
                confirmed_at TEXT DEFAULT '2026-10-01 10:00:00.123456',
                custom_note TEXT DEFAULT 'keep',
                source_parent_id INTEGER REFERENCES voucher_records(id))""")
            connection.exec_driver_sql("CREATE INDEX keep_status ON voucher_records(status)")
            connection.exec_driver_sql("CREATE TABLE references_to_vouchers (id INTEGER REFERENCES voucher_records(id))")
            connection.exec_driver_sql("CREATE TABLE audit (id INTEGER)")
            connection.exec_driver_sql("CREATE TRIGGER keep_audit AFTER INSERT ON voucher_records BEGIN INSERT INTO audit VALUES (new.id); END")
            for ident, flag in enumerate(flags, 1):
                connection.exec_driver_sql("""INSERT INTO voucher_records
                    (id,current_json,ai_json,manual_entry,source_parent_id) VALUES (?,?,?,?,?)""",
                    (ident, raw, '{"original":true}', flag, 1 if ident > 1 else None))
            connection.exec_driver_sql("INSERT INTO references_to_vouchers VALUES (1)")
        return raw

    def snapshot(self):
        with self.engine.connect() as connection:
            return connection.exec_driver_sql("SELECT id,current_json,ai_json,status,confirmed_at,custom_note,source_parent_id FROM voucher_records ORDER BY id").all()

    def test_false_flags_leave_all_remaining_data_and_database_objects_unchanged(self):
        self.legacy()
        before = self.snapshot()
        backup = retire_manual_entry(self.engine)
        self.assertEqual(self.snapshot(), before)
        with closing(sqlite3.connect(backup)) as saved:
            self.assertIn("manual_entry", {r[1] for r in saved.execute("PRAGMA table_info(voucher_records)")})
            self.assertEqual(saved.execute("SELECT COUNT(*) FROM voucher_records").fetchone()[0], 3)
        with self.engine.begin() as connection:
            self.assertNotIn("manual_entry", {r[1] for r in connection.exec_driver_sql("PRAGMA table_info(voucher_records)")})
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
            self.assertIn("keep_status", {r[1] for r in connection.exec_driver_sql("PRAGMA index_list(voucher_records)")})
            connection.exec_driver_sql("INSERT INTO voucher_records (id) VALUES (8)")
            self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM audit WHERE id=8").scalar_one(), 1)
        self.assertIsNone(retire_manual_entry(self.engine))
        self.assertEqual(len(list((self.path.parent / "backups").glob("*.db"))), 1)

    def test_true_flags_become_selected_manual_evidence_without_losing_values(self):
        current = {"fields": {"total_amount": 125, "tax": None}, "items": [], "entries": [],
                   "doc_type_key": "receipt", "doc_type": "收據", "note": "手動備註",
                   "field_evidence": {"fields.total_amount": [
                       {"source": "manual", "value": 125, "selected": True}],
                       "doc_type_key": [{"source": "route", "value": "receipt", "selected": True}]}}
        self.legacy((1, "true", "0"), current)
        before = self.snapshot()
        retire_manual_entry(self.engine)
        after = self.snapshot()
        for row in after[:2]:
            migrated = json.loads(row[1])
            self.assertEqual({k: v for k, v in migrated.items() if k != "field_evidence"},
                             {k: v for k, v in current.items() if k != "field_evidence"})
            for path in ("fields.total_amount", "fields.tax", "items", "entries", "note", "doc_type_key", "doc_type"):
                self.assertTrue(any(r.get("source") == "manual" and r.get("selected")
                                    for r in migrated["field_evidence"][path]))
            self.assertEqual(migrated["field_evidence"]["fields.total_amount"], current["field_evidence"]["fields.total_amount"])
            self.assertFalse(migrated["field_evidence"]["doc_type_key"][0]["selected"])
        self.assertEqual(after[2], before[2])
        self.assertEqual([r[2:] for r in after], [r[2:] for r in before])

    def test_invalid_flag_or_flagged_json_aborts_without_changing_database(self):
        self.legacy((1,))
        for assignment in ("manual_entry=7", "manual_entry=1,current_json='not json'"):
            with self.engine.begin() as connection:
                connection.exec_driver_sql(f"UPDATE voucher_records SET {assignment}")
            before = self.snapshot()
            with self.assertRaises(ValueError):
                retire_manual_entry(self.engine)
            self.assertEqual(self.snapshot(), before)
            with self.engine.connect() as connection:
                self.assertIn("manual_entry", {r[1] for r in connection.exec_driver_sql("PRAGMA table_info(voucher_records)")})

    def test_drop_failure_rolls_back_converted_evidence_and_keeps_column(self):
        self.legacy((1,))
        before = self.snapshot()

        def fail_drop(connection, cursor, statement, parameters, context, executemany):
            if statement == "ALTER TABLE voucher_records DROP COLUMN manual_entry":
                raise RuntimeError("synthetic drop failure")

        event.listen(self.engine, "before_cursor_execute", fail_drop)
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic drop failure"):
                retire_manual_entry(self.engine)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_drop)
        self.assertEqual(self.snapshot(), before)
        with self.engine.connect() as connection:
            self.assertIn("manual_entry", {r[1] for r in connection.exec_driver_sql("PRAGMA table_info(voucher_records)")})

    def test_backup_failure_prevents_migration(self):
        self.legacy((1,))
        before = self.snapshot()
        with patch("app.db.manual_entry_retirement._backup", side_effect=OSError("backup failed")):
            with self.assertRaisesRegex(OSError, "backup failed"):
                retire_manual_entry(self.engine)
        self.assertEqual(self.snapshot(), before)

    def test_new_database_and_repeated_startup_do_not_create_flag(self):
        SQLModel.metadata.create_all(self.engine)
        with patch.object(db_session, "_engine", self.engine):
            db_session._migrate()
            db_session._migrate()
        with self.engine.connect() as connection:
            self.assertNotIn("manual_entry", {r[1] for r in connection.exec_driver_sql("PRAGMA table_info(voucher_records)")})
        self.assertNotIn("manual_entry", VoucherRecord.model_fields)
        self.assertFalse((self.path.parent / "backups").exists())

    def test_migrated_record_keeps_manual_content_when_new_model_result_is_saved(self):
        SQLModel.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE voucher_records ADD COLUMN manual_entry INTEGER DEFAULT 0")
        with patch.object(review, "get_session", side_effect=lambda: Session(self.engine, expire_on_commit=False)):
            original = Voucher.model_validate({"doc_type_key": "receipt", "doc_type": "收據",
                "fields": {"total_amount": 125}, "note": "人工備註", "items": [], "entries": []})
            record = review.save_voucher(original, status=ReviewStatus.confirmed)
            with self.engine.begin() as connection:
                connection.exec_driver_sql("UPDATE voucher_records SET manual_entry=1")
            retire_manual_entry(self.engine)
            replacement = original.model_copy(deep=True)
            replacement.fields["total_amount"] = 900
            replacement.note = "模型備註"
            with patch("app.core.pipeline.canonicalize_voucher", side_effect=lambda value: value), \
                 patch("app.core.model_invocation.source_versions", return_value={"version": "synthetic"}):
                updated = review._save_recognition(record.id, replacement)
            current = json.loads(updated.current_json)
            self.assertEqual(current["fields"]["total_amount"], 125)
            self.assertEqual(current["note"], "人工備註")
            self.assertEqual(current["items"], [])
            self.assertEqual(current["entries"], [])
            self.assertEqual(updated.status, ReviewStatus.pending)
            self.assertEqual(json.loads(updated.recognition_history)[-1]["model_result"]["fields"]["total_amount"], 900)


if __name__ == "__main__":
    unittest.main()
