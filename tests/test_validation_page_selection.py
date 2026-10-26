"""Offline regression tests for validation-run page selection."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import validation


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class ValidationPageSelectionTests(unittest.TestCase):
    def setUp(self):
        tmp_parent = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
        tmp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=tmp_parent)
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "validation.db"
        with self._connect() as con:
            con.executescript("""
                CREATE TABLE datasets (id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE pages (
                    id TEXT PRIMARY KEY, dataset_id TEXT, relative_path TEXT,
                    page_no INTEGER, voucher_key TEXT, sha256 TEXT,
                    render_seconds REAL, original_path TEXT, page_path TEXT
                );
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY, dataset_id TEXT, name TEXT, status TEXT,
                    created_at TEXT, settings_json TEXT, total INTEGER,
                    done INTEGER DEFAULT 0, succeeded INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0, current TEXT DEFAULT '',
                    cancel_requested INTEGER DEFAULT 0
                );
                CREATE TABLE results (
                    id TEXT PRIMARY KEY, run_id TEXT, page_id TEXT,
                    combo_id TEXT, combo_name TEXT, mode TEXT,
                    repeat_no INTEGER, warmup INTEGER, status TEXT, queued_at TEXT
                );
                INSERT INTO datasets VALUES ('dataset-a', 'Synthetic A');
                INSERT INTO datasets VALUES ('dataset-b', 'Synthetic B');
                INSERT INTO pages VALUES
                    ('a-page-1', 'dataset-a', 'a/one.png', 1, 'a/one', 'sha-a1', 0, '', ''),
                    ('a-page-2', 'dataset-a', 'a/two.png', 2, 'a/two', 'sha-a2', 0, '', ''),
                    ('b-page-1', 'dataset-b', 'b/one.png', 1, 'b/one', 'sha-b1', 0, '', '');
            """)

    def _connect(self):
        con = sqlite3.connect(self.db_path, factory=_ClosingConnection)
        con.row_factory = sqlite3.Row
        return con

    def _run(self, page_ids):
        combo = {"id": "synthetic", "name": "Synthetic", "mode": "ocr_llm",
                 "structure_model": "fake/model", "classify_model": "fake/model"}
        with patch("app.validation._connect", side_effect=self._connect), \
                patch("app.validation.runtime_status", return_value={"ready": True}), \
                patch("app.validation._normalize_combos", return_value=[combo]), \
                patch("app.validation._wake.set"), \
                patch("app.validation.get_run", side_effect=lambda run_id: {"id": run_id}), \
                patch("app.core.model_invocation.source_versions", return_value={
                    "execution_version_sha256": "synthetic-execution",
                    "code_version_sha256": "synthetic-code",
                    "prompt_version_sha256": "synthetic-prompt",
                    "policy_version_sha256": "synthetic-policy",
                }):
            return validation.create_run({"dataset_id": "dataset-a", "page_ids": page_ids,
                                          "combinations": [combo]})

    def test_selected_page_creates_only_its_task_and_snapshots_exact_selection(self):
        response = self._run(["a-page-2"])
        with self._connect() as con:
            run = con.execute("SELECT id,settings_json,total FROM runs").fetchone()
            settings = json.loads(run["settings_json"])
            result_pages = [row[0] for row in con.execute(
                "SELECT page_id FROM results WHERE run_id=?", (run["id"],))]
        self.assertEqual(response["id"], run["id"])
        self.assertEqual(settings["page_ids"], ["a-page-2"])
        self.assertEqual(result_pages, ["a-page-2"])
        self.assertEqual(run["total"], 1)

    def test_empty_duplicate_and_foreign_page_ids_are_rejected(self):
        for selected in ([], ["a-page-1", "a-page-1"], ["b-page-1"]):
            with self.subTest(page_ids=selected):
                response = self._run(selected)
                self.assertEqual(response.status_code, 400)
                with self._connect() as con:
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
