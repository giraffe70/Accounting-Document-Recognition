"""Offline retirement contracts using synthetic SQLite data and HTTP routes."""
import ast
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlmodel import SQLModel

from app.db import models, session as db_session
from app.db.entry_retirement import RETIRED_COLUMNS, retire_entry_management
from app.core import activity, model_invocation, settings_store
from app.services import reports

ROOT = Path(__file__).resolve().parents[1]


class EntryRetirementMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "app.db"
        self.engine = create_engine(f"sqlite:///{self.path}")
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def enable_fks(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

    def legacy_database(self):
        with self.engine.begin() as c:
            c.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            c.exec_driver_sql("INSERT INTO users VALUES (7)")
            c.exec_driver_sql("""CREATE TABLE voucher_records (
                id INTEGER PRIMARY KEY, current_json TEXT NOT NULL, ai_json TEXT,
                recognition_history TEXT, status TEXT, confirmed_by INTEGER,
                confirmed_at TEXT, confirmed_by_name TEXT, recognition_confirmed_version INTEGER,
                company_id INTEGER, source_parent_id INTEGER,
                custom_note TEXT DEFAULT 'commas, and (parentheses)',
                entries_status TEXT, entries_confirmed_at TEXT,
                entries_confirmed_by INTEGER, entries_confirmed_by_name TEXT,
                entries_proposal_json TEXT, entries_history TEXT,
                FOREIGN KEY(confirmed_by) REFERENCES users(id),
                FOREIGN KEY(entries_confirmed_by) REFERENCES users(id),
                FOREIGN KEY(source_parent_id) REFERENCES voucher_records(id)
            )""")
            c.exec_driver_sql("CREATE INDEX ix_voucher_records_entries_status ON voucher_records(entries_status)")
            c.exec_driver_sql("CREATE INDEX keep_status ON voucher_records(status)")
            c.exec_driver_sql("CREATE TABLE mappings (id INTEGER PRIMARY KEY, voucher_id INTEGER REFERENCES voucher_records(id))")
            c.exec_driver_sql("CREATE TABLE audit (voucher_id INTEGER)")
            c.exec_driver_sql("CREATE TRIGGER keep_audit AFTER INSERT ON voucher_records BEGIN INSERT INTO audit VALUES (new.id); END")
            for number, status in enumerate(("none", "draft", "ready"), 1):
                c.exec_driver_sql("""INSERT INTO voucher_records
                    (id,current_json,ai_json,recognition_history,status,confirmed_by,
                     confirmed_at,confirmed_by_name,recognition_confirmed_version,
                     company_id,source_parent_id,entries_status,entries_confirmed_by,
                     entries_proposal_json,entries_history)
                    VALUES (?, ?, ?, ?, 'confirmed', 7, '2026-10-01', '覆核者', 1,
                            4, ?, ?, 7, '{"entries":[]}', '[{"action":"confirmed"}]')""",
                    (number, '{"entries":[{"amount":null}],"items":[],"details_version":1}',
                     '{"原始": "資料"}', '[{"model_result":{"entries":[]}}]',
                     1 if number > 1 else None, status))
            c.exec_driver_sql("INSERT INTO mappings VALUES (1, 2)")

    def snapshot(self):
        with self.engine.connect() as c:
            columns = [r[1] for r in c.exec_driver_sql("PRAGMA table_info(voucher_records)")
                       if r[1] not in RETIRED_COLUMNS]
            names = ",".join('"' + name + '"' for name in columns)
            return columns, c.exec_driver_sql(f"SELECT {names} FROM voucher_records ORDER BY id").all()

    def test_rebuild_preserves_json_unknown_columns_indexes_triggers_and_foreign_keys(self):
        self.legacy_database()
        before = self.snapshot()
        backup = retire_entry_management(self.engine)
        self.assertTrue(backup.is_file())
        self.assertEqual(self.snapshot(), before)
        with closing(sqlite3.connect(backup)) as c:
            self.assertTrue(RETIRED_COLUMNS <= {r[1] for r in c.execute("PRAGMA table_info(voucher_records)")})
            self.assertEqual(c.execute("SELECT COUNT(*) FROM voucher_records").fetchone()[0], 3)
        with self.engine.begin() as c:
            self.assertTrue(RETIRED_COLUMNS.isdisjoint(r[1] for r in c.exec_driver_sql("PRAGMA table_info(voucher_records)")))
            self.assertEqual(c.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
            self.assertEqual(c.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
            indexes = {r[1] for r in c.exec_driver_sql("PRAGMA index_list(voucher_records)")}
            self.assertEqual(indexes, {"keep_status"})
            c.exec_driver_sql("INSERT INTO voucher_records (id,current_json) VALUES (8,'{}')")
            self.assertEqual(c.exec_driver_sql("SELECT COUNT(*) FROM audit WHERE voucher_id=8").scalar_one(), 1)
        self.assertIsNone(retire_entry_management(self.engine))
        self.assertEqual(len(list((self.path.parent / "backups").glob("*.db"))), 1)

    def test_failure_after_old_table_drop_rolls_back_schema_data_and_foreign_key_state(self):
        self.legacy_database()
        before = self.snapshot()

        def fail_rename(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith('ALTER TABLE "_voucher_records_without_posting" RENAME'):
                raise RuntimeError("synthetic migration failure")

        event.listen(self.engine, "before_cursor_execute", fail_rename)
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic migration failure"):
                retire_entry_management(self.engine)
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_rename)
        self.assertEqual(self.snapshot(), before)
        with self.engine.connect() as c:
            self.assertTrue(RETIRED_COLUMNS <= {r[1] for r in c.exec_driver_sql("PRAGMA table_info(voucher_records)")})
            self.assertEqual(c.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
            self.assertEqual(c.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
            self.assertIsNone(c.exec_driver_sql("SELECT name FROM sqlite_master WHERE name='_voucher_records_without_posting'").first())

    def test_backup_failure_prevents_any_schema_change(self):
        self.legacy_database()
        with patch("app.db.entry_retirement._backup", side_effect=OSError("backup failed")):
            with self.assertRaisesRegex(OSError, "backup failed"):
                retire_entry_management(self.engine)
        with self.engine.connect() as c:
            self.assertTrue(RETIRED_COLUMNS <= {r[1] for r in c.exec_driver_sql("PRAGMA table_info(voucher_records)")})

    def test_new_database_startup_never_creates_retired_columns(self):
        with patch.object(db_session, "_engine", self.engine), \
             patch.object(db_session.config, "DATA_DIR", Path(self.tmp.name)):
            db_session.init_db()
            db_session.init_db()
        with self.engine.connect() as c:
            columns = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(voucher_records)")}
            self.assertTrue(RETIRED_COLUMNS.isdisjoint(columns))
            self.assertIn("confirmed_by", columns)
        self.assertFalse((self.path.parent / "backups").exists())


class RetiredInterfacesTests(unittest.TestCase):
    def test_retired_routes_are_unregistered_and_summary_is_the_only_template(self):
        tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
        routes = set()
        report_node = None
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.args:
                        if isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                            routes.add(decorator.args[0].value)
                if node.name == "api_report":
                    report_node = node
        self.assertTrue({"/api/vouchers/{rec_id}/classify", "/api/vouchers/{rec_id}/entries/{action}",
                         "/api/journal", "/api/journal/periods"}.isdisjoint(routes))
        self.assertIn("/api/vouchers/{rec_id}/recognition-report", routes)
        self.assertEqual([r["id"] for r in reports.TEMPLATES], ["summary"])
        self.assertIsNotNone(report_node)
        report_node.decorator_list = []
        from fastapi.responses import HTMLResponse, JSONResponse
        namespace = {"HTMLResponse": HTMLResponse, "JSONResponse": JSONResponse, "reports": reports}
        exec(compile(ast.Module(body=[report_node], type_ignores=[]), "report-route", "exec"), namespace)
        for template in ("journal_voucher", "unknown"):
            response = namespace["api_report"](template, "2026-10", "")
            self.assertEqual(response.status_code, 404)
        with patch.object(reports, "render", return_value="synthetic summary"):
            self.assertEqual(namespace["api_report"]("summary", "2026-10", "").status_code, 200)

    def test_old_accounting_setting_is_ignored_and_not_resaved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"ocr":"disabled/none","structure":"fake/recognition","classify":"fake/posting"}', encoding="utf-8")
            with patch.object(settings_store.config, "SETTINGS_FILE", path), patch.object(settings_store, "_settings", None):
                self.assertNotIn("classify", settings_store.get_settings())
                self.assertNotIn("classify", settings_store.update_settings({"classify": "fake/ignored"}))
                self.assertNotIn('"classify"', path.read_text(encoding="utf-8"))
        self.assertEqual(activity.STAGES, ("ocr", "structure"))

    def test_v3_traces_are_not_treated_as_current_execution(self):
        with patch.object(model_invocation, "source_versions", return_value={"hash_version": 4, "execution_version_sha256": "new"}):
            self.assertFalse(model_invocation.same_execution_version({"hash_version": 3, "execution_version_sha256": "new"}))
            self.assertTrue(model_invocation.same_execution_version({"hash_version": 4, "execution_version_sha256": "new"}))


if __name__ == "__main__":
    unittest.main()
