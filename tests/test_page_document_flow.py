import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from sqlmodel import Session, SQLModel, create_engine

from app import config, validation
from app.db.models import ReviewStatus, VoucherRecord
from app.models.schemas import Voucher
from app.services import reports, review


def _temporary_directory():
    temp_root = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    return tempfile.TemporaryDirectory(dir=temp_root)


def _layout(regions):
    return {"content_kind": "document", "auto_split": True, "regions": regions}


def _region(type_key, box):
    return {"type": type_key, "rotate": 0, "reason": "synthetic test", "confidence": "high", "box": box}


class PreparedVoucherFlowTests(unittest.TestCase):
    def test_source_parent_children_listing_approval_guard_and_manual_recognition_history(self):
        with _temporary_directory() as temp:
            root = Path(temp)
            engine = create_engine(f"sqlite:///{root / 'review.db'}", connect_args={"check_same_thread": False})
            SQLModel.metadata.create_all(engine)
            source = root / "scan.png"
            Image.new("RGB", (120, 80), "white").save(source)
            before_hash = hashlib.sha256(source.read_bytes()).hexdigest()

            def voucher(role="voucher", amount=None, token="source-token"):
                data = {"doc_type_key": "receipt", "doc_type": "收據／雜項開銷",
                        "fields": {"vendor": "合成店家", "total_amount": amount},
                        "date": "2026-10-01", "origin": {"source_token": token}, "source_file": "scan.pdf"}
                if role == "source_page":
                    data["origin"].update(record_role="source_page", page_no=1)
                result = Voucher.model_validate(data)
                result.source_image = str(source)
                return result

            prepared = [voucher("source_page"), voucher(amount=100), voucher(amount=200), voucher(amount=300)]
            with patch.object(review, "get_session", side_effect=lambda: Session(engine)), \
                 patch.object(config, "UPLOAD_DIR", root / "uploads"):
                records = review.save_prepared_vouchers(prepared, company="Synthetic Co")
                self.assertEqual(len(records), 4)
                self.assertEqual(records[0].record_role, "source_page")
                self.assertTrue(all(r.source_parent_id == records[0].id for r in records[1:]))
                self.assertEqual([r.record_role for r in review.list_vouchers()], ["voucher"] * 3)
                self.assertEqual(len(review.list_vouchers(include_sources=True)), 4)
                self.assertIn("共 3 張", reports._summary("2026-10", "Synthetic Co"))
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before_hash)

                with self.assertRaises(review.RecognitionProtected):
                    review.update_current(records[0].id, {"fields": {"total_amount": 123}})
                with self.assertRaises(review.RecognitionProtected):
                    review.clerk_review(records[0].id)
                with self.assertRaises(review.RecognitionProtected):
                    review.crop_image(records[0].id, [0, 0, 0.5, 0.5])

                approved = review.save_voucher(voucher(amount=400), status=ReviewStatus.accountant_approved)
                with self.assertRaises(review.RecognitionProtected):
                    review._save_recognition(approved.id, voucher(amount=999))

                original_ai = json.loads(records[1].ai_json)
                manual_edit = {"fields": {"total_amount": 200},
                               "items": [{"description": "人工品項", "amount": 200}],
                               "entries": [{"description": "人工分錄", "amount": 200}]}
                with patch("app.core.pipeline.canonicalize_voucher", side_effect=lambda value: value):
                    review.update_current(records[1].id, manual_edit)
                current_record = review.get_voucher(records[1].id)
                current = json.loads(current_record.current_json)
                self.assertTrue(current["field_evidence"]["entries"][-1]["source"] == "manual")
                self.assertTrue(current["field_evidence"]["entries"][-1]["selected"])

                model_result = voucher(amount=900)
                with patch("app.core.pipeline.canonicalize_voucher", side_effect=lambda value: value), \
                     patch("app.core.model_invocation.source_versions", return_value={"version": "synthetic"}):
                    review._save_recognition(records[1].id, model_result)
                updated = review.get_voucher(records[1].id)
                new_current = json.loads(updated.current_json)
                history = json.loads(updated.recognition_history)
                self.assertEqual(json.loads(updated.ai_json), original_ai)
                self.assertEqual(new_current["fields"]["total_amount"], 200)
                self.assertEqual(new_current["items"][0]["amount"], 200)
                self.assertEqual(new_current["entries"][0]["amount"], 200)
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["model_result"]["fields"]["total_amount"], 900)
            engine.dispose()


class PageDocumentValidationTests(unittest.TestCase):
    def test_zero_pixel_region_keeps_other_document_and_reports_failure(self):
        with _temporary_directory() as temp:
            root = Path(temp)
            image = self._page_image(root)
            calls = []
            def runner(payload, timeout):
                if payload["phase"] == "layout":
                    return {"ok":True,"data":{"layout":_layout([
                        _region("receipt", [0,0,1,1]), _region("receipt", [500,0,1000,1000])])}}
                calls.append(payload["phase"])
                return {"ok":True,"data":{"voucher":{"voucher_id":"KEPT"}}}
            with patch.object(validation, "RUNS", root / "runs"):
                result = validation._run_page_child(self._payload(image),30,runner=runner)["data"]
            self.assertEqual(result["page_status"], "partial")
            self.assertEqual([d["status"] for d in result["documents"]], ["failed","succeeded"])
            self.assertEqual(result["documents"][1]["voucher"]["voucher_id"], "KEPT")
            self.assertEqual(calls, ["document"])
            self.assertTrue(Path(result["documents"][0]["artifacts"]["trace_json"]["path"]).is_file())

    def _page_image(self, root):
        path = root / "page.png"
        Image.new("RGB", (200, 120), "white").save(path)
        return path

    def _payload(self, image, run_id="run-synthetic", result_id="page-synthetic"):
        return {"run_id": run_id, "id": result_id, "page_no": 1,
                "page_path": str(image), "combo": {"mode": "image"}}

    def test_partial_multi_document_and_single_document_compatibility(self):
        with _temporary_directory() as temp:
            root = Path(temp)
            image = self._page_image(root)
            with patch.object(validation, "RUNS", root / "runs"):
                regions = [_region("receipt", [0, 0, 450, 1000]), _region("invoice", [550, 0, 1000, 1000])]

                def partial_runner(payload, _timeout):
                    if payload["phase"] == "layout":
                        return {"ok": True, "data": {"layout": _layout(regions)}}
                    if payload["type_key"] == "receipt":
                        return {"ok": True, "wall_seconds": 1.25,
                                "data": {"voucher": {"fields": {"total_amount": 125}}, "timings": {"complete_seconds": 1.25},
                                         "artifacts": {"annotated": {"path": "doc-0.png", "media_type": "image/png"}}}}
                    return {"ok": False, "error": "synthetic document failure", "wall_seconds": 0.5,
                            "data": {"trace": [{"kind": "failure"}],
                                     "artifacts": {"trace_json": {"path": "doc-1-trace.json", "media_type": "application/json"}}}}

                partial = validation._run_page_child(self._payload(image), 30, runner=partial_runner)["data"]
                self.assertEqual(partial["page_status"], "partial")
                self.assertEqual([d["status"] for d in partial["documents"]], ["succeeded", "failed"])
                self.assertEqual(partial["documents"][0]["voucher"]["fields"]["total_amount"], 125)
                self.assertEqual(partial["documents"][1]["trace"][0]["kind"], "failure")
                self.assertEqual(partial["artifacts"]["document_0_annotated"]["path"], "doc-0.png")
                self.assertEqual(partial["artifacts"]["document_1_trace_json"]["path"], "doc-1-trace.json")

                def single_runner(payload, _timeout):
                    if payload["phase"] == "layout":
                        return {"ok": True, "data": {"layout": _layout([regions[0]])}}
                    return {"ok": True, "data": {"voucher": {"voucher_id": "R-1", "total_amount": 0},
                                                   "timings": {"complete_seconds": 2.0}}, "wall_seconds": 2.0}

                single = validation._run_page_child(self._payload(image, "run-one", "page-one"), 30,
                                                    runner=single_runner)["data"]
                self.assertEqual(single["page_status"], "succeeded")
                self.assertEqual(single["voucher"]["voucher_id"], "R-1")
                self.assertEqual(len(single["documents"]), 1)

    def test_resume_reuses_succeeded_region_without_child_call(self):
        with _temporary_directory() as temp:
            root = Path(temp)
            image = self._page_image(root)
            db_path = root / "validation.db"
            document_id = uuid.uuid5(uuid.NAMESPACE_URL, "page-resume:0").hex

            @contextmanager
            def connect():
                con = sqlite3.connect(db_path)
                con.row_factory = sqlite3.Row
                try:
                    yield con
                finally:
                    con.close()

            with connect() as con:
                con.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT NOT NULL, cancel_requested INTEGER DEFAULT 0)")
                con.execute("INSERT INTO runs VALUES(?,?,?)", ("run-resume", "running", 0))
                con.execute("""CREATE TABLE document_results (
                    id TEXT PRIMARY KEY, parent_result_id TEXT NOT NULL, document_index INTEGER NOT NULL,
                    status TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}', error TEXT DEFAULT '',
                    UNIQUE(parent_result_id,document_index))""")
                con.execute("INSERT INTO document_results VALUES(?,?,?,?,?,?)", (
                    document_id, "page-resume", 0, "succeeded",
                    json.dumps({"voucher": {"voucher_id": "SAVED"}, "timings": {"complete_seconds": 3},
                                "artifacts": {"annotated": {"path": "saved-doc.png", "media_type": "image/png"}}}), ""))
                con.commit()
            with connect() as con:
                self.assertEqual(validation._documents(con, "page-resume")[0]["id"], document_id)
            calls = []

            def child_runner(payload, _timeout):
                calls.append(payload["phase"])
                if payload["phase"] == "layout":
                    return {"ok": True, "data": {"layout": _layout([
                        _region("receipt", [0, 0, 450, 1000]), _region("invoice", [550, 0, 1000, 1000])])}}
                return {"ok": True, "data": {"voucher": {"voucher_id": "NEW"},
                                               "artifacts": {"trace_json": {"path": "new-trace.json", "media_type": "application/json"}}}}

            with patch.object(validation, "DB", db_path), patch.object(validation, "RUNS", root / "runs"), \
                 patch.object(validation, "_connect", side_effect=connect), \
                 patch.object(validation, "_run_child", side_effect=child_runner):
                result = validation._run_page_child(self._payload(image, "run-resume", "page-resume"), 30)["data"]
            self.assertEqual(calls, ["layout", "document"])
            self.assertEqual(result["documents"][0]["voucher"]["voucher_id"], "SAVED")
            self.assertEqual(result["documents"][1]["voucher"]["voucher_id"], "NEW")
            self.assertEqual(result["artifacts"]["document_0_annotated"]["path"], "saved-doc.png")
            self.assertEqual(result["artifacts"]["document_1_trace_json"]["path"], "new-trace.json")

    def test_csv_keeps_page_timing_on_pages_row_only(self):
        data = {"results": [{"id": "page-1", "relative_path": "set/page.png", "page_no": 1,
                             "status": "succeeded", "combo_name": "synthetic", "mode": "image",
                             "actual_path": "image", "repeat_no": 1, "attempt": 1, "error": "",
                             "timings": {"complete_seconds": 12.0, "recognition_seconds": 10.0},
                             "documents": [
                                 {"id": "doc-1", "status": "succeeded", "type_key": "receipt",
                                  "voucher": {"voucher_id": "R1", "total_amount": 10},
                                  "timings": {"complete_seconds": 2.0, "recognition_seconds": 1.5}},
                                 {"id": "doc-2", "status": "succeeded", "type_key": "invoice",
                                  "voucher": {"voucher_id": "I1", "total_amount": 20},
                                  "timings": {"complete_seconds": 3.0, "recognition_seconds": 2.5}},
                             ]}], "artifacts": []}
        files = validation._csv_files(data)
        pages = list(csv.DictReader(files["pages.csv"].lstrip("\ufeff").splitlines()))
        vouchers = list(csv.DictReader(files["vouchers.csv"].lstrip("\ufeff").splitlines()))
        self.assertEqual(pages[0]["complete_seconds"], "12.0")
        self.assertEqual(pages[0]["document_count"], "2")
        self.assertEqual([row["complete_seconds"] for row in vouchers], ["2.0", "3.0"])
        self.assertNotIn("12.0", [row["complete_seconds"] for row in vouchers])


if __name__ == "__main__":
    unittest.main()
