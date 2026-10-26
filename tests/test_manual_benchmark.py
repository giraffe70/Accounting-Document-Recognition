"""Offline input and explicit-execution guards for the manual OCR benchmark."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts import benchmark_manual_ocr


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _tempdir():
    root = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


def _inputs(folder, *, modes=("single",)):
    folder = Path(folder)
    image_paths = []
    pages = []
    reviewed_pages = {}
    for index, mode in enumerate(modes):
        page_id = f"page-{index + 1}"
        image = folder / f"synthetic-{index + 1}.png"
        bitmap = Image.new("RGB", (80, 60), "white")
        bitmap.putpixel((index + 1, index + 1), (0, 0, 0))
        bitmap.save(image)
        image_paths.append(image)
        pages.append({"id": page_id, "path": str(image), "sha256": _sha(image),
                      "source": f"synthetic-{index + 1}.pdf", "page": index + 1})
        reviewed_pages[page_id] = {"rotation_cw": 0, "document_mode": mode,
                                   "reviewed": True, "reference": "synthetic-review"}

    gold = folder / "synthetic-gold.json"
    gold.write_text(json.dumps({"documents": []}, ensure_ascii=False), encoding="utf-8")
    manifest = {"gold_path": str(gold), "gold_sha256": _sha(gold), "pages": pages,
                "combo": {"structure_model": "offline-structure", "classify_model": "offline-classify"}}
    directions = {"pages": reviewed_pages}
    manifest_path, directions_path = folder / "manifest.json", folder / "directions.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    directions_path.write_text(json.dumps(directions, ensure_ascii=False), encoding="utf-8")
    return manifest_path, directions_path, manifest, directions, gold, image_paths


class ManualBenchmarkInputTests(unittest.TestCase):
    def test_valid_synthetic_manifest_and_direction_reviews_load(self):
        with _tempdir() as temporary:
            manifest_path, directions_path, manifest, directions, _, _ = _inputs(temporary)
            loaded_manifest, loaded_directions = benchmark_manual_ocr.load_inputs(manifest_path, directions_path)
            self.assertEqual(loaded_manifest, manifest)
            self.assertEqual(loaded_directions, directions)

    def test_changed_gold_and_changed_page_image_are_rejected(self):
        with _tempdir() as temporary:
            manifest_path, directions_path, _, _, gold, _ = _inputs(temporary)
            gold.write_text('{"documents":["changed"]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Frozen gold changed"):
                benchmark_manual_ocr.load_inputs(manifest_path, directions_path)

        with _tempdir() as temporary:
            manifest_path, directions_path, _, _, _, image_paths = _inputs(temporary)
            image_paths[0].write_bytes(image_paths[0].read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "Frozen source image changed"):
                benchmark_manual_ocr.load_inputs(manifest_path, directions_path)

    def test_duplicate_manifest_pages_and_missing_direction_review_are_rejected(self):
        with _tempdir() as temporary:
            manifest_path, directions_path, manifest, _, _, _ = _inputs(temporary)
            manifest["pages"].append(dict(manifest["pages"][0]))
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly the manifest pages"):
                benchmark_manual_ocr.load_inputs(manifest_path, directions_path)

        with _tempdir() as temporary:
            manifest_path, directions_path, _, directions, _, _ = _inputs(temporary, modes=("single", "single"))
            directions["pages"].pop("page-2")
            directions_path.write_text(json.dumps(directions), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly the manifest pages"):
                benchmark_manual_ocr.load_inputs(manifest_path, directions_path)

    def test_boolean_and_non_quarter_turn_rotations_are_rejected(self):
        for rotation in (True, 45, 360, "90"):
            with self.subTest(rotation=rotation), _tempdir() as temporary:
                manifest_path, directions_path, _, directions, _, _ = _inputs(temporary)
                directions["pages"]["page-1"]["rotation_cw"] = rotation
                directions_path.write_text(json.dumps(directions), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "explicit clockwise direction"):
                    benchmark_manual_ocr.load_inputs(manifest_path, directions_path)

    def test_single_and_auto_split_modes_are_accepted_but_implicit_mode_is_not(self):
        with _tempdir() as temporary:
            manifest_path, directions_path, _, _, _, _ = _inputs(temporary, modes=("single", "auto_split"))
            manifest, directions = benchmark_manual_ocr.load_inputs(manifest_path, directions_path)
            self.assertEqual([directions["pages"][page["id"]]["document_mode"] for page in manifest["pages"]],
                             ["single", "auto_split"])

        with _tempdir() as temporary:
            manifest_path, directions_path, _, directions, _, _ = _inputs(temporary)
            directions["pages"]["page-1"].pop("document_mode")
            directions_path.write_text(json.dumps(directions), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "explicit single/auto_split decision"):
                benchmark_manual_ocr.load_inputs(manifest_path, directions_path)

    def test_main_requires_execute_before_creating_outputs_or_starting_work(self):
        with _tempdir() as temporary:
            folder = Path(temporary)
            manifest_path, directions_path, *_ = _inputs(folder)
            output = folder / "must-not-be-created"
            argv = ["benchmark_manual_ocr.py", "--manifest", str(manifest_path),
                    "--directions", str(directions_path), "--output-dir", str(output)]
            stderr = StringIO()
            with (patch.object(benchmark_manual_ocr.sys, "argv", argv),
                  patch.object(benchmark_manual_ocr.subprocess, "run") as subprocess_run,
                  patch.object(benchmark_manual_ocr, "run_page") as run_page,
                  patch.object(benchmark_manual_ocr, "candidate") as candidate,
                  patch("app.core.model_invocation.source_versions") as source_versions,
                  patch("app.core.model_factory.get_ocr") as get_ocr,
                  patch("app.core.model_factory.get_chat_model") as get_chat,
                  redirect_stderr(stderr)):
                with self.assertRaises(SystemExit) as raised:
                    benchmark_manual_ocr.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("Real OCR/VLM requires --execute", stderr.getvalue())
            self.assertFalse(output.exists())
            subprocess_run.assert_not_called()
            run_page.assert_not_called()
            candidate.assert_not_called()
            source_versions.assert_not_called()
            get_ocr.assert_not_called()
            get_chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
