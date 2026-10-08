import unittest

from app.core.normalization import norm_date
from app.core.quality import evaluate_quality


class DateQualityNormalizationTests(unittest.TestCase):
    def test_date_nfkc_roc_padding_and_time(self):
        self.assertEqual(norm_date("民國 １１３ 年 ０３ 月 ０９ 日"), "2024-03-09")
        self.assertEqual(norm_date("0089/03/09"), "2000-03-09")
        self.assertEqual(norm_date("1900/03/09"), "1900-03-09")
        self.assertEqual(norm_date("2024-03-09 14:05:06"), "2024-03-09")
        self.assertEqual(norm_date("2024-03"), "2024-03")
        self.assertEqual(norm_date("2024/02/30"), "2024/02/30")


class QualityEvaluationTests(unittest.TestCase):
    def test_empty_extracted_content_is_insufficient_but_prepared_is_unassessed(self):
        self.assertEqual(evaluate_quality({})["state"], "insufficient")
        prepared = evaluate_quality({"quality": {"state": "unassessed"}, "fields": {}})
        self.assertEqual(prepared["state"], "unassessed")
        self.assertIn("empty_content", [issue["code"] for issue in prepared["issues"]])
        extracted_empty = evaluate_quality({"quality": {"state": "unassessed"},
            "fields": {"vendor": None, "currency": "TWD", "direction": "進口"}, "items": [], "entries": []})
        self.assertEqual(extracted_empty["state"], "insufficient")

    def test_confirmed_blank_is_page_level_and_known_other_has_unknown_issue(self):
        blank = evaluate_quality({"origin": {"layout": {"content_kind": "blank"}},
                                  "quality": {"state": "usable", "content_kind": "blank"}})
        self.assertEqual(blank["state"], "usable")
        self.assertEqual(blank["issues"], [])
        evidenced_blank = evaluate_quality({"content_kind": "blank", "pixel_evidence": {"ink_ratio": 0.0}})
        self.assertEqual(evidenced_blank["state"], "usable")

        other = evaluate_quality({"doc_type_key": "other", "fields": {"note": "內容"}})
        self.assertEqual(other["content_kind"], "unknown")
        self.assertIn("unknown_type", [issue["code"] for issue in other["issues"]])
        self.assertEqual(other["state"], "needs_review")

    def test_blank_and_unknown_type(self):
        result = evaluate_quality({"doc_type_key": "missing", "fields": {"note": "text"}})
        self.assertEqual(result["content_kind"], "unknown")
        self.assertEqual(result["state"], "needs_review")
        self.assertIn("unknown_type", [issue["code"] for issue in result["issues"]])

    def test_zero_is_content_and_missing_total_is_insufficient(self):
        result = evaluate_quality({"doc_type_key": "invoice", "fields": {"total_amount": 0}, "total_amount": None})
        self.assertEqual(result["state"], "insufficient")
        self.assertIn("missing_total", [issue["code"] for issue in result["issues"]])

    def test_valid_items_are_content_even_when_fields_are_empty(self):
        result = evaluate_quality({"doc_type_key": "invoice", "fields": {},
                                   "items": [{"description": "服務", "amount": 0}],
                                   "total_amount": 0, "date": "2024-03-09", "voucher_id": "AB123"})
        self.assertEqual(result["state"], "usable")

    def test_supporting_content_does_not_require_total_identity_or_date(self):
        result = evaluate_quality({"doc_type_key": "bill_detail", "fields": {"period": "2024-03"}})
        self.assertEqual(result["state"], "usable")
        self.assertEqual(result["content_kind"], "supporting")

    def test_journal_requires_entries_and_missing_identity_date_review(self):
        result = evaluate_quality({"doc_type_key": "journal_voucher", "fields": {"remark": "調整"},
                                   "total_amount": 0})
        self.assertEqual(result["state"], "insufficient")
        codes = [issue["code"] for issue in result["issues"]]
        self.assertIn("missing_entries", codes)
        self.assertIn("missing_identity", codes)
        self.assertIn("missing_date", codes)
        no_amount = evaluate_quality({"doc_type_key": "journal_voucher", "fields": {},
                                      "entries": [{"account_name": "現金", "description": "摘要"}],
                                      "total_amount": 0, "voucher_id": "V1", "date": "2024-03-09"})
        self.assertEqual(no_amount["state"], "insufficient")
        self.assertIn("missing_entries", [issue["code"] for issue in no_amount["issues"]])
        usable = evaluate_quality({"doc_type_key": "journal_voucher", "fields": {},
                                   "entries": [{"account_name": "現金", "debit": 0}],
                                   "total_amount": 0, "voucher_id": "V1", "date": "2024-03-09"})
        self.assertEqual(usable["state"], "usable")

    def test_warning_mapping_and_zero_values(self):
        result = evaluate_quality({"doc_type_key": "receipt", "fields": {"amount_words": "壹佰元", "total_amount": 90},
                                   "total_amount": 0, "voucher_id": "R1", "date": "not a date",
                                   "box_issues": ["座標超出頁面"],
                                   "validation_warnings": ["中文大寫與數字金額衝突，保留數字欄與大寫原文供人工核對",
                                                           "原始傳票借貸不平衡，未補造分錄"]})
        self.assertEqual(result["state"], "needs_review")
        by_code = {issue["code"]: issue for issue in result["issues"]}
        self.assertEqual(by_code["location_issue"]["severity"], "review")
        self.assertEqual(by_code["source_anomaly"]["severity"], "advisory")
        self.assertEqual(by_code["amount_conflict"]["severity"], "review")
        self.assertIn("date_parse", by_code)

        conflicts = evaluate_quality({"doc_type_key": "receipt", "fields": {"total_amount": "bad", "amount_words": "壹佰元"},
                                      "total_amount": "bad", "voucher_id": "R2", "date": "2024-03-09",
                                      "validation_warnings": ["多個有效 QR 內容衝突，保留圖像擷取值，請人工確認",
                                                              "total_amount 補讀候選缺乏獨立證據，待人工確認"]})
        evidence_issues = [issue for issue in conflicts["issues"] if issue["code"] == "evidence_conflict"]
        self.assertEqual(len(evidence_issues), 2)
        self.assertTrue(all(issue["severity"] == "review" for issue in evidence_issues))
        self.assertFalse(any(issue["code"] == "amount_conflict" for issue in conflicts["issues"]))

    def test_historical_date_remains_advisory(self):
        result = evaluate_quality({"doc_type_key": "receipt", "fields": {"vendor": "店家"},
                                   "total_amount": 0, "voucher_id": "R1", "date": "2010-01-01"})
        historical = next(issue for issue in result["issues"] if issue["code"] == "historical_date")
        self.assertEqual(historical["severity"], "advisory")


if __name__ == "__main__":
    unittest.main()
