import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.core import geometry, invocation_trace, model_invocation, page_layout, prompts, supplement


def _temporary_directory():
    temp_root = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    return tempfile.TemporaryDirectory(dir=temp_root)


class BoxAndLayoutTests(unittest.TestCase):
    def test_bad_receipt_boxes_do_not_invalidate_valid_content(self):
        data = prompts.extraction_example("receipt")
        data["fields"].update(vendor="店家", total_amount=0)
        data["boxes"] = {
            "receipt_type": None,
            "total_amount": [100, [200], 300, 400],
            "mystery": [10, 10, 20, 20],
            "date": [500, 500, 500, 600],
            "tax_amount": [0, 0, 1001, 100],
            "vendor": [10, 10, 20, 20],
        }
        result = prompts.validate_content_and_boxes(data, "receipt")
        self.assertEqual(result["fields"]["total_amount"], 0)
        self.assertEqual(result["boxes"]["vendor"], [10, 10, 20, 20])
        self.assertEqual(result["boxes"]["total_amount"] if "total_amount" in result["boxes"] else None, None)
        self.assertEqual(len(result["box_issues"]), 5)

    def test_box_conversion_requires_explicit_units(self):
        with self.assertRaises(ValueError):
            geometry.convert_box([10, 20, 100, 200], unit=None, width=400, height=300)
        self.assertEqual(geometry.convert_box([10, 20, 100, 200], unit="pixels", width=200, height=400),
                         [50, 50, 500, 500])
        self.assertEqual(geometry.convert_box([0.1, 0.2, 0.8, 0.9], unit="fraction", width=200, height=400),
                         [100, 200, 800, 900])

    def test_high_confidence_regions_split_and_uncertain_layout_stays_manual(self):
        regions = [
            {"type": "receipt", "rotate": 0, "reason": "A", "confidence": "high", "box": [0, 0, 300, 500]},
            {"type": "cheque", "rotate": 0, "reason": "B", "confidence": "high", "box": [350, 0, 650, 500]},
            {"type": "cheque", "rotate": 0, "reason": "C", "confidence": "high", "box": [700, 0, 1000, 500]},
        ]
        accepted = page_layout.validate_layout({"type": "other", "rotate": 0, "reason": "multiple",
                                                "regions": regions})
        self.assertEqual(len(accepted["regions"]), 3)
        self.assertFalse(accepted["auto_split"])  # Geometry alone is insufficient independent evidence.

        uncertain = page_layout.validate_layout({"type": "other", "rotate": 0, "reason": "uncertain",
            "regions": [{**regions[0], "confidence": "low"},
                        {**regions[0], "type": "invoice", "confidence": "high",
                         "box": [50, 20, 450, 450]}]})
        self.assertFalse(uncertain["auto_split"])
        self.assertTrue(uncertain["issues"])

        with self.assertRaisesRegex(ValueError, "region count"):
            page_layout.validate_layout({"type": "other", "rotate": 0, "reason": "too many", "regions": [regions[0]] * 17})

    def test_crop_preserves_source_and_obeys_native_edge_limit(self):
        with _temporary_directory() as temp:
            source = Path(temp) / "source.png"
            target = Path(temp) / "crop.png"
            Image.new("RGB", (4000, 2000), "white").save(source)
            before = source.read_bytes()
            result_path = page_layout.crop(source, {"box": [0, 0, 1000, 1000], "rotate": 0},
                                           target, max_edge=2400)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(result_path, str(target))
            with Image.open(target) as crop:
                self.assertEqual(crop.size, (2400, 1200))


class InvocationTests(unittest.TestCase):
    def test_trace_file_remains_readable_and_redacts_embedded_image_after_error(self):
        with _temporary_directory() as temp:
            path = Path(temp) / "trace.json"
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                with invocation_trace.capture(path) as records:
                    invocation_trace.add({"kind": "request", "image": {"url": "data:image/png;base64,AAAA"}})
                    raise RuntimeError("synthetic failure")
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted[-1]["kind"], "failure")
            self.assertNotIn("base64,AAAA", path.read_text(encoding="utf-8"))
            self.assertIn("embedded image", persisted[0]["image"]["url"])

    def test_second_semantic_round_includes_previous_raw_output(self):
        requests = []

        def invoke(_chat, request):
            requests.append(request)
            return '{"ok": "wrong"}' if len(requests) == 1 else '{"ok": true}'

        def validate(data):
            if data.get("ok") is not True:
                raise ValueError("ok must be true")
            return data

        result = model_invocation.invoke_structured(object(), "synthetic", validate, invoke=invoke)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1][-1]["role"], "user")
        retry = json.loads(requests[1][-1]["content"])
        self.assertEqual(retry["previous_output_raw"], '{"ok": "wrong"}')

    @patch("app.core.request_budget.runtime_profile", return_value=({"profile_id":"offline", "processor":{}},
        type("OfflineTokenizer",(),{"encode":lambda self,text,**kw:type("Encoding",(),{"ids":list(text)})()})()))
    def test_ollama_format_rejection_falls_back_but_generic_error_does_not(self, _profile):
        from langchain_ollama import ChatOllama
        from ollama import ChatResponse, Message, ResponseError
        chat = ChatOllama(model="offline-test", base_url="http://127.0.0.1:1", temperature=0, seed=42, num_ctx=8192)
        response = ChatResponse(model="offline-test",message=Message(role="assistant",content='{"ok":true}'),
                                done=True,done_reason="stop")
        def reject_schema_then_return_json(**kwargs):
            self.assertNotIn("num_predict",kwargs)
            self.assertEqual(kwargs["options"]["num_predict"],4096)
            self.assertEqual(kwargs["options"]["seed"],42)
            if isinstance(kwargs["format"],dict):
                raise ResponseError("unsupported format",status_code=400)
            return iter([response])
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        with patch.object(chat._client,"chat",autospec=True,side_effect=reject_schema_then_return_json) as client_chat:
            result = model_invocation.invoke_structured(chat, "synthetic", lambda raw: raw, schema=schema)
        self.assertEqual(result, {"ok": True})
        self.assertEqual([call.kwargs["format"] for call in client_chat.call_args_list],[schema,"json"])
        # Transport retries remain in schema mode; generic failures do not weaken it.
        with patch.object(chat._client,"chat",autospec=True,side_effect=RuntimeError("connection failed")) as client_chat, \
             patch("time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "connection failed"):
                model_invocation.invoke_structured(chat,"synthetic",lambda raw:raw,schema=schema)
        self.assertEqual([call.kwargs["format"] for call in client_chat.call_args_list],[schema]*3)


class SupplementMoneyTests(unittest.TestCase):
    def _data(self):
        return {"fields": {"currency": "TWD", "subtotal": 14000, "tax_amount": 1500,
                            "total_amount": 8500, "tax_id": None, "vendor": "店家"},
                "validation_warnings": [], "field_evidence": {}}

    def test_footer_evidence_and_arithmetic_gate_money_reread(self):
        data = self._data()
        with patch("app.core.pipeline.reread_fields", return_value={"total_amount": 15500}), \
             patch("app.core.pipeline._merge_evidence_fields",
                   side_effect=lambda d, values, _source: d["fields"].update(values)):
            with invocation_trace.capture():
                supplement.apply(data, "receipt", None, "unused.png", mode="ocr_text",
                                 ocr_text="未稅合計 14,000\n稅額 1,500\n總計 15,500", context={})
        self.assertEqual(data["fields"]["total_amount"], 15500)

    def test_missing_ocr_or_confirmed_field_prevents_adoption(self):
        no_ocr = self._data()
        with patch("app.core.pipeline.reread_fields", return_value={"total_amount": 15500}), \
             patch("app.core.pipeline._merge_evidence_fields",
                   side_effect=lambda d, values, _source: d["fields"].update(values)):
            with invocation_trace.capture():
                supplement.apply(no_ocr, "receipt", None, "unused.png", mode="ocr_text",
                                 ocr_text="", context={})
        self.assertEqual(no_ocr["fields"]["total_amount"], 8500)

        protected = self._data()
        with patch("app.core.pipeline.reread_fields", return_value={"total_amount": 15500}), \
             patch("app.core.pipeline._merge_evidence_fields",
                   side_effect=lambda d, values, _source: d["fields"].update(values)):
            with invocation_trace.capture():
                supplement.apply(protected, "receipt", None, "unused.png", mode="ocr_text",
                                 ocr_text="總計 15,500", context={}, confirmed_fields={"total_amount": True})
        self.assertEqual(protected["fields"]["total_amount"], 8500)


if __name__ == "__main__":
    unittest.main()
