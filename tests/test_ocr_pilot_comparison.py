"""Synthetic, offline contracts for the OCR pilot comparison CLI."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.compare_ocr_pilot import (
    _entries_comparison,
    _items_comparison,
    compare,
)


class OcrPilotComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gold = self.root / "gold.json"
        self.gold.write_text('[{"frozen":true}]', encoding="utf-8")
        self.manifest = {
            "gold_path": str(self.gold),
            "gold_sha256": hashlib.sha256(self.gold.read_bytes()).hexdigest(),
            "frozen_commit": "commit-synthetic",
            "baseline_run_id": "baseline-run",
            "candidate_run_id": "candidate-run",
            "combo": {"mode": "image", "model": "synthetic"},
            "pages": [self._page(index) for index in range(12)],
        }
        self.manifest_path = self.root / "manifest.json"
        self._write_manifest()

    def _page(self, index):
        return {"id": f"page-{index:02d}", "source": f"set/{index:02d}.png",
                "page": index + 1, "path": str(self.root / f"{index:02d}.png"),
                "sha256": f"image-hash-{index}",
                "gold": {"date": "2025-10-05", "voucher_id": f"V{index:02d}",
                         "total_amount": "100.00", "doc_type_key": "receipt",
                         "seller_tax_id": "SECRET-TAX-ID"}}

    def _write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def _result(self, page, *, amount="100.00", seconds=100.0, run_id):
        return {"page_id": page["id"], "source": page["source"], "page": page["page"],
                "image_sha256": page["sha256"], "wall_seconds": seconds,
                "response": {"ok": True, "data": {
                    "page_status": "succeeded",
                    "layout": {"rotate": 0, "regions": [{"type": "receipt", "box": [0, 0, 1000, 1000],
                                                                  "rotate": 0}]},
                    "image_transforms": {"ocr_size": [1000, 1000], "target_size": [1000, 1000],
                                         "rotation": 0, "coordinate_unit": "pixel",
                                         "target_frame": "ocr", "matrix": [1, 0, 0, 1, 0, 0],
                                         "cache_key": "cache-baseline"},
                    "documents": [{"status": "succeeded", "voucher": {
                        "date": "2025/10/05", "voucher_id": page["gold"]["voucher_id"],
                        "total_amount": amount, "doc_type_key": "receipt",
                        "seller": {"tax_id": "SECRET-TAX-ID"}, "items": [], "entries": []}}],
                    "timings": {"ocr_seconds": 10.0, "ocr_cache_seconds": 1.0,
                                "ocr_cache_hits": 1, "ocr_cache_misses": 0,
                                "ocr_init_seconds": 0.25, "orientation_model_seconds": 0.0,
                                "orientation_model_init_seconds": 0.0,
                                "resolution_assessment_seconds": 0.0},
                    "trace": [{"kind": "snapshot", "versions": {
                        "execution_version_sha256": "execution-" + run_id,
                        "code_version_sha256": "code-" + run_id,
                        "prompt_version_sha256": "prompt-" + run_id,
                        "policy_version_sha256": "policy-" + run_id}}]}},
                "_wrapper_run_id": run_id}

    def _save_results(self, name, run_id, *, seconds=100.0, wrong_page=None, page_status=None):
        rows = []
        for page in self.manifest["pages"]:
            row = self._result(page, seconds=seconds, run_id=run_id,
                               amount="999.00" if page["id"] == wrong_page else "100.00")
            row.pop("_wrapper_run_id")
            if page["id"] == page_status:
                row["response"]["data"]["page_status"] = "partial"
            rows.append(row)
        path = self.root / f"{name}.json"
        path.write_text(json.dumps({"run_id": run_id, "frozen_commit": "commit-synthetic",
                                    "combo": self.manifest["combo"], "results": rows}), encoding="utf-8")
        return path

    def _orientation(self):
        path = self.root / "orientation.json"
        path.write_text(json.dumps([
            {"page_id": page["id"], "input_rotation_cw": rotation,
             "expected_angle": 0, "angle": None, "accepted": False,
             "reference": "legacy_ocr_orientation"}
            for page in self.manifest["pages"]
            for rotation in (0, 90, 180, 270)
        ]), encoding="utf-8")
        return path

    def test_alignment_rejects_duplicate_missing_and_wrong_hash_before_output(self):
        baseline = self._save_results("baseline", "baseline-run")
        candidate = self._save_results("candidate", "candidate-run")
        output = self.root / "new-output"

        value = json.loads(baseline.read_text(encoding="utf-8"))
        value["results"].append(value["results"][0])
        baseline.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate baseline page id"):
            compare(self.manifest_path, baseline, candidate, output, self._orientation())
        self.assertFalse(output.exists())

        baseline = self._save_results("baseline", "baseline-run")
        value = json.loads(baseline.read_text(encoding="utf-8"))
        value["results"].pop()
        baseline.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Missing baseline pages"):
            compare(self.manifest_path, baseline, candidate, output, self._orientation())
        self.assertFalse(output.exists())

        baseline = self._save_results("baseline", "baseline-run")
        value = json.loads(baseline.read_text(encoding="utf-8"))
        value["results"][0]["image_sha256"] = "wrong-hash"
        baseline.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "image_sha256 mismatch"):
            compare(self.manifest_path, baseline, candidate, output, self._orientation())
        self.assertFalse(output.exists())

    def test_gate_fails_speed_and_non_regression_without_emitting_gold_values(self):
        baseline = self._save_results("baseline", "baseline-run", seconds=100.0)
        candidate = self._save_results("candidate", "candidate-run", seconds=80.0,
                                       wrong_page="page-03")
        output = self.root / "comparison"
        summary = compare(self.manifest_path, baseline, candidate, output, self._orientation())

        self.assertFalse(summary["gate_passed"])
        self.assertFalse(summary["gates"]["median_improvement_at_least_25_percent"])
        self.assertFalse(summary["gates"]["no_baseline_correct_to_candidate_incorrect"])
        self.assertEqual(summary["evaluation"]["field_regressions"],
                         [{"page_id": "page-03", "field": "total_amount"}])
        self.assertEqual(summary["wall_seconds"]["median_improvement_percent"], 20.0)
        serialized = (output / "summary.json").read_text(encoding="utf-8") + (output / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("SECRET-TAX-ID", serialized)
        self.assertNotIn("999.00", serialized)

    def test_complete_verified_12_page_run_passes_all_gates(self):
        baseline = self._save_results("baseline", "baseline-run", seconds=100.0)
        candidate = self._save_results("candidate", "candidate-run", seconds=70.0)
        output = self.root / "comparison"
        summary = compare(self.manifest_path, baseline, candidate, output, self._orientation())

        self.assertTrue(summary["gate_passed"])
        self.assertTrue(all(summary["gates"].values()))
        self.assertEqual(summary["evaluation"]["complete_pages"], {"baseline": 12, "candidate": 12})
        self.assertEqual(summary["provenance"]["metadata_status"], "verified")
        self.assertEqual(summary["orientation"]["observed_case_count"], 48)
        self.assertEqual(summary["orientation"]["expected_case_count"], 48)
        self.assertEqual(summary["average_timings"]["baseline"]["first_result_seconds"],
                         {"average": None, "observed_pages": 0})
        self.assertEqual(summary["average_timings"]["candidate"]["first_result_seconds"],
                         {"average": None, "observed_pages": 0})
        self.assertTrue((output / "summary.json").is_file())
        self.assertTrue((output / "report.md").is_file())

    def test_incomplete_page_layout_change_and_orientation_error_fail_gate(self):
        baseline = self._save_results("baseline", "baseline-run", seconds=100.0)
        candidate = self._save_results("candidate", "candidate-run", seconds=70.0,
                                       page_status="page-00")
        value = json.loads(candidate.read_text(encoding="utf-8"))
        value["results"][1]["response"]["data"]["layout"]["rotate"] = 90
        candidate.write_text(json.dumps(value), encoding="utf-8")
        orientation = self._orientation()
        rows = json.loads(orientation.read_text(encoding="utf-8"))
        target = next(i for i, row in enumerate(rows)
                      if row["page_id"] == "page-02" and row["input_rotation_cw"] == 0)
        rows[target].update(accepted=True, correct=False)
        orientation.write_text(json.dumps(rows), encoding="utf-8")

        summary = compare(self.manifest_path, baseline, candidate, self.root / "comparison", orientation)
        self.assertFalse(summary["gates"]["twelve_pages_complete"])
        self.assertFalse(summary["gates"]["no_unreviewed_layout_or_transform_changes"])
        self.assertFalse(summary["gates"]["orientation_accuracy_verified"])
        self.assertIn("page-01", summary["evaluation"]["layout_or_transform_review_page_ids"])
        self.assertIn("page-02", summary["orientation"]["review_page_ids"])

    def test_orientation_rejects_duplicate_cases_and_invalid_rotations(self):
        baseline = self._save_results("baseline", "baseline-run", seconds=100.0)
        candidate = self._save_results("candidate", "candidate-run", seconds=70.0)
        orientation = self._orientation()
        rows = json.loads(orientation.read_text(encoding="utf-8"))
        rows.append(rows[0])
        orientation.write_text(json.dumps(rows), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "case duplicated"):
            compare(self.manifest_path, baseline, candidate, self.root / "duplicate", orientation)

        rows.pop()
        rows[0]["input_rotation_cw"] = 45
        orientation.write_text(json.dumps(rows), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "input rotation invalid"):
            compare(self.manifest_path, baseline, candidate, self.root / "invalid", orientation)

    def test_layout_signature_ignores_cache_key_but_reviews_geometry_changes(self):
        baseline = self._save_results("baseline", "baseline-run", seconds=100.0)
        orientation = self._orientation()

        candidate = self._save_results("candidate-cache", "candidate-run", seconds=70.0)
        value = json.loads(candidate.read_text(encoding="utf-8"))
        for row in value["results"]:
            row["response"]["data"]["image_transforms"]["cache_key"] = "cache-candidate"
        candidate.write_text(json.dumps(value), encoding="utf-8")
        cache_summary = compare(self.manifest_path, baseline, candidate,
                                self.root / "cache-only", orientation)
        self.assertEqual(cache_summary["evaluation"]["layout_or_transform_review_page_ids"], [])
        self.assertTrue(cache_summary["gates"]["no_unreviewed_layout_or_transform_changes"])

        candidate = self._save_results("candidate-matrix", "candidate-run", seconds=70.0)
        value = json.loads(candidate.read_text(encoding="utf-8"))
        value["results"][0]["response"]["data"]["image_transforms"]["matrix"] = [
            0, 1, -1, 0, 1000, 0]
        candidate.write_text(json.dumps(value), encoding="utf-8")
        matrix_summary = compare(self.manifest_path, baseline, candidate,
                                 self.root / "matrix-change", orientation)
        self.assertIn("page-00", matrix_summary["evaluation"]["layout_or_transform_review_page_ids"])

        candidate = self._save_results("candidate-region-rotate", "candidate-run", seconds=70.0)
        value = json.loads(candidate.read_text(encoding="utf-8"))
        value["results"][1]["response"]["data"]["layout"]["regions"][0]["rotate"] = 90
        candidate.write_text(json.dumps(value), encoding="utf-8")
        region_summary = compare(self.manifest_path, baseline, candidate,
                                 self.root / "region-rotate-change", orientation)
        self.assertIn("page-01", region_summary["evaluation"]["layout_or_transform_review_page_ids"])

    def test_orientation_missing_rotation_is_explicit_gate_failure(self):
        baseline = self._save_results("baseline", "baseline-run", seconds=100.0)
        candidate = self._save_results("candidate", "candidate-run", seconds=70.0)
        orientation = self._orientation()
        rows = json.loads(orientation.read_text(encoding="utf-8"))
        rows.remove(next(row for row in rows if row["page_id"] == "page-04" and
                         row["input_rotation_cw"] == 180))
        orientation.write_text(json.dumps(rows), encoding="utf-8")

        summary = compare(self.manifest_path, baseline, candidate,
                          self.root / "missing-rotation", orientation)
        self.assertFalse(summary["gates"]["orientation_accuracy_verified"])
        self.assertIn("orientation_case_coverage_incomplete", summary["orientation"]["issues"])
        self.assertEqual(summary["orientation"]["missing_cases"], [
            {"page_id": "page-04", "input_rotation_cw": 180}])
        self.assertIn("page-04", summary["orientation"]["review_page_ids"])

    def test_missing_orientation_and_unverified_source_metadata_are_explicit_gate_failures(self):
        baseline = self._save_results("baseline", "baseline-run", seconds=100.0)
        candidate = self._save_results("candidate", "candidate-run", seconds=70.0)
        candidate_value = json.loads(candidate.read_text(encoding="utf-8"))
        candidate_value["results"][0]["response"]["data"].pop("trace")
        candidate.write_text(json.dumps(candidate_value), encoding="utf-8")
        summary = compare(self.manifest_path, baseline, candidate, self.root / "comparison")

        self.assertFalse(summary["gates"]["orientation_accuracy_verified"])
        self.assertFalse(summary["gates"]["source_metadata_verified"])
        self.assertEqual(summary["orientation"]["status"], "missing")
        self.assertEqual(summary["provenance"]["metadata_status"], "unverified")

    def test_csv_json_empty_items_and_entries_are_unscored(self):
        # CSV manifests preserve JSON collections as strings.
        gold = {"items": "[]", "entries": "[]"}
        self.assertEqual(_items_comparison(gold, {"items": []}), "unscored")
        self.assertEqual(_entries_comparison(gold, {"entries": []}),
                         ("unscored", "unscored"))

    def test_csv_json_null_collections_are_unscored_but_malformed_values_are_unassessed(self):
        gold = {"items": "null", "entries": "null"}
        self.assertEqual(_items_comparison(gold, {"items": []}), "unscored")
        self.assertEqual(_entries_comparison(gold, {"entries": []}),
                         ("unscored", "unscored"))

        malformed = {"items": "not-json", "entries": "not-json"}
        self.assertEqual(_items_comparison(malformed, {"items": []}), "unassessed")
        self.assertEqual(_entries_comparison(malformed, {"entries": []}),
                         ("unassessed", "unassessed"))

    def test_explicit_non_journal_gold_does_not_assess_entries(self):
        gold = {
            "doc_type_key": "receipt",
            "entries": "[]",
            "items": '[{"side":"debit","amount":"125.50","account_name":"辦公用品"}]',
        }
        self.assertEqual(_entries_comparison(gold, {"entries": []}),
                         ("unscored", "unscored"))

    def test_empty_csv_entries_fall_back_to_printed_gold_items(self):
        gold = {
            "entries": "[]",
            "items": '[{"side":"debit","amount":"125.50","account_name":"辦公用品"}]',
        }
        voucher = {
            "entries": [{"side": "debit", "amount": "125.50",
                         "account_name": "辦公用品"}],
        }
        self.assertEqual(_entries_comparison(gold, voucher), ("correct", "correct"))


if __name__ == "__main__":
    unittest.main()
