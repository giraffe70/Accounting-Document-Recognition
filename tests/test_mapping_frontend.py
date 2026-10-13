"""Async race checks for the production item-account lookup helper."""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "js" / "app.js"
NODE = Path(r"C:\Program Files\nodejs\node.exe")

NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const start = input.source.indexOf('  async function refreshItemMatch(tr)');
const end = input.source.indexOf('  $("addItem").addEventListener', start);
if (start < 0 || end < 0) throw new Error('could not locate refreshItemMatch');
let companyId = 3;
const lookups = [];
const fields = {
  description: { value: input.name },
  account_code: { value: '' },
  account_name: { value: '' },
};
const tr = {
  dataset: { itemId: 'item-1', context: JSON.stringify([3, 'Old']), source: '', needsReview: '0' },
  isConnected: true,
  querySelector(selector) { return fields[selector.match(/"([^"]+)"/)?.[1]]; },
};
const context = {
  currentId: 12,
  reviewedItems: {},
  selectedCompanyId: () => companyId,
  normalizedItemName: (value) => String(value || '').trim().replace(/\s+/g, ' '),
  itemContext: (company, name) => JSON.stringify([company, String(name || '').trim().replace(/\s+/g, ' ')]),
  json: () => ({ 'Content-Type': 'application/json' }),
  responseError: async () => 'lookup failed',
  setItemAccount(row, match) {
    fields.account_code.value = match?.account_code || '';
    fields.account_name.value = match?.account_name || '';
    row.dataset.source = match ? 'mapping' : '';
    row.dataset.needsReview = '0';
  },
  showItemAccountStatus() {},
  fetch() { return new Promise((resolve) => { lookups.push(resolve); }); },
};
vm.createContext(context);
vm.runInContext(input.source.slice(start, end) + '\nglobalThis.refresh = refreshItemMatch;', context);
(async () => {
  tr.dataset.context = JSON.stringify([3, 'Old']);
  fields.description.value = input.name;
  const pending = context.refresh(tr);
  if (input.scenario === 'stale-context') {
    companyId = 4;
    fields.description.value = 'New';
  } else if (input.scenario === 'generation') {
    fields.description.value = 'Newest';
    const newer = context.refresh(tr);
    lookups[1]({ ok: true, json: async () => ({ items: [{ match: {
      account_code: '0088', account_name: '新回應科目',
    } }] }) });
    await newer;
    lookups[0]({ ok: true, json: async () => ({ items: [{ match: {
      account_code: '0099', account_name: '舊回應科目',
    } }] }) });
    await pending;
    process.stdout.write(JSON.stringify({ accountCode: fields.account_code.value,
      accountName: fields.account_name.value, match: tr._accountMatch || null }));
    return;
  } else {
    // Mirrors the production input handler invalidating an in-flight lookup.
    tr._lookupGeneration += 1;
    fields.account_code.value = '0007';
    fields.account_name.value = '手填科目';
  }
  lookups[0]({ ok: true, json: async () => ({ items: [{ match: {
    account_code: '0099', account_name: '延遲回應科目',
  } }] }) });
  await pending;
  process.stdout.write(JSON.stringify({
    accountCode: fields.account_code.value, accountName: fields.account_name.value,
    match: tr._accountMatch || null,
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""


ITEM_ROW_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const start = input.source.indexOf('  function itemSpecs()');
const end = input.source.indexOf('  function bindItemRows()', start);
if (start < 0 || end < 0) throw new Error('could not locate item row renderer');
const nodes = { revCompanySel: { value: '3' }, revTypeSel: { value: 'other' } };
const context = {
  currentVoucher: { doc_type_key: 'other' },
  DOC_TYPES: { other: { item_fields: [{ key: 'batch_tag', label: '批次備註', kind: 'text' }] } },
  $: (id) => nodes[id] || null,
  esc: (value) => String(value ?? '').replace(/[<>&"]/g, (c) => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c])),
  selectedCompanyId: () => 3,
  normalizedItemName: (value) => String(value || '').trim().replace(/\s+/g, ' '),
  itemContext: (company, name) => JSON.stringify([company, String(name || '').trim().replace(/\s+/g, ' ')]),
};
vm.createContext(context);
vm.runInContext(input.source.slice(start, end) + '\nglobalThis.renderItem = itemRow;', context);
process.stdout.write(JSON.stringify({ html: context.renderItem(input.item) }));
"""


class MappingFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_JS.read_text(encoding="utf-8")
        if not NODE.is_file():
            raise unittest.SkipTest(f"Node.js not found at {NODE}")

    def run_lookup(self, scenario):
        payload = {"source": self.source, "scenario": scenario, "name": "New"}
        completed = subprocess.run(
            [str(NODE), "-e", NODE_HARNESS],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout.decode("utf-8"))

    def render_item(self, item):
        completed = subprocess.run(
            [str(NODE), "-e", ITEM_ROW_HARNESS],
            input=json.dumps({"source": self.source, "item": item}, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout.decode("utf-8"))["html"]

    def test_item_editor_uses_wrapping_description_and_expanded_extras_with_opt_in_mapping(self):
        html = self.render_item({"item_id": "item-1", "description": "長品名\n續行"})

        self.assertIn('<textarea data-item-key="description" data-kind="text" rows="2">', html)
        self.assertIn('<details class="item-extras"><summary>展開</summary>', html)
        self.assertIn("批次備註", html)
        self.assertIn('class="remember-mapping"', html)
        self.assertNotRegex(html, r'class="remember-mapping"[^>]*checked')

        remembered = self.render_item({"item_id": "item-2", "description": "商品", "remember_mapping": True})
        self.assertRegex(remembered, r'class="remember-mapping" checked')

    def test_old_company_and_name_response_does_not_replace_new_context(self):
        result = self.run_lookup("stale-context")
        self.assertEqual(result["accountCode"], "")
        self.assertEqual(result["accountName"], "")
        self.assertIsNone(result["match"])

    def test_delayed_lookup_does_not_replace_manual_account_entry(self):
        result = self.run_lookup("manual-race")
        self.assertEqual(result["accountCode"], "0007")
        self.assertEqual(result["accountName"], "手填科目")
        self.assertIsNone(result["match"])

    def test_slower_old_request_cannot_replace_newer_lookup_generation(self):
        result = self.run_lookup("generation")
        self.assertEqual(result["accountCode"], "0088")
        self.assertEqual(result["accountName"], "新回應科目")
        self.assertEqual(result["match"]["account_code"], "0088")


if __name__ == "__main__":
    unittest.main()
