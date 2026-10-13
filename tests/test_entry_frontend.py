"""Contracts for confirmation details while keeping unedited accounting data."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "app" / "static" / "css" / "styles.css"


class EntryFrontendWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        cls.css = APP_CSS.read_text(encoding="utf-8")

    def test_saved_items_keep_the_editor_visible_even_for_other_types(self):
        self.assertRegex(self.js, re.compile(
            r"const hasItems\s*=\s*!sourcePage",
            re.S,
        ))
        self.assertIn('$("itemsSection").classList.toggle("hidden", !hasItems)', self.js)

    def test_confirmation_has_full_width_recognition_details_and_six_columns(self):
        split = self.html.index('id="revSplit"')
        details = self.html.index('id="recognitionDetails"')
        items = self.html.index('id="itemsSection"')
        self.assertLess(split, details)
        self.assertLess(details, items)
        self.assertRegex(self.html[details - 20:details + 50], r'<section id="recognitionDetails" class="prep-hide"')
        self.assertRegex(self.css, r"table\.items-edit\s*\{[^}]*width:\s*100%")
        for marker in ('id="recognitionStatus"', 'id="detailWarnings"', 'id="itemsBody"', 'id="addItem"',
                       'id="btnSave"', 'id="btnConfirm"', 'id="btnPrintVoucher"', 'id="btnReturnImport"'):
            self.assertIn(marker, self.html)
        self.assertIn('<th>科目代號</th><th>科目名稱</th><th>品名／摘要</th><th>數量</th><th>單價</th><th>金額</th>', self.html)

    def test_entry_workflow_dom_is_removed_but_unedited_entries_are_preserved_on_save(self):
        removed = (
            'entriesStatus', 'addEntry', 'btnClassify', 'entriesBody', 'balanceInfo',
            'btnConfirmEntries', 'entryProposal', 'adoptEntryProposal', 'discardEntryProposal',
            'entryReplaceDialog', 'entries-edit', '會計分錄', '分錄建議',
        )
        for marker in removed:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.html)
        self.assertIn('id="btnConfirm"', self.html)
        start = self.js.index('  function collectForm()')
        end = self.js.index('  async function persistCurrentEdits(', start)
        self.assertRegex(self.js[start:end], r"entries\s*:\s*currentVoucher\.entries\s*\|\|\s*\[\]")


if __name__ == "__main__":
    unittest.main()
