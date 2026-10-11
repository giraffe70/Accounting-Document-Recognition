"""Offline regressions for classification evidence and the bounded reread."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core import classification_evidence, invocation_trace, supplement


def _line(text, x, y, *, width=120, height=24):
    return {"text": text, "box": [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]}


def _data(type_key, fields=None, *, items=None, entries=None):
    return {"doc_type": type_key, "doc_type_key": type_key, "fields": dict(fields or {}),
            "items": list(items or []), "entries": list(entries or []),
            "validation_warnings": [], "field_evidence": {}, "boxes": {}}


def _journal_lines():
    return [_line("借方", 500, 100), _line("貸方", 800, 100),
            _line("辦公用品", 120, 250, width=180), _line("1000", 550, 250, width=70)]


class ClassificationEvidenceV3Tests(unittest.TestCase):
    def test_document_semantics_correct_journal_postage_and_ticket_but_ignore_quoted_invoice(self):
        cases = [
            ("invoice", "轉帳傳票 A911 借方 貸方", "journal_voucher"),
            ("invoice", "郵局收據 郵資 60", "receipt"),
            ("invoice", "高鐵車票 票價 1000 出發 2026/10/11", "receipt"),
        ]
        for model_type, text, expected in cases:
            with self.subTest(text=text):
                effective, evidence = classification_evidence.resolve(model_type, text)
                self.assertEqual(effective, expected)
                self.assertTrue(evidence["adopted"])

        # The policy document may refer to an invoice without itself being one.
        effective, evidence = classification_evidence.resolve(
            "other", "要保書 保障額度 1,000,000；申請時請檢附統一發票影本")
        self.assertEqual(effective, "other")
        self.assertFalse(evidence["adopted"])

    def test_manual_type_protects_classification_correction(self):
        data = _data("invoice", {"voucher_id": None, "date": None, "seller_name": None, "seller_tax_id": None})
        with patch("app.core.pipeline.reread_fields", return_value={}) as reread_fields, \
                patch("app.core.pipeline.reread_document") as reread_document, \
                invocation_trace.capture():
            supplement.apply(data, "invoice", None, "synthetic.png", mode="ocr_text",
                             ocr_text="轉帳傳票 借方 貸方", context={"confirmed_type": True})
        reread_fields.assert_called_once()
        self.assertEqual(reread_fields.call_args.args[1], "invoice")
        reread_document.assert_not_called()
        self.assertEqual(data["doc_type_key"], "invoice")


class SupplementContractV3Tests(unittest.TestCase):
    def test_journal_correction_rereads_full_contract_once_and_adopts_position_supported_entry(self):
        data = _data("invoice", {"voucher_id": None, "date": None, "seller_name": None, "seller_tax_id": None})
        text = "轉帳傳票 A911 借方 貸方\n辦公用品 1000"
        result = {"fields": {"voucher_id": "A911"},
                  "entries": [{"account_name": "辦公用品", "debit": 1000, "credit": None}]}
        context = {"ocr_lines": _journal_lines()}
        with patch("app.core.pipeline.reread_document", return_value=result) as reread_document, \
                patch("app.core.pipeline.reread_fields") as reread_fields, \
                invocation_trace.capture():
            supplement.apply(data, "invoice", None, "synthetic.png", mode="ocr_text", ocr_text=text, context=context)
            # A second pass shares the same one-task budget, including the type correction.
            supplement.apply(data, "invoice", None, "synthetic.png", mode="ocr_text", ocr_text=text, context=context)

        reread_document.assert_called_once()
        self.assertEqual(reread_document.call_args.args[1], "journal_voucher")
        reread_fields.assert_not_called()
        self.assertEqual(context["effective_type"], "journal_voucher")
        self.assertEqual(data["fields"]["voucher_id"], "A911")
        self.assertEqual(data["entries"], result["entries"])
        evidence = data["field_evidence"]["entries[0]"][-1]
        self.assertTrue(evidence["selected"])
        self.assertTrue(evidence["independent_evidence"])

    def test_entries_without_original_text_and_position_evidence_remain_unadopted(self):
        data = _data("journal_voucher", {"voucher_id": "A911", "date": "2026/10/11"})
        candidate = {"account_name": "辦公用品", "debit": 1000, "credit": None}
        with patch("app.core.pipeline.reread_document", return_value={"fields": {}, "entries": [candidate]}) as reread, \
                invocation_trace.capture():
            supplement.apply(data, "journal_voucher", None, "synthetic.png", mode="ocr_text",
                             ocr_text="會計傳票", context={})
        reread.assert_called_once()
        self.assertEqual(data["entries"], [])
        self.assertFalse(data["field_evidence"]["entries[0]"][0]["selected"])
        self.assertTrue(any("entries" in warning and "待人工確認" in warning for warning in data["validation_warnings"]))

    def test_items_are_reread_and_only_adopted_with_matching_row_text(self):
        data = _data("invoice", {"voucher_id": "AB12345678", "date": "2026/10/11",
                                 "seller_name": "店家", "seller_tax_id": "12345678"})
        row = {"description": "原子筆", "quantity": 2, "unit_price": 10, "amount": 20}
        lines = [_line("原子筆 2 10 20", 100, 300, width=300)]
        with patch("app.core.pipeline.reread_document", return_value={"fields": {}, "items": [row]}) as reread, \
                invocation_trace.capture():
            supplement.apply(data, "invoice", None, "synthetic.png", mode="ocr_text",
                             ocr_text="品名 明細 數量 單價\n原子筆 2 10 20", context={"ocr_lines": lines})
        reread.assert_called_once()
        self.assertEqual(data["items"], [row])
        self.assertTrue(data["field_evidence"]["items[0]"][0]["selected"])

    def test_confirmed_fields_and_entries_paths_are_not_overwritten_or_reread(self):
        receipt = _data("receipt", {"currency": "TWD", "subtotal": 14000, "tax_amount": 1500,
                                     "total_amount": 8500, "tax_id": None, "vendor": "店家"})
        fields_result = {"total_amount": 15500}
        with patch("app.core.pipeline.reread_fields", return_value=fields_result) as reread_fields, \
                patch("app.core.pipeline._merge_evidence_fields", side_effect=lambda d, values, _source: d["fields"].update(values)), \
                invocation_trace.capture():
            supplement.apply(receipt, "receipt", None, "synthetic.png", mode="ocr_text",
                             ocr_text="未稅合計 14,000\n稅額 1,500\n總計 15,500", context={},
                             confirmed_fields={"total_amount": True})
        self.assertEqual(receipt["fields"]["total_amount"], 8500)
        self.assertNotIn("total_amount", reread_fields.call_args.args[3])

        voucher = _data("journal_voucher", {"voucher_id": "A911", "date": "2026/10/11",
                                            "debit_total": 1000, "credit_total": 1000})
        with patch("app.core.pipeline.reread_document") as reread_document, \
                patch("app.core.pipeline.reread_fields") as reread_fields, \
                invocation_trace.capture():
            supplement.apply(voucher, "journal_voucher", None, "synthetic.png", mode="ocr_text",
                             ocr_text="傳票 明細 借方 貸方", context={"confirmed_paths": ["entries[0]"]})
        reread_document.assert_not_called()
        reread_fields.assert_not_called()
        self.assertEqual(voucher["entries"], [])


if __name__ == "__main__":
    unittest.main()
