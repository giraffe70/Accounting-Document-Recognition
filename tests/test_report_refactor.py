"""Offline presentation regressions for the summary report audit appendix."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.db.models import ReviewStatus
from app.services import reports


def record(current: dict, status=ReviewStatus.confirmed):
    return SimpleNamespace(current_json=json.dumps(current, ensure_ascii=False), status=status,
                           company_id=1, entries_status="ready")


class ReportAuditTests(unittest.TestCase):
    def setUp(self):
        self.hostile = "</pre><script>alert('x')</script>&\""
        self.current = {
            "date": "2026-06-01",
            "voucher_id": "INV-001",
            "doc_type": "進口報單",
            "seller": {"name": "Vendor"},
            "buyer": {"name": "Buyer"},
            "subtotal": 100,
            "tax_amount": 5,
            "total_amount": 105,
            "note": self.hostile,
            "field_evidence": {"fields.total_amount": [{"source": "ocr", "text": self.hostile}]},
            "validation_warnings": ["warning " + self.hostile],
            "classification_meta": {"original_checks": {"single_sided": True}},
            "items": [{"description": "coffee", "amount": 105,
                       "hs_code": "0901.21", "origin_country": self.hostile}],
            "entries": [{"amount": 105, "debit_account": "6000", "debit_account_name": self.hostile,
                         "credit_account": "", "credit_account_name": "",
                         "evidence_paths": ["fields.total_amount", self.hostile]},
                        {"amount": 105, "debit_account": "", "debit_account_name": "",
                         "credit_account": "1000", "credit_account_name": "Cash"}],
        }

    def test_summary_renders_audit_metadata_and_escapes_source_data(self):
        item = record(self.current)
        with patch.object(reports.review, "list_vouchers", return_value=[item]):
            summary = reports._summary("2026-06", "Example")
        html = summary
        self.assertIn("來源與驗證紀錄", html)
        self.assertIn("field_evidence", html)
        self.assertIn("validation_warnings", html)
        self.assertIn("classification_meta", html)
        self.assertIn("original_checks", html)
        self.assertIn("item_extra_fields", html)
        self.assertIn("hs_code", html)
        self.assertIn("origin_country", html)
        self.assertIn("entry_evidence_paths", html)
        self.assertIn("evidence_paths", html)
        self.assertIn("&lt;/pre&gt;&lt;script&gt;", html)
        self.assertNotIn("</pre><script>", html)
        self.assertNotIn(self.hostile, html)
        self.assertIn("<tr class='audit-row'>", summary)

    def test_old_records_without_audit_data_get_no_empty_appendix(self):
        old_record = record({
            "date": "2024-01-01", "voucher_id": "OLD-1", "doc_type": "收據",
            "seller": {}, "buyer": {}, "subtotal": None, "tax_amount": None,
            "total_amount": None, "entries": [{"amount": 5, "debit_account": "6000"}],
            "items": [{"description": "old item", "amount": 5}],
        })
        with patch.object(reports.review, "list_vouchers", return_value=[old_record]):
            summary = reports._summary("2024-01", "")
        self.assertNotIn("來源與驗證紀錄", summary)
        self.assertNotIn("class='audit-row'", summary)


if __name__ == "__main__":
    unittest.main()
