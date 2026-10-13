"""Offline contracts for editable entries and their source identity."""
from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from app.db.models import ReviewStatus, VoucherRecord
from app.services import entry_workflow


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
        first = entry_workflow.normalize_entries(
            [_row(description="甲"), _row(description="乙")], origin="manual"
        )
        reordered = entry_workflow.normalize_entries(
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

        normalized = entry_workflow.normalize_entries([row], origin="manual")[0]

        self.assertEqual(normalized["entry_origin"], "manual")
        self.assertIsNone(normalized["source_entry_index"])
        self.assertEqual(normalized["evidence_paths"], [])
        self.assertIsNone(normalized["amount_source"])

    def test_observed_rows_keep_original_source_index_when_reordered(self):
        observed = entry_workflow.normalize_entries([_row(description="來源甲"), _row(description="來源乙")],
                                                    origin="observed")
        edited = entry_workflow.normalize_entries([dict(observed[1]), dict(observed[0])],
                                                  previous=observed, origin="manual")

        self.assertEqual([row["entry_origin"] for row in edited], ["observed", "observed"])
        self.assertEqual([row["source_entry_index"] for row in edited], [1, 0])

    def test_duplicate_and_malformed_entry_ids_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ID 重複"):
            entry_workflow.normalize_entries([{"entry_id": "00000000-0000-0000-0000-000000000001"},
                                              {"entry_id": "00000000-0000-0000-0000-000000000001"}])
        with self.assertRaisesRegex(ValueError, "ID 格式"):
            entry_workflow.normalize_entries([{"entry_id": "not-a-uuid"}])


class EntryChecksTests(unittest.TestCase):
    def test_balanced_entries_use_decimal_tolerance_and_compute_totals(self):
        summary = entry_workflow.checks({"entries": [
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
        one_sided = entry_workflow.checks({"entries": [
            _row(amount="10.00", debit="6000", debit_name="費用"),
        ]})
        malformed = entry_workflow.checks({"entries": [
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
        summary = entry_workflow.checks({"entries": [
            _row(description="本頁以下空白", amount=0, debit="6000", debit_name="費用"),
            _row(amount=0, credit="1000", credit_name="現金"),
        ]})

        self.assertFalse(summary["complete"])
        self.assertFalse(summary["balance_check"])
        self.assertTrue(any("版面空白標記" in issue for issue in summary["issues"]))

    def test_source_fingerprint_ignores_entry_proposals_but_tracks_source_changes(self):
        data = {"doc_type_key": "receipt", "fields": {"total_amount": 100},
                "items": [{"description": "文具", "amount": 100}],
                "entries": [_row(amount=100, debit="6000", debit_name="費用")],
                "classification_meta": {"status": "candidate"}}
        record = SimpleNamespace(current_json=json.dumps(data, ensure_ascii=False), company_id=3,
                                 company="甲", image_path="scan.png")
        original = entry_workflow.source_fingerprint(record)

        proposed = deepcopy(data)
        proposed["entries"][0]["debit_account"] = "6100"
        proposed["classification_meta"]["status"] = "proposed"
        self.assertEqual(original, entry_workflow.source_fingerprint(record, proposed))

        for changed in (
            {**data, "fields": {"total_amount": 101}},
            {**data, "items": [{"description": "耗材", "amount": 100}]},
        ):
            self.assertNotEqual(original, entry_workflow.source_fingerprint(record, changed))
        record.company_id = 4
        self.assertNotEqual(original, entry_workflow.source_fingerprint(record))
        record.company_id = 3
        record.image_path = "other.png"
        self.assertNotEqual(original, entry_workflow.source_fingerprint(record))


class EntryMigrationTests(unittest.TestCase):
    def test_legacy_entries_become_draft_split_to_single_sided_rows_and_migration_is_idempotent(self):
        from app.db import session as db_session

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            engine = create_engine(f"sqlite:///{Path(tmp) / 'legacy.db'}",
                                   connect_args={"check_same_thread": False})
            SQLModel.metadata.create_all(engine)
            legacy = VoucherRecord(
                current_json=json.dumps({"doc_type_key": "journal_voucher", "entries": [
                    {"description": "舊雙邊列", "amount": 25, "debit_account": "6000",
                     "debit_account_name": "費用", "credit_account": "1000",
                     "credit_account_name": "現金", "evidence_paths": ["fields.total_amount"],
                     "amount_source": "ocr"},
                ]}, ensure_ascii=False),
                ai_json='{"raw":"preserve"}', status=ReviewStatus.confirmed,
                confirmed_at=datetime(2026, 10, 1, 9, 0), entries_status="ready",
            )
            ready_rows = entry_workflow.normalize_entries([
                _row(amount=25, debit="6000", debit_name="費用"),
                _row(amount=25, credit="1000", credit_name="現金"),
            ], origin="observed")
            already_ready = VoucherRecord(
                current_json=json.dumps({"doc_type_key": "journal_voucher", "entries": ready_rows}),
                status=ReviewStatus.confirmed, entries_status="ready",
            )
            empty = VoucherRecord(current_json='{"doc_type_key":"receipt","entries":[]}',
                                  entries_status="draft")
            with Session(engine) as session:
                session.add(legacy)
                session.add(already_ready)
                session.add(empty)
                session.commit()
                legacy_id, ready_id, empty_id = legacy.id, already_ready.id, empty.id

            with patch.object(db_session, "_engine", engine):
                db_session._migrate_entries()
                with Session(engine) as session:
                    migrated = session.get(VoucherRecord, legacy_id)
                    still_ready = session.get(VoucherRecord, ready_id)
                    data = json.loads(migrated.current_json)
                    self.assertEqual(migrated.entries_status, "draft")
                    self.assertEqual(migrated.status, ReviewStatus.confirmed)
                    self.assertEqual(migrated.confirmed_at, datetime(2026, 10, 1, 9, 0))
                    self.assertEqual(migrated.ai_json, '{"raw":"preserve"}')
                    self.assertEqual(len(data["entries"]), 2)
                    self.assertEqual({row["entry_origin"] for row in data["entries"]}, {"observed"})
                    self.assertTrue(all(row["entry_id"] for row in data["entries"]))
                    debit, credit = data["entries"]
                    self.assertTrue(debit["debit_account"])
                    self.assertFalse(debit["credit_account"])
                    self.assertTrue(credit["credit_account"])
                    self.assertFalse(credit["debit_account"])
                    self.assertEqual(debit["evidence_paths"], ["fields.total_amount"])
                    self.assertEqual(debit["amount_source"], "ocr")
                    self.assertEqual(still_ready.entries_status, "ready")
                    self.assertEqual(session.get(VoucherRecord, empty_id).entries_status, "none")

                db_session._migrate_entries()
                with Session(engine) as session:
                    self.assertEqual(session.get(VoucherRecord, legacy_id).entries_status, "draft")
                    self.assertEqual(session.get(VoucherRecord, ready_id).entries_status, "ready")
                    self.assertEqual(session.get(VoucherRecord, empty_id).entries_status, "none")
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
