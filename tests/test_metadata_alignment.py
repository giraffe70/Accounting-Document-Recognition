"""Offline alignment checks for new voucher, CSV, and report metadata."""
from __future__ import annotations

import csv
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.models.schemas import Voucher
from app.services import reports
from app import validation


class MetadataSchemaTests(unittest.TestCase):
    def test_legacy_voucher_reads_with_new_metadata_defaults(self):
        voucher = Voucher.model_validate({
            "voucher_id": "OLD-1",
            "items": [{"description": "legacy item", "amount": 10}],
            "entries": [{"description": "legacy entry", "amount": 10}],
        })
        self.assertEqual(voucher.payment_state, {})
        self.assertIsNone(voucher.items[0].fee_code)
        self.assertIsNone(voucher.entries[0].amount_source)
        self.assertEqual(voucher.entries[0].evidence_paths, [])

    def test_new_metadata_round_trips_through_schema(self):
        voucher = Voucher.model_validate({
            "payment_state": {"raw": "已扣款", "normalized": "paid"},
            "items": [{"fee_code": "A01", "hs_code": "0901", "origin_country": "TW"}],
            "entries": [{"amount_source": "fields.total_amount",
                         "evidence_paths": ["fields.total_amount", "items.1.amount"]}],
        })
        dumped = voucher.model_dump(mode="json")
        self.assertEqual(dumped["payment_state"], {"raw": "已扣款", "normalized": "paid"})
        self.assertEqual(dumped["items"][0]["fee_code"], "A01")
        self.assertEqual(dumped["entries"][0]["amount_source"], "fields.total_amount")
        self.assertEqual(dumped["entries"][0]["evidence_paths"], ["fields.total_amount", "items.1.amount"])


class MetadataCsvTests(unittest.TestCase):
    def test_csv_appends_new_fields_and_preserves_json_metadata(self):
        voucher = {
            "doc_type": "invoice", "voucher_id": "INV-1", "date": "2026-10-01",
            "total_amount": 10, "payment_state": {"raw": "已付款", "normalized": "paid"},
            "items": [{"description": "service", "fee_code": "B12"}],
            "entries": [{"description": "service expense", "amount": 10,
                         "evidence_paths": ["fields.total_amount", "items.1.amount"],
                         "amount_source": "fields.total_amount"}],
        }
        files = validation._csv_files({"results": [{
            "id": "result-1", "relative_path": "source.pdf", "page_no": 1,
            "combo_name": "offline", "mode": "image", "actual_path": "",
            "repeat_no": 1, "attempt": 1, "status": "succeeded",
            "timings": {"recognition_seconds": 0.1, "complete_seconds": 0.2},
            "error": "", "voucher": voucher,
        }]})

        def read_csv(name):
            return list(csv.DictReader(io.StringIO(files[name].removeprefix("\ufeff"))))

        vouchers_text = files["vouchers.csv"].removeprefix("\ufeff")
        items_text = files["items.csv"].removeprefix("\ufeff")
        entries_text = files["entries.csv"].removeprefix("\ufeff")
        self.assertIn("payment_state_json", next(csv.reader(io.StringIO(vouchers_text))))
        self.assertEqual(next(csv.reader(io.StringIO(items_text)))[-1], "fee_code")
        self.assertEqual(next(csv.reader(io.StringIO(entries_text)))[-1], "amount_source")
        self.assertEqual(json.loads(read_csv("vouchers.csv")[0]["payment_state_json"]), voucher["payment_state"])
        self.assertEqual(read_csv("items.csv")[0]["fee_code"], "B12")
        entry = read_csv("entries.csv")[0]
        self.assertEqual(entry["amount_source"], "fields.total_amount")
        self.assertEqual(json.loads(entry["evidence_paths_json"]), voucher["entries"][0]["evidence_paths"])


class MetadataReportTests(unittest.TestCase):
    def test_summary_shows_primary_sources_payment_and_original_checks(self):
        voucher = {
            "date": "2026-10-01", "voucher_id": "INV-1", "doc_type": "發票",
            "seller": {"name": "Seller"}, "buyer": {"name": "Buyer"},
            "subtotal": 10, "tax_amount": 0, "total_amount": 10,
            "payment_state": {"raw": "已扣款", "normalized": "paid"},
            "classification_meta": {"original_checks": {"single_sided": True}},
            "items": [{"description": "service", "amount": 10, "fee_code": "B12"}],
            "entries": [{"description": "service expense", "amount": 10,
                         "debit_account": "6000", "debit_account_name": "費用",
                         "credit_account": "", "credit_account_name": "",
                         "amount_source": "fields.total_amount",
                         "evidence_paths": ["fields.total_amount", "items.1.amount"]},
                        {"description": "cash", "amount": 10,
                         "debit_account": "", "debit_account_name": "",
                         "credit_account": "1000", "credit_account_name": "現金"}],
        }
        record = SimpleNamespace(current_json=json.dumps(voucher, ensure_ascii=False), status="confirmed",
                                 entries_status="ready", company_id=1)
        with patch.object(reports.review, "list_vouchers", return_value=[record]) as listed:
            summary = reports._summary("2026-10", "Example")
        self.assertEqual(listed.call_args.kwargs, {"company": "Example", "period": "2026-10"})
        self.assertIn("classification_meta", summary)
        self.assertIn("original_checks", summary)
        self.assertIn("amount_source", summary)
        self.assertIn("fields.total_amount", summary)
        self.assertIn("items.1.amount", summary)
        self.assertIn("payment_state", summary)
        self.assertIn("fee_code", summary)


if __name__ == "__main__":
    unittest.main()
