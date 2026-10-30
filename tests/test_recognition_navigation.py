"""Offline runtime checks for navigation after single and batch recognition."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"


class RecognitionNavigationTests(unittest.TestCase):
    def run_flow(self, **scenario):
        result = subprocess.run([NODE, str(ROOT / "tests/fixtures/recognition_navigation_synthetic.js"),
                                 str(ROOT / "app/static/js/app.js")], input=json.dumps(scenario),
                                capture_output=True, text=True, encoding="utf-8", check=True)
        return json.loads(result.stdout)

    @staticmethod
    def opened(result):
        return [event for event in result["events"] if event["type"] == "open"]

    def test_single_success_opens_that_voucher_in_fourth_step(self):
        for initial_dirty in (False, True):
            result = self.run_flow(action="single", initialDirty=initial_dirty)
            self.assertEqual(result["stage"], "pending")
            self.assertEqual(self.opened(result), [{"type": "open", "id": 12, "stage": "pending"}])
            self.assertFalse(result["dirty"])

    def test_single_failure_stays_on_scan_list(self):
        result = self.run_flow(action="single", singleFailure=True)
        self.assertEqual(result["stage"], "queued")
        self.assertEqual(self.opened(result), [])
        self.assertTrue(any(event["type"] == "alert" for event in result["events"]))

    def test_batch_uses_result_order_and_skips_successes_no_longer_pending(self):
        result = self.run_flow(results=[22, 12])
        self.assertEqual(self.opened(result), [{"type": "open", "id": 22, "stage": "pending"}])
        skipped = self.run_flow(results=[22, 12], pendingItems=[{"id": 3, "status": "pending"},
                               {"id": 22, "status": "confirmed"}, {"id": 12, "status": "pending"}])
        self.assertEqual(self.opened(skipped)[0]["id"], 12)
        old_only = self.run_flow(pendingItems=[{"id": 3, "status": "pending"}])
        self.assertEqual(old_only["stage"], "queued")
        self.assertEqual(self.opened(old_only), [])

    def test_partial_batch_opens_success_but_all_failures_stay(self):
        partial = self.run_flow(results=[12], errors=["#22: 合成失敗"])
        self.assertEqual(self.opened(partial)[0]["id"], 12)
        self.assertTrue(any(event["type"] == "alert" and "部分失敗" in event["message"] for event in partial["events"]))
        failed = self.run_flow(results=[], errors=["#12: 合成失敗", "#22: 合成失敗"])
        self.assertEqual(failed["stage"], "queued")
        self.assertEqual(self.opened(failed), [])
        self.assertFalse(failed["disabled"])

    def test_user_actions_during_recognition_or_target_lookup_preserve_current_view(self):
        for action, phase in (("single", "recognize"), ("batch", "poll"), ("single", "pending"), ("batch", "pending")):
            for change in ("stage", "roundtrip", "selection", "edit", "saving", "logout"):
                with self.subTest(action=action, phase=phase, change=change):
                    result = self.run_flow(action=action, mutateAt=phase, change=change)
                    self.assertEqual(self.opened(result), [])
                    self.assertEqual(result["stage"], "import" if change == "stage" else "queued")
                    if change == "selection":
                        self.assertEqual(result["id"], 99)
                    if change == "edit":
                        self.assertTrue(result["dirty"])

    def test_existing_batch_edits_are_preserved_and_fourth_step_rerecognition_stays(self):
        edited = self.run_flow(initialDirty=True)
        self.assertEqual(edited["stage"], "queued")
        self.assertTrue(edited["dirty"])
        self.assertEqual(self.opened(edited), [])
        repeated = self.run_flow(action="single", stage="pending")
        self.assertEqual(self.opened(repeated), [{"type": "open", "id": 12, "stage": "pending"}])
        self.assertFalse(any(request["url"].endswith("?status=pending") for request in repeated["requests"]))

    def test_overlapping_and_repeated_completion_polls_only_navigate_once(self):
        result = self.run_flow(deferPoll=True, overlap=True)
        self.assertEqual(len(self.opened(result)), 1)
        self.assertEqual(sum(request["url"] == "/api/extract-job" for request in result["requests"]), 1)
        self.assertFalse(result["disabled"])
        stale = self.run_flow(change="newBatch", mutateAt="pending")
        self.assertEqual(self.opened(stale), [])
        self.assertEqual(stale["stage"], "queued")

    def test_start_progress_and_list_failures_do_not_claim_recognition_failed(self):
        for flag in ("startFailure", "pollFailure", "wrongJob", "pendingFailure"):
            with self.subTest(flag=flag):
                result = self.run_flow(**{flag: True})
                self.assertEqual(self.opened(result), [])
                self.assertEqual(result["stage"], "queued")
                self.assertFalse(result["disabled"])
        lookup = self.run_flow(action="single", pendingFailure=True)
        self.assertTrue(any(event["type"] == "toast" and "辨識完成" in event["message"] for event in lookup["events"]))
        self.assertFalse(any(event["type"] == "alert" for event in lookup["events"]))

    def test_detail_response_cannot_overwrite_after_navigation_selection_or_edit(self):
        for change in ("stage", "selection", "edit", "roundtrip"):
            with self.subTest(change=change):
                result = self.run_flow(action="openRace", change=change)
                self.assertEqual(result["version"], "original-version")
                self.assertEqual(result["id"], 99 if change == "selection" else 12)


if __name__ == "__main__":
    unittest.main()
