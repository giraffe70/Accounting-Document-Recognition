"""Offline synthetic coverage for multi-document voucher scoring."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analysis.document_scoring import _assignment, score_documents
from analysis.evaluate_validation import _has_multi_document_pages, evaluate


class DocumentScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gold_path = self.root / "gold.json"
        self.runs = {name: self.root / name for name in ("alpha", "beta")}
        for path in self.runs.values():
            path.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _csv(path, columns, rows):
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)

    def _run(self, path, documents=(), pages=()):
        self._csv(path / "vouchers.csv", ["source", "page", "parent_result_id", "page_result_id",
                                           "document_id", "doc_type", "source_region", "status"], documents)
        self._csv(path / "pages.csv", ["source", "page", "status", "sha256"], pages)

    def _gold(self, rows):
        self.gold_path.write_text(json.dumps(rows), encoding="utf-8")

    def test_maximum_total_overlap_is_one_to_one_and_ignores_class_and_order(self):
        gold_docs = [
            {"doc_type_key": "invoice", "origin": {"region": [0, 0, 600, 1000]}},
            {"doc_type_key": "receipt", "origin": {"region": [400, 0, 1000, 1000]}},
        ]
        gold = [{"source": "a.pdf", "page": 1, "confidence": "high", "documents": gold_docs}]
        predictions = [
            {"source": "a.pdf", "page": 1, "parent_result_id": "p", "page_result_id": "d1",
             "document_id": "first", "doc_type": "unrelated", "source_region": "[0,0,500,1000]", "status": "succeeded"},
            {"source": "a.pdf", "page": 1, "parent_result_id": "p", "page_result_id": "d2",
             "document_id": "second", "doc_type": "unrelated", "source_region": "[500,0,1000,1000]", "status": "succeeded"},
        ]
        for path in self.runs.values():
            self._run(path, predictions)
        self._gold(gold)
        result = score_documents(self.runs, self.gold_path)
        self.assertEqual(result["document_metrics"]["alpha"]["true_positive"], 2)
        self.assertEqual(result["document_metrics"]["alpha"]["false_positive"], 0)
        self.assertEqual(result["document_metrics"]["alpha"]["false_negative"], 0)

    def test_assignment_maximizes_total_iou_before_pair_count(self):
        matrix = {("g0", "p0"): 0.9, ("g1", "p0"): 0.2,
                  ("g0", "p1"): 0.2, ("g1", "p1"): 0.0}
        with patch("analysis.document_scoring._iou", side_effect=lambda g, p: matrix[(g, p)]):
            self.assertEqual(_assignment(["g0", "g1"], ["p0", "p1"], 0.1), ((0, 0),))

    def test_manifest_failure_and_missing_predictions_remain_in_gold_denominator(self):
        gold = [{"source": "a.pdf", "page": 1, "confidence": "high",
                 "documents": [{"origin": {"region": [0, 0, 500, 1000]}},
                               {"origin": {"region": [500, 0, 1000, 1000]}}]},
                {"source": "b.pdf", "page": 1, "confidence": "medium",
                 "documents": [{"origin": {"region": [0, 0, 1000, 1000]}}]}]
        alpha_docs = [{"source": "a.pdf", "page": 1, "parent_result_id": "p", "page_result_id": "d",
                       "document_id": "d", "source_region": "[0,0,500,1000]", "status": "succeeded"}]
        pages = [{"source": "a.pdf", "page": 1, "status": "succeeded", "sha256": "a"},
                 {"source": "b.pdf", "page": 1, "status": "failed", "sha256": "b"}]
        for path in self.runs.values():
            self._run(path, alpha_docs, pages)
        self._gold(gold)
        scored = score_documents(self.runs, self.gold_path)
        counts = scored["document_metrics"]["alpha"]
        self.assertEqual((counts["true_positive"], counts["false_negative"]), (1, 2))
        self.assertEqual(scored["raw_common_pages"], 2)

    def test_legacy_gold_without_regions_reports_unscorable_multiple_predictions(self):
        gold = [{"source": "a.pdf", "page": 1, "confidence": "high", "doc_type_key": "invoice"}]
        predictions = [{"source": "a.pdf", "page": 1, "page_result_id": str(i), "document_id": str(i),
                        "source_region": f"[{i * 500},0,{(i + 1) * 500},1000]"} for i in (0, 1)]
        for path in self.runs.values():
            self._run(path, predictions)
        self._gold(gold)
        result = score_documents(self.runs, self.gold_path)
        self.assertEqual(result["document_metrics"]["alpha"]["unscorable_gold_count"], 1)
        self.assertEqual(result["document_metrics"]["alpha"]["scorable_gold_count"], 0)
        self.assertEqual(result["annotation_coverage_insufficient"], [("a.pdf", 1)])

    def test_identical_page_hashes_use_sorted_representative_and_conflicts_reject(self):
        docs = [{"source": source, "page": page, "page_result_id": f"{source}{page}",
                 "document_id": f"{source}{page}", "source_region": "[0,0,1000,1000]"}
                for source, page in (("z.pdf", 1), ("a.pdf", 1))]
        pages = [{"source": "z.pdf", "page": 1, "status": "succeeded", "sha256": "same"},
                 {"source": "a.pdf", "page": 1, "status": "succeeded", "sha256": "same"}]
        for path in self.runs.values():
            self._run(path, docs, pages)
        self._gold([{"source": source, "page": 1, "confidence": "high",
                     "documents": [{"origin": {"region": [0, 0, 1000, 1000]}}]}
                    for source in ("a.pdf", "z.pdf")])
        scored = score_documents(self.runs, self.gold_path)
        self.assertEqual(scored["unique_common_pages"], 1)
        self.assertEqual(scored["duplicate_image_groups"], [[ ["a.pdf", 1], ["z.pdf", 1] ]])
        self._gold([{"source": "a.pdf", "page": 1, "confidence": "high", "documents": []},
                    {"source": "z.pdf", "page": 1, "confidence": "high", "documents": [
                        {"origin": {"region": [0, 0, 1000, 1000]}}]}])
        with self.assertRaisesRegex(ValueError, "Conflicting gold"):
            score_documents(self.runs, self.gold_path)

    def test_evaluate_integrates_multi_document_summary(self):
        one = [{"source": "a.pdf", "page": 1, "page_result_id": "a", "document_id": "a",
                "source_region": "[0,0,500,1000]"},
               {"source": "a.pdf", "page": 1, "page_result_id": "b", "document_id": "b",
                "source_region": "[500,0,1000,1000]"}]
        for path in self.runs.values():
            self._run(path, one, [{"source": "a.pdf", "page": 1, "status": "succeeded", "sha256": "one"}])
        self._gold([{"source": "a.pdf", "page": 1, "confidence": "high",
                     "documents": [{"origin": {"region": [0, 0, 500, 1000]}},
                                   {"origin": {"region": [500, 0, 1000, 1000]}}]}])
        summary = evaluate(self.runs, self.gold_path, self.root / "out", "fixture")
        self.assertEqual(summary["document_scoring"]["document_metrics"]["alpha"]["true_positive"], 2)
        self.assertTrue((self.root / "out" / "document_comparison.csv").is_file())

    def test_missing_manifest_keeps_all_gold_pages_in_each_run_denominator(self):
        gold = [
            {"source": "a.pdf", "page": 1, "confidence": "high", "documents": [
                {"origin": {"region": [0, 0, 1000, 1000]}, "items": [{"amount": "12.00"}]}]},
            {"source": "b.pdf", "page": 1, "confidence": "high", "documents": [
                {"origin": {"region": [0, 0, 1000, 1000]}}]},
        ]
        alpha = [{"source": "a.pdf", "page": 1, "result_id": "a-doc", "document_id": "a-doc",
                  "parent_result_id": "a-page", "source_region": "[0,0,1000,1000]",
                  "origin_json": "{}", "sha256": "hash-a"}]
        beta = alpha + [{"source": "b.pdf", "page": 1, "result_id": "b-doc", "document_id": "b-doc",
                         "parent_result_id": "b-page", "source_region": "[0,0,1000,1000]",
                         "origin_json": "{}", "sha256": "hash-b"}]
        self._csv(self.runs["alpha"] / "vouchers.csv",
                  ["source", "page", "result_id", "document_id", "parent_result_id",
                   "source_region", "origin_json", "quality_json", "sha256"],
                  [{**alpha[0], "quality_json": "{}"}])
        self._csv(self.runs["beta"] / "vouchers.csv",
                  ["source", "page", "result_id", "document_id", "parent_result_id",
                   "source_region", "origin_json", "quality_json", "sha256"],
                  [{**row, "quality_json": "{}"} for row in beta])
        self._csv(self.runs["beta"] / "pages.csv",
                  ["source", "page", "result_id", "status", "image_sha256", "complete_seconds"],
                  [{"source": "a.pdf", "page": 1, "result_id": "a-page", "status": "succeeded",
                    "image_sha256": "hash-a", "complete_seconds": "1"},
                   {"source": "b.pdf", "page": 1, "result_id": "b-page", "status": "succeeded",
                    "image_sha256": "hash-b", "complete_seconds": "1"}])
        for name in self.runs:
            self._csv(self.runs[name] / "items.csv", ["result_id", "description", "quantity", "unit_price", "amount"],
                      ([{"result_id": "a-doc", "description": "line", "quantity": "1",
                         "unit_price": "12", "amount": "12"}] if name == "alpha" else
                       [{"result_id": "a-doc", "description": "line", "quantity": "1",
                         "unit_price": "12", "amount": "12"}]))
        self._gold(gold)
        result = score_documents(self.runs, self.gold_path)
        self.assertEqual(result["gold_coverage"]["reviewed_documents"], 2)
        self.assertEqual(result["run_raw_pages"]["alpha"], 1)
        self.assertEqual(result["gold_coverage"]["covered_pages"], 2)
        self.assertEqual(result["gold_coverage"]["by_run"]["alpha"]["uncovered_pages"], [["b.pdf", 1]])
        self.assertEqual(result["document_metrics"]["alpha"]["false_negative"], 1)
        self.assertEqual(result["document_metrics"]["beta"]["true_positive"], 2)
        self.assertEqual(result["item_amount_metrics"]["alpha"],
                         {"correct": 1, "total": 1, "reviewed": 1, "unscorable": 0})
        integrated = evaluate(self.runs, self.gold_path, self.root / "single-output", "fixture")
        self.assertEqual(integrated["document_scoring"]["document_metrics"]["alpha"]["false_negative"], 1)

    def test_one_prediction_for_two_reviewed_documents_scores_one_false_negative(self):
        gold = [{"source": "one.pdf", "page": 1, "confidence": "high", "documents": [
            {"origin": {"region": [0, 0, 500, 1000]}},
            {"origin": {"region": [500, 0, 1000, 1000]}},
        ]}]
        predictions = [{"source": "one.pdf", "page": 1, "result_id": "one-doc",
                        "document_id": "one-doc", "parent_result_id": "one-page",
                        "source_region": "[0,0,500,1000]"}]
        for path in self.runs.values():
            self._csv(path / "vouchers.csv", ["source", "page", "result_id", "document_id",
                                                  "parent_result_id", "source_region"], predictions)
        self._gold(gold)
        scored = score_documents(self.runs, self.gold_path)
        self.assertEqual(scored["document_metrics"]["alpha"]["true_positive"], 1)
        self.assertEqual(scored["document_metrics"]["alpha"]["false_negative"], 1)

    def test_matched_document_fields_null_gold_and_failed_status(self):
        gold = [{"source": "field.pdf", "page": 1, "confidence": "high", "documents": [{
            "origin": {"region": [0, 0, 1000, 1000]},
            "fields": {"doc_type_key": "receipt", "date": "2026-02-01",
                       "voucher_id": "A-1", "total_amount": None, "custom_field": "reviewed"},
        }]}]
        for name, path in self.runs.items():
            status = "failed" if name == "alpha" else "succeeded"
            self._csv(path / "vouchers.csv", ["source", "page", "result_id", "document_id",
                                                "parent_result_id", "source_region", "status"],
                      [{"source": "field.pdf", "page": 1, "result_id": "doc-1", "document_id": "doc-1",
                        "parent_result_id": "page-1", "source_region": "[0,0,1000,1000]", "status": status}])
            field_rows = [{"result_id": "doc-1", "field": field, "value": value}
                          for field, value in {"doc_type_key": "receipt", "date": "2026-02-01",
                                               "voucher_id": "A1", "total_amount": "",
                                               "custom_field": "reviewed"}.items()]
            self._csv(path / "fields.csv", ["result_id", "field", "value"], field_rows)
        self._gold(gold)
        scored = score_documents(self.runs, self.gold_path)
        self.assertEqual(scored["document_metrics"]["alpha"]["true_positive"], 0)
        self.assertEqual(scored["document_metrics"]["alpha"]["false_negative"], 1)
        self.assertEqual(scored["document_metrics"]["alpha"]["unscorable_gold_count"], 0)
        self.assertEqual(scored["field_metrics"]["beta"]["total_amount"],
                         {"correct": 1, "total": 1, "reviewed": 1})
        self.assertEqual(scored["field_metrics"]["beta"]["custom_field"]["correct"], 1)
        self.assertEqual(scored["field_metrics"]["alpha"]["total_amount"]["incorrect"], 1)
        self.assertIn("null or empty", scored["null_gold_field_policy"])

    def test_gold_region_does_not_match_old_prediction_by_order_when_prediction_has_no_region(self):
        self._gold([{"source": "boxed.pdf", "page": 1, "confidence": "high", "documents": [
            {"origin": {"region": [0, 0, 1000, 1000]}, "fields": {"date": "2026-01-01"}},
        ]}])
        for path in self.runs.values():
            self._csv(path / "vouchers.csv", ["source", "page", "result_id", "doc_type"],
                      [{"source": "boxed.pdf", "page": 1, "result_id": "old-result", "doc_type": "收據／雜項開銷"}])
        scored = score_documents(self.runs, self.gold_path)
        metrics = scored["document_metrics"]["alpha"]
        self.assertEqual((metrics["true_positive"], metrics["false_positive"], metrics["false_negative"]), (0, 1, 1))
        self.assertEqual(metrics["unscorable_gold_count"], 0)

    def test_empty_new_export_columns_do_not_change_legacy_route(self):
        for path in self.runs.values():
            self._csv(path / "vouchers.csv", ["source", "page", "result_id", "document_id",
                                                "parent_result_id", "source_region"],
                      [{"source": "legacy.pdf", "page": 1, "result_id": "r1",
                        "document_id": "", "parent_result_id": "", "source_region": ""}])
        self.assertFalse(_has_multi_document_pages(self.runs))


if __name__ == "__main__":
    unittest.main()
