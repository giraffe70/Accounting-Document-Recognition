"""Regression contracts for OCR cache and atomic journal behavior.

All images and OCR results in this module are synthetic; no model or network is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from app.core import (invocation_trace, ocr_cache, ocr_engine, ocr_orientation,
                      ocr_policy, ocr_resolution)


class AtomicJournalRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_atomic_json_writes_through_long_same_directory_path(self):
        # Keep the output filename under the component limit while the temporary
        # sibling path crosses MAX_PATH on Windows.
        directory = self.root / ("segment-" + "x" * 140)
        directory.mkdir(parents=True)
        journal = directory / "journal.json"
        if len(str(journal)) + 55 < 260:
            self.skipTest("workspace path does not reach the Windows long-path boundary")
        if os.name == "nt":
            journal = Path("\\\\?\\" + str(journal))

        payload = {"records": [{"kind": "synthetic", "text": "測試"}]}
        invocation_trace.atomic_json(journal, payload)

        self.assertEqual(json.loads(journal.read_text(encoding="utf-8")), payload)
        self.assertEqual([name for name in os.listdir(directory) if name.endswith(".tmp")], [])
        journal.unlink()

    def test_replace_failure_preserves_previous_journal_and_cleans_temporary_file(self):
        journal = self.root / "journal.json"
        previous = {"records": [{"kind": "previous"}]}
        journal.write_text(json.dumps(previous), encoding="utf-8")

        with patch.object(invocation_trace.os, "replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                invocation_trace.atomic_json(journal, {"records": [{"kind": "new"}]})

        self.assertEqual(json.loads(journal.read_text(encoding="utf-8")), previous)
        self.assertEqual(list(self.root.glob(journal.name + ".*.tmp")), [])


class DiskCachePerformanceTests(unittest.TestCase):
    def test_cache_hit_uses_independent_cache_directory_without_loading_ocr(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            root = Path(temporary)
            source_dir = root / "source-files"
            cache_dir = root / "work-cache"
            source_dir.mkdir()
            source = source_dir / "scan.png"
            Image.new("RGB", (37, 29), (245, 244, 243)).save(source)

            class FakeOCR:
                def predict(self, _path):
                    return [{"rec_texts": ["合成快取"],
                             "dt_polys": [[[2, 3], [20, 3], [20, 12], [2, 12]]],
                             "rec_scores": [.98]}]

            engine = ocr_engine.PaddleOCREngine()
            signature = {"provider": "synthetic", "revision": 1}
            with patch.object(ocr_engine, "runtime_signature", return_value=signature), \
                    patch.object(ocr_engine, "_get_paddle", return_value=FakeOCR()) as loader:
                with ocr_cache.scope(cache_dir):
                    first = engine.recognize(str(source))
                self.assertEqual(first[0]["text"], "合成快取")
                loader.reset_mock()
                with ocr_cache.scope(cache_dir):
                    second = engine.recognize(str(source))

            self.assertEqual(second, first)
            loader.assert_not_called()
            self.assertTrue(cache_dir.is_dir())
            self.assertNotEqual(source.parent, cache_dir)


class OrientationFallbackTests(unittest.TestCase):
    def test_auto_orientation_with_missing_model_returns_safe_fallback(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            root = Path(temporary)
            image = root / "source.png"
            Image.new("RGB", (40, 30), "white").save(image)
            missing_model = root / "missing-model"
            with ocr_policy.isolated_policy({"orientation_mode": "auto",
                                             "orientation_model_dir": str(missing_model)}), \
                    patch.object(ocr_orientation, "_load_classifier") as loader:
                prediction = ocr_orientation.classify(image)

        self.assertEqual(prediction["angle"], None)
        self.assertFalse(prediction["accepted"])
        self.assertEqual(prediction["reason"], "model_missing")
        loader.assert_not_called()

    def test_classifier_error_returns_fallback_without_network_or_retry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            root = Path(temporary)
            image = root / "source.png"
            Image.new("RGB", (40, 30), "white").save(image)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "synthetic.pdiparams").write_bytes(b"synthetic")
            with ocr_policy.isolated_policy({"orientation_mode": "auto",
                                             "orientation_model_dir": str(model_dir)}), \
                    patch.object(ocr_orientation, "_load_classifier",
                                 side_effect=RuntimeError("offline classifier failure")) as loader:
                prediction = ocr_orientation.classify(image)

        self.assertIsNone(prediction["angle"])
        self.assertFalse(prediction["accepted"])
        self.assertEqual(prediction["reason"], "classifier_error")
        self.assertIn("offline classifier failure", prediction["error"])
        loader.assert_called_once_with(str(model_dir))

    def test_auto_orientation_accepts_only_predictions_at_the_configured_threshold(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            root = Path(temporary)
            image = root / "source.png"
            Image.new("RGB", (40, 30), "white").save(image)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "synthetic.pdiparams").write_bytes(b"synthetic")

            class FakeClassifier:
                calls = 0

                def predict(self, _path):
                    self.calls += 1
                    score = .95 if self.calls == 1 else .89
                    return [{"label_names": ["90"], "scores": [score]}]

            classifier = FakeClassifier()
            with ocr_policy.isolated_policy({"orientation_mode": "auto",
                                             "orientation_threshold": .90,
                                             "orientation_model_dir": str(model_dir)}), \
                    patch.object(ocr_orientation, "_load_classifier", return_value=classifier) as loader:
                accepted = ocr_orientation.classify(image)
                uncertain = ocr_orientation.classify(image)

        self.assertEqual((accepted["angle"], accepted["score"], accepted["accepted"]),
                         (270, .95, True))
        self.assertEqual(accepted["reason"], "high_confidence")
        self.assertEqual((uncertain["angle"], uncertain["score"], uncertain["accepted"]),
                         (270, .89, False))
        self.assertEqual(uncertain["reason"], "low_confidence")
        self.assertEqual(classifier.calls, 2)
        self.assertEqual(loader.call_count, 2)


class AdaptiveResolutionRegressionTests(unittest.TestCase):
    def test_fixed_resolution_policies_select_their_named_edge(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            source = Path(temporary) / "source.png"
            Image.new("RGB", (80, 60), "white").save(source)
            for policy, expected in (("fixed_1600", 1600), ("fixed_2000", 2000),
                                     ("fixed_2400", 2400)):
                with self.subTest(policy=policy), ocr_policy.isolated_policy({"resolution": policy}):
                    edge, assessment = ocr_resolution.choose_edge(source)
                    self.assertEqual(edge, expected)
                    self.assertEqual(assessment["reason"], "fixed")

    def test_low_quality_2000_input_upgrades_once_then_reuses_2400_cache(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            root = Path(temporary)
            source = root / "source.png"
            target = root / "ocr-input_text.png"
            cache_dir = root / "cache"
            Image.new("RGB", (3000, 3000), (245, 244, 243)).save(source)
            original = source.read_bytes()
            calls = []
            signature = {"provider": "synthetic", "revision": 1}

            class FakeOCR:
                lang = "synthetic"

                def recognize(self, input_path):
                    def compute():
                        with Image.open(input_path) as image:
                            width, height = image.size
                        calls.append((width, height))
                        score = .80 if max(width, height) == 2000 else .99
                        return [{"text": "合成辨識", "box": [[10, 10], [40, 10],
                                                                  [40, 28], [10, 28]],
                                 "score": score}]

                    return ocr_cache.recognize(input_path, signature, compute, {})

            assessment = {"policy": "adaptive", "reason": "synthetic_low_quality", "blank": False}
            with ocr_policy.isolated_policy({"resolution": "adaptive"}), \
                    ocr_cache.scope(cache_dir), \
                    patch.object(ocr_engine, "runtime_signature", return_value=signature), \
                    patch.object(ocr_resolution, "choose_edge", return_value=(2000, assessment)) as choose:
                first, first_path = ocr_resolution.read(source, target, FakeOCR())
                second, second_path = ocr_resolution.read(source, target, FakeOCR())
                source_unchanged = source.read_bytes() == original

        self.assertEqual(first[0]["text"], "合成辨識")
        self.assertEqual(second, first)
        self.assertTrue(first_path.endswith("_text.png"))
        self.assertTrue(second_path.endswith("_text.png"))
        self.assertEqual(calls, [(2000, 2000), (2400, 2400)])
        self.assertEqual(choose.call_count, 1)
        self.assertTrue(source_unchanged)


if __name__ == "__main__":
    unittest.main()
