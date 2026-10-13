"""Contracts for the split recognition and journal-entry review controls."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EntryFrontendWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")

    def test_saved_items_keep_the_editor_visible_even_for_other_types(self):
        self.assertRegex(self.js, re.compile(
            r"const hasItems\s*=\s*!!\(DOC_TYPES\[typeKey\].*?has_items\).*?\|\|\s*!!v\.items\?\.length",
            re.S,
        ))
        self.assertIn('$("itemsSection").classList.toggle("hidden", !hasItems)', self.js)

    def test_recognition_and_posting_confirmation_have_separate_controls(self):
        self.assertIn('id="btnConfirmEntries"', self.html)
        self.assertIn('id="btnConfirm"', self.html)
        self.assertIn("確認分錄可供入帳", self.html)
        self.assertIn("確認辨識內容", self.html)
        self.assertIn('id="entriesStatus"', self.html)

    def test_optional_classification_updates_proposal_state_without_replacing_draft(self):
        start = self.js.index('$("btnClassify").addEventListener')
        end = self.js.index('$("addEntry").addEventListener', start)
        handler = self.js[start:end]
        self.assertIn('/classify', handler)
        self.assertIn('applyRecordStatus(record)', handler)
        self.assertNotRegex(handler, r"currentVoucher\.entries\s*=")
        self.assertIn('id="entryProposal"', self.html)
        self.assertIn('id="adoptEntryProposal"', self.html)
        self.assertIn('id="discardEntryProposal"', self.html)
        self.assertIn('`/api/vouchers/${currentId}/entries/${action}`', self.js)


if __name__ == "__main__":
    unittest.main()
