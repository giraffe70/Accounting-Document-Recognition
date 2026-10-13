"""Exercise the production review-form functions in a dependency-free Node VM."""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from app.core import doc_types


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "js" / "app.js"
NODE = Path(r"C:\Program Files\nodejs\node.exe")

NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = input.source;
const start = source.indexOf('  function itemSpecs()');
const end = source.indexOf('  $("btnClassify").addEventListener("click"', start);
const escStart = source.indexOf('  const esc = ');
const escEnd = source.indexOf('\n', escStart);
if (start < 0 || end < 0 || escStart < 0 || escEnd <= escStart) {
  throw new Error('could not locate production form functions in app.js');
}
const formCode = source.slice(start, end);
const escCode = source.slice(escStart, escEnd);
const fieldNodes = Object.fromEntries(Object.entries(input.fieldInputs || {}).map(([key, value]) =>
  ['dyn-' + key, { value }]));
const rows = (input.rows || []).map((row) => ({
  dataset: { original: row.original || '{}' },
  querySelectorAll: (selector) => selector === '[data-item-key]' ? row.inputs : [],
  querySelector: (selector) => selector === '.remember-mapping' ? { checked: row.remember !== false } : null,
}));
const entryRows = (input.entryRows || []).map((row) => {
  const fields = Object.fromEntries(Object.entries(row.fields || {}).map(([key, value]) => [key, { value }]));
  return {
    dataset: { entryId: row.entryId, original: row.original || '{}' },
    querySelector: (selector) => {
      const match = selector.match(/data-entry-key="([^"]+)"/);
      return match ? (fields[match[1]] || null) : null;
    },
  };
});
const nodes = {
  revTypeSel: { value: input.typeKey },
  revCompanySel: { value: String(input.companyId ?? 3) },
  itemsBody: { querySelectorAll: (selector) => selector === 'tr' ? rows : [] },
  entriesBody: { querySelectorAll: (selector) => selector === 'tr[data-entry-id]' ? entryRows : [] },
  'f-note': { value: input.note || '' },
  addItem: { addEventListener: () => {} },
  ...fieldNodes,
};
const context = {
  console,
  input,
  DOC_TYPES: input.docTypes,
  currentVoucher: input.voucher,
  activeFields: input.activeFields,
  selectedCompanyId: () => input.companyId ?? 3,
  itemContext: (company, name) => JSON.stringify([company, String(name || '').trim().replace(/\s+/g, ' ')]),
  newItemId: () => 'generated-item-id',
  $: (id) => nodes[id] || null,
};
vm.createContext(context);
vm.runInContext(`${escCode}\n${formCode}\nglobalThis.result = {
  itemSpecs: itemSpecs(),
  itemHtml: itemRow(input.rowItem || {}),
  collectedEntries: collectEntries(),
  collected: collectForm(),
};`, context);
process.stdout.write(JSON.stringify(context.result));
"""


def frontend_doc_types() -> dict[str, dict[str, object]]:
    """Pass the real Python document contracts as the frontend API response."""
    return {
        key: {
            "label": spec["label"],
            "fields": spec["fields"],
            "item_fields": spec["item_fields"],
        }
        for key, spec in doc_types.DOC_TYPES.items()
    }


class FrontendFormRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_JS.read_text(encoding="utf-8")
        if not NODE.is_file():
            raise unittest.SkipTest(f"Node.js not found at {NODE}")

    def run_form(self, **fixture):
        payload = {"source": self.source, "docTypes": frontend_doc_types(), **fixture}
        completed = subprocess.run(
            [str(NODE), "-e", NODE_HARNESS],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout.decode("utf-8"))

    def test_import_item_fields_and_collect_preserve_unrendered_data(self):
        original_item = {
            "description": "",
            "quantity": None,
            "unit_price": None,
            "amount": 0,
            "hs_code": "0901.21",
            "origin_country": "TW",
            "legacyattr": "keep-me",
        }
        item_inputs = [
            {"dataset": {"itemKey": field["key"], "kind": field["kind"]},
             "type": "text" if field["key"] in {"hs_code", "origin_country"} else "number",
             "value": str(original_item.get(field["key"]) if original_item.get(field["key"]) is not None else "")}
            for field in doc_types.DOC_TYPES["import_declaration"]["item_fields"]
        ]
        # The item form keeps numeric zero as a populated row; the server still owns
        # mapping unrendered document fields such as direction.
        result = self.run_form(
            typeKey="import_declaration",
            activeFields=[{"key": "declaration_no", "kind": "text", "el": "dyn-declaration_no"},
                          {"key": "unrecognized_empty", "kind": "text", "el": "dyn-unrecognized_empty"}],
            fieldInputs={"declaration_no": "IM-001", "unrecognized_empty": ""},
            voucher={"doc_type_key": "import_declaration",
                     "fields": {"direction": "import", "old_field": "keep"},
                     "items": [original_item], "note": "existing"},
            rows=[{"original": json.dumps(original_item, ensure_ascii=False), "inputs": item_inputs}],
        )

        item_keys = {field["key"] for field in result["itemSpecs"]}
        self.assertTrue({"hs_code", "origin_country"} <= item_keys)
        collected = result["collected"]
        self.assertEqual(collected["fields"]["direction"], "import")
        self.assertEqual(collected["fields"]["old_field"], "keep")
        self.assertIsNone(collected["fields"]["unrecognized_empty"])
        self.assertEqual(len(collected["items"]), 1)
        item = collected["items"][0]
        self.assertEqual(item["amount"], 0)
        self.assertEqual(item["hs_code"], "0901.21")
        self.assertEqual(item["origin_country"], "TW")
        self.assertEqual(item["legacyattr"], "keep-me")

    def test_nonnumeric_number_text_is_kept_for_backend_and_html_is_escaped(self):
        hostile = 'x"><img src=x onerror="alert(1)">&'
        original_item = {"description": hostile, "quantity": "about 3", "amount": None,
                         "legacyattr": hostile}
        inputs = [
            {"dataset": {"itemKey": "description", "kind": "text"}, "type": "text", "value": hostile},
            # A number field with nonnumeric source text renders as a text input.
            {"dataset": {"itemKey": "quantity", "kind": "number"}, "type": "text", "value": "about 3"},
            {"dataset": {"itemKey": "amount", "kind": "number"}, "type": "number", "value": ""},
        ]
        result = self.run_form(
            typeKey="import_declaration",
            activeFields=[],
            voucher={"doc_type_key": "import_declaration", "fields": {}, "items": []},
            rows=[{"original": json.dumps(original_item, ensure_ascii=False), "inputs": inputs}],
            rowItem=original_item,
        )

        html = result["itemHtml"]
        self.assertIn('data-kind="number" type="text" value="about 3"', html)
        self.assertIn("&lt;img", html)
        self.assertIn("onerror=&quot;alert(1)&quot;", html)
        self.assertNotIn("<img", html)
        self.assertNotIn('onerror="alert(1)"', html)

        item = result["collected"]["items"][0]
        self.assertEqual(item["quantity"], "about 3")
        self.assertEqual(item["legacyattr"], hostile)

    def test_account_fields_lead_each_row_and_manual_codes_keep_leading_zeroes(self):
        inputs = [
            {"dataset": {"itemKey": "account_code", "kind": "text"}, "type": "text", "value": "00120"},
            {"dataset": {"itemKey": "account_name", "kind": "text"}, "type": "text", "value": "手填科目"},
            {"dataset": {"itemKey": "description", "kind": "text"}, "type": "text", "value": "零件"},
        ]
        result = self.run_form(
            typeKey="other", activeFields=[],
            voucher={"doc_type_key": "other", "fields": {}, "items": []},
            rows=[{"original": "{}", "inputs": inputs, "remember": False}],
            rowItem={"account_code": "00120", "account_name": "手填科目", "description": "零件",
                     "remember_mapping": False},
        )

        specs = result["itemSpecs"]
        self.assertEqual([field["key"] for field in specs[:2]], ["account_code", "account_name"])
        self.assertLess(result["itemHtml"].index('data-item-key="account_code"'),
                        result["itemHtml"].index('data-item-key="description"'))
        self.assertIn('data-item-key="account_code" data-kind="text" type="text" list="itemAccountCodes" value="00120"',
                      result["itemHtml"])
        self.assertIn('class="remember-mapping" >記住對應', result["itemHtml"])
        item = result["collected"]["items"][0]
        self.assertEqual(item["account_code"], "00120")
        self.assertEqual(item["account_name"], "手填科目")
        self.assertFalse(item["remember_mapping"])

    def test_empty_metadata_only_item_is_filtered(self):
        result = self.run_form(
            typeKey="other", activeFields=[],
            voucher={"doc_type_key": "other", "fields": {}, "items": []},
            rows=[{"original": json.dumps({"item_id": "empty-id", "mapping_id": None,
                                            "mapping_version": None, "account_source": "",
                                            "account_needs_review": False, "account_updated_by": None,
                                            "account_updated_by_name": "", "account_updated_at": None},
                   ensure_ascii=False),
                   "inputs": [], "remember": True}],
        )
        self.assertEqual(result["collected"]["items"], [])

    def test_entry_editor_preserves_fixed_id_and_manual_account_code(self):
        entry = {
            "entry_id": "fixed-entry-uuid",
            "entry_origin": "observed",
            "source_entry_index": 2,
            "debit_account": "00120",
            "debit_account_name": "原始科目",
            "credit_account": "",
            "credit_account_name": "",
            "description": "運費",
            "legacy_field": "keep-entry-metadata",
        }
        item = {"item_id": "fixed-item-uuid", "description": "運費", "amount": 660}
        result = self.run_form(
            typeKey="other", activeFields=[],
            voucher={"doc_type_key": "other", "fields": {}, "items": [item], "entries": [entry]},
            # A non-item type must still retain and show persisted rows during edit.
            rows=[{"original": json.dumps(item, ensure_ascii=False), "inputs": []}],
            entryRows=[{"entryId": entry["entry_id"], "original": json.dumps(entry, ensure_ascii=False),
                        "fields": {"direction": "debit", "account_code": "00120", "account_name": "手填科目",
                                   "amount": "660", "description": "運費修正", "dept": "", "offset_code": ""}}],
        )
        collected = result["collected"]
        self.assertEqual(collected["items"][0]["item_id"], "fixed-item-uuid")
        self.assertEqual(len(collected["entries"]), 1)
        row = collected["entries"][0]
        self.assertEqual(row["entry_id"], "fixed-entry-uuid")
        self.assertEqual(row["entry_origin"], "observed")
        self.assertEqual(row["source_entry_index"], 2)
        self.assertEqual(row["debit_account"], "00120")
        self.assertEqual(row["debit_account_name"], "手填科目")
        self.assertEqual(row["credit_account"], "")
        self.assertEqual(row["description"], "運費修正")
        self.assertEqual(row["legacy_field"], "keep-entry-metadata")


class ValidationTimingFrontendContractTests(unittest.TestCase):
    def test_timing_details_are_versioned_and_rendered_for_page_and_document(self):
        source = (ROOT / "app" / "static" / "js" / "validation.js").read_text(encoding="utf-8")

        self.assertIn("function timingBlock(timings, version, heading)", source)
        self.assertIn("(version === 2 || legacyTotal)", source)
        self.assertIn('key === "queue_seconds" || key === "complete_seconds"', source)
        self.assertIn("timings?.incomplete === true", source)
        self.assertIn("處理曾中斷，細項僅包含已完成的呼叫，耗時可能未完整記錄", source)
        self.assertIn('value == null || value === ""', source)
        self.assertIn('"未記錄"', source)
        self.assertIn("esc(label)", source)
        self.assertIn("esc(formatted)", source)
        self.assertIn('"選取單據耗時"', source)
        self.assertIn('"頁面耗時"', source)
        for field in (
            "orientation_ocr_seconds", "full_ocr_seconds", "supplement_ocr_seconds",
            "model_seconds", "ocr_cache_seconds", "ocr_init_seconds",
            "ocr_cache_hits", "ocr_cache_misses", "queue_seconds", "complete_seconds",
        ):
            with self.subTest(field=field):
                self.assertIn(f'"{field}"', source)

    def test_timeout_scope_and_cache_acceptance_are_documented(self):
        html = (ROOT / "app" / "static" / "validation.html").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "dev-notes" / "agent-workflow.md").read_text(encoding="utf-8")

        self.assertIn("每子行程上限（秒）", html)
        self.assertIn("版面與各張單據的子行程", html)
        self.assertIn("不跨模型組合、重複次數或執行嘗試共用", guide)
        self.assertIn("source_token", guide)
        self.assertIn("離線契約測試只驗證欄位與呈現，不代表實測節省時間", guide)
        self.assertIn("完整重新啟動服務並建立新批次驗收", guide)


if __name__ == "__main__":
    unittest.main()
