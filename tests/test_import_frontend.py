"""Synthetic regression tests for the single-file, sequential import UI."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"


class ImportFrontendTests(unittest.TestCase):
    def run_synthetic(self):
        result = subprocess.run(
            [NODE, str(ROOT / "tests/fixtures/import_jobs_synthetic.js"), str(ROOT / "app/static/js/app.js")],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        return json.loads(result.stdout)

    def test_homepage_has_single_file_picker_and_no_import_job_cards(self):
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")

        file_tag = html[html.index('id="fileInput"') - 30:html.index(">", html.index('id="fileInput"')) + 1]
        self.assertNotIn("multiple", file_tag.lower())
        company_select = html[html.index('<select id="uploadCompany"'):html.index("</select>", html.index('<select id="uploadCompany"'))]
        self.assertIn("required", company_select)
        self.assertIn('value="" disabled selected hidden', company_select)
        self.assertIn('id="uploadCompanyStatus"', html)
        self.assertNotIn("未指定", company_select)
        for marker in ('id="importState"', 'id="importHint"', 'id="btnImportReview"', 'id="btnImportRetry"', 'id="btnImportCancel"',
                       'id="appearancePosition"', 'id="appearanceRecovery"', 'id="btnAppearanceRetry"', 'id="btnAppearanceCancel"', 'id="btnExtractAll"'):
            self.assertIn(marker, html)
        for obsolete in ('id="importJobs"', 'id="appearanceBatch"', 'id="batchType"', 'id="dirInput"', 'id="uploadMode"', 'id="btnMerge"'):
            self.assertNotIn(obsolete, html)
        self.assertNotIn("renderImportJobs", app_js)
        self.assertNotIn('href="/api/import-jobs/', html)
        self.assertNotIn('src="/api/import-jobs/', html)

        import_panel = html[html.index('<div id="importView">'):html.index('<!-- ②–④ 佇列視圖 -->')]
        self.assertNotIn('class="progress"', import_panel)
        self.assertNotIn("preview", import_panel.lower())

    def test_upload_rejects_multiple_files_and_locks_after_single_upload(self):
        result = self.run_synthetic()["upload"]

        self.assertTrue(result["rejectedMulti"])
        self.assertIn("一次只能上傳一個檔案", result["multiAlert"])
        self.assertTrue(result["lockedDuringUpload"])
        self.assertEqual(result["requestCountWhileLocked"], 1)
        self.assertEqual(result["requestCount"], 1)
        self.assertEqual(result["formData"][0][0], "files")
        self.assertEqual(result["formData"][0][1]["name"], "a.pdf")
        self.assertEqual(dict(result["formData"])["mode"], "manual_review")
        self.assertEqual(dict(result["formData"])["company"], "甲")
        self.assertTrue(result["blockedAfterAccepted"])
        self.assertTrue(result["disabledAfterAccepted"])
        self.assertTrue(result["lockedDropZone"])
        self.assertTrue(result["companyLockedDuringUpload"])
        self.assertEqual(result["selectedAfterUpload"], "甲")

    def test_import_requires_a_loaded_company_and_manual_selection(self):
        result = self.run_synthetic()["company"]

        self.assertEqual(result["initialState"], "loading")
        self.assertEqual(result["initialRejectedPosts"], 0)
        self.assertEqual(result["initialFocusCount"], 4)
        self.assertEqual(result["failedState"], "error")
        self.assertTrue(result["failedDisabled"])
        self.assertIn("載入失敗", result["failedMessage"])
        self.assertEqual(result["emptyState"], "ready")
        self.assertTrue(result["emptyDisabled"])
        self.assertIn("沒有可選公司", result["emptyMessage"])
        self.assertEqual(result["readyState"], "ready")
        self.assertEqual(result["placeholderValue"], "")
        self.assertTrue(result["placeholderDisabled"])
        self.assertTrue(result["placeholderHidden"])
        self.assertTrue(result["selectorEnabledWithoutSelection"])
        self.assertTrue(result["readyImportDisabledWithoutSelection"])
        self.assertTrue(result["readyBlankDropDisabled"])
        self.assertEqual(result["readyBlankRejectedPosts"], 0)
        self.assertIn("請先選擇歸屬公司", result["unselectedMessage"])
        self.assertEqual(result["invalidRejectedPosts"], 0)
        self.assertTrue(result["invalidCompanyRejected"])
        self.assertEqual(result["taxId"], "統編：12345678")
        self.assertTrue(result["controlsEnabledAfterSelection"])
        self.assertTrue(result["selectedDropZoneEnabled"])
        self.assertEqual(result["selectedCompany"], "甲")
        self.assertEqual(result["reloadedValue"], "")
        self.assertEqual(result["reloadedState"], "ready")

    def test_scan_source_import_requires_manual_appearance_review(self):
        result = self.run_synthetic()["scan"]

        self.assertEqual(result["filenames"], ["scan.pdf"])
        self.assertTrue(result["background"])
        self.assertEqual(result["mode"], "manual_review")
        self.assertEqual(result["company"], "合成乙公司")

    def test_polling_tracks_page_changes_but_respects_dirty_review_and_logout(self):
        result = self.run_synthetic()["poll"]

        self.assertEqual(result["idsPreferJobReviewList"], "[41]")
        self.assertEqual(result["first"], 1)
        self.assertGreaterEqual(result["afterPageTwo"], 1)
        self.assertEqual(result["afterDirtyPage"], result["afterPageTwo"])
        self.assertIn("2／3", result["pagePosition"])
        self.assertTrue(result["timersAtAwaiting"])
        self.assertTrue(result["reviewButtonVisible"])
        self.assertTrue(result["reviewButtonHiddenWithoutIds"])
        self.assertFalse(result["staleTimerAfterLogout"])
        self.assertEqual(result["jobsAfterLogout"], 0)

    def test_restored_interrupted_job_stays_locked_and_offers_retry(self):
        result = self.run_synthetic()["recovery"]

        self.assertEqual(result["restoredId"], "recovered")
        self.assertTrue(result["blocked"])
        self.assertTrue(result["retryVisible"])
        self.assertFalse(result["restorePending"])

    def test_prepared_list_is_single_review_item_and_queued_has_no_checkboxes(self):
        result = self.run_synthetic()["list"]

        self.assertEqual(result["preparedIds"], [2])
        self.assertFalse(result["preparedHasCheckboxes"])
        self.assertEqual(result["queuedIds"], [1, 2, 3])
        self.assertFalse(result["queuedHasCheckboxes"])
        self.assertTrue(result["queuedExtractVisible"])
        self.assertEqual(result["queuedOpenedId"], 3)
        self.assertTrue(result["queuedEmptyVisible"])
        self.assertTrue(result["pendingExtractHidden"])
        self.assertTrue(result["importVisible"])
        self.assertIn("沒有可確認的單據", result["emptyTextWhenNoReview"])


if __name__ == "__main__":
    unittest.main()
