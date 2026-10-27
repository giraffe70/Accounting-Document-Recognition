"""Offline regression tests for selected-company defaults and registration fallback."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from app import config
from app.core import company_defaults, doc_types, pipeline, prompts, recognition_checkpoint, supplement
from app.db import session as db_session
from app.db.models import Company, ReviewStatus, VoucherRecord
from app.models.schemas import Voucher
from app.services import companies, review, taxid


def valid_tax_id(start: int = 10000000) -> str:
    return next(value for number in range(start, start + 100000)
                if taxid.validate_tax_id(value := str(number)))


class CompanyDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.tax_id = valid_tax_id()
        self.company = {"id": 7, "name": "甲企業股份有限公司", "short": "甲企", "tax_id": self.tax_id}

    def test_invoice_matches_by_name_or_tax_id_and_only_fills_empty_party_fields(self):
        by_name = {"doc_type_key": "invoice", "fields": {"seller_name": "甲企業股份有限公司"}}
        company_defaults.apply(by_name, self.company)
        self.assertEqual(by_name["fields"]["seller_tax_id"], self.tax_id)
        self.assertIsNone(by_name["fields"].get("buyer_name"))

        by_tax_id = {"doc_type_key": "invoice", "fields": {"buyer_tax_id": self.tax_id,
                                                               "buyer_name": "紙本買方"}}
        company_defaults.apply(by_tax_id, self.company)
        self.assertEqual(by_tax_id["fields"]["buyer_name"], "紙本買方")
        self.assertEqual(by_tax_id["fields"]["buyer_tax_id"], self.tax_id)

    def test_default_buyer_and_document_specific_roles(self):
        for doc_type, name_key, tax_key in (
            ("invoice", "buyer_name", "buyer_tax_id"),
            ("statement", "customer", "customer_tax_id"),
            ("purchase_order", "buyer", "buyer_tax_id"),
            ("import_declaration", "taxpayer", "taxpayer_tax_id"),
            ("export_declaration", "exporter", "exporter_tax_id"),
            ("utility", "customer_name", "customer_tax_id"),
        ):
            with self.subTest(doc_type=doc_type):
                data = {"doc_type_key": doc_type, "fields": {}}
                company_defaults.apply(data, self.company)
                self.assertIsNone(data["fields"].get(name_key))
                self.assertEqual(data["fields"][tax_key], self.tax_id)

    def test_receipt_only_matches_vendor_and_other_has_no_default_role(self):
        receipt = {"doc_type_key": "receipt", "fields": {"vendor": "無關店家"}}
        company_defaults.apply(receipt, self.company)
        self.assertNotIn("tax_id", receipt["fields"])
        self.assertEqual(receipt["fields"]["vendor"], "無關店家")

        receipt["fields"]["vendor"] = self.company["name"]
        company_defaults.apply(receipt, self.company)
        self.assertEqual(receipt["fields"]["tax_id"], self.tax_id)

        other = {"doc_type_key": "other", "fields": {}}
        company_defaults.apply(other, self.company)
        self.assertEqual(other["fields"], {})

    def test_company_provenance_is_selected_and_repeated_apply_is_idempotent(self):
        data = {"doc_type_key": "invoice", "fields": {}}
        company_defaults.apply(data, self.company)
        path = "fields.buyer_tax_id"
        self.assertEqual(data["field_evidence"][path], [{
            "source": "company", "value": self.tax_id, "company_id": 7,
            "company_name": self.company["name"], "selected": True,
        }])
        original = json.dumps(data, ensure_ascii=False, sort_keys=True)
        company_defaults.apply(data, self.company)
        self.assertEqual(json.dumps(data, ensure_ascii=False, sort_keys=True), original)

    def test_company_switch_and_clear_remove_only_company_values(self):
        second_id = valid_tax_id(20000000)
        second = {"id": 8, "name": "乙企業有限公司", "short": "乙企", "tax_id": second_id}
        data = {"doc_type_key": "invoice", "fields": {}}
        company_defaults.apply(data, self.company)
        company_defaults.apply(data, second)
        self.assertIsNone(data["fields"].get("buyer_name"))
        self.assertEqual(data["fields"]["buyer_tax_id"], second_id)
        company_defaults.apply(data, None)
        self.assertIsNone(data["fields"]["buyer_tax_id"])
        self.assertEqual(data["company_info"], {})

    def test_same_name_with_different_printed_tax_id_is_preserved_and_warned(self):
        printed_tax_id = valid_tax_id(20000000)
        data = {"doc_type_key": "invoice", "fields": {
            "seller_name": self.company["name"], "seller_tax_id": printed_tax_id}}
        company_defaults.apply(data, self.company)
        self.assertEqual(data["fields"]["seller_tax_id"], printed_tax_id)
        self.assertFalse(any(record.get("source") == "company" and record.get("selected")
                             for record in data["field_evidence"].get("fields.seller_tax_id", [])))
        self.assertTrue(any("不同" in warning for warning in data["validation_warnings"]))

    def test_changing_invoice_to_unrelated_receipt_vendor_removes_invoice_defaults(self):
        data = {"doc_type_key": "invoice", "fields": {}}
        company_defaults.apply(data, self.company)
        self.assertEqual(data["fields"]["buyer_tax_id"], self.tax_id)
        data["doc_type_key"] = "receipt"
        data["fields"]["vendor"] = "無關店家"
        company_defaults.apply(data, self.company)
        self.assertIsNone(data["fields"]["buyer_tax_id"])
        self.assertNotIn("tax_id", data["fields"])
        self.assertFalse(any(record.get("source") == "company" and record.get("selected")
                             for path, records in data["field_evidence"].items()
                             if path.startswith("fields.") for record in records))

    def test_invalid_or_missing_company_does_not_fill_and_manual_blank_is_protected(self):
        for company in (None, {**self.company, "tax_id": "bad"}):
            data = {"doc_type_key": "invoice", "fields": {}}
            company_defaults.apply(data, company)
            self.assertIsNone(data["fields"].get("buyer_tax_id"))

        data = {"doc_type_key": "invoice", "fields": {"buyer_tax_id": None},
                "field_evidence": {"fields.buyer_tax_id": [
                    {"source": "manual", "value": None, "selected": True}]}}
        company_defaults.apply(data, self.company)
        self.assertIsNone(data["fields"]["buyer_tax_id"])

    def test_company_metadata_and_evidence_are_not_quality_content(self):
        data = {"doc_type_key": "invoice", "fields": {}}
        company_defaults.apply(data, self.company)
        voucher = pipeline._build_voucher(data, "invoice", derive_entries=False)
        self.assertEqual(voucher.company_info["id"], self.company["id"])
        self.assertEqual(voucher.quality["state"], "insufficient")
        self.assertIn("empty_content", {issue["code"] for issue in voucher.quality["issues"]})

    def test_extract_evidence_applies_company_after_model_snapshot(self):
        raw = prompts.extraction_example("invoice", "ocr_text")
        raw["fields"].update(seller_name=self.company["name"], seller_tax_id=None,
                             total_amount="105", voucher_id="AB12345678", date="2026-10-01")
        with patch("app.services.taxid.load_companies", return_value=[self.company]), \
                patch("app.core.pipeline._invoke_text", return_value=json.dumps(raw, ensure_ascii=False)), \
                patch.object(supplement, "apply"), \
                patch.object(recognition_checkpoint, "save", return_value="workspace-temp-checkpoint"), \
                patch.object(recognition_checkpoint, "complete"):
            voucher = pipeline.extract_evidence("synthetic.png", "invoice", object(), [],
                "發票賣方：" + self.company["name"], mode="ocr_text", company="甲企")
        self.assertEqual(voucher.company_info["id"], self.company["id"])
        self.assertEqual(voucher.fields["seller_tax_id"], self.tax_id)
        self.assertEqual(voucher.seller.tax_id, voucher.fields["seller_tax_id"])
        company_records = [record for record in voucher.field_evidence["fields.seller_tax_id"]
                           if record.get("source") == "company"]
        self.assertEqual(len(company_records), 1)
        self.assertTrue(company_records[0]["selected"])
        self.assertNotEqual(voucher.initial_ai.get("fields", {}).get("seller_tax_id"), self.tax_id)


class CompanyRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.engine = create_engine(f"sqlite:///{self.data_dir / 'companies.db'}")
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.registrations = [{"name": "登錄名稱", "short": "甲企", "tax_id": valid_tax_id(),
                               "account_chart_key": "ignored"}]

    def test_public_falls_back_by_exact_chart_key_and_preserves_present_values(self):
        registered = self.registrations
        with Session(self.engine) as session:
            missing = Company(name="DB 名稱", short="db", tax_id="", account_chart_key="甲企")
            existing = Company(name="保留名稱", short="keep", tax_id=valid_tax_id(20000000),
                               account_chart_key="乙企")
            session.add_all([missing, existing]); session.commit()
            session.refresh(missing); session.refresh(existing)
            self.assertEqual(companies.public(missing, registered)["tax_id"], registered[0]["tax_id"])
            public = companies.public(existing, registered)
            self.assertEqual((public["name"], public["tax_id"]), ("保留名稱", existing.tax_id))
            missing.account_chart_key = "甲"
            self.assertEqual(companies.public(missing, registered)["tax_id"], "")

    def test_seed_existing_rows_only_fills_tax_id_and_is_repeatable(self):
        with Session(self.engine) as session:
            existing = Company(name="已人工命名", short="人工簡稱", tax_id="", account_chart_key="甲企")
            session.add(existing); session.commit(); session.refresh(existing)
            original_id = existing.id
            companies.seed(session, self.registrations)
            session.commit()
            companies.seed(session, self.registrations)
            session.commit()
            rows = session.exec(select(Company)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0].id, rows[0].name, rows[0].short, rows[0].account_chart_key),
                             (original_id, "已人工命名", "人工簡稱", "甲企"))
            self.assertEqual(rows[0].tax_id, self.registrations[0]["tax_id"])

    def test_taxid_load_prefers_database_and_applies_registration_fallback(self):
        with Session(self.engine) as session:
            session.add(Company(name="DB 公司名稱", short="db", tax_id="", account_chart_key="甲企"))
            session.commit()
        with patch.object(db_session, "_engine", self.engine), patch.object(config, "DATA_DIR", self.data_dir), \
                patch.object(taxid, "_companies", None):
            (self.data_dir / "companies.json").write_text(json.dumps(self.registrations), encoding="utf-8")
            rows = taxid.load_companies()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["name"], rows[0]["tax_id"]),
                         ("DB 公司名稱", self.registrations[0]["tax_id"]))

    def test_check_party_with_blank_name_and_known_tax_id_has_no_name_mismatch(self):
        tax_id = self.registrations[0]["tax_id"]
        company = {"name": "登錄名稱", "short": "甲企", "tax_id": tax_id}
        with patch.object(taxid, "load_companies", return_value=[company]):
            result = taxid.check_party("", tax_id)
        self.assertEqual(result["known_company"], company["name"])
        self.assertIsNone(result["name_match"])

    def test_pending_prepared_and_company_switch_respect_default_and_manual_values(self):
        first = {"name": "甲企業", "short": "甲企", "tax_id": valid_tax_id(), "account_chart_key": "甲企"}
        second = {"name": "乙企業", "short": "乙企", "tax_id": valid_tax_id(20000000), "account_chart_key": "乙企"}
        with Session(self.engine) as session:
            session.add_all([Company(**first), Company(**second)])
            session.commit()
        with patch.object(review, "get_session", side_effect=lambda: Session(self.engine, expire_on_commit=False)):
            voucher = Voucher.model_validate({"doc_type_key": "invoice", "doc_type": "發票",
                "fields": {}, "items": [], "date": "2026-10-01"})
            pending = review.save_voucher(voucher, company="甲企", status=ReviewStatus.pending)
            pending_data = json.loads(pending.current_json)
            self.assertEqual(pending_data["fields"]["buyer_tax_id"], first["tax_id"])

            switched = review.set_company(pending.id, "乙企", expected_updated_at=pending.updated_at.isoformat())
            switched_data = json.loads(switched.current_json)
            self.assertEqual(switched_data["fields"]["buyer_tax_id"], second["tax_id"])
            self.assertEqual(switched_data["company_info"]["id"], switched.company_id)

            manual = review.save_voucher(voucher, company="甲企", status=ReviewStatus.pending)
            manual_data = json.loads(manual.current_json)
            manual_tax_id = valid_tax_id(30000000)
            updated = review.update_current(manual.id, {"fields": {"buyer_tax_id": manual_tax_id}},
                expected_updated_at=manual.updated_at)
            protected = review.set_company(updated.id, "乙企", expected_updated_at=updated.updated_at.isoformat())
            protected_data = json.loads(protected.current_json)
            self.assertEqual(protected_data["fields"]["buyer_tax_id"], manual_tax_id)
            self.assertTrue(any(row.get("source") == "manual" and row.get("selected")
                                and row.get("value") == manual_tax_id
                                for row in protected_data["field_evidence"]["fields.buyer_tax_id"]))

            prepared = review.save_voucher(voucher, company="甲企", status=ReviewStatus.prepared)
            prepared_data = json.loads(prepared.current_json)
            self.assertNotIn("buyer_tax_id", prepared_data["fields"])
            self.assertEqual(prepared_data["company_info"]["tax_id"], first["tax_id"])
            prepared_switched = review.set_company(prepared.id, "乙企",
                                                    expected_updated_at=prepared.updated_at.isoformat())
            prepared_switched_data = json.loads(prepared_switched.current_json)
            self.assertNotIn("buyer_tax_id", prepared_switched_data["fields"])
            self.assertEqual(prepared_switched_data["company_info"]["tax_id"], second["tax_id"])

    def test_same_company_tax_id_update_invalidates_existing_confirmation(self):
        tax_id = valid_tax_id()
        with Session(self.engine) as session:
            company = Company(name="甲企業", short="甲企", tax_id="", account_chart_key="甲企")
            session.add(company); session.commit(); session.refresh(company)
            company_id = company.id
        voucher = Voucher.model_validate({"doc_type_key": "invoice", "doc_type": "發票",
            "fields": {}, "items": [], "date": "2026-10-01"})
        with patch.object(config, "DATA_DIR", self.data_dir), \
                patch.object(review, "get_session", side_effect=lambda: Session(self.engine, expire_on_commit=False)):
            rec = review.save_voucher(voucher, company="甲企", status=ReviewStatus.pending)
            self.assertNotIn("buyer_tax_id", json.loads(rec.current_json)["fields"])
            with Session(self.engine) as session:
                company = session.get(Company, company_id)
                company.tax_id = tax_id
                stored = session.get(VoucherRecord, rec.id)
                stored.status = ReviewStatus.confirmed
                stored.confirmed_at = datetime(2026, 10, 27, 12, 0, 0)
                stored.recognition_confirmed_version = 3
                session.add_all([company, stored]); session.commit()
            updated = review.set_company(rec.id, "甲企", expected_updated_at=rec.updated_at.isoformat())
        data = json.loads(updated.current_json)
        self.assertEqual(data["fields"]["buyer_tax_id"], tax_id)
        self.assertEqual(data["field_evidence"]["fields.buyer_tax_id"][-1]["source"], "company")
        self.assertEqual(updated.status, ReviewStatus.pending)
        self.assertIsNone(updated.confirmed_at)
        self.assertEqual(updated.recognition_confirmed_version, 0)

    def test_every_company_party_field_is_in_its_document_contract(self):
        for doc_type, pairs in company_defaults.PARTIES.items():
            with self.subTest(doc_type=doc_type):
                contract_fields = {field["key"] for field in doc_types.get(doc_type)["fields"]}
                for name_key, tax_key in pairs:
                    self.assertIn(name_key, contract_fields)
                    self.assertIn(tax_key, contract_fields)

    def test_review_save_and_update_keep_party_projection_and_manual_clear(self):
        with Session(self.engine) as session:
            company = Company(name="甲企業股份有限公司", short="甲企", tax_id=valid_tax_id(),
                              account_chart_key="甲企")
            session.add(company); session.commit(); session.refresh(company)
            company_id = company.id
        with patch.object(review, "get_session", side_effect=lambda: Session(self.engine, expire_on_commit=False)):
            voucher = Voucher.model_validate({"doc_type_key": "invoice", "doc_type": "發票",
                "fields": {"seller_name": "甲企業股份有限公司"}, "items": [], "date": "2026-10-01"})
            rec = review.save_voucher(voucher, company="甲企")
            saved = json.loads(rec.current_json)
            self.assertEqual(saved["seller"]["name"], saved["fields"]["seller_name"])
            self.assertEqual(saved["seller"]["tax_id"], saved["fields"]["seller_tax_id"])
            self.assertEqual(saved["buyer"]["tax_id"], saved["fields"].get("buyer_tax_id") or "")

            updated = review.update_current(rec.id,
                {"fields": {"buyer_name": "", "buyer_tax_id": ""}},
                expected_updated_at=rec.updated_at)
            current = json.loads(updated.current_json)
            self.assertEqual(current["buyer"]["name"], current["fields"]["buyer_name"])
            self.assertEqual(current["buyer"]["tax_id"], current["fields"]["buyer_tax_id"] or "")
            self.assertEqual(current["fields"]["buyer_name"], "")
            self.assertTrue(any(row.get("source") == "manual" and row.get("selected")
                                and row.get("value") == ""
                                for row in current["field_evidence"]["fields.buyer_tax_id"]))
            self.assertEqual(updated.company_id, company_id)
            self.assertEqual(updated.status, ReviewStatus.pending)


if __name__ == "__main__":
    unittest.main()
