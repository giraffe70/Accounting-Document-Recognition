"""Offline contracts for editable entries and their source identity."""
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
from app.services import classify, entry_workflow
from app.services import reports, review


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

    def test_voucher_brief_excludes_entries_and_entry_level_evidence(self):
        voucher = Voucher.model_validate({
            "doc_type_key": "receipt", "fields": {"total_amount": 100},
            "entries": [_row(amount=100, debit="6000", debit_name="費用")],
            "field_evidence": {
                "fields.total_amount": [{"source": "manual", "selected": True, "value": 100}],
                "entries[0].amount": [{"source": "manual", "selected": True, "value": 100}],
                "entries": [{"source": "manual", "selected": True, "value": []}],
            },
        })

        brief = classify._voucher_brief(voucher)

        self.assertNotIn("entries", brief)
        self.assertEqual(list(brief["field_evidence"]), ["fields.total_amount"])
        self.assertEqual(brief["field_evidence"]["fields.total_amount"][0]["value"], 100)

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

        aligned = entry_workflow.alignment_payload(data)

        self.assertEqual(len(aligned["entries"]), 1)
        self.assertEqual(aligned["entries"][0]["description"], "")
        self.assertNotIn("entry_id", aligned["entries"][0])
        self.assertEqual(aligned["field_evidence"], {})


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


class EntryReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'workflow.db'}",
                                    connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        with Session(self.engine) as session:
            session.add(Company(name="合成甲公司", short="甲", tax_id="", account_chart_key="synthetic"))
            session.add(UserAccount(username="entry-reviewer", name="分錄審核者"))
            session.commit()
        session_patch = patch.object(review, "get_session",
                                     side_effect=lambda: Session(self.engine, expire_on_commit=False))
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.actor = {"username": "entry-reviewer", "name": "不採用客戶名稱", "is_admin": False}

    def voucher(self, *, doc_type="receipt", amount=100, entries=None):
        return Voucher.model_validate({
            "doc_type_key": doc_type,
            "doc_type": "收據" if doc_type == "receipt" else "傳票",
            "date": "2026-10-12",
            "fields": {"total_amount": amount},
            "entries": entries or [],
        })

    @staticmethod
    def balanced_entries(amount=100):
        return [
            _row(description="費用", amount=amount, debit="6000", debit_name="費用"),
            _row(description="現金", amount=amount, credit="1000", credit_name="現金"),
        ]

    def test_saving_entries_keeps_raw_confirmation_but_requires_separate_entry_confirmation(self):
        record = review.save_voucher(self.voucher(), company="甲", status=ReviewStatus.confirmed)
        original_confirmed_at = record.confirmed_at

        payload = json.loads(record.current_json)
        payload["entries"] = self.balanced_entries()
        saved, _ = review.save_current(record.id, {
            "action": "save", "voucher": payload,
            "expected_updated_at": record.updated_at.isoformat(),
        }, self.actor)

        self.assertEqual(saved.status, ReviewStatus.confirmed)
        self.assertEqual(saved.confirmed_at, original_confirmed_at)
        self.assertEqual(saved.entries_status, "draft")
        self.assertFalse(entry_workflow.is_ready(saved))
        self.assertTrue(entry_workflow.checks(json.loads(saved.current_json))["balance_check"])

        confirmed = review.manage_entries(saved.id, "confirm", saved.updated_at.isoformat(), self.actor)
        self.assertEqual(confirmed.status, ReviewStatus.confirmed)
        self.assertEqual(confirmed.entries_status, "ready")
        self.assertEqual(confirmed.entries_confirmed_by_name, "分錄審核者")
        self.assertIsNotNone(confirmed.entries_confirmed_at)
        self.assertTrue(entry_workflow.is_ready(confirmed))

    def test_raw_source_edit_revokes_both_confirmations_and_records_actor_history(self):
        record = review.save_voucher(self.voucher(entries=self.balanced_entries()), company="甲",
                                     status=ReviewStatus.confirmed)
        ready = review.manage_entries(record.id, "confirm", record.updated_at.isoformat(), self.actor)
        self.assertTrue(entry_workflow.is_ready(ready))
        payload = json.loads(ready.current_json)
        payload["fields"]["total_amount"] = 125
        changed, _ = review.save_current(ready.id, {
            "action": "save", "voucher": payload,
            "expected_updated_at": ready.updated_at.isoformat(),
        }, self.actor)

        self.assertEqual(changed.status, ReviewStatus.pending)
        self.assertIsNone(changed.confirmed_at)
        self.assertEqual(changed.entries_status, "draft")
        self.assertIsNone(changed.entries_confirmed_at)
        history = json.loads(changed.entries_history)
        revoked = [event for event in history if event["action"] == "confirmation_revoked"]
        self.assertTrue(revoked)
        self.assertEqual(revoked[-1]["actor_name"], "分錄審核者")
        self.assertTrue(revoked[-1]["before"])
        self.assertTrue(entry_workflow.checks(json.loads(changed.current_json))["balance_check"])

    def test_old_raw_confirmation_endpoint_cannot_mark_entries_ready(self):
        record = review.save_voucher(self.voucher(entries=self.balanced_entries()), company="甲",
                                     status=ReviewStatus.pending)
        confirmed = review.confirm_record(record.id, record.updated_at.isoformat())

        self.assertEqual(confirmed.status, ReviewStatus.confirmed)
        self.assertEqual(confirmed.entries_status, "draft")
        self.assertFalse(entry_workflow.is_ready(confirmed))

    def test_manage_confirm_rejects_unbalanced_or_blank_marker_rows(self):
        for entries in (
            [_row(amount=100, debit="6000", debit_name="費用")],
            [_row(description="以下空白", amount=0, debit="6000", debit_name="費用"),
             _row(amount=0, credit="1000", credit_name="現金")],
        ):
            record = review.save_voucher(self.voucher(entries=entries), company="甲",
                                         status=ReviewStatus.confirmed)
            with self.subTest(entries=entries), self.assertRaises(review.RecognitionProtected):
                review.manage_entries(record.id, "confirm", record.updated_at.isoformat(), self.actor)
            self.assertEqual(review.get_voucher(record.id).entries_status, "draft")

        not_raw_confirmed = review.save_voucher(self.voucher(entries=self.balanced_entries()), company="甲",
                                                status=ReviewStatus.pending)
        with self.assertRaises(review.RecognitionProtected):
            review.manage_entries(not_raw_confirmed.id, "confirm", not_raw_confirmed.updated_at.isoformat(),
                                  self.actor)
        without_company = review.save_voucher(self.voucher(entries=self.balanced_entries()),
                                              status=ReviewStatus.confirmed)
        with self.assertRaises(review.RecognitionProtected):
            review.manage_entries(without_company.id, "confirm", without_company.updated_at.isoformat(),
                                  self.actor)

    def test_entry_conflict_uses_latest_version_without_partial_write(self):
        record = review.save_voucher(self.voucher(entries=self.balanced_entries()), company="甲",
                                     status=ReviewStatus.confirmed)
        stale_version = "2000-01-01T00:00:00"

        with self.assertRaises(entry_workflow.EntryConflict) as raised:
            review.manage_entries(record.id, "confirm", stale_version, self.actor)

        latest = review.get_voucher(record.id)
        self.assertEqual(raised.exception.latest["updated_at"], latest.updated_at.isoformat())
        self.assertEqual(latest.entries_status, "draft")
        self.assertEqual(json.loads(latest.entries_history), [])

    def test_proposal_generation_rejects_stale_version_after_entries_only_edit(self):
        record = review.save_voucher(self.voucher(), company="甲", status=ReviewStatus.confirmed)
        normalized, _ = review.save_current(record.id, {
            "action": "save", "voucher": json.loads(record.current_json),
            "expected_updated_at": record.updated_at.isoformat(),
        }, self.actor)
        record = review.get_voucher(normalized.id)
        expected = record.updated_at.isoformat()
        fingerprint = entry_workflow.source_fingerprint(record)
        payload = json.loads(record.current_json)
        payload["entries"] = self.balanced_entries()
        review.save_current(record.id, {"action": "save", "voucher": payload,
                                       "expected_updated_at": expected}, self.actor)
        latest = review.get_voucher(record.id)
        self.assertNotEqual(latest.updated_at.isoformat(), expected)
        self.assertEqual(fingerprint, entry_workflow.source_fingerprint(latest))

        with self.assertRaises(entry_workflow.EntryConflict):
            review.save_entry_proposal(record.id, self.voucher(entries=self.balanced_entries()), self.actor,
                                       fingerprint, expected_updated_at=expected)

        latest = review.get_voucher(record.id)
        self.assertEqual(latest.entries_proposal_json, "{}")
        self.assertEqual(len(json.loads(latest.current_json)["entries"]), 2)

    def test_proposal_is_independent_can_be_adopted_or_discarded_and_stale_adopt_rolls_back(self):
        record = review.save_voucher(self.voucher(), company="甲", status=ReviewStatus.confirmed)
        original_current = record.current_json
        original_raw_status = record.status
        fingerprint = entry_workflow.source_fingerprint(record)
        proposal_voucher = self.voucher(entries=self.balanced_entries())
        proposed = review.save_entry_proposal(record.id, proposal_voucher, self.actor, fingerprint)
        proposal = json.loads(proposed.entries_proposal_json)
        self.assertEqual(proposed.current_json, original_current)
        self.assertEqual(proposed.status, original_raw_status)
        self.assertEqual(proposed.entries_status, "none")
        self.assertEqual(proposal["status"], "pending")

        changed_data = json.loads(proposed.current_json)
        changed_data["fields"]["total_amount"] = 125
        changed, _ = review.save_current(proposed.id, {
            "action": "save", "voucher": changed_data,
            "expected_updated_at": proposed.updated_at.isoformat(),
        }, self.actor)
        stale_current = changed.current_json
        with self.assertRaises(entry_workflow.EntryConflict):
            review.manage_entries(changed.id, "adopt", changed.updated_at.isoformat(), self.actor,
                                  proposal_id=proposal["id"], replace_existing=True)
        after_failed_adopt = review.get_voucher(changed.id)
        self.assertEqual(after_failed_adopt.current_json, stale_current)
        self.assertEqual(json.loads(after_failed_adopt.entries_proposal_json)["status"], "stale")

        second = review.save_voucher(self.voucher(), company="甲", status=ReviewStatus.confirmed)
        proposed_again = review.save_entry_proposal(second.id, proposal_voucher, self.actor,
                                                     entry_workflow.source_fingerprint(second))
        proposal_again = json.loads(proposed_again.entries_proposal_json)
        adopted = review.manage_entries(proposed_again.id, "adopt", proposed_again.updated_at.isoformat(),
                                        self.actor, proposal_id=proposal_again["id"])
        self.assertEqual(json.loads(adopted.entries_proposal_json)["status"], "adopted")
        self.assertEqual(adopted.entries_status, "draft")
        self.assertEqual(adopted.status, ReviewStatus.confirmed)
        self.assertEqual(len(json.loads(adopted.current_json)["entries"]), 2)
        self.assertTrue(any(item["action"] == "proposal_adopted"
                            for item in json.loads(adopted.entries_history)))
        discarded = review.save_entry_proposal(second.id, proposal_voucher, self.actor,
                                                entry_workflow.source_fingerprint(second))
        discard_proposal = json.loads(discarded.entries_proposal_json)
        discarded = review.manage_entries(discarded.id, "discard", discarded.updated_at.isoformat(),
                                           self.actor, proposal_id=discard_proposal["id"])
        self.assertEqual(json.loads(discarded.entries_proposal_json)["status"], "discarded")

        existing_ready = review.save_voucher(self.voucher(entries=self.balanced_entries()), company="甲",
                                             status=ReviewStatus.confirmed)
        existing_ready = review.manage_entries(existing_ready.id, "confirm",
                                               existing_ready.updated_at.isoformat(), self.actor)
        old_entries = json.loads(existing_ready.current_json)["entries"]
        replacement_entries = [
            _row(description="替換費用", amount=100, debit="6100", debit_name="其他費用"),
            _row(description="替換現金", amount=100, credit="1100", credit_name="銀行存款"),
        ]
        replacement = review.save_entry_proposal(existing_ready.id,
            self.voucher(entries=replacement_entries), self.actor,
            entry_workflow.source_fingerprint(existing_ready))
        replacement_id = json.loads(replacement.entries_proposal_json)["id"]
        replaced = review.manage_entries(replacement.id, "adopt", replacement.updated_at.isoformat(),
                                         self.actor, proposal_id=replacement_id, replace_existing=True)
        replacement_history = json.loads(replaced.entries_history)
        adopted_event = next(item for item in replacement_history if item["action"] == "proposal_adopted")
        revoked_event = next(item for item in replacement_history if item["action"] == "confirmation_revoked")
        self.assertEqual(adopted_event["before"], old_entries)
        self.assertEqual(adopted_event["actor_name"], "分錄審核者")
        self.assertEqual(revoked_event["before"], old_entries)
        self.assertEqual(replaced.entries_status, "draft")
        self.assertEqual(replaced.status, ReviewStatus.confirmed)

    def test_journal_report_renders_only_ready_entries(self):
        ready_entries = [
            _row(description="已確認費用", amount=100, debit="6000", debit_name="費用"),
            _row(description="已確認現金", amount=100, credit="1000", credit_name="現金"),
        ]
        ready = review.save_voucher(self.voucher(entries=ready_entries), company="甲",
                                    status=ReviewStatus.confirmed)
        ready.period = "2026-10"
        with Session(self.engine, expire_on_commit=False) as session:
            session.add(ready)
            session.commit()
        review.manage_entries(ready.id, "confirm", ready.updated_at.isoformat(), self.actor)

        draft_entries = [
            _row(description="草稿費用", amount=100, debit="7000", debit_name="草稿費用"),
            _row(description="草稿現金", amount=100, credit="2000", credit_name="草稿現金"),
        ]
        draft = review.save_voucher(self.voucher(entries=draft_entries), company="甲",
                                    status=ReviewStatus.confirmed)
        draft.period = "2026-10"
        with Session(self.engine, expire_on_commit=False) as session:
            session.add(draft)
            session.commit()

        html = reports.render("journal_voucher", "2026-10", "甲")

        self.assertIn("6000 費用", html)
        self.assertNotIn("7000 草稿費用", html)
        self.assertIn("另有 1 張尚未確認分錄", html)

    def test_source_edit_version_conflict_precedes_image_write_and_rotation_revokes_both_states(self):
        uploads = Path(self.tmp.name) / "uploads"
        uploads.mkdir()
        image_path = uploads / "source.png"
        Image.new("RGB", (32, 20), color=(25, 50, 75)).save(image_path)
        with patch.object(config, "UPLOAD_DIR", uploads):
            record = review.save_voucher(self.voucher(entries=self.balanced_entries()), company="甲",
                                         status=ReviewStatus.confirmed)
            record.image_path = "source.png"
            record.confirmed_at = datetime(2026, 10, 12, 9, 0)
            record.updated_at = datetime(2026, 10, 12, 9, 1)
            with Session(self.engine, expire_on_commit=False) as session:
                session.add(record)
                session.commit()
            ready = review.manage_entries(record.id, "confirm", record.updated_at.isoformat(), self.actor)
            original_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
            raw_confirmed_at = ready.confirmed_at

            for operation in (
                lambda: review.set_company(record.id, "乙", expected_updated_at="2000-01-01T00:00:00",
                                           actor=self.actor),
                lambda: review.rotate_image(record.id, "cw", expected_updated_at="2000-01-01T00:00:00",
                                            actor=self.actor),
            ):
                with self.assertRaises(entry_workflow.EntryConflict):
                    operation()
                self.assertEqual(hashlib.sha256(image_path.read_bytes()).hexdigest(), original_hash)

            current = review.get_voucher(record.id)
            self.assertEqual(current.entries_status, "ready")
            self.assertEqual(current.status, ReviewStatus.confirmed)
            self.assertEqual(current.company, "甲")
            self.assertEqual(current.confirmed_at, raw_confirmed_at)

            self.assertTrue(review.rotate_image(record.id, "cw",
                                                expected_updated_at=current.updated_at.isoformat(),
                                                actor=self.actor))
            rotated_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
            rotated = review.get_voucher(record.id)
            with Image.open(image_path) as image:
                self.assertEqual(image.size, (20, 32))
            self.assertNotEqual(rotated_hash, original_hash)
            self.assertEqual(rotated.status, ReviewStatus.pending)
            self.assertIsNone(rotated.confirmed_at)
            self.assertEqual(rotated.entries_status, "draft")
            self.assertIsNone(rotated.entries_confirmed_at)
            revoked = [event for event in json.loads(rotated.entries_history)
                       if event["action"] == "confirmation_revoked"]
            self.assertTrue(revoked)
            self.assertEqual(revoked[-1]["actor_name"], "分錄審核者")

    def test_stale_recognition_result_rolls_back_when_record_version_changed(self):
        record = review.save_voucher(self.voucher(), company="甲", status=ReviewStatus.pending)
        expected = record.updated_at
        payload = json.loads(record.current_json)
        payload["fields"]["total_amount"] = 125
        updated = review.update_current(record.id, payload)
        stored = updated.current_json
        recognition_history = updated.recognition_history

        with patch("app.core.pipeline.canonicalize_voucher") as canonicalize:
            with self.assertRaises(entry_workflow.EntryConflict):
                review._save_recognition(record.id, self.voucher(amount=999), expected_updated_at=expected)

        latest = review.get_voucher(record.id)
        canonicalize.assert_not_called()
        self.assertEqual(latest.current_json, stored)
        self.assertEqual(latest.recognition_history, recognition_history)


if __name__ == "__main__":
    unittest.main()
