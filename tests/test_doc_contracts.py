"""Offline smoke regression for document contracts, router, and pure pipeline helpers."""
from __future__ import annotations

import ast
import json
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from app.core import doc_router, doc_types, pipeline

ROOT = Path(__file__).resolve().parents[1]


def literal_assignments(path: Path) -> dict[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        found[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass
    return found


class DocumentContractTests(unittest.TestCase):
    def test_all_fields_have_unique_keys_and_supported_kinds(self):
        self.assertTrue(doc_types.DOC_TYPES)
        for type_key, spec in doc_types.DOC_TYPES.items():
            with self.subTest(type_key=type_key):
                fields = spec["fields"]
                keys = [field["key"] for field in fields]
                self.assertEqual(len(keys), len(set(keys)), "field keys must be unique")
                self.assertTrue(all(key for key in keys))
                self.assertTrue(all(field.get("kind", "text") in {"text", "date", "number"} for field in fields))

    def test_document_types_and_prompt_box_contract_matches(self):
        self.assertEqual(len(doc_types.DOC_TYPES), 18)
        self.assertLessEqual(doc_types.NO_VLM_BOX_TYPES, doc_types.DOC_TYPES.keys())
        for type_key in doc_types.DOC_TYPES:
            prompt = doc_types.build_extract_prompt(type_key)
            with self.subTest(type_key=type_key):
                if type_key in doc_types.NO_VLM_BOX_TYPES:
                    self.assertNotIn('"boxes": {', prompt)
                else:
                    # Boxes are optional in the JSON contract. The prompt gives one
                    # empty example and the shared coordinate/omission rule; field
                    # eligibility is enforced by the validator, not repeated inline.
                    self.assertIn('"boxes":{}', prompt)
                    self.assertIn("0至1000整數", prompt)
                    self.assertIn("未知省略", prompt)

    def test_manual_generator_constants_stay_in_sync_without_importing_writer(self):
        source = ROOT / "scripts" / "build_agents_section.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        constants = literal_assignments(source)
        self.assertEqual(set(constants["ORDER"]), set(doc_types.DOC_TYPES))
        self.assertEqual(set(constants["GROUP"]), set(doc_types.DOC_TYPES))
        self.assertEqual(set(constants["TASK"]), set(doc_types.DOC_TYPES))
        # This is deliberately an AST-only test: importing the module writes the manual HTML.
        self.assertTrue(any(isinstance(node, ast.For) for node in tree.body))

    def test_report_type_labels_cover_gold_categories_and_have_no_unknown_keys(self):
        score_constants = literal_assignments(ROOT / "scripts" / "score_full_image_validation.py")
        report_constants = literal_assignments(ROOT / "scripts" / "build_full_image_validation_report.py")
        gold = score_constants["GROUND_TRUTH"]
        labels = report_constants["TYPE_LABELS"]
        gold_categories = {record["doc_type_key"] for record in gold.values() if "doc_type_key" in record}
        self.assertTrue(gold_categories <= labels.keys(), f"gold categories lack report labels: {gold_categories - labels.keys()}")
        self.assertLessEqual(labels.keys(), doc_types.DOC_TYPES.keys())

    def test_evaluation_labels_cover_every_document_type_exactly(self):
        constants = literal_assignments(ROOT / "analysis" / "evaluate_validation.py")
        labels = constants["TYPE_LABELS"]
        expected = {spec["label"]: key for key, spec in doc_types.DOC_TYPES.items()}
        self.assertEqual(labels, expected)

    def test_accuracy_report_labels_are_a_valid_document_type_subset(self):
        constants = literal_assignments(ROOT / "analysis" / "export_accuracy_report.py")
        labels = constants["DOC_TYPE_LABELS"]
        self.assertLessEqual(labels.keys(), doc_types.DOC_TYPES.keys())

    def test_router_classifies_alias_and_valid_rotation_offline(self):
        result = self._classify_with_response('前言 {"type":"電子發票","rotate":90} 後記')
        self.assertEqual(result, ("invoice", 90))

    def test_router_rejects_bad_rotation_and_falls_back_for_unknown_or_failed_response(self):
        self.assertEqual(self._classify_with_response('{"type":"支票","rotate":45}'), ("cheque", 0))
        self.assertEqual(self._classify_with_response('{"type":"unlisted","rotate":180}'), ("other", 180))
        self.assertEqual(self._classify_with_response(error=RuntimeError("mock failure")), ("other", 0))

    @staticmethod
    def _classify_with_response(response: str | None = None, error: Exception | None = None):
        settings = {"structure": "fake/provider"}
        with patch("app.core.settings_store.get_settings", return_value=settings), \
             patch("app.core.settings_store.parse_setting", return_value=("fake", "model")), \
             patch("app.core.model_factory.get_chat_model", return_value=object()), \
             patch("app.core.pipeline._data_uri", return_value="data:image/png;base64,offline"), \
             patch("app.core.pipeline._invoke_text", side_effect=error or [response]), \
             patch("app.core.activity.stage", return_value=nullcontext()):
            return doc_router.classify("does-not-exist.png")

    def test_pure_pipeline_normalization_helpers(self):
        self.assertEqual(pipeline._norm_roc_date("民國115/06/04"), "2026-06-04")
        self.assertEqual(pipeline._norm_roc_date("114.07.23"), "2025-07-23")
        self.assertEqual(pipeline._norm_amount("NT$１,０５０．５"), 1050.5)
        self.assertEqual(pipeline._norm_amount("not-money"), "not-money")
        repaired = pipeline._parse_json("```json\n{type: 'receipt', total: 1,050,}\n```")
        self.assertEqual(repaired["type"], "receipt")
        self.assertEqual(repaired["total"], 1050)


if __name__ == "__main__":
    unittest.main()
