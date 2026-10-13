"""Offline tests for company-scoped item account mapping and review saves."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlmodel import Session, SQLModel, select, create_engine

from app.db import models, session as db_session
from app.db.models import Company, ItemAccountMapping, MappingHistory, ReviewStatus, UserAccount, VoucherRecord
from app.models.schemas import Voucher
from app.services import companies, item_account_mapping as mapping, review


class ItemAccountMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'mapping.db'}",
                                    connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        with Session(self.engine) as session:
            self.companies = []
            for name in ("甲公司", "乙公司", "丙公司"):
                company = Company(name=name, short=name[0], tax_id="", account_chart_key=name[0])
                session.add(company)
                self.companies.append(company)
            self.user = UserAccount(username="reviewer", name="審核者")
            session.add(self.user)
            session.commit()
            for item in (*self.companies, self.user):
                session.refresh(item)
            self.company_ids = [c.id for c in self.companies]
            self.user_id = self.user.id
        self.session_patch = patch.object(review, "get_session",
            side_effect=lambda: Session(self.engine, expire_on_commit=False))
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def actor(self, username="reviewer"):
        return {"username": username, "id": 987654, "name": "偽造名稱", "role": "admin"}

    def voucher(self, description="影印紙", *, doc_type="receipt", account_code="", account_name="",
                **extra):
        item = {"description": description, "quantity": 1, "amount": 100,
                "account_code": account_code, "account_name": account_name,
                "remember_mapping": False, **extra}
        return Voucher.model_validate({"doc_type_key": doc_type, "doc_type": doc_type,
            "date": "2026-10-01", "items": [item], "fields": {}, "field_evidence": {}})

    def new_record(self, company_index=0, description="影印紙", *, doc_type="receipt"):
        return review.save_voucher(self.voucher(description, doc_type=doc_type),
                                   company=self.companies[company_index].short)

    def payload(self, rec, *, action="save", company_index=None, items=None, choices=None,
                reviewed=None, extra=None):
        data = json.loads(rec.current_json)
        if items is not None:
            data["items"] = items
        if extra:
            data.update(extra)
        return {"action": action, "voucher": data,
                "company_id": rec.company_id if company_index is None else self.company_ids[company_index],
                "expected_updated_at": rec.updated_at.isoformat(),
                "mapping_choices": choices or {}, "reviewed_items": reviewed or {}}

    def test_company_isolation_and_lookup_across_document_types(self):
        rec = self.new_record(0)
        row = json.loads(rec.current_json)["items"][0]
        row.update(account_code="00120", account_name="辦公用品", remember_mapping=True)
        review.save_current(rec.id, self.payload(rec, items=[row]), self.actor())
        other = self.new_record(1, doc_type="invoice")
        with Session(self.engine) as session:
            hit = mapping.lookup(session, self.company_ids[0], [{"description": "影印紙", "item_id": "x"}])[0]
            miss = mapping.lookup(session, self.company_ids[1], [{"description": "影印紙"}])[0]
            cross_type = mapping.prepare_items(session, {"items": [{"description": "影印紙"}]},
                self.company_ids[0], model_result=True)
        self.assertEqual(hit["match"]["account_code"], "00120")
        self.assertEqual(hit["match"]["account_name"], "辦公用品")
        self.assertIsNone(miss["match"])
        self.assertEqual(cross_type[0]["account_code"], "00120")
        self.assertEqual(json.loads(other.current_json)["doc_type_key"], "invoice")

    def test_key_collapses_whitespace_but_preserves_case_and_model_code(self):
        self.assertEqual(mapping.item_key("  A\t型號\n X  "), "A 型號 X")
        self.assertNotEqual(mapping.item_key("ABC-1"), mapping.item_key("abc-1"))
        rec = self.new_record(description=" 型號  X ")
        normalized = json.loads(rec.current_json)["items"][0]
        self.assertEqual(normalized["account_item_key"], "型號 X")
        data = {"items": [{"description": "型號 X", "account_code": "999", "account_name": "惡意"}]}
        with Session(self.engine) as session:
            rows = mapping.prepare_items(session, data, self.company_ids[0], model_result=True)
        self.assertEqual((rows[0]["account_code"], rows[0]["account_name"]), ("", ""))

    def test_lookup_is_read_only_and_temporary_does_not_publish(self):
        rec = self.new_record()
        with self.engine.connect() as connection:
            before = connection.exec_driver_sql("SELECT count(*) FROM item_account_mappings").scalar()
        row = json.loads(rec.current_json)["items"][0]
        row.update(account_code="00600", account_name="文具費", remember_mapping=True)
        review.save_current(rec.id, self.payload(rec, action="temporary", items=[row]), self.actor())
        with Session(self.engine) as session:
            mapping.lookup(session, self.company_ids[0], [{"description": "影印紙"}])
            self.assertEqual(session.exec(select(ItemAccountMapping)).all(), [])
        with self.engine.connect() as connection:
            after = connection.exec_driver_sql("SELECT count(*) FROM item_account_mappings").scalar()
        self.assertEqual(before, after)

    def test_save_remember_false_and_same_value_noop(self):
        rec = self.new_record()
        row = json.loads(rec.current_json)["items"][0]
        self.assertFalse(row["remember_mapping"])
        row.update(account_code="00600", account_name="文具費", remember_mapping=False)
        _, skipped = review.save_current(rec.id, self.payload(rec, items=[row]), self.actor())
        self.assertEqual(skipped, [])
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(ItemAccountMapping)).all(), [])
        row["remember_mapping"] = True
        current = review.get_voucher(rec.id)
        _, skipped = review.save_current(rec.id, self.payload(current, items=[row]), self.actor())
        self.assertEqual(skipped, [])
        with Session(self.engine) as session:
            rules = session.exec(select(ItemAccountMapping)).all()
            self.assertEqual(len(rules), 1)
            version = rules[0].version
            histories = session.exec(select(MappingHistory)).all()
            self.assertEqual(len(histories), 1)
        current = review.get_voucher(rec.id)
        _, skipped = review.save_current(current.id, self.payload(current), self.actor())
        with Session(self.engine) as session:
            rule = session.exec(select(ItemAccountMapping)).one()
            self.assertEqual(rule.version, version)
            self.assertEqual(len(session.exec(select(MappingHistory)).all()), 1)

    def test_leading_zero_new_rule_and_actor_is_resolved_from_user_table(self):
        rec = self.new_record()
        row = json.loads(rec.current_json)["items"][0]
        row.update(account_code="00120", account_name="辦公用品", remember_mapping=True)
        review.save_current(rec.id, self.payload(rec, items=[row]), self.actor())
        with Session(self.engine) as session:
            rule = session.exec(select(ItemAccountMapping)).one()
            history = session.exec(select(MappingHistory)).one()
            self.assertEqual(rule.account_code, "00120")
            self.assertEqual(rule.created_by, self.user_id)
            self.assertEqual(rule.updated_by, self.user_id)
            self.assertEqual(rule.created_by_name, "審核者")
            self.assertEqual(history.actor_id, self.user_id)
            self.assertEqual(history.actor_name, "審核者")

    def test_new_rule_conflicts_save_locally_without_publishing(self):
        rec = self.new_record()
        data = json.loads(rec.current_json)
        first = data["items"][0]
        first.update(account_code="00600", account_name="文具費", remember_mapping=True)
        second = dict(first, item_id="", account_code="00610", account_name="耗材費")
        data["items"].append(second)
        saved, skipped = review.save_current(rec.id, self.payload(rec, items=data["items"]), self.actor())
        self.assertEqual(len(skipped), 2)
        saved_rows = json.loads(saved.current_json)["items"]
        self.assertEqual([row["account_code"] for row in saved_rows], ["00600", "00610"])
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(ItemAccountMapping)).all(), [])
            self.assertNotEqual(session.get(VoucherRecord, rec.id).updated_at, rec.updated_at)

    def test_existing_rule_requires_explicit_versioned_update_or_local_choice(self):
        rec = self.new_record()
        row = json.loads(rec.current_json)["items"][0]
        row.update(account_code="00600", account_name="文具費", remember_mapping=True)
        review.save_current(rec.id, self.payload(rec, items=[row]), self.actor())
        other_doc = self.new_record(doc_type="invoice")
        row2 = json.loads(other_doc.current_json)["items"][0]
        row2.update(account_code="00620", account_name="辦公耗材", remember_mapping=True)
        with Session(self.engine) as session:
            with self.assertRaises(mapping.MappingConflict):
                mapping.publish(session, other_doc, {"doc_type_key": "invoice", "items": [row2]}, self.actor())
        _, skipped = review.save_current(other_doc.id, self.payload(other_doc, items=[row2]), self.actor())
        self.assertEqual(len(skipped), 1)
        saved_row = json.loads(review.get_voucher(other_doc.id).current_json)["items"][0]
        self.assertEqual(saved_row["account_code"], "00620")
        current = review.get_voucher(other_doc.id)
        _, skipped = review.save_current(current.id, self.payload(current, items=[row2], choices={
            "影印紙": {"action": "update", "expected_version": 9, "item_id": row2["item_id"]}}), self.actor())
        self.assertEqual(len(skipped), 1)
        current = review.get_voucher(other_doc.id)
        review.save_current(current.id, self.payload(current, items=[row2], choices={
            "影印紙": {"action": "local"}}), self.actor())
        with Session(self.engine) as session:
            unchanged = session.exec(select(ItemAccountMapping)).one()
            self.assertEqual((unchanged.version, unchanged.account_code), (1, "00600"))
        current = review.get_voucher(other_doc.id)
        review.save_current(current.id, self.payload(current, items=[row2], choices={
            "影印紙": {"action": "update", "expected_version": 1, "item_id": row2["item_id"]}}), self.actor())
        with Session(self.engine) as session:
            rule = session.exec(select(ItemAccountMapping)).one()
            self.assertEqual((rule.version, rule.account_code), (2, "00620"))
            self.assertEqual([h.version for h in session.exec(select(MappingHistory).order_by(MappingHistory.version))], [1, 2])

    def test_different_pairs_on_one_document_need_a_choice(self):
        rec = self.new_record()
        rows = json.loads(rec.current_json)["items"]
        rows[0].update(account_code="00600", account_name="文具費", remember_mapping=True)
        rows.append(dict(rows[0], item_id="00000000-0000-0000-0000-000000000002",
                         account_code="00610", account_name="耗材費", remember_mapping=True))
        _, skipped = review.save_current(rec.id, self.payload(rec, items=rows), self.actor())
        self.assertEqual(len(skipped), 2)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(ItemAccountMapping)).all(), [])
        current = review.get_voucher(rec.id)
        chosen = rows[1]
        review.save_current(current.id, self.payload(current, items=rows, choices={"影印紙": {
            "action": "update", "expected_version": 0, "item_id": chosen["item_id"]}}), self.actor())
        with Session(self.engine) as session:
            rule = session.exec(select(ItemAccountMapping)).one()
            self.assertEqual((rule.account_code, rule.account_name), ("00610", "耗材費"))

    def test_untrusted_metadata_is_ignored_and_item_ids_keep_evidence_alignment(self):
        rec = self.new_record()
        data = json.loads(rec.current_json)
        first = data["items"][0]
        first.update(account_code="00999", account_name="偽造科目", mapping_id=999,
                     mapping_version=999, account_source="mapping", account_company_id=999,
                     account_item_key="偽造", account_needs_review=True)
        data["field_evidence"] = {"items[0].description": [{"value": "影印紙", "selected": True}],
                                  "items[0].account_code": [{"value": "00999", "selected": True}]}
        review.save_current(rec.id, self.payload(rec, items=[first], extra={"field_evidence": data["field_evidence"]}), self.actor())
        saved = json.loads(review.get_voucher(rec.id).current_json)
        self.assertNotEqual(saved["items"][0]["mapping_id"], 999)
        self.assertEqual(saved["items"][0]["account_source"], "manual")
        self.assertNotIn("items[0].account_code", saved["field_evidence"])
        self.assertNotIn("account_code", json.loads(rec.ai_json)["items"][0])

    def test_manual_review_after_company_or_item_change_and_acknowledgement(self):
        rec = self.new_record()
        row = json.loads(rec.current_json)["items"][0]
        row.update(account_code="00600", account_name="文具費", remember_mapping=True)
        review.save_current(rec.id, self.payload(rec, items=[row]), self.actor())
        current = review.get_voucher(rec.id)
        payload = self.payload(current, company_index=1)
        review.save_current(current.id, payload, self.actor())
        saved = json.loads(review.get_voucher(rec.id).current_json)["items"][0]
        self.assertTrue(saved["account_needs_review"])
        current = review.get_voucher(rec.id)
        saved["description"] = "碳粉匣"
        review.save_current(current.id, self.payload(current, items=[saved]), self.actor())
        saved = json.loads(review.get_voucher(rec.id).current_json)["items"][0]
        self.assertTrue(saved["account_needs_review"])
        reviewed = {saved["item_id"]: {"company_id": self.company_ids[1],
            "item_name_key": "碳粉匣", "account_code": "00600", "account_name": "文具費"}}
        current = review.get_voucher(rec.id)
        review.save_current(current.id, self.payload(current, reviewed=reviewed), self.actor())
        self.assertFalse(json.loads(review.get_voucher(rec.id).current_json)["items"][0]["account_needs_review"])

    def test_account_metadata_is_stripped_from_raw_ai_and_alignment(self):
        data = {"items": [{"description": "A", "account_code": "1", "item_id": "x"}],
                "field_evidence": {"items[0].description": [1], "items[0].account_code": [2]}}
        stripped = mapping.strip_accounting(data)
        self.assertEqual(stripped["items"], [{"description": "A"}])
        self.assertEqual(stripped["field_evidence"], {"items[0].description": [1]})

    def test_item_identity_reorders_and_deletes_with_matching_evidence(self):
        first = {"item_id": "00000000-0000-0000-0000-000000000001", "description": "A"}
        second = {"item_id": "00000000-0000-0000-0000-000000000002", "description": "B"}
        previous = {"items": [first, second], "field_evidence": {
            "items[0].description": ["A-evidence"], "items[1].description": ["B-evidence"], "fields.date": ["date"]}}
        evidence = mapping.remap_evidence(previous, [second, first])
        self.assertEqual(evidence["items[0].description"], ["B-evidence"])
        self.assertEqual(evidence["items[1].description"], ["A-evidence"])
        self.assertEqual(evidence["fields.date"], ["date"])
        self.assertNotIn("items[1].description", mapping.remap_evidence(previous, [first]))

    def test_account_chart_is_not_modified_by_item_mapping(self):
        rec = self.new_record()
        row = json.loads(rec.current_json)["items"][0]
        row.update(account_code="00120", account_name="辦公用品", remember_mapping=True)
        review.save_current(rec.id, self.payload(rec, items=[row]), self.actor())
        with Session(self.engine) as session:
            company = session.get(Company, self.company_ids[0])
            self.assertEqual(company.account_chart_key, "甲")
            self.assertEqual(session.exec(select(ItemAccountMapping)).one().account_code, "00120")

    def test_company_rename_keeps_chart_key_and_manual_mapping_does_not_edit_chart(self):
        from app.services import accounts
        chart_path = Path(self.tmp.name) / "accounts.json"
        chart_path.write_text(json.dumps({"甲": [{"code": "0007", "name": "原科目"}],
                                          "_default": [{"code": "9999", "name": "通用"}]}), encoding="utf-8")
        original = chart_path.read_bytes()
        with Session(self.engine) as session:
            company = session.get(Company, self.company_ids[0])
            company.short = "甲新"
            session.add(company)
            session.commit()
        with patch.object(db_session, "get_session", side_effect=lambda: Session(self.engine)), \
             patch.object(accounts, "_ACCOUNTS_FILE", chart_path):
            self.assertEqual(accounts.load_accounts("甲新", strict=True), [{"code": "0007", "name": "原科目"}])
            self.assertEqual(accounts.load_accounts("乙", strict=True), [])
            rec = review.save_voucher(self.voucher(), company="甲新")
            row = json.loads(rec.current_json)["items"][0]
            row.update(account_code="0008", account_name="手填科目")
            review.save_current(rec.id, self.payload(rec, items=[row]), self.actor())
        self.assertEqual(chart_path.read_bytes(), original)

    def test_source_and_children_delete_together_with_foreign_keys_enabled(self):
        with Session(self.engine) as session:
            session.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
            source = VoucherRecord(record_role="source_page")
            session.add(source)
            session.flush()
            child = VoucherRecord(source_parent_id=source.id)
            session.add(child)
            session.commit()
            session.refresh(source)
            source_id = source.id
        with patch.object(review, "_cleanup_deleted_files", return_value=(0, [])), patch.object(review, "_write_delete_audit"):
            result = review.delete_vouchers(source_id, "record", is_admin=True, actor="reviewer")
        self.assertEqual(len(result["deleted_ids"]), 2)
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(VoucherRecord)).all(), [])
            self.assertEqual(session.connection().exec_driver_sql("PRAGMA foreign_key_check").all(), [])

    def test_deleting_source_voucher_preserves_mapping_history(self):
        rec = self.new_record()
        row = json.loads(rec.current_json)["items"][0]
        row.update(account_code="00120", account_name="辦公用品", remember_mapping=True)
        review.save_current(rec.id, self.payload(rec, items=[row]), self.actor())
        with patch.object(review, "_cleanup_deleted_files", return_value=(0, [])), \
             patch.object(review, "_write_delete_audit"):
            result = review.delete_vouchers(rec.id, "record", is_admin=True, actor="reviewer")
        self.assertEqual(result["deleted_ids"], [rec.id])
        with Session(self.engine) as session:
            rule = session.exec(select(ItemAccountMapping)).one()
            history = session.exec(select(MappingHistory)).one()
            self.assertIsNone(rule.source_voucher_id)
            self.assertIsNone(history.source_voucher_id)
            self.assertEqual(history.source_item_id, row["item_id"])
            self.assertEqual(json.loads(history.after_json)["account_code"], "00120")

    def test_company_rename_keeps_identity_and_chart_key(self):
        company_id = self.company_ids[0]
        with Session(self.engine) as session:
            company = session.get(Company, company_id)
            company.name, company.short = "甲公司改名", "甲新"
            session.add(company)
            session.commit()
            session.refresh(company)
            chart_key = company.account_chart_key
            companies.seed(session, [{"name": "另一家公司", "short": "甲新", "tax_id": "OTHER"}])
            resolved = companies.resolve(session, company_id=company_id)
            self.assertEqual((resolved.id, resolved.name, resolved.short), (company_id, "甲公司改名", "甲新"))
            self.assertEqual(resolved.account_chart_key, chart_key)
            self.assertEqual(len(session.exec(select(Company)).all()), 3)

    def test_inactive_and_partial_rules_do_not_suggest_accounts(self):
        with Session(self.engine) as session:
            session.add(ItemAccountMapping(company_id=self.company_ids[0], item_name="停用項",
                item_name_key="停用項", account_code="00600", account_name="文具費", is_active=False))
            session.add(ItemAccountMapping(company_id=self.company_ids[0], item_name="未完成項",
                item_name_key="未完成項", account_code="00610", account_name="", is_active=True))
            session.commit()
            found = mapping.lookup(session, self.company_ids[0], [
                {"description": "停用項"}, {"description": "未完成項"}])
            self.assertEqual([row["match"] for row in found], [None, None])

    def test_malformed_and_cross_voucher_item_ids_are_rejected(self):
        first = self.new_record(description="第一張")
        second = self.new_record(description="第二張")
        first_row = json.loads(first.current_json)["items"][0]
        changed = dict(first_row, item_id="not-a-uuid")
        with self.assertRaises(ValueError):
            review.save_current(first.id, self.payload(first, items=[changed]), self.actor())
        with self.assertRaises(ValueError):
            review.save_current(second.id, self.payload(second, items=[first_row]), self.actor())
        self.assertEqual(json.loads(review.get_voucher(second.id).current_json)["items"][0]["description"], "第二張")

    def test_concurrent_saves_reject_stale_voucher_version(self):
        rec = self.new_record()
        rows = json.loads(rec.current_json)["items"]
        payloads = [self.payload(rec, items=[dict(rows[0], amount=amount)]) for amount in (101, 102)]
        barrier = threading.Barrier(2)
        outcomes = []

        def save(payload):
            barrier.wait(timeout=5)
            try:
                review.save_current(rec.id, payload, self.actor())
                outcomes.append("saved")
            except Exception as exc:  # collect both thread outcomes for a deterministic assertion
                outcomes.append(exc)

        threads = [threading.Thread(target=save, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads), "concurrent saves did not finish")
        self.assertEqual(outcomes.count("saved"), 1)
        conflicts = [result for result in outcomes if isinstance(result, mapping.MappingConflict)]
        self.assertEqual(len(conflicts), 1)
        self.assertIn("updated_at", conflicts[0].latest)

    def test_concurrent_new_rule_conflict_saves_both_and_publishes_one(self):
        records = [self.new_record(), self.new_record()]
        codes = (("00600", "文具費"), ("00610", "耗材費"))
        payloads = []
        for rec, (code, name) in zip(records, codes):
            row = json.loads(rec.current_json)["items"][0]
            row.update(account_code=code, account_name=name, remember_mapping=True)
            payloads.append(self.payload(rec, items=[row]))
        barrier = threading.Barrier(2)
        outcomes = []

        def save(rec, payload):
            barrier.wait(timeout=5)
            try:
                _, skipped = review.save_current(rec.id, payload, self.actor())
                outcomes.append(("saved", rec.id, skipped))
            except Exception as exc:
                outcomes.append((exc, rec.id, []))

        threads = [threading.Thread(target=save, args=args) for args in zip(records, payloads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads), "concurrent saves did not finish")
        self.assertEqual(sum(outcome[0] == "saved" for outcome in outcomes), 2)
        self.assertEqual(sum(bool(outcome[2]) for outcome in outcomes), 1)
        with Session(self.engine) as session:
            rules = session.exec(select(ItemAccountMapping)).all()
            history = session.exec(select(MappingHistory)).all()
            self.assertEqual((len(rules), len(history), rules[0].version), (1, 1, 1))
            saved_codes = {json.loads(session.get(VoucherRecord, row.id).current_json)["items"][0]["account_code"]
                           for row in records}
            self.assertEqual(saved_codes, {"00600", "00610"})

    def test_company_change_invalidates_auto_value_and_raw_confirmation_keeps_manual_value_pending(self):
        source = self.new_record(company_index=0)
        source_row = json.loads(source.current_json)["items"][0]
        source_row.update(account_code="00600", account_name="文具費", remember_mapping=True)
        review.save_current(source.id, self.payload(source, items=[source_row]), self.actor())
        automatic = self.new_record(company_index=0)
        automatic_row = json.loads(automatic.current_json)["items"][0]
        self.assertEqual(automatic_row["account_source"], "mapping")
        review.save_current(automatic.id, self.payload(automatic, action="temporary", company_index=1), self.actor())
        invalidated = json.loads(review.get_voucher(automatic.id).current_json)["items"][0]
        self.assertEqual((invalidated["account_code"], invalidated["account_name"]), ("", ""))

        manual = self.new_record(company_index=0)
        manual_row = json.loads(manual.current_json)["items"][0]
        manual_row.update(account_code="00700", account_name="差旅費", remember_mapping=True)
        review.save_current(manual.id, self.payload(manual, action="temporary", items=[manual_row]), self.actor())
        current = review.get_voucher(manual.id)
        review.save_current(current.id, self.payload(current, action="temporary", company_index=1), self.actor())
        current = review.get_voucher(manual.id)
        needs_review = json.loads(current.current_json)["items"][0]
        self.assertTrue(needs_review["account_needs_review"])
        confirmed, skipped = review.save_current(current.id, self.payload(current, action="confirm"), self.actor())
        self.assertEqual(confirmed.status, ReviewStatus.confirmed)
        self.assertTrue(skipped)
        needs_review = json.loads(review.get_voucher(manual.id).current_json)["items"][0]
        self.assertTrue(needs_review["account_needs_review"])
        acknowledgement = {needs_review["item_id"]: {"company_id": self.company_ids[1],
            "item_name_key": "影印紙", "account_code": "00700", "account_name": "差旅費"}}
        current = review.get_voucher(manual.id)
        confirmed, _ = review.save_current(current.id, self.payload(current, action="confirm",
            reviewed=acknowledgement), self.actor())
        self.assertEqual(confirmed.status, ReviewStatus.confirmed)

    def test_recognition_preserves_manual_accounts_and_rebuilds_model_item_identity(self):
        from app.core import model_invocation, pipeline

        manual = self.new_record()
        manual_row = json.loads(manual.current_json)["items"][0]
        manual_row.update(account_code="00700", account_name="差旅費")
        review.save_current(manual.id, self.payload(manual, action="temporary", items=[manual_row]), self.actor())
        protected_id = json.loads(review.get_voucher(manual.id).current_json)["items"][0]["item_id"]
        model_row = {"description": "模型改名", "item_id": "", "account_code": "99999",
                     "account_name": "模型偽造", "mapping_id": 999, "account_source": "mapping"}
        model_result = self.voucher(description="模型改名", **{
            "item_id": "", "account_code": "99999", "account_name": "模型偽造",
            "mapping_id": 999, "account_source": "mapping"})
        model_result.items[0].description = model_row["description"]
        with patch.object(pipeline, "canonicalize_voucher", side_effect=lambda data: data), \
             patch.object(model_invocation, "source_versions", return_value={"fixture": "offline"}):
            review._save_recognition(manual.id, model_result)
        protected = json.loads(review.get_voucher(manual.id).current_json)["items"][0]
        self.assertEqual(protected["item_id"], protected_id)
        self.assertEqual((protected["account_code"], protected["account_name"], protected["account_source"]),
                         ("00700", "差旅費", "manual"))

        replaceable = self.new_record(description="辨識前品名")
        previous_id = json.loads(replaceable.current_json)["items"][0]["item_id"]
        replacement = Voucher.model_validate({"doc_type_key": "receipt", "date": "2026-10-02",
            "items": [{"description": "辨識後品名", "item_id": "", "account_code": "99999",
                       "account_name": "模型偽造", "mapping_id": 999, "account_source": "mapping"}]})
        with patch.object(pipeline, "canonicalize_voucher", side_effect=lambda data: data), \
             patch.object(model_invocation, "source_versions", return_value={"fixture": "offline"}):
            review._save_recognition(replaceable.id, replacement)
        replaced = json.loads(review.get_voucher(replaceable.id).current_json)["items"][0]
        self.assertNotEqual(replaced["item_id"], previous_id)
        self.assertTrue(replaced["item_id"])
        self.assertEqual((replaced["account_code"], replaced["account_name"]), ("", ""))
        self.assertNotEqual(replaced.get("mapping_id"), 999)


class ItemAccountMigrationTests(unittest.TestCase):
    def test_migration_is_idempotent_and_seeds_only_synthetic_companies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = create_engine(f"sqlite:///{root / 'legacy.db'}")
            self.addCleanup(engine.dispose)
            SQLModel.metadata.create_all(engine)
            with Session(engine) as session:
                session.add(VoucherRecord(company="甲", current_json=json.dumps({"items": [{"description": "  A   型號 "}]})))
                session.commit()
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "companies.json").write_text(json.dumps([
                {"name": "合成甲公司", "short": "甲", "tax_id": "TEST001"},
                {"name": "合成乙公司", "short": "乙", "tax_id": "TEST002"},
                {"name": "合成丙公司", "short": "丙", "tax_id": "TEST003"}], ensure_ascii=False), encoding="utf-8")
            with patch.object(db_session, "_engine", engine), patch.object(db_session.config, "DATA_DIR", data_dir):
                db_session.init_db()
                db_session.init_db()
            with Session(engine) as session:
                self.assertEqual(len(session.exec(select(Company)).all()), 3)
                record = session.exec(select(VoucherRecord)).one()
                self.assertIsNotNone(record.company_id)
                item = json.loads(record.current_json)["items"][0]
                self.assertRegex(item["item_id"], r"^[0-9a-f-]{36}$")
                self.assertEqual(item["account_item_key"], "A 型號")
                self.assertEqual(len(session.exec(select(ItemAccountMapping)).all()), 0)
            engine.dispose()

    def test_legacy_voucher_schema_without_company_id_is_migrated_idempotently(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = create_engine(f"sqlite:///{Path(temp) / 'legacy-schema.db'}")
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql("""
                        CREATE TABLE voucher_records (
                            id INTEGER PRIMARY KEY, company TEXT DEFAULT '', current_json TEXT DEFAULT '{}',
                            status TEXT DEFAULT 'pending', manual_entry VARCHAR DEFAULT '0',
                            clerk_note TEXT DEFAULT '', accountant_note TEXT DEFAULT '', dev_note TEXT DEFAULT '',
                            clerk_reviewed_at TEXT, accountant_approved_at TEXT
                        )
                    """)
                    connection.exec_driver_sql("INSERT INTO voucher_records (id, company, current_json) VALUES (1, '甲', '{}')")
                with patch.object(db_session, "_engine", engine):
                    SQLModel.metadata.create_all(engine)
                    db_session._migrate()
                    db_session._migrate()
                with engine.connect() as connection:
                    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(voucher_records)")}
                    self.assertIn("company_id", columns)
                    self.assertIn("recognition_history", columns)
                    self.assertNotIn("clerk_note", columns)
                    self.assertNotIn("accountant_approved_at", columns)
                    migrated = connection.exec_driver_sql(
                        "SELECT company_id, manual_entry FROM voucher_records WHERE id=1").one()
                    self.assertIsNone(migrated[0])
                    self.assertEqual(migrated[1], 0)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
