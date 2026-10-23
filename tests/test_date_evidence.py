"""Offline regressions: model-converted ROC years are flagged by OCR evidence."""
import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from app.core import date_evidence, pipeline, prompts
from app.core.quality import evaluate_quality
from app.services import review, taxid

OCR_DATE = "三聯式統一發票 中華民國115年7月10日 總計 1,050"
OCR_PERIOD = "115年七、八月份 統一發票 中華民國115年7月2日"


def invoice(date, ocr, **extra):
    data = prompts.extraction_example("invoice")
    data["fields"].update(date=date, total_amount=1050, **extra)
    data["field_evidence"] = {"fields.date": [{"source": "model", "value": date, "selected": True}]}
    date_evidence.record(data, ocr, "invoice")
    return data


class OcrDateEvidenceTests(unittest.TestCase):
    def test_extracts_unique_roc_date_and_period_without_changing_model_value(self):
        data = invoice("2023-07-10", OCR_PERIOD)
        self.assertEqual(data["fields"]["date"], "2023-07-10")
        records = data["field_evidence"]["fields.date"]
        self.assertTrue(records[0]["selected"])
        kinds = {r["kind"]: r for r in records if r["source"] == "ocr_candidate"}
        self.assertEqual(kinds["roc_date"]["value"], "2026-07-02")
        self.assertEqual((kinds["invoice_period"]["year"], kinds["invoice_period"]["months"]), (2026, [7, 8]))
        self.assertFalse(any(r["selected"] for r in records[1:]))

    def test_missing_trailing_day_marker_and_fullwidth_digits_are_read(self):
        self.assertEqual(date_evidence.extract("中華民國１１５年7月14")[0]["value"], "2026-07-14")

    def test_multiple_different_dates_or_invalid_dates_are_not_guessed(self):
        self.assertEqual(date_evidence.extract("115年7月2日 列印 115年7月9日"), [])
        self.assertEqual(date_evidence.extract("115年2月30日"), [])
        self.assertEqual(date_evidence.extract("21年7月2日"), [])
        self.assertEqual(date_evidence.extract("民國21年7月2日")[0]["value"], "1932-07-02")
        self.assertEqual(date_evidence.extract("115年7、8月份 115年9、10月份", include_period=True), [])
        self.assertEqual(date_evidence.extract("115年7、9月份", include_period=True), [])

    def test_period_is_invoice_only_and_other_types_are_untouched(self):
        data = prompts.extraction_example("receipt")
        data["fields"]["date"] = "2023-07-10"
        date_evidence.record(data, OCR_PERIOD, "receipt")
        self.assertEqual([r["kind"] for r in data["field_evidence"]["fields.date"]], ["roc_date"])
        other = prompts.extraction_example("cheque")
        self.assertEqual(date_evidence.record(other, OCR_DATE, "cheque"), [])
        self.assertNotIn("field_evidence", other)

    def test_repeated_recording_does_not_duplicate_candidates(self):
        data = invoice("2023-07-10", OCR_DATE)
        date_evidence.record(data, OCR_DATE, "invoice")
        self.assertEqual(len(data["field_evidence"]["fields.date"]), 2)


class DateCheckTests(unittest.TestCase):
    def test_wrong_year_and_wrong_day_are_flagged_but_correct_date_is_not(self):
        for model in ("2023-07-10", "2016-07-10", "2026-07-19"):
            with self.subTest(model=model):
                result = taxid.check_date(model, invoice(model, OCR_DATE)["field_evidence"])
                self.assertFalse(result["ok"])
                self.assertIn("日期與 OCR 原文不一致", result["warn"])
                self.assertIn("2026-07-10", result["warn"])
        self.assertTrue(taxid.check_date("2026-07-10", invoice("2026-07-10", OCR_DATE)["field_evidence"])["ok"])

    def test_period_mismatch_is_flagged_even_without_full_date(self):
        evidence = invoice("2023-07-09", "115年七、八月份")["field_evidence"]
        result = taxid.check_date("2023-07-09", evidence)
        self.assertEqual(result["warnings"][0][:11], "日期與發票期別不一致：")
        self.assertIn("西元 2026 年", result["warn"])
        self.assertTrue(taxid.check_date("2026-08-31", evidence)["ok"])
        self.assertFalse(taxid.check_date("2026-09-01", evidence)["ok"])

    def test_old_date_message_no_longer_guesses_a_1900_offset(self):
        result = taxid.check_date("2016-07-02")
        self.assertFalse(result["ok"])
        self.assertIn("民國 105 年", result["warn"])
        self.assertNotIn("正確西元", result["warn"])

    def test_manual_or_qr_selected_date_is_not_second_guessed(self):
        evidence = invoice("2023-07-10", OCR_DATE)["field_evidence"]
        for source in ("manual", "qr"):
            records = [{**r, "selected": False} for r in evidence["fields.date"]]
            records.append({"source": source, "value": "2023-07-10", "selected": True})
            self.assertTrue(taxid.check_date("2023-07-10", {"fields.date": records})["ok"], source)

    def test_no_evidence_or_blank_date_is_unchanged(self):
        self.assertEqual(taxid.check_date("", None), {"ok": True, "warn": "", "warnings": []})
        self.assertTrue(taxid.check_date("2026-07-10", {})["ok"])
        self.assertEqual(taxid.check_date("abc")["warnings"], ["日期格式無法解析：abc"])


class VoucherDateWarningTests(unittest.TestCase):
    def test_build_voucher_flags_review_issue_and_keeps_model_value(self):
        voucher = pipeline._build_voucher(invoice("2023-07-10", OCR_DATE), "invoice")
        self.assertEqual(voucher.date, "2023-07-10")
        self.assertTrue(any(w.startswith("日期與 OCR 原文不一致：") for w in voucher.validation_warnings))
        issue = next(i for i in voucher.quality["issues"] if i["code"] == "date_ocr_conflict")
        self.assertEqual((issue["severity"], voucher.quality["state"]), ("review", "needs_review"))

    def test_quality_maps_both_warning_kinds_to_review(self):
        for text in ("日期與 OCR 原文不一致：x", "日期與發票期別不一致：x"):
            issue = next(i for i in evaluate_quality({"doc_type_key": "invoice", "validation_warnings": [text]})["issues"]
                         if i["code"] == "date_ocr_conflict")
            self.assertEqual(issue["severity"], "review")

    def test_manual_correction_clears_warning_and_survives_resave(self):
        original = pipeline._build_voucher(invoice("2023-07-10", OCR_DATE), "invoice").model_dump()
        self.assertTrue(any("OCR 原文" in w for w in original["validation_warnings"]))
        rec = SimpleNamespace(current_json=json.dumps(original), period="", updated_at=None)
        session = SimpleNamespace(get=lambda *_: rec, add=lambda *_: None, commit=lambda: None, refresh=lambda *_: None)
        with patch("app.services.review.get_session", return_value=nullcontext(session)):
            review.update_current(1, {"fields": {"date": "2026-07-10"}})
        stored = json.loads(rec.current_json)
        self.assertEqual(stored["date"], "2026-07-10")
        self.assertFalse(any("日期與" in w for w in stored["validation_warnings"]))
        self.assertNotIn("date_ocr_conflict", [i["code"] for i in stored["quality"]["issues"]])
        # The candidate stays as audit evidence next to the manual value.
        sources = [r["source"] for r in stored["field_evidence"]["fields.date"]]
        self.assertIn("ocr_candidate", sources)
        self.assertEqual(sources[-1], "manual")


if __name__ == "__main__":
    unittest.main()
