import re
import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

from app.services.recognition_report import render


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.sources.append(dict(attrs)["src"])


class RecognitionReportTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "id": 17,
            "company": "甲 & <乙>",
            "company_id": 3,
            "confirmed_at": datetime(2026, 10, 13, 9, 30),
            "confirmed_by_name": "王&小姐",
            "updated_at": datetime(2026, 10, 13, 8, 0),
            "image_path": "page-1.png",
            "attachments": '["page 2.png", "page&3.png"]',
        }

    def test_images_precede_report_and_urls_are_encoded(self):
        result = render(self.record, {"doc_type_key": "invoice", "items": []})
        parser = _Images()
        parser.feed(result)
        self.assertEqual(len(parser.sources), 3)
        self.assertLess(result.index('class="image-page'), result.index("<article class=\"report\""))
        parsed = [urlparse(src) for src in parser.sources]
        self.assertTrue(all(url.path == "/api/vouchers/17/image" for url in parsed))
        self.assertEqual(parse_qs(parsed[0].query), {"expected_updated_at": ["2026-10-13T08:00:00" ]})
        self.assertEqual(parse_qs(parsed[1].query)["att"], ["0"])
        self.assertEqual(parse_qs(parsed[2].query)["att"], ["1"])
        self.assertIn("@page { size: A4 portrait", result)
        self.assertIn("page-break-after: always", result)
        self.assertIn("display: table-header-group", result)
        self.assertIn("td:nth-child(4), td:nth-child(5), td:nth-child(6) { text-align: right; }", result)
        self.assertIn("tr { break-inside: avoid; page-break-inside: avoid; }", result)

    def test_fields_escaping_missing_values_and_extra_item_fields(self):
        voucher = {
            "doc_type_key": "import_declaration",
            "voucher_id": 'A"&<',
            "fields": {"declaration_no": "D&<123"},
            "items": [{"description": "長品名 & <tag> " * 12, "quantity": None,
                       "unit_price": 10, "amount": None, "hs_code": "8708&<",
                       "origin_country": "日本"}],
        }
        result = render(self.record, voucher)
        self.assertIn("A&quot;&amp;&lt;", result)
        self.assertIn("貨品分類號列：", result)
        self.assertIn("—", result)
        self.assertIn("報單號碼", result)
        self.assertIn("D&amp;&lt;123", result)
        self.assertNotIn("<tag>", result)
        self.assertIn("overflow-wrap: anywhere", result)

    def test_journal_voucher_prints_distinct_source_totals_and_no_technical_json(self):
        result = render(
            self.record,
            {"doc_type_key": "journal_voucher", "items": [], "fields": {
                "debit_total": 1250, "credit_total": 1200,
                "validation_warnings": ["不應列印"],
            }, "note": "此摘要不需列印", "validation_warnings": ["請確認傳票日期"]},
            account_warnings=["請確認科目"],
        )
        self.assertIn("借方合計", result)
        self.assertIn("貸方合計", result)
        self.assertIn("1250", result)
        self.assertIn("1200", result)
        self.assertNotIn("<h2>摘要</h2>", result)
        self.assertNotIn("此摘要不需列印", result)
        self.assertNotIn("注意事項", result)
        self.assertNotIn("請確認傳票日期", result)
        self.assertNotIn("請確認科目", result)
        self.assertNotIn("不應列印", result)
        self.assertNotIn("recognition_diagnostics", result)
        self.assertNotIn("classification_meta", result)

    def test_no_image_placeholder_and_failed_image_blocks_print(self):
        record = {**self.record, "image_path": "", "attachments": "[]"}
        result = render(record, {"doc_type_key": "receipt", "items": []})
        self.assertIn("無單據影像", result)
        self.assertIn("img.addEventListener('error', fail)", result)
        self.assertIn("button.disabled = true", result)
        self.assertIn("phase === 'ready') window.print()", result)
        self.assertIn("await document.fonts.ready", result)
        self.assertIn("await img.decode()", result)
        self.assertIn("影像載入失敗，報告無法列印", result)

    def test_confirmed_user_and_taipei_time(self):
        record = {**self.record, "confirmed_by_name": "", "entries_confirmed_by_name": "分錄確認者",
                  "confirmed_at": datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)}
        result = render(record, {"doc_type_key": "receipt", "items": []})
        self.assertIn("未留存", result)
        self.assertNotIn("分錄確認者", result)
        self.assertIn("2026-10-13 09:00:00+08:00", result)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript runtime checks")
    def test_javascript_print_success_can_reprint_and_failure_blocks_both_paths(self):
        result = render(self.record, {"doc_type_key": "receipt", "items": []})
        script = re.search(r"<script>\s*(.*?)\s*</script>", result, re.DOTALL).group(1)
        harness = r'''const vm = require("vm");
const assert = require("assert");
const fs = require("fs");
const source = fs.readFileSync(0, "utf8");
function element() {
  return { disabled: true, textContent: "", handlers: {},
    addEventListener(name, fn) { this.handlers[name] = fn; },
    click() { if (!this.disabled && this.handlers.click) this.handlers.click(); } };
}
async function tick() { await new Promise(resolve => setImmediate(resolve)); }
async function scenario(imageFails) {
  const button = element(), status = element();
  let prints = 0;
  const img = { complete: false, naturalWidth: 0,
    handlers: {}, addEventListener(name, fn) { this.handlers[name] = fn; },
    async decode() {} };
  let resolveFonts;
  const fontsReady = new Promise(resolve => { resolveFonts = resolve; });
  const windowHandlers = {};
  const context = { document: { getElementById(id) { return id === "print-button" ? button : status; },
      images: [img], fonts: { ready: fontsReady } },
    window: { addEventListener(name, fn) { windowHandlers[name] = fn; }, print() { prints++; } },
    Array, Promise };
  vm.runInNewContext(source, context);
  assert.strictEqual(button.disabled, true);
  if (imageFails) {
    img.complete = true;
    img.handlers.error();
    windowHandlers.load();
    await tick();
    button.click();
    assert.strictEqual(button.disabled, true);
    assert.match(status.textContent, /影像載入失敗/);
    assert.strictEqual(prints, 0);
  } else {
    windowHandlers.load();
    await tick();
    button.click();
    assert.strictEqual(prints, 0);
    assert.strictEqual(button.disabled, true);
    resolveFonts();
    await tick();
    img.complete = true;
    img.naturalWidth = 20;
    img.handlers.load();
    await tick();
    assert.strictEqual(button.disabled, false);
    assert.strictEqual(prints, 1);
    button.click();
    assert.strictEqual(prints, 2);
  }
}
(async () => { await scenario(false); await scenario(true); })().catch(err => { console.error(err); process.exit(1); });
'''
        process = subprocess.run([shutil.which("node"), "-e", harness], input=script,
                                 text=True, capture_output=True, check=False, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    unittest.main()
