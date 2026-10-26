"""Synthetic contract tests for background import job frontend behavior."""
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

    def test_page_updates_deduplicate_and_preserve_open_dirty_review(self):
        result = self.run_synthetic()
        self.assertEqual(result["loadListAfterPage"], 1)
        self.assertEqual(result["loadListAfterDuplicate"], 1)
        self.assertEqual(result["dirtyPreserved"], True)
        self.assertEqual(result["currentIdPreserved"], 77)
        self.assertEqual(result["seen"], ["id:10:completed:91", "id:11:completed:"])
        self.assertTrue(result["awaitingReviewStoppedPolling"])
        self.assertEqual(result["pollReviewClicks"], [])
        self.assertTrue(result["dirtyClicksBlocked"])
        self.assertEqual(result["reviewButtonIds"], [91, 92, 96])
        self.assertEqual(result["reviewClicks"], [["setStage", "prepared"], ["openReview", 91], ["setStage", "prepared"], ["openReview", 92], ["setStage", "prepared"], ["openReview", 96]])
        self.assertEqual(result["selectedIdsAfterRefresh"], ["2"])

    def test_each_job_polls_independently_and_actions_target_job(self):
        result = self.run_synthetic()
        self.assertEqual(result["jobIds"], ["a", "b", "failed-job", "restored", "awaiting-review"])
        self.assertEqual(result["restoredStatus"], "interrupted")
        self.assertEqual(result["timerJobIds"], ["a", "b"])
        self.assertTrue(all(delay == 1500 for delay in result["timers"]))
        self.assertIn({"url": "/api/import-jobs/a/retry-failed", "method": "POST"}, result["requests"])
        self.assertIn({"url": "/api/import-jobs/a/cancel", "method": "POST"}, result["requests"])

    def test_upload_mode_defaults_to_manual_review_and_is_sent_with_import(self):
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="importJobs"', html)
        self.assertIn('id="dropZone"', html)
        self.assertIn('data-stage="prepared"', html)
        self.assertRegex(html, r'<select id="uploadMode"[^>]*>\s*<option value="manual_review">')
        self.assertIn('<option value="automatic">', html)
        javascript = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
        self.assertIn('fd.append("mode", $("uploadMode").value);', javascript)
        self.assertIn('mode: $("uploadMode").value', javascript)

    def test_auth_errors_stop_polling_and_logout_invalidates_in_flight_response(self):
        result = self.run_synthetic()
        self.assertIn("登入已過期", result["authHtml"])
        self.assertIn("找不到此匯入工作", result["authHtml"])
        self.assertFalse(result["staleTimerAfterLogout"])
        self.assertEqual(result["createdUrls"], 1)
        self.assertEqual(result["revokedUrls"], 1)


if __name__ == "__main__":
    unittest.main()

