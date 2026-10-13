import unittest
from copy import deepcopy

from app.services import recognition_details


class RecognitionDetailsTests(unittest.TestCase):
    def test_ensure_converts_observed_entries_and_omits_generated_rows(self):
        data = {
            "doc_type_key": "journal_voucher",
            "entries": [
                {"entry_id": "source-1", "entry_origin": "observed", "description": "差旅費",
                 "amount": 120, "debit_account": "6100", "debit_account_name": "旅費"},
                {"entry_id": "draft-1", "entry_origin": "generated", "description": "模型草稿",
                 "amount": 999, "debit_account": "6200", "debit_account_name": "交際費"},
            ],
        }

        result = recognition_details.ensure(data, record_id=81)

        self.assertEqual(len(result["items"]), 1)
        row = result["items"][0]
        self.assertEqual(row["description"], "差旅費")
        self.assertEqual(row["amount"], 120)
        self.assertEqual((row["account_code"], row["account_name"]), ("6100", "旅費"))
        self.assertEqual(row["source_direction"], "debit")
        self.assertEqual(row["source_entry_id"], "source-1")
        self.assertEqual(row["quantity"], None)
        self.assertEqual(row["unit_price"], None)
        self.assertEqual(row["account_source"], "observed")
        self.assertEqual(row["remember_mapping"], False)
        self.assertEqual(result["details_version"], recognition_details.VERSION)

    def test_empty_observed_account_keeps_its_explicit_source_direction(self):
        data = {"doc_type_key": "journal_voucher", "entries": [{
            "entry_id": "empty-account", "entry_origin": "observed", "source_direction": "credit",
            "description": "空科目列", "amount": 55,
        }]}

        row = recognition_details.ensure(data)["items"][0]

        self.assertEqual(row["source_direction"], "credit")
        self.assertEqual(row["source_entry_id"], "empty-account")
        self.assertEqual(row["account_code"], "")
        self.assertEqual(row["account_name"], "")
        self.assertEqual(row["account_source"], "")

    def test_version_one_intentionally_empty_items_are_not_repopulated(self):
        data = {"doc_type_key": "journal_voucher", "details_version": 1, "items": [],
                "entries": [{"entry_id": "existing", "debit_account": "6100"}]}

        result = recognition_details.ensure(data)

        self.assertEqual(result["items"], [])
        self.assertEqual(result["details_version"], 1)

    def test_protect_sources_restores_trusted_metadata_and_removes_forged_values(self):
        previous = {"items": [{"item_id": "known", "source_entry_id": "trusted-entry",
                                "source_direction": "credit", "original_account_code": "2100",
                                "original_account_name": "應付帳款"}]}
        submitted = {"items": [
            {"item_id": "known", "description": "修正摘要", "source_entry_id": "forged",
             "source_direction": "debit", "original_account_code": "9999",
             "original_account_name": "偽造科目"},
            {"item_id": "new", "description": "新列", "source_entry_id": "forged-new",
             "source_direction": "credit", "original_account_code": "8888",
             "original_account_name": "另一偽造科目"},
        ]}

        recognition_details.protect_sources(submitted, previous)

        self.assertEqual(submitted["items"][0]["description"], "修正摘要")
        self.assertEqual(submitted["items"][0]["source_entry_id"], "trusted-entry")
        self.assertEqual(submitted["items"][0]["source_direction"], "credit")
        self.assertEqual(submitted["items"][0]["original_account_code"], "2100")
        self.assertEqual(submitted["items"][0]["original_account_name"], "應付帳款")
        self.assertTrue(all(key not in submitted["items"][1] for key in recognition_details.SOURCE_FIELDS))
        self.assertEqual(submitted["details_version"], recognition_details.VERSION)

    def test_content_tracks_visible_account_and_detail_values_but_ignores_mapping_metadata(self):
        original = {"doc_type_key": "invoice", "voucher_id": "AB123", "fields": {"date": "2026-01-02"},
                    "items": [{"account_code": "6100", "account_name": "旅費", "description": "車資",
                               "quantity": 2, "unit_price": 30, "amount": 60,
                               "account_source": "manual", "mapping_id": 7, "mapping_version": 3}]}
        mapping_only = deepcopy(original)
        mapping_only["items"][0].update(account_source="mapped", mapping_id=99, mapping_version=12,
                                          account_needs_review=True)
        self.assertEqual(recognition_details.content(original), recognition_details.content(mapping_only))

        account_changed = deepcopy(original)
        account_changed["items"][0]["account_code"] = "6200"
        detail_changed = deepcopy(original)
        detail_changed["items"][0]["description"] = "停車費"
        self.assertNotEqual(recognition_details.content(original), recognition_details.content(account_changed))
        self.assertNotEqual(recognition_details.content(original), recognition_details.content(detail_changed))

    def test_account_warnings_cover_blank_unknown_and_mismatched_accounts(self):
        data = {"items": [
            {"account_code": "", "account_name": ""},
            {"account_code": "6100", "account_name": "旅費"},
            {"account_code": "9999", "account_name": "未知科目"},
            {"account_code": "2100", "account_name": "現金"},
        ]}
        chart = [{"code": "6100", "name": "旅費"}, {"code": "2100", "name": "銀行存款"}]

        warnings = recognition_details.account_warnings(data, chart)

        self.assertEqual(warnings, [
            "第 1 列：科目待指定",
            "第 3 列：科目 9999 未列於公司科目表",
            "第 4 列：科目代號與名稱不符",
        ])

    def test_report_checks_each_original_side_without_adding_sides_together(self):
        data = {"doc_type_key": "journal_voucher", "fields": {"debit_total": 100, "credit_total": 100},
                "items": [{"account_code": "6200", "account_name": "運費", "source_direction": side, "amount": 100}
                          for side in ("debit", "credit")]}
        chart = [{"code": "6200", "name": "運費"}]
        self.assertEqual(recognition_details.report_warnings(data, chart), [])
        data["items"][0]["amount"] = 120
        self.assertEqual(recognition_details.report_warnings(data, chart), ["明細借方金額與原稿借方合計不符，請對照原圖核對"])


if __name__ == "__main__":
    unittest.main()
