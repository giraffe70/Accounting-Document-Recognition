import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app import validation
from app.core import invocation_trace


def _temporary_directory():
    temp_root = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    return tempfile.TemporaryDirectory(dir=temp_root)


class DiagnosticRecoveryTests(unittest.TestCase):
    def test_time_text_rotated_coordinates_and_embedded_raw_redaction(self):
        from app.core.geometry import rotate_box
        from app.core.normalization import norm_date
        self.assertEqual(norm_date("民國 0089 年 03 月 09 日 下午 3:12"), "2000-03-09")
        self.assertEqual(norm_date("2026年10月08日 下午 3時12分"), "2026-10-08")
        self.assertEqual(norm_date("2026/10/08–2026/10/09"), "2026/10/08–2026/10/09")
        box = [100,200,300,700]
        self.assertEqual(rotate_box(rotate_box(box,90),270), box)
        safe = invocation_trace._safe({"raw": 'prefix {"image":"data:image/png;base64,AAAA"}'})
        self.assertNotIn("base64", safe["raw"])

    def test_child_exception_returns_durable_trace_and_redacted_artifact(self):
        with _temporary_directory() as temp:
            root = Path(temp)
            runs = root / "runs"
            payload = {"run_id": "run-error", "id": "result-error", "setting": "synthetic", "worker_protocol":validation.WORKER_PROTOCOL, "parent_build":validation.LOADED_BUILD, "execution_version_sha256":validation._versioning.source_versions()["execution_version_sha256"]}
            trace_path = runs / payload["run_id"] / payload["id"] / "trace.json"

            def fail_after_trace(_payload):
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                with invocation_trace.capture(trace_path):
                    invocation_trace.add({"kind": "ocr", "lines": [{"text": "SYNTHETIC OCR"}]})
                    invocation_trace.add({"kind": "structured_response", "task": "extract",
                                          "raw": '{"voucher_id":"RAW-123", broken-json'})
                    invocation_trace.add({"kind": "model_parameters", "provider": "fake", "temperature": 0})
                    invocation_trace.add({"kind": "model_call_start", "input": [{"url": "data:image/png;base64,SECRET-PIXELS"}]})
                    raise json.JSONDecodeError("synthetic invalid JSON", "{broken", 1)

            class FakePipe:
                def __init__(self):
                    self.response = None
                    self.closed = False

                def send(self, response):
                    self.response = response

                def close(self):
                    self.closed = True

            pipe = FakePipe()
            with patch.object(validation, "RUNS", runs), patch.object(validation, "_recognize", side_effect=fail_after_trace):
                validation._child_entry(pipe, payload)
                recovered = validation._recover_diagnostics(payload)

            response = pipe.response
            self.assertTrue(pipe.closed)
            self.assertFalse(response["ok"])
            self.assertIn("JSONDecodeError", response["error"])
            kinds = [record["kind"] for record in response["data"]["trace"]]
            self.assertIn("ocr", kinds)
            self.assertIn("structured_response", kinds)
            self.assertIn("child_failure", kinds)
            self.assertEqual(response["data"]["trace"][-1]["parameters"]["setting"], "synthetic")
            self.assertEqual(response["data"]["ocr"][0]["text"], "SYNTHETIC OCR")
            self.assertEqual(recovered["ocr"][0]["text"], "SYNTHETIC OCR")
            self.assertEqual(response["data"]["artifacts"]["trace_json"]["path"], str(trace_path))
            self.assertTrue(trace_path.is_file())
            persisted = json.loads(trace_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted[-1]["kind"], "child_failure")
            self.assertNotIn("SECRET-PIXELS", trace_path.read_text(encoding="utf-8"))
            self.assertNotIn("SECRET-PIXELS", json.dumps(response["data"], ensure_ascii=False))

    def test_parent_timeout_recovers_last_completed_response_and_marks_interruption(self):
        with _temporary_directory() as temp:
            root = Path(temp)
            runs = root / "runs"
            database = root / "validation.db"
            payload = {"run_id": "run-timeout", "id": "result-timeout", "settings": {"model": "fake"}, "worker_protocol":validation.WORKER_PROTOCOL, "parent_build":validation.LOADED_BUILD, "execution_version_sha256":validation._versioning.source_versions()["execution_version_sha256"]}
            trace_path = runs / payload["run_id"] / payload["id"] / "trace.json"
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            invocation_trace.atomic_json(trace_path, [
                {"kind": "ocr", "lines": [{"text": "DURABLE OCR"}]},
                {"kind": "structured_response", "task": "extract", "round": 2,
                 "raw": '{"total_amount":8500,"status":"complete"}'},
            ])
            con = sqlite3.connect(database)
            try:
                con.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT, cancel_requested INTEGER)")
                con.execute("INSERT INTO runs VALUES(?,?,?)", (payload["run_id"], "running", 0))
                con.commit()
            finally:
                con.close()

            @contextmanager
            def connect():
                con = sqlite3.connect(database)
                con.row_factory = sqlite3.Row
                try:
                    yield con
                    con.commit()
                finally:
                    con.close()

            class FakePipe:
                def __init__(self):
                    self.closed = False

                def poll(self, _timeout):
                    return False

                def close(self):
                    self.closed = True

            class FakeProcess:
                def __init__(self, *_args, **_kwargs):
                    self.started = False
                    self.terminated = False
                    self.joins = []

                def start(self):
                    self.started = True

                def terminate(self):
                    self.terminated = True

                def join(self, timeout=None):
                    self.joins.append(timeout)

            parent, child = FakePipe(), FakePipe()
            process = FakeProcess()

            class FakeContext:
                def Pipe(self, _duplex):
                    return parent, child

                def Process(self, *_args, **_kwargs):
                    return process

            with patch.object(validation, "RUNS", runs), patch.object(validation, "DB", database), \
                 patch.object(validation, "_connect", side_effect=connect), \
                 patch.object(validation.mp, "get_context", return_value=FakeContext()), \
                 patch.object(validation, "runtime_status", return_value={"ready":True}):
                response = validation._run_child(payload, timeout=0.01)

            self.assertTrue(process.started)
            self.assertTrue(process.terminated)
            self.assertEqual(process.joins, [10])
            self.assertTrue(parent.closed)
            self.assertTrue(child.closed)
            self.assertIsNone(validation._active_process)
            self.assertTrue(response["timeout"])
            self.assertEqual(response["data"]["ocr"][0]["text"], "DURABLE OCR")
            records = response["data"]["trace"]
            self.assertEqual(records[-2]["raw"], '{"total_amount":8500,"status":"complete"}')
            self.assertEqual(records[-1]["kind"], "child_interruption")
            self.assertEqual(records[-1]["parameters"]["settings"]["model"], "fake")
            self.assertEqual(response["data"]["artifacts"]["trace_json"]["path"], str(trace_path))
            persisted = json.loads(trace_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted[-1]["kind"], "child_interruption")
            self.assertNotIn("data:image", trace_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
