"""Offline contracts for original voucher rows, amounts and source identity."""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine
from PIL import Image

from app import config
from app.db.models import Company, ReviewStatus, UserAccount, VoucherRecord
from app.models.schemas import Voucher
from app.services import recognition_sources


def _row(*, description="", amount=100, debit="", debit_name="", credit="", credit_name=""):
    return {
        "description": description,
        "amount": amount,
        "debit_account": debit,
        "debit_account_name": debit_name,
        "credit_account": credit,
        "credit_account_name": credit_name,
    }


class EntryNormalizationTests(unittest.TestCase):
    def test_ids_survive_reorder_edits_and_deletions_while_new_manual_ids_are_created(self):
        first = recognition_sources.normalize_entries(
            [_row(description="甲"), _row(description="乙")], origin="manual"
        )
        reordered = recognition_sources.normalize_entries(
            [dict(first[1], description="乙修正"), _row(description="新增"), dict(first[0])],
            previous=first,
            origin="manual",
        )

        self.assertEqual(reordered[0]["entry_id"], first[1]["entry_id"])
        self.assertEqual(reordered[2]["entry_id"], first[0]["entry_id"])
        self.assertNotEqual(reordered[1]["entry_id"], first[0]["entry_id"])
        self.assertEqual([row["entry_origin"] for row in reordered], ["manual"] * 3)
        self.assertEqual([row["source_entry_index"] for row in reordered], [None] * 3)
        self.assertEqual(len({row["entry_id"] for row in reordered}), 3)

    def test_manual_input_cannot_claim_observed_origin_or_source_evidence(self):
        row = _row(description="人工列")
        row.update(entry_origin="observed", source_entry_index=92,
                   evidence_paths=["fields.total_amount"], amount_source="ocr")

        normalized = recognition_sources.normalize_entries([row], origin="manual")[0]

        self.assertEqual(normalized["entry_origin"], "manual")
        self.assertIsNone(normalized["source_entry_index"])
        self.assertEqual(normalized["evidence_paths"], [])
        self.assertIsNone(normalized["amount_source"])

    def test_observed_rows_keep_original_source_index_when_reordered(self):
        observed = recognition_sources.normalize_entries([_row(description="來源甲"), _row(description="來源乙")],
                                                    origin="observed")
        edited = recognition_sources.normalize_entries([dict(observed[1]), dict(observed[0])],
                                                  previous=observed, origin="manual")

        self.assertEqual([row["entry_origin"] for row in edited], ["observed", "observed"])
        self.assertEqual([row["source_entry_index"] for row in edited], [1, 0])

    def test_duplicate_and_malformed_entry_ids_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ID 重複"):
            recognition_sources.normalize_entries([{"entry_id": "00000000-0000-0000-0000-000000000001"},
                                              {"entry_id": "00000000-0000-0000-0000-000000000001"}])
        with self.assertRaisesRegex(ValueError, "ID 格式"):
            recognition_sources.normalize_entries([{"entry_id": "not-a-uuid"}])


class EntryChecksTests(unittest.TestCase):
    def test_balanced_entries_use_decimal_tolerance_and_compute_totals(self):
        summary = recognition_sources.checks({"entries": [
            _row(amount="10.005", debit="6000", debit_name="費用"),
            _row(amount="10.000", credit="1000", credit_name="現金"),
        ]})

        self.assertEqual(summary["debit_total"], 10.005)
        self.assertEqual(summary["credit_total"], 10.0)
        self.assertAlmostEqual(summary["difference"], 0.005)
        self.assertTrue(summary["complete"])
        self.assertTrue(summary["balance_check"])
        self.assertEqual(summary["issues"], [])

    def test_one_sided_balanced_and_malformed_rows_are_distinguished(self):
        one_sided = recognition_sources.checks({"entries": [
            _row(amount="10.00", debit="6000", debit_name="費用"),
        ]})
        malformed = recognition_sources.checks({"entries": [
            _row(amount="NaN", debit="6000", debit_name="費用"),
            _row(amount=10, debit="6000", debit_name="費用", credit="1000", credit_name="現金"),
        ]})

        self.assertTrue(one_sided["complete"])
        self.assertFalse(one_sided["balance_check"])
        self.assertIn("未平衡", " ".join(one_sided["issues"]))
        self.assertFalse(malformed["complete"])
        self.assertFalse(malformed["balance_check"])
        self.assertTrue(any("金額未填" in issue for issue in malformed["issues"]))
        self.assertTrue(any("單一借方或貸方" in issue for issue in malformed["issues"]))

    def test_blank_marker_row_is_never_complete_or_ready(self):
        summary = recognition_sources.checks({"entries": [
            _row(description="本頁以下空白", amount=0, debit="6000", debit_name="費用"),
            _row(amount=0, credit="1000", credit_name="現金"),
        ]})

        self.assertFalse(summary["complete"])
        self.assertFalse(summary["balance_check"])
        self.assertTrue(any("版面空白標記" in issue for issue in summary["issues"]))


    def test_alignment_keeps_observed_rows_and_excludes_generated_or_manual_rows(self):
        data = {"doc_type_key": "receipt", "fields": {"total_amount": 100}, "field_evidence": {
            "entries[0].amount": [{"source": "model", "value": 100}],
            "entries[1].amount": [{"source": "manual", "value": 100}],
        }, "entries": [
            {**_row(amount=100, debit="6000", debit_name="費用"), "entry_id": "observed-id",
             "entry_origin": "observed", "source_entry_index": 0},
            {**_row(amount=100, credit="1000", credit_name="現金"), "entry_id": "generated-id",
             "entry_origin": "generated", "source_entry_index": None},
            {**_row(amount=100, credit="1000", credit_name="現金"), "entry_id": "manual-id",
             "entry_origin": "manual", "source_entry_index": None},
        ]}

        aligned = recognition_sources.alignment_payload(data)

        self.assertEqual(len(aligned["entries"]), 1)
        self.assertEqual(aligned["entries"][0]["description"], "")
        self.assertNotIn("entry_id", aligned["entries"][0])
        self.assertEqual(aligned["field_evidence"], {})


if __name__ == "__main__":
    unittest.main()
