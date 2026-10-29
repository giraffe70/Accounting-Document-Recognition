"""Isolated offline regressions for API and validation-version contracts."""
from __future__ import annotations

import ast
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from app.core import doc_types
from app import validation


ROOT = Path(__file__).resolve().parents[1]


def load_endpoint(name: str, namespace: dict[str, object]) -> object:
    """Compile one undecorated endpoint from main.py without importing app.main."""
    path = ROOT / "app" / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    isolated = copy.deepcopy(function)
    isolated.decorator_list = []
    module = ast.Module(body=[isolated], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class ApiContractTests(unittest.TestCase):
    def test_doc_types_endpoint_exposes_import_item_contract(self):
        endpoint = load_endpoint("api_doc_types", {"doc_types": doc_types})
        response = endpoint()
        import_item_keys = {field["key"] for field in response["types"]["import_declaration"]["item_fields"]}
        self.assertTrue({"hs_code", "origin_country"} <= import_item_keys)
        self.assertIn("fee_code", {f["key"] for f in response["types"]["social_insurance"]["item_fields"]})
        self.assertTrue(response["types"]["export_declaration"]["has_items"])
        broker_tax = next(f for f in response["types"]["broker_note"]["fields"] if f["key"] == "tax")
        self.assertEqual(broker_tax["money"]["purpose"], "vat")

class ValidationVersionGateTests(unittest.TestCase):
    @staticmethod
    def _make_database(path: Path, settings: dict) -> None:
        con = sqlite3.connect(path)
        try:
            con.executescript("""
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY, status TEXT, settings_json TEXT,
                    finished_at TEXT, current TEXT, done INTEGER, succeeded INTEGER,
                    failed INTEGER, cancel_requested INTEGER, total INTEGER
                );
                CREATE TABLE results (
                    id TEXT PRIMARY KEY, run_id TEXT, status TEXT,
                    error TEXT, finished_at TEXT
                );
            """)
            con.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("run-1", "cancelled", json.dumps(settings), "finished", "old current", 3, 2, 1, 1, 4),
            )
            con.execute(
                "INSERT INTO results VALUES(?,?,?,?,?)",
                ("result-1", "run-1", "failed", "historical failure", "result-finished"),
            )
            con.commit()
        finally:
            con.close()

    def _assert_conflict_and_unchanged(self, action: str, settings: dict, *, patch_versions=None):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "validation.sqlite"
            self._make_database(db_path, settings)

            @contextmanager
            def connect():
                connection = sqlite3.connect(db_path)
                connection.row_factory = sqlite3.Row
                try:
                    yield connection
                finally:
                    connection.close()

            wake = Mock()
            version_patch = patch_versions or patch(
                "app.core.model_invocation.source_versions",
                return_value={"execution_version_sha256": "current-execution-version"},
            )
            with patch.object(validation, "_connect", side_effect=connect), \
                 patch.object(validation, "_wake", wake), version_patch, \
                 patch.object(validation, "runtime_status", return_value={"ready":True}):
                response = getattr(validation, action)("run-1")

            self.assertEqual(response.status_code, 409)
            response_body = json.loads(response.body.decode("utf-8"))
            self.assertIn("歷史結果仍可查看", response_body["detail"])
            wake.set.assert_not_called()
            con = sqlite3.connect(db_path)
            try:
                run = con.execute(
                    "SELECT status,settings_json,finished_at,current,done,succeeded,failed,cancel_requested,total "
                    "FROM runs WHERE id='run-1'"
                ).fetchone()
                result = con.execute(
                    "SELECT status,error,finished_at FROM results WHERE id='result-1'"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(run, ("cancelled", json.dumps(settings), "finished", "old current", 3, 2, 1, 1, 4))
            self.assertEqual(result, ("failed", "historical failure", "result-finished"))

    def test_resume_rejects_legacy_settings_without_touching_history(self):
        self._assert_conflict_and_unchanged("resume_run", {"hash_version": 3})

    def test_retry_rejects_legacy_settings_without_touching_history(self):
        self._assert_conflict_and_unchanged("retry_run", {"hash_version": 3})

    def test_resume_rejects_execution_version_mismatch_without_touching_history(self):
        settings = {"hash_version": 4, "execution_version_sha256": "old-execution-version"}
        self._assert_conflict_and_unchanged("resume_run", settings)

    def test_retry_rejects_execution_version_mismatch_without_touching_history(self):
        settings = {"hash_version": 4, "execution_version_sha256": "old-execution-version"}
        self._assert_conflict_and_unchanged("retry_run", settings)


if __name__ == "__main__":
    unittest.main()
