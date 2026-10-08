"""Offline regressions for normalization, evidence, policy and stored data flow."""
from __future__ import annotations
import csv
import io
import json
import tempfile
import unittest
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import config, validation
from app.core import barcode, doc_types, model_invocation, pipeline, prompts
from app.models.schemas import Voucher
from app.services import accounting_policy, classify, importdecl, journal, review

CHART = [{"code": code, "name": name} for code, name in (
    ("1210", "預付費用"), ("1290", "進項稅額"), ("6230", "其他費用"),
    ("1110", "銀行存款"), ("2130", "應付帳款"), ("2200", "應付費用"),
    ("6200", "文具用品"), ("4100", "銷貨收入"))]


def proposal(amount, path, *, debit="", credit=""):
    return {"description": "測試項目", "amount": amount, "debit_account": debit, "debit_account_name": "",
            "credit_account": credit, "credit_account_name": "", "evidence_paths": [path]}


def qr(number="AA12345678", roc="1150604", subtotal=100, total=105):
    return f"{number}{roc}1234{subtotal:08X}{total:08X}0000000012345678"


class RefactorFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_patch = patch.object(config, "DATA_DIR", Path(self.temp.name))
        self.data_patch.start(); self.addCleanup(self.data_patch.stop)
        self.party_patch = patch("app.services.taxid.load_companies", return_value=[])
        self.party_patch.start(); self.addCleanup(self.party_patch.stop)

    def test_normalization_preserves_zero_raw_words_and_invalid_values(self):
        self.assertEqual(pipeline._norm_roc_date("1150604"), "2026-06-04")
        self.assertEqual(pipeline._norm_roc_date("20260604"), "2026-06-04")
        self.assertEqual(pipeline._norm_roc_date("1900/01/01"), "1900-01-01")
        self.assertEqual(pipeline._norm_roc_date("1150230"), "1150230")
        self.assertEqual(pipeline._norm_amount("玖仟肆佰貳拾貳圓整"), 9422)
        self.assertEqual(pipeline._norm_amount("壹萬零伍元貳角參分"), 10005.23)
        self.assertEqual(pipeline._norm_amount("零元整"), 0)
        self.assertEqual(pipeline._norm_amount("伍分"), .05)
        raw = prompts.extraction_example("receipt")
        raw["fields"].update(total_amount=0, subtotal=0, tax_amount=0, withholding_tax=30)
        v = pipeline._build_voucher(raw, "receipt")
        self.assertEqual((v.total_amount, v.subtotal, v.tax_amount), (0, 0, 0))
        raw["fields"].update(total_amount="看不清", tax_amount=None)
        v = pipeline._build_voucher(raw, "receipt")
        self.assertIsNone(v.total_amount)
        self.assertIsNone(v.tax_amount)
        self.assertEqual(v.field_evidence["fields.total_amount"][0]["value"], "看不清")
        self.assertTrue(any("無法解析" in w for w in v.validation_warnings))

    def test_statement_mapping_is_canonical_and_reference_invoice_is_not_id(self):
        raw = prompts.extraction_example("statement")
        raw["fields"].update(issuer="開立方", issuer_tax_id="12345678", customer="客戶", customer_tax_id="87654321",
                             ref_invoice_no="AA12345678", total_amount=0, date="1150604")
        v = pipeline._build_voucher(raw, "statement")
        self.assertEqual(v.voucher_id, "")
        self.assertEqual(v.seller.name, "開立方")
        self.assertEqual(v.buyer.name, "客戶")
        saved = pipeline.canonicalize_voucher({**v.model_dump(), "total_amount": 999, "seller": {}, "fields": {**v.fields, "total_amount": 105}})
        self.assertEqual(saved["total_amount"], 105)
        self.assertEqual(saved["seller"]["name"], "開立方")

    def test_legacy_receipt_tax_stays_ambiguous_after_edit_until_manually_corrected(self):
        old = Voucher(doc_type_key="receipt", fields={"total_amount": 100, "tax_amount": 30}).model_dump()
        saved = pipeline.canonicalize_voucher(old)
        self.assertIsNone(saved["tax_amount"])
        self.assertTrue(any("舊收據稅額" in w for w in saved["validation_warnings"]))
        v = Voucher.model_validate(pipeline.canonicalize_voucher(saved))
        context = classify._accounting_context(v, accounting_policy.load_policy("A", v.model_dump(), CHART))
        self.assertNotIn("fields.tax_amount", context["amount_sources"])
        self.assertNotIn("tax_amount", context["amount_sources"])
        rec = SimpleNamespace(current_json=json.dumps(saved), period="", updated_at=None)
        session = SimpleNamespace(get=lambda *_: rec, add=lambda *_: None, commit=lambda: None, refresh=lambda *_: None)
        with patch("app.services.review.get_session", return_value=nullcontext(session)):
            review.update_current(1, {"fields": {"tax_amount": None, "withholding_tax": 30}})
        corrected = json.loads(rec.current_json)
        self.assertEqual(corrected["fields"]["withholding_tax"], 30)
        self.assertIsNone(corrected["tax_amount"])
        self.assertFalse(any("舊收據稅額" in w for w in corrected["validation_warnings"]))

    def test_uppercase_amount_conflict_retains_both_and_missing_numeric_can_be_normalized(self):
        raw = prompts.extraction_example("cheque")
        raw["fields"].update(amount_words="壹佰元整", total_amount=90)
        v = pipeline._build_voucher(raw, "cheque")
        self.assertEqual(v.total_amount, 90)
        self.assertTrue(any("大寫" in w for w in v.validation_warnings))
        raw["fields"]["total_amount"] = None
        v = pipeline._build_voucher(raw, "cheque")
        self.assertEqual(v.total_amount, 100)
        self.assertEqual(v.field_evidence["fields.total_amount"][0]["source_path"], "fields.amount_words")

    def test_qr_conflicts_are_not_silently_selected_and_calendar_is_validated(self):
        self.assertEqual(barcode.parse_einvoice_qr([qr(), qr()])["total_amount"], 105)
        self.assertEqual(barcode.parse_einvoice_qr([qr(), qr(total=106)]), {})
        self.assertEqual(barcode.parse_einvoice_qr([qr(roc="1150230")]), {})
        data = prompts.extraction_example("invoice")
        data["fields"].update(total_amount=90, seller_tax_id="12345678")
        with patch("app.core.barcode.decode", return_value=[qr()]), patch("app.core.pipeline._data_uri", return_value="data:image/png;base64,test"), \
             patch("app.core.pipeline._invoke_text", return_value=json.dumps(data)):
            v = pipeline.extract_evidence("fake.png", "invoice", object(), [], "", confirmed_fields={"total_amount": 99})
        self.assertEqual(v.total_amount, 99)
        evidence = v.field_evidence["fields.total_amount"]
        self.assertEqual([r["source"] for r in evidence], ["model", "qr", "manual"])
        self.assertEqual([r["selected"] for r in evidence], [False, False, True])
        with patch("app.core.barcode.decode", return_value=[qr(), qr(total=106)]), patch("app.core.pipeline._data_uri", return_value="data:image/png;base64,test"), \
             patch("app.core.pipeline._invoke_text", return_value=json.dumps(data)):
            v = pipeline.extract_evidence("fake.png", "invoice", object(), [], "")
        self.assertEqual(v.total_amount, 90)
        self.assertTrue(any("多個有效 QR" in w for w in v.validation_warnings))

    def test_receipt_reread_targets_actual_tax_id_and_never_overwrites_existing_vendor(self):
        initial = prompts.extraction_example("receipt", "ocr_text")
        initial["fields"].update(vendor="原廠商", total_amount=100, date="2026-06-04")
        for key in doc_types.get("receipt")["common_fields"]["voucher_id"]:
            if key in initial["fields"]:
                initial["fields"][key] = "R-001"
        requests = []
        def invoke(chat, content):
            requests.append(content)
            return json.dumps(initial if len(requests) == 1 else {"fields": {"tax_id": "12345678"}})
        with patch("app.core.pipeline._invoke_text", side_effect=invoke), patch("app.core.barcode.decode") as decoder:
            v = pipeline.extract_evidence("fake.png", "receipt", object(), [], "原始OCR 12345678", mode="ocr_text")
        self.assertEqual(len(requests), 2)
        self.assertEqual(v.seller.name, "原廠商")
        self.assertEqual(v.seller.tax_id, "12345678")
        self.assertIn('"tax_id":null', requests[1][0]["content"])
        self.assertNotIn('"vendor":null', requests[1][0]["content"])
        decoder.assert_not_called()
        self.assertFalse(any(p.get("type") == "image_url" for req in requests for p in req))

    def test_original_journal_single_side_missing_values_and_printed_totals_are_kept(self):
        data = prompts.extraction_example("journal_voucher")
        data["fields"].update(voucher_kind="現支傳票", debit_total=110, credit_total=None)
        row = {s["key"]: None for s in doc_types.ENTRY_FIELDS}
        row.update(account_code="6200", account_name="文具用品", description="文具", debit="100", credit=None)
        data["entries"] = [row]
        v = pipeline._build_voucher(data, "journal_voucher")
        self.assertEqual(len(v.entries), 1)
        self.assertEqual(v.entries[0].credit_account, "")
        self.assertEqual(v.fields["debit_total"], 110)
        self.assertEqual(v.debit_total, 100)
        self.assertFalse(v.balance_check)  # a complete original form is not a balanced journal
        self.assertTrue(v.classification_meta["original_checks"]["complete"])
        self.assertTrue(v.classification_meta["original_checks"]["single_sided"])
        self.assertFalse(v.classification_meta["original_checks"]["printed_totals_match"])
        self.assertTrue(any("印刷合計" in w for w in v.validation_warnings))
        data["entries"][0]["debit"] = None
        unknown = pipeline._build_voucher(data, "journal_voucher")
        self.assertEqual(len(unknown.entries), 1)
        self.assertIsNone(unknown.entries[0].amount)
        self.assertFalse(unknown.balance_check)
        preserved = pipeline.canonicalize_voucher(v.model_dump())
        self.assertEqual(preserved["entries"][0]["amount"], 100)
        self.assertEqual(preserved["debit_total"], 100)
        with patch("app.core.model_factory.get_chat_model") as factory:
            classified = classify.classify(v)
        factory.assert_not_called()
        self.assertEqual(classified.entries[0].amount, 100)
        self.assertEqual(classified.classification_meta["reason"], "original_entries")
        self.assertTrue(classified.classification_meta["original_checks"]["single_sided"])
        self.assertFalse(classified.balance_check)

    def test_box_coordinate_units_are_explicit_and_invalid_boxes_do_not_drop_content(self):
        from PIL import Image
        image = Path(self.temp.name, "synthetic.png")
        Image.new("RGB", (1200, 1200)).save(image)
        raw = prompts.extraction_example("invoice")
        raw["boxes"] = {"total_amount": [100,100,900,900]}
        result = pipeline._validate_image_extraction(raw, "invoice", "image", str(image))
        self.assertEqual(result["boxes"]["total_amount"], [100,100,900,900])
        self.assertEqual(pipeline._normalize_boxes({"total_amount":[120,120,1080,1080]}, str(image), unit="pixels"), {"total_amount":[100,100,900,900]})
        for box in ([.1,.1,.9,.9], [120,120,1080,1080], [True,100,900,900], [100,100,900,900,1], [0,0,float("inf"),100]):
            raw["boxes"] = {"total_amount":box}
            result = pipeline._validate_image_extraction(raw, "invoice", "image", str(image))
            self.assertEqual(result["fields"], raw["fields"])
            self.assertEqual(result["boxes"], {})
            self.assertEqual(len(result["box_issues"]), 1)

    def test_import_routing_does_not_use_notes_or_export_substrings(self):
        self.assertTrue(importdecl.is_import({"doc_type_key": "customs_tax"}))
        self.assertFalse(importdecl.is_import({"doc_type_key": "export_declaration", "note": "進口／出口報單"}))
        self.assertFalse(importdecl.is_import({"doc_type": "出口報單", "note": "進口"}))
        self.assertFalse(importdecl.is_import({"doc_type_key": "declaration", "note": "進口"}))
        self.assertTrue(importdecl.is_import({"doc_type_key": "declaration", "fields": {"direction": "進口"}}))

    def test_policy_company_mapping_and_invalid_settings_fall_back_with_warning(self):
        Path(self.temp.name, "accounting_policies.json").write_text(json.dumps({"_default": {"version": "client-v1", "roles": {"fees": {"code": "AF"}}}}), encoding="utf-8")
        policy = accounting_policy.load_policy("A", {"doc_type_key": "import_declaration"}, [{"code": "AF", "name": "手續費"}])
        self.assertEqual(policy["account_roles"]["fees"], {"code": "AF", "name": "手續費"})
        self.assertEqual(policy["account_roles"]["input_tax"]["code"], "")
        Path(self.temp.name, "accounting_policies.json").write_text('{"A":{"roles":[]}}', encoding="utf-8")
        policy = accounting_policy.load_policy("A", {"doc_type_key": "import_declaration"}, CHART)
        self.assertEqual(policy["policy_version"], "default-v1")
        self.assertTrue(policy["warnings"])

    def _classify(self, voucher, response, company=""):
        chat = SimpleNamespace(invoke=Mock(return_value=SimpleNamespace(content=json.dumps(response))))
        with patch("app.services.accounts.load_accounts", return_value=CHART), patch("app.services.accounts.companies_with_chart", return_value=[company]), \
             patch("app.core.model_factory.get_chat_model", return_value=chat), patch("app.core.settings_store.get_settings", return_value={"classify": "fake/model"}):
            result = classify.classify(voucher, company=company)
        return result, chat

    def test_import_full_context_and_program_derived_balanced_amount(self):
        raw = prompts.extraction_example("import_declaration")
        raw["fields"].update(duty=200028, business_tax=60008, total_tax=260036, invoice_amount=999,
                             payment_status="已付款", payment_method="銀行轉帳", currency="USD", declaration_no="SYNTHETIC")
        raw["items"] = [{"description": "貨品", "quantity": 2, "unit_price": 10, "amount": 20, "hs_code": "1234", "origin_country": "TW"}]
        v = pipeline._build_voucher(raw, "import_declaration")
        response = {"entries": [proposal(200028, "fields.duty", debit="1210"), proposal(60008, "fields.business_tax", debit="1290"),
                                proposal(260036, "derived.import_payment", credit="1110")]}
        result, chat = self._classify(v, response)
        self.assertTrue(result.balance_check)
        self.assertEqual(result.credit_total, 260036)
        self.assertEqual(result.classification_meta["status"], "proposed")
        messages = chat.invoke.call_args.args[0]
        self.assertEqual(messages[1]["role"], "user")
        request = "\n".join(part.get("text", "") for part in messages[1]["content"] if part.get("type") == "text")
        self.assertIn('"hs_code":"1234"', request)
        self.assertIn('"invoice_amount":999', request)
        ctx = classify._accounting_context(v, accounting_policy.load_policy("", v.model_dump(), CHART))
        self.assertNotIn("fields.invoice_amount", ctx["amount_sources"])
        self.assertNotIn("total_amount", ctx["amount_sources"])
        self.assertNotIn("items[0].amount", ctx["amount_sources"])
        self.assertEqual(ctx["derived_amounts"]["derived.import_payment"]["source_paths"], ["fields.duty", "fields.business_tax"])

    def test_insufficient_evidence_candidates_never_create_money_or_unknown_codes(self):
        v = Voucher(doc_type_key="receipt", fields={"total_amount": 100}, total_amount=100)
        response = {"entries": [proposal(100, "fields.total_amount", debit="BAD"), proposal(400, "not-a-path", credit="1110")]}
        result, _ = self._classify(v, response)
        self.assertEqual(result.classification_meta["status"], "candidate")
        self.assertEqual(result.entries[0].debit_account, "")
        self.assertIsNone(result.entries[1].amount)
        self.assertTrue(result.classification_meta["assumptions"])
        self.assertFalse(result.balance_check)

    def test_supporting_page_skips_model_and_income_credit_is_not_replaced_by_bank(self):
        with patch("app.core.model_factory.get_chat_model") as factory:
            v = classify.classify(Voucher(doc_type_key="bill_detail"))
        factory.assert_not_called(); self.assertEqual(v.classification_meta["status"], "skipped")
        v = Voucher(doc_type_key="invoice", fields={"total_amount": 100, "payment_status": "已付款", "payment_method": "銀行轉帳"}, total_amount=100)
        result, _ = self._classify(v, {"entries": [proposal(100, "fields.total_amount", debit="1110"), proposal(100, "fields.total_amount", credit="4100")]})
        self.assertEqual(result.entries[1].credit_account, "4100")

    def test_company_examples_exclude_other_company_and_policy_or_amount_mismatch(self):
        v = Voucher(doc_type_key="receipt", fields={"total_amount": 100, "payment_status": "未付款", "payment_method": None}, total_amount=100)
        policy = accounting_policy.load_policy("A", v.model_dump(), CHART)
        good = v.model_dump()
        good.update(entries=[proposal(100, "fields.total_amount", debit="6200"), proposal(100, "fields.total_amount", credit="2200")],
                    classification_meta={"status": "proposed", "policy_sha256": policy["policy_sha256"]})
        good["entries"][0]["debit_account_name"] = "文具用品"
        good["entries"][1]["credit_account_name"] = "應付費用"
        bad = deepcopy(good); bad["entries"][0]["amount"] = bad["entries"][1]["amount"] = 999
        records = [SimpleNamespace(company=company, current_json=json.dumps(item)) for company, item in (("B", good), ("A", bad), ("A", good))]
        with patch("app.services.review.list_vouchers", return_value=records) as search, patch("app.services.classify._embed", return_value=[1.0]):
            examples = classify._retrieve_examples(v, company="A", chart=CHART, policy=policy)
        self.assertEqual(len(examples), 1)
        self.assertEqual(search.call_args.kwargs["company"], "A")
        Path(self.temp.name, "entry_examples.json").write_text(json.dumps([{"分錄": good["entries"]}]), encoding="utf-8")
        self.assertEqual(classify._seed_examples("A", v, CHART, policy), [])

    def test_manual_save_and_model_roundtrip_keep_extra_fields_items_and_metadata(self):
        original = Voucher(doc_type_key="import_declaration", fields={"direction": "進口", "total_tax": 100},
                           items=[{"description": "貨品", "amount": 0, "hs_code": "1234", "origin_country": "TW", "legacy_tag": "keep"}],
                           classification_meta={"status": "proposed", "policy_version": "v1"}, unknown_saved_attribute="retain").model_dump()
        rec = SimpleNamespace(current_json=json.dumps(original), period="202606", updated_at=None)
        session = SimpleNamespace(get=lambda *_: rec, add=lambda *_: None, commit=lambda: None, refresh=lambda *_: None)
        updated = deepcopy(original); updated["fields"]["total_tax"] = 200
        with patch("app.services.review.get_session", return_value=nullcontext(session)):
            review.update_current(1, updated)
        stored = Voucher.model_validate(json.loads(rec.current_json)).model_dump()
        self.assertEqual(stored["unknown_saved_attribute"], "retain")
        self.assertEqual(stored["items"][0]["legacy_tag"], "keep")
        self.assertEqual(stored["items"][0]["hs_code"], "1234")
        self.assertEqual(stored["total_amount"], 200)
        self.assertEqual(stored["field_evidence"]["fields.total_tax"][-1]["source"], "manual")
        self.assertTrue(stored["classification_meta"]["stale"])
        # A repeated save of a stale browser snapshot cannot erase server audit/status.
        with patch("app.services.review.get_session", return_value=nullcontext(session)):
            review.update_current(1, {**original, "fields": {**original["fields"], "total_tax": 200}})
        repeated = json.loads(rec.current_json)
        self.assertEqual(repeated["field_evidence"]["fields.total_tax"][-1]["value"], 200)
        self.assertTrue(repeated["classification_meta"]["stale"])
        self.assertEqual(repeated["classification_meta"]["status"], "candidate")

    def test_edit_refreshes_current_checks_but_keeps_acquisition_diagnostics_and_raw_values(self):
        raw = prompts.extraction_example("receipt")
        raw["fields"].update(total_amount=200, subtotal=100, tax_amount=5, date="1150230")
        raw["validation_warnings"] = ["來源衝突，請核對"]
        original = pipeline._build_voucher(raw, "receipt").model_dump()
        self.assertTrue(any(w.startswith("金額兜不攏") for w in original["validation_warnings"]))
        self.assertTrue(any(w.startswith("日期格式無法解析") for w in original["validation_warnings"]))
        rec = SimpleNamespace(current_json=json.dumps(original), period="", updated_at=None)
        session = SimpleNamespace(get=lambda *_: rec, add=lambda *_: None, commit=lambda: None, refresh=lambda *_: None)
        with patch("app.services.review.get_session", return_value=nullcontext(session)):
            review.update_current(1, {"fields": {"total_amount": 105, "date": "1150604"}})
        stored = json.loads(rec.current_json)
        self.assertEqual(stored["date"], "2026-06-04")
        self.assertEqual(stored["validation_warnings"], ["來源衝突，請核對"])
        self.assertEqual(stored["field_evidence"]["fields.date"][-1]["value"], "1150604")
        self.assertEqual(stored["field_evidence"]["fields.date"][-1]["source"], "manual")
        count = len(stored["field_evidence"]["fields.date"])
        with patch("app.services.review.get_session", return_value=nullcontext(session)):
            review.update_current(1, {"fields": {"date": "1150604"}})
        repeated = json.loads(rec.current_json)
        self.assertEqual(len(repeated["field_evidence"]["fields.date"]), count)
        # Unresolved unreadable amounts retain their diagnostic through subsequent saves.
        raw["fields"]["total_amount"] = "看不清"
        incomplete = pipeline._build_voucher(raw, "receipt").model_dump()
        self.assertTrue(any("無法解析" in w for w in pipeline.canonicalize_voucher(incomplete)["validation_warnings"]))

    def test_csv_appends_new_attributes_without_removing_old_columns(self):
        v = Voucher(items=[{"description": "貨品", "amount": 0, "hs_code": "1234", "origin_country": "TW"}],
                    entries=[proposal(0, "fields.total_amount", debit="1210")], field_evidence={"fields.total_amount": [{"source": "manual", "value": 0}]},
                    validation_warnings=["核對"], classification_meta={"status": "candidate"}).model_dump()
        result = dict(id="synthetic", relative_path="synthetic.png", page_no=1, combo_name="test", mode="ocr_llm", actual_path="ocr_llm",
                      repeat_no=1, attempt=1, status="succeeded", timings={}, error="", voucher=v)
        files = validation._csv_files({"results": [result]})
        self.assertEqual(set(files), {"vouchers.csv", "fields.csv", "items.csv", "entries.csv", "pages.csv"})
        read = lambda name: list(csv.DictReader(io.StringIO(files[name].lstrip("\ufeff"))))
        self.assertEqual(read("items.csv")[0]["hs_code"], "1234")
        self.assertEqual(json.loads(read("entries.csv")[0]["evidence_paths_json"]), ["fields.total_amount"])
        self.assertEqual(json.loads(read("vouchers.csv")[0]["classification_meta_json"])["status"], "candidate")

    def test_journal_preserves_missing_amount_evidence_and_ignores_supporting_pages(self):
        v = Voucher(doc_type_key="receipt", date="2026-06-04", entries=[proposal(None, "fields.total_amount", debit="6200")],
                    field_evidence={"fields.total_amount": [{"source": "manual", "value": None}]},
                    validation_warnings=["金額缺漏"], classification_meta={"status": "candidate"})
        records = [SimpleNamespace(current_json=v.model_dump_json()),
                   SimpleNamespace(current_json=Voucher(doc_type_key="bill_detail", entries=[proposal(999, "fields.total_amount", debit="6200")]).model_dump_json())]
        with patch("app.services.review.list_vouchers", return_value=records):
            result = journal.build_journal("2026-06")
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertIsNone(row["debit_amount"])
        self.assertEqual(row["evidence_paths"], ["fields.total_amount"])
        self.assertEqual(row["classification_meta"]["status"], "candidate")
        self.assertEqual(row["validation_warnings"], ["金額缺漏"])
        self.assertFalse(result["balance_check"])

    def test_source_hash_v2_is_stable_sensitive_to_runtime_and_rejects_legacy_resume(self):
        versions = model_invocation.source_versions()
        self.assertEqual(versions, model_invocation.source_versions())
        self.assertTrue(model_invocation.same_execution_version(versions))
        self.assertFalse(model_invocation.same_execution_version({"code_version_sha256": versions["code_version_sha256"]}))
        Path(self.temp.name, "accounting_policies.json").write_text('{"_default":{"version":"new"}}', encoding="utf-8")
        self.assertFalse(model_invocation.same_execution_version(versions))
        self.assertIn("core/normalization.py", versions["source_sha256"])
        self.assertIn("services/classify.py", versions["source_sha256"])

    def test_parser_does_not_invent_truncated_strings_or_arithmetic_results(self):
        for response in ('{"fields":{"date":"115/06', '{"total": 1+2=3}'):
            with self.assertRaises(ValueError):
                model_invocation._parse_json(response)


if __name__ == "__main__":
    unittest.main()
