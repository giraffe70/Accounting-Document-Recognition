"""Offline regression tests for frozen-gold combination and CSV evaluation."""

import csv
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analysis.evaluate_validation import evaluate
from analysis.gold_io import combine, load_frozen
from scripts.score_full_image_validation import score


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gold_path = self.root / "gold.json"
        self.runs = {"alpha": self.root / "alpha", "beta": self.root / "beta"}
        for run_path in self.runs.values():
            run_path.mkdir()
        self._write_run(self.runs["alpha"], [
            {"source": "invoice-a.pdf", "page": 1, "result_id": "a1", "total_amount": "10"},
        ])
        self._write_run(self.runs["beta"], [
            {"source": "invoice-a.pdf", "page": 1, "result_id": "b1", "total_amount": "20"},
        ])
        self._write_gold([self._gold("invoice-a.pdf", 1, total_amount="10")])

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _write_csv(path, fieldnames, rows):
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _write_run(self, path, pages):
        self._write_csv(path / "vouchers.csv", [
            "source", "page", "result_id", "doc_type", "status", "recognition_seconds",
            "debit_total", "credit_total",
        ], [{"source": p["source"], "page": p["page"], "result_id": p["result_id"],
             "doc_type": "收據／雜項開銷", "status": "succeeded", "recognition_seconds": "1.0",
             "debit_total": "", "credit_total": ""} for p in pages])
        fields = []
        for p in pages:
            for key in ("date", "voucher_id", "total_amount", "subtotal", "tax_amount",
                        "seller_name", "seller_tax_id", "buyer_name", "buyer_tax_id"):
                fields.append({"result_id": p["result_id"], "field": key,
                               "value": p.get(key, "")})
        self._write_csv(path / "fields.csv", ["result_id", "field", "value"], fields)
        self._write_csv(path / "items.csv", ["result_id", "description", "quantity", "unit_price", "amount"], [])
        self._write_csv(path / "entries.csv", ["result_id", "debit_account", "debit_account_name",
                     "credit_account", "credit_account_name", "amount"], [])

    @staticmethod
    def _gold(source, page, **updates):
        row = {"source": source, "page": page, "confidence": "high", "doc_type_key": "receipt",
               "date": "2026-01-02", "voucher_id": "R-1", "total_amount": "10",
               "subtotal": "", "tax_amount": "", "seller_name": "Example Shop",
               "seller_tax_id": "", "buyer_name": "", "buyer_tax_id": "", "items": []}
        row.update(updates)
        return row

    def _write_gold(self, rows, path=None):
        target = path or self.gold_path
        target.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return target

    def _db(self, pages=(), settings=None):
        path = self.root / "validation.sqlite"
        con = sqlite3.connect(path)
        try:
            con.executescript("""
                CREATE TABLE pages (dataset_id TEXT, relative_path TEXT, page_no INTEGER, page_path TEXT);
                CREATE TABLE runs (id TEXT PRIMARY KEY, dataset_id TEXT, settings_json TEXT);
                CREATE TABLE results (run_id TEXT, status TEXT, trace_json TEXT);
            """)
            for source, page, image_path in pages:
                con.execute("INSERT INTO pages VALUES (?, ?, ?, ?)",
                            ("fixture", source, page, str(image_path)))
            for name, config in (settings or {}).items():
                con.execute("INSERT INTO runs VALUES (?, ?, ?)",
                            (name + "-id", "fixture", json.dumps(config)))
            con.commit()
        finally:
            con.close()
        return path

    def _evaluate(self, output_name="out", **kwargs):
        return evaluate(self.runs, self.gold_path, self.root / output_name, "fixture", **kwargs)

    def test_named_batches_score_accuracy_and_disagreement_and_skip_empty_gold(self):
        summary = self._evaluate()
        self.assertFalse(summary["provenance"]["comparability_verified"])
        self.assertIsNone(summary["unique_image_pages"])
        scores = summary["field_scores"]["high"]
        self.assertEqual(scores["alpha"]["total_amount"], {"correct": 1})
        self.assertEqual(scores["beta"]["total_amount"], {"incorrect": 1})
        self.assertEqual(scores["alpha"]["buyer_name"], {})
        self.assertEqual(summary["model_disagreement"]["total_amount"], {"different": 1})
        with (self.root / "out" / "validation_comparison.csv").open(encoding="utf-8-sig", newline="") as stream:
            details = list(csv.DictReader(stream))
        self.assertEqual(details[0]["alpha_buyer_name_match"], "")

    def test_runs_with_no_common_page_fail_before_output_creation(self):
        self._write_run(self.runs["alpha"], [{"source": "a.pdf", "page": 1, "result_id": "a"}])
        self._write_run(self.runs["beta"], [{"source": "b.pdf", "page": 1, "result_id": "b"}])
        with self.assertRaisesRegex(ValueError, "no common"):
            self._evaluate()
        self.assertFalse((self.root / "out").exists())

    def test_gold_and_csv_input_hashes_remain_unchanged(self):
        inputs = [self.gold_path] + [d / name for d in self.runs.values()
                                     for name in ("vouchers.csv", "fields.csv", "items.csv", "entries.csv")]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
        summary = self._evaluate()
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
        self.assertEqual(before, after)
        self.assertEqual(set(summary["provenance"]["input_sha256"].values()), set(before.values()))

    def test_existing_output_is_never_overwritten(self):
        output = self.root / "out"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be new"):
            self._evaluate()
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_duplicate_gold_key_is_rejected(self):
        path = self._write_gold([self._gold("x.pdf", 1), self._gold("x.pdf", 1)])
        with self.assertRaisesRegex(ValueError, "Duplicate gold"):
            load_frozen(path)

    def test_combine_requires_explicit_override_for_overlapping_gold(self):
        base = self._write_gold([self._gold("x.pdf", 1, total_amount="10")], self.root / "base.json")
        extra = self._write_gold([self._gold("x.pdf", 1, total_amount="12")], self.root / "extra.json")
        with self.assertRaisesRegex(ValueError, "Duplicate base gold"):
            combine([base, extra])
        combined = combine([base], [extra])
        self.assertEqual(combined[("x.pdf", 1)]["total_amount"], "12")

    def test_identical_image_pages_are_deduplicated(self):
        second = {"source": "invoice-a.pdf", "page": 2, "result_id": "a2", "total_amount": "10"}
        self._write_run(self.runs["alpha"], [
            {"source": "invoice-a.pdf", "page": 1, "result_id": "a1", "total_amount": "10"}, second])
        self._write_run(self.runs["beta"], [
            {"source": "invoice-a.pdf", "page": 1, "result_id": "b1", "total_amount": "20"},
            {"source": "invoice-a.pdf", "page": 2, "result_id": "b2", "total_amount": "20"}])
        self._write_gold([self._gold("invoice-a.pdf", 1), self._gold("invoice-a.pdf", 2)])
        image1, image2 = self.root / "one.png", self.root / "two.png"
        image1.write_bytes(b"same synthetic image")
        image2.write_bytes(b"same synthetic image")
        db = self._db([( "invoice-a.pdf", 1, image1), ("invoice-a.pdf", 2, image2)])
        summary = self._evaluate(page_db=db, deduplicate=True)
        self.assertEqual(summary["raw_common_pages"], 2)
        self.assertEqual(summary["common_pages"], 1)
        self.assertEqual(summary["image_hash_coverage"], 2)
        self.assertEqual(len(summary["duplicate_image_groups"]), 1)
        moved_db = db.with_name("renamed-validation.sqlite")
        db.rename(moved_db)
        moved_db.unlink()

    def test_deduplication_rejects_conflicting_gold_for_same_image(self):
        second = {"source": "invoice-a.pdf", "page": 2, "result_id": "a2"}
        self._write_run(self.runs["alpha"], [
            {"source": "invoice-a.pdf", "page": 1, "result_id": "a1"}, second])
        self._write_run(self.runs["beta"], [
            {"source": "invoice-a.pdf", "page": 1, "result_id": "b1"},
            {"source": "invoice-a.pdf", "page": 2, "result_id": "b2"}])
        self._write_gold([self._gold("invoice-a.pdf", 1),
                          self._gold("invoice-a.pdf", 2, total_amount="99")])
        image1, image2 = self.root / "one.png", self.root / "two.png"
        image1.write_bytes(b"same synthetic image")
        image2.write_bytes(b"same synthetic image")
        db = self._db([("invoice-a.pdf", 1, image1), ("invoice-a.pdf", 2, image2)])
        with self.assertRaisesRegex(ValueError, "Conflicting gold"):
            self._evaluate(page_db=db, deduplicate=True)
        self.assertFalse((self.root / "out").exists())

    def test_database_batch_settings_must_match_and_database_is_read_only(self):
        settings = {"alpha": {"max_edge": 1000, "repeats": 1},
                    "beta": {"max_edge": 2000, "repeats": 1}}
        db = self._db(settings=settings)
        db_hash = hashlib.sha256(db.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "Batch conditions differ: max_edge"):
            self._evaluate(page_db=db, run_ids={"alpha": "alpha-id", "beta": "beta-id"})
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), db_hash)
        con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("INSERT INTO pages VALUES ('fixture', 'x', 1, 'x')")
        finally:
            con.close()

    def test_full_image_scorer_reports_missing_evidence_and_rejects_basename_collision(self):
        result = score({}, {"absent.jpg": {"total_amount": 42, "date": "", "buyer_tax_id": None}})
        self.assertEqual(result["total_scored_fields"], 1)
        self.assertEqual(result["results"][0]["error"], "No recognition record")
        self.assertFalse(result["results"][0]["comparisons"][0]["match"])

        colliding_evidence = {"results": [
            {"source_image": "batch-a/receipt.jpg", "recognized": {"total_amount": 10}},
            {"source_image": "batch-b/receipt.jpg", "recognized": {"total_amount": 10}},
        ]}
        with self.assertRaisesRegex(ValueError, "Duplicate source image basename: receipt.jpg"):
            score(colliding_evidence, {"receipt.jpg": {"total_amount": 10}})

    def test_full_image_scorer_cli_is_read_only_and_refuses_existing_output(self):
        evidence_path = self.root / "evidence.json"
        gold_path = self.root / "visible-gold.json"
        evidence = {"results": [{
            "source_image": "synthetic/page.jpg", "doc_type_key": "receipt",
            "recognized": {"fields": {"total_amount": "1,250"}},
        }]}
        gold = {"page.jpg": {"doc_type_key": "receipt", "total_amount": 1250}}
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        gold_path.write_text(json.dumps(gold), encoding="utf-8")
        hashes_before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (evidence_path, gold_path)}
        output = self.root / "full-image-score"
        command = [sys.executable, "-B", "-X", "utf8",
                   str(PROJECT_ROOT / "scripts" / "score_full_image_validation.py"),
                   "--evidence", str(evidence_path), "--gold", str(gold_path),
                   "--dataset-id", "synthetic-dataset", "--run-id", "synthetic-run",
                   "--output-dir", str(output)]
        completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True,
                                   text=True, encoding="utf-8", timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        scored = json.loads((output / "scored_evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(scored["correct_fields"], 2)
        self.assertEqual(scored["total_scored_fields"], 2)
        self.assertEqual(scored["provenance"]["dataset_id"], "synthetic-dataset")
        self.assertEqual(scored["provenance"]["run_id"], "synthetic-run")
        self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in (evidence_path, gold_path)}, hashes_before)

        saved_output = (output / "scored_evidence.json").read_bytes()
        repeated = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True,
                                  text=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("error:", repeated.stderr.lower())
        self.assertEqual((output / "scored_evidence.json").read_bytes(), saved_output)
        self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in (evidence_path, gold_path)}, hashes_before)

    def test_csv_evaluation_cli_and_gold_export_use_new_directories(self):
        inputs = [self.gold_path] + [p for run in self.runs.values() for p in run.glob("*.csv")]
        original = {p: p.read_bytes() for p in inputs}
        output = self.root / "csv-cli"
        command = [sys.executable, "-B", "-X", "utf8", "-m", "analysis.evaluate_validation",
                   "--run", f"alpha={self.runs['alpha']}", "--run", f"beta={self.runs['beta']}",
                   "--gold", str(self.gold_path), "--dataset-id", "fixture", "--output-dir", str(output)]
        result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((output / "validation_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["common_pages"], 1)
        self.assertFalse((output / "gold_standard.json").exists())
        repeated = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(repeated.returncode, 0)
        export_dir = self.root / "gold-export"
        export_command = [sys.executable, "-B", "-X", "utf8", "-m", "analysis.prepare_gold",
                          "--base", str(self.gold_path), "--output-dir", str(export_dir)]
        result = subprocess.run(export_command, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_frozen(export_dir / "gold_standard.json"), load_frozen(self.gold_path))
        self.assertTrue((export_dir / "manifest.json").is_file())
        self.assertEqual({p: p.read_bytes() for p in inputs}, original)

if __name__ == "__main__":
    unittest.main()
