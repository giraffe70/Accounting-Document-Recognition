"""Offline contract and retry regressions for generated model prompts."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core import doc_types, invocation_trace, model_invocation, prompts


class PromptContractTests(unittest.TestCase):
    def test_every_type_and_mode_has_a_valid_null_example(self):
        self.assertEqual(len(doc_types.DOC_TYPES), 18)
        for type_key in doc_types.DOC_TYPES:
            for mode in ("image", "ocr_text"):
                with self.subTest(type_key=type_key, mode=mode):
                    example = prompts.extraction_example(type_key, mode)
                    self.assertEqual(prompts.validate_extraction(example, type_key, mode), example)

    def test_route_prompt_uses_mode_rotation_rule_and_ocr_context(self):
        image_prompt = prompts.build_route_prompt("image", ocr_text="OCR clues", hint="top heading")
        text_prompt = prompts.build_route_prompt("ocr_text", ocr_text="OCR clues", hint="top heading")
        self.assertIn("順時針 0、90、180、270", image_prompt)
        self.assertIn("rotate 固定為 0", text_prompt)
        for prompt in (image_prompt, text_prompt):
            self.assertIn("OCR clues", prompt)
            self.assertIn("top heading", prompt)

    def test_contract_rejects_wrong_types_missing_fields_unknown_keys_and_bad_boxes(self):
        good = prompts.extraction_example("invoice", "image")

        wrong_type = json.loads(json.dumps(good))
        wrong_type["fields"]["voucher_id"] = 123
        with self.assertRaises((ValueError, TypeError)):
            prompts.validate_extraction(wrong_type, "invoice", "image")

        missing = json.loads(json.dumps(good))
        del missing["fields"]["voucher_id"]
        with self.assertRaises((ValueError, TypeError)):
            prompts.validate_extraction(missing, "invoice", "image")

        unknown = json.loads(json.dumps(good))
        unknown["hallucinated"] = "value"
        with self.assertRaises((ValueError, TypeError)):
            prompts.validate_extraction(unknown, "invoice", "image")

        for box in ([0, 0, 1001, 100], [0, 0, 10], [100, 0, 10, 20], [0, 0, 0, 10]):
            invalid_box = json.loads(json.dumps(good))
            invalid_box["boxes"] = {"voucher_id": box}
            with self.subTest(box=box), self.assertRaises((ValueError, TypeError)):
                prompts.validate_extraction(invalid_box, "invoice", "image")

        unknown_box = json.loads(json.dumps(good))
        unknown_box["boxes"] = {"not_a_field": [0, 0, 10, 10]}
        with self.assertRaises((ValueError, TypeError)):
            prompts.validate_extraction(unknown_box, "invoice", "image")

    def test_input_modes_change_prompt_and_image_boxes_are_contractual(self):
        image_prompt = prompts.build_extract_prompt("invoice", mode="image", ocr_text="OCR fragment")
        text_prompt = prompts.build_extract_prompt("invoice", mode="ocr_text", ocr_text="OCR fragment")
        self.assertIn('"boxes":{}', image_prompt)
        self.assertNotIn('"boxes":{}', text_prompt)
        self.assertIn("不得輸出圖片定位框", text_prompt)
        self.assertIn("OCR fragment", text_prompt)

        text_data = prompts.extraction_example("invoice", "ocr_text")
        text_data["boxes"] = {}
        with self.assertRaises((ValueError, TypeError)):
            prompts.validate_extraction(text_data, "invoice", "ocr_text")

        # Dense-form types never request VLM boxes, even when an image is present.
        self.assertNotIn('"boxes":{}', prompts.build_extract_prompt("import_declaration"))

    def test_supplement_fields_are_a_nonempty_contract_subset(self):
        data = prompts.extraction_example("invoice", "ocr_text", ["date", "total_amount"])
        self.assertEqual(set(data["fields"]), {"date", "total_amount"})
        self.assertEqual(prompts.validate_extraction(data, "invoice", "ocr_text", ["date", "total_amount"]), data)
        with self.assertRaises(ValueError):
            prompts.extraction_example("invoice", "ocr_text", ["unknown"])
        with self.assertRaises(ValueError):
            prompts.extraction_example("invoice", "ocr_text", [])


class StructuredInvocationTests(unittest.TestCase):
    @staticmethod
    def _valid_invoice():
        return prompts.extraction_example("invoice", "image")

    def test_contract_retry_preserves_original_image_and_ocr(self):
        secret = "SENSITIVE_IMAGE_BASE64_SHOULD_NOT_LEAK"
        content = [
            {"type": "image_url", "url": "data:image/png;base64," + secret},
            {"type": "text", "text": "OCR: 原始 OCR"},
        ]
        invalid = {"fields": {"voucher_id": None}}
        requests = []

        def invoke(_chat, request):
            requests.append(request)
            return json.dumps(invalid if len(requests) == 1 else self._valid_invoice(), ensure_ascii=False)

        with invocation_trace.capture() as trace:
            result = model_invocation.invoke_structured(
                object(), content,
                lambda value: prompts.validate_extraction(value, "invoice", "image"),
                invoke=invoke,
            )

        self.assertEqual(result, self._valid_invoice())
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], content)
        self.assertEqual(requests[1][:2], content)
        self.assertIn("原始 OCR", requests[1][1]["text"])
        self.assertIn("依原任務及全部原證據", requests[1][-1]["text"])
        self.assertEqual([r["round"] for r in trace if r["kind"] == "structured_round"], [1, 2])
        self.assertEqual([r["round"] for r in trace if r["kind"] == "contract_error"], [1])

    def test_two_invalid_semantic_rounds_are_the_limit(self):
        calls = []

        def invoke(_chat, _content):
            calls.append(1)
            return '{"wrong":"contract"}'

        with self.assertRaisesRegex(ValueError, "after 2 rounds"):
            model_invocation.invoke_structured(
                object(), "prompt", lambda value: prompts.validate_extraction(value, "invoice", "ocr_text"),
                invoke=invoke,
            )
        self.assertEqual(len(calls), 2)

    def test_transport_retries_three_times_on_empty_and_exception_then_stops(self):
        chat = SimpleNamespace(invoke=unittest.mock.Mock(side_effect=[RuntimeError("offline"), SimpleNamespace(content=""), RuntimeError("offline")]))
        with patch("time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "連續 3 次"):
                model_invocation._invoke_text(chat, "prompt")
        self.assertEqual(chat.invoke.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_trace_sanitizes_image_data_and_records_call_kind(self):
        secret = "PRIVATE_IMAGE_PAYLOAD"
        content = [{"type": "image_url", "url": "data:image/png;base64," + secret}]
        chat = SimpleNamespace(invoke=lambda _messages: SimpleNamespace(content='{"ok":true}'))
        with invocation_trace.capture() as trace:
            self.assertEqual(model_invocation._invoke_text(chat, content), '{"ok":true}')
        calls = [r for r in trace if r["kind"] == "model_call"]
        self.assertEqual(len(calls), 1)
        self.assertNotIn(secret, json.dumps(trace))
        self.assertIn("<embedded image:", json.dumps(calls[0]["input"]))


if __name__ == "__main__":
    unittest.main()
