"""Offline approval, provenance, report API and OCR geometry regressions."""
import ast
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from sqlmodel import Session, SQLModel, create_engine

from app.core import pipeline
from app.db.models import Company, UserAccount, ReviewStatus, VoucherRecord
from app.models.schemas import Voucher
from app.services import review, recognition_sources, item_account_mapping, recognition_details


def endpoint(name, namespace):
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app/main.py").read_text(encoding="utf-8"))
    fn = deepcopy(next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name))
    fn.decorator_list = []
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "app/main.py", "exec"), namespace)
    return namespace[name]


class RecognitionReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name)/'review.db'}")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        self.session_patch = patch.object(review, "get_session", side_effect=lambda: Session(self.engine, expire_on_commit=False))
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        with Session(self.engine) as s:
            user = UserAccount(username="reviewer", name="辨識核對者")
            company = Company(name="合成公司", short="合成", account_chart_key="synthetic")
            s.add(user); s.add(company); s.commit(); s.refresh(user); s.refresh(company)
            self.user_id = user.id
        self.actor = {"username": "reviewer"}

    def voucher(self):
        return Voucher.model_validate({"doc_type_key": "journal_voucher", "doc_type": "會計傳票",
            "fields": {"voucher_id": "J1", "date": "2026-10-13", "debit_total": 100},
            "entries": [{"description": "運費", "amount": 100, "debit_account": "6200", "debit_account_name": "運費",
                         "source_direction": "debit"}]})

    def record(self):
        return review.save_voucher(self.voucher(), "合成")

    def save(self, rec, action="save", mutate=None):
        data = json.loads(rec.current_json)
        if mutate:
            mutate(data)
        return review.save_current(rec.id, {"voucher": data, "action": action, "company_id": rec.company_id,
            "expected_updated_at": rec.updated_at.isoformat()}, self.actor)[0]

    def report(self, rec, version=None, actor=True):
        auth = SimpleNamespace(get_user_by_token=lambda token: self.actor if actor else None)
        route = endpoint("api_recognition_report", {"Request": Request, "HTMLResponse": HTMLResponse,
            "JSONResponse": JSONResponse, "auth": auth, "review": review, "json": json, "ReviewStatus": ReviewStatus})
        with patch.object(review, "recognition_account_warnings", return_value=[]):
            return route(SimpleNamespace(cookies={"session": "synthetic"}), rec.id, version or rec.updated_at.isoformat())

    def test_detail_save_preserves_original_entries_instead_of_creating_posting_drafts(self):
        rec = self.record()
        original = json.loads(rec.current_json)["entries"]
        saved = self.save(rec, mutate=lambda data: data.update(entries=[{
            "description": "舊瀏覽器送來的分錄草稿", "amount": 999,
            "credit_account": "9999", "entry_origin": "generated",
        }]))
        self.assertEqual(json.loads(saved.current_json)["entries"], original)
        self.assertFalse(hasattr(saved, "entries_status"))

    def test_confirm_records_actor_and_preserves_original_source_entries(self):
        rec = self.record()
        original = json.loads(rec.current_json)["entries"]
        data = json.loads(rec.current_json)
        self.assertEqual(data["items"][0]["account_source"], "observed")
        self.assertIsNone(data["items"][0]["quantity"])
        self.assertEqual(self.report(rec).status_code, 409)
        confirmed = self.save(rec, "confirm")
        self.assertEqual(confirmed.confirmed_by, self.user_id)
        self.assertEqual(confirmed.confirmed_by_name, "辨識核對者")
        self.assertEqual(confirmed.recognition_confirmed_version, 1)
        self.assertFalse(hasattr(confirmed, "entries_status"))
        self.assertEqual(json.loads(confirmed.current_json)["entries"], original)
        result = self.report(confirmed)
        self.assertEqual(result.status_code, 200)
        self.assertIn("會計憑證辨識報告", result.body.decode())
        self.assertEqual(self.report(confirmed, actor=False).status_code, 401)
        self.assertEqual(self.report(confirmed, version=rec.updated_at.isoformat()).status_code, 409)

    def test_visible_edit_revokes_confirmation_but_noop_preserves_it(self):
        rec = self.save(self.record(), "confirm")
        unchanged = self.save(rec)
        self.assertEqual(unchanged.status, ReviewStatus.confirmed)
        self.assertEqual(unchanged.confirmed_at, rec.confirmed_at)
        changed = self.save(unchanged, mutate=lambda data: data["items"][0].update(account_name="人工名稱"))
        self.assertEqual(changed.status, ReviewStatus.pending)
        self.assertEqual(changed.recognition_confirmed_version, 0)
        self.assertIsNone(changed.confirmed_by)
        self.assertEqual(self.report(changed).status_code, 409)
        confirmed = self.save(changed, "confirm")
        self.assertEqual(self.report(confirmed).status_code, 200)

    def test_empty_details_and_manual_changes_survive_rerecognition(self):
        rec = self.record()
        rec = self.save(rec, mutate=lambda data: data.update(items=[]))
        with patch("app.core.model_invocation.source_versions", return_value={}):
            rec = review._save_recognition(rec.id, self.voucher(), expected_updated_at=rec.updated_at, actor=self.actor)
        self.assertEqual(json.loads(rec.current_json)["items"], [])
        self.assertEqual(json.loads(rec.current_json)["details_version"], 1)
        rec = self.record()
        rec = self.save(rec, mutate=lambda data: data["items"][0].update(description="人工品名", account_code="0010", account_name="人工科目"))
        with patch("app.core.model_invocation.source_versions", return_value={}):
            reread = review._save_recognition(rec.id, self.voucher(), expected_updated_at=rec.updated_at, actor=self.actor)
        row = json.loads(reread.current_json)["items"][0]
        self.assertEqual((row["description"], row["account_code"], row["account_name"]), ("人工品名", "0010", "人工科目"))

    def test_legacy_confirmation_requires_new_confirmation(self):
        rec = self.record()
        with Session(self.engine) as s:
            stored = s.get(VoucherRecord, rec.id)
            data = json.loads(stored.current_json)
            data.pop("details_version")
            data["items"] = []
            stored.current_json = json.dumps(data, ensure_ascii=False)
            stored.status = ReviewStatus.confirmed
            stored.recognition_confirmed_version = 0
            s.add(stored); s.commit()
        rec = review.get_voucher(rec.id)
        self.assertEqual(self.report(rec).status_code, 409)
        rec = review.confirm_record(rec.id, rec.updated_at.isoformat(), self.actor)
        self.assertEqual(self.report(rec).status_code, 200)
        self.assertEqual(json.loads(rec.current_json)["items"][0]["account_code"], "6200")

    def test_report_image_version_is_checked(self):
        rec = self.save(self.record(), "confirm")
        route = endpoint("api_voucher_image", {"JSONResponse": JSONResponse, "FileResponse": FileResponse,
            "review": review, "json": json, "config": SimpleNamespace(UPLOAD_DIR=Path(self.tmp.name))})
        self.assertEqual(route(rec.id, expected_updated_at="old-version").status_code, 409)

    def test_account_validation_uses_default_only_when_company_has_no_own_chart(self):
        from app.services import accounts
        rec = self.record()
        data = {"doc_type_key": "receipt", "items": [{"account_code": "6050", "account_name": "運費"}]}
        default = [{"code": "6050", "name": "運費"}]
        with patch.object(accounts, "_company_key", side_effect=lambda value: value), \
             patch.object(accounts, "_load_all", return_value={"_default": default}):
            self.assertEqual(review.recognition_account_warnings(rec, data), [])
        own = [{"code": "6205", "name": "運費"}]
        with patch.object(accounts, "_company_key", side_effect=lambda value: value), \
             patch.object(accounts, "_load_all", return_value={"_default": default, "synthetic": own}):
            self.assertEqual(review.recognition_account_warnings(rec, data), ["第 1 列：科目 6050 未列於公司科目表"])
            data["items"][0]["account_code"] = "6205"
            self.assertEqual(review.recognition_account_warnings(rec, data), [])

    def test_missing_company_is_distinguished_from_missing_account(self):
        rec = SimpleNamespace(company_id=None)
        data = {"items": [{"account_code": "6050", "account_name": "運費"}]}
        self.assertEqual(review.recognition_account_warnings(rec, data), ["尚未指定歸屬公司，無法核對明細科目"])


class EntryGeometryTests(unittest.TestCase):
    @staticmethod
    def line(text, x, y):
        return {"text": text, "box": [[x-8,y-5],[x+8,y-5],[x+8,y+5],[x-8,y+5]]}

    def test_unique_side_clears_duplicate_opposite_amount_and_keeps_direction_without_code(self):
        data = {"entries": [{"description": "費用", "debit": 100, "credit": 100}]}
        lines = [self.line("借方金額", 300, 10), self.line("貸方金額", 500, 10), self.line("100", 300, 50)]
        pipeline._fix_entry_sides_with_ocr(data, lines)
        self.assertEqual(data["entries"][0]["debit"], 100)
        self.assertIsNone(data["entries"][0]["credit"])
        result = pipeline._entries_from_voucher_form(data, {"has_entries": True})
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["source_direction"], "debit")

    def test_physical_duplicates_and_footer_removed_but_distinct_equal_rows_remain(self):
        data = {"entries": [{"description": "費用", "debit": 100, "source_direction": "debit", "source_row_y": y}
                            for y in (40, 40, 70, 120, 150)], "field_evidence": {"entries[2].debit": [{"value": 100}]}}
        lines = [self.line("借方金額", 300, 10), self.line("貸方金額", 500, 10), self.line("合計", 200, 120)]
        self.assertEqual(pipeline._filter_entry_rows_with_ocr(data, lines), 2)
        self.assertEqual([row["source_row_y"] for row in data["entries"]], [40, 70, 150])
        self.assertIn("entries[1].debit", data["field_evidence"])

    def test_ambiguous_equal_amounts_are_not_deleted(self):
        data = {"entries": [{"description": "費用", "debit": 100}, {"description": "費用", "debit": 100}]}
        lines = [self.line("借方金額", 300, 10), self.line("貸方金額", 500, 10),
                 self.line("100", 300, 40), self.line("100", 300, 70)]
        pipeline._fix_entry_sides_with_ocr(data, lines)
        self.assertEqual(pipeline._filter_entry_rows_with_ocr(data, lines), 0)
        self.assertEqual(len(data["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
