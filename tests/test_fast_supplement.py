"""Offline contracts for deadline-limited supplemental reads."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from app.core import invocation_trace, model_invocation, supplement


def _line(text, box, score=.99):
    x0, y0, x1, y1 = box
    return {"text": text, "score": score,
            "box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def _data(total_amount=None):
    return {"doc_type": "收據", "doc_type_key": "receipt",
            "fields": {"total_amount": total_amount, "currency": "TWD"},
            "items": [], "entries": [], "validation_warnings": [],
            "field_evidence": {}, "boxes": {}}


def _tempdir():
    root = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


def _image_fixture(test, *, score=.99):
    temporary = _tempdir()
    test.addCleanup(temporary.cleanup)
    image_path = Path(temporary.name) / "receipt.png"
    Image.new("RGB", (1000, 800), "white").save(image_path)
    lines = [_line("應付金額", (100, 150, 260, 175), score),
             _line("新臺幣 100 元", (330, 150, 520, 175), score)]
    context = {"ocr_lines": lines, "ocr_enabled": True}
    return str(image_path), context, lines


class LimitedModelInvocationTests(unittest.TestCase):
    def test_limited_call_disables_transport_retries(self):
        invoke = Mock(side_effect=RuntimeError("temporary transport failure"))
        chat = SimpleNamespace(invoke=invoke)
        with (model_invocation.limited(deadline=9999999999),
              patch("app.core.model_invocation.time.sleep", side_effect=AssertionError("must not retry"))):
            with self.assertRaisesRegex(RuntimeError, "temporary transport failure"):
                model_invocation._invoke_text(chat, "offline request")
        invoke.assert_called_once()

    def test_limited_structured_call_disables_semantic_and_format_retries(self):
        from langchain_ollama import ChatOllama
        chat = ChatOllama(model="offline", base_url="http://127.0.0.1:1", num_ctx=8192)
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        preflight = lambda _chat, messages, _format, _limit, **_kwargs: (messages, {"estimated_input_tokens": 1})

        format_call = Mock(side_effect=ValueError("unsupported format"))
        with (patch.object(model_invocation.request_budget, "preflight", side_effect=preflight),
              model_invocation.limited(deadline=9999999999)):
            with self.assertRaisesRegex(ValueError, "unsupported format"):
                model_invocation.invoke_structured(chat, "offline", lambda value: value,
                                                   invoke=format_call, schema=schema)
        format_call.assert_called_once()

        semantic_call = Mock(return_value="{\"ok\": false}")
        with (patch.object(model_invocation.request_budget, "preflight", side_effect=preflight),
              model_invocation.limited(deadline=9999999999)):
            with self.assertRaisesRegex(ValueError, "output invalid after 1 rounds"):
                model_invocation.invoke_structured(chat, "offline", lambda _value: (_ for _ in ()).throw(ValueError("bad contract")),
                                                   invoke=semantic_call)
        semantic_call.assert_called_once()

    def test_expired_deadline_raises_before_work(self):
        with (model_invocation.limited(deadline=10),
              patch("app.core.model_invocation.time.perf_counter", return_value=10)):
            with self.assertRaises(TimeoutError):
                model_invocation.check_deadline()

    def test_timeout_bound_uses_sdk_timeout_and_preserves_ollama_options(self):
        from langchain_ollama import ChatOllama
        chat = ChatOllama(model="offline", base_url="http://127.0.0.1:1", num_ctx=8192,
                          seed=23, num_predict=321, format="json",
                          client_kwargs={"timeout": 180}, sync_client_kwargs={"timeout": 180})
        with (model_invocation.limited(deadline=100),
              patch("app.core.model_invocation.time.perf_counter", side_effect=[10, 20])):
            bounded = model_invocation._timeout_bound(chat)
        self.assertEqual(bounded.client_kwargs["timeout"], 80)
        self.assertEqual(bounded.sync_client_kwargs["timeout"], 80)
        self.assertEqual((bounded.num_ctx, bounded.seed, bounded.num_predict, bounded.format),
                         (8192, 23, 321, "json"))
        self.assertEqual(chat.client_kwargs["timeout"], 180)

    def test_timeout_bound_sets_chatopenai_remaining_timeout_and_preserves_options(self):
        from langchain_openai import ChatOpenAI
        chat = ChatOpenAI(model="offline", api_key="offline", base_url="http://127.0.0.1:1/v1",
                          timeout=180, max_retries=4, seed=23, max_tokens=321,
                          model_kwargs={"response_format": {"type": "json_object"}})
        with (model_invocation.limited(deadline=100),
              patch("app.core.model_invocation.time.perf_counter", side_effect=[10, 20])):
            bounded = model_invocation._timeout_bound(chat)
        self.assertEqual(bounded.request_timeout, 80)
        self.assertEqual(bounded.max_retries, 0)
        self.assertEqual((bounded.seed, bounded.max_tokens), (23, 321))
        self.assertEqual(bounded.model_kwargs["response_format"], {"type": "json_object"})
        self.assertEqual((chat.request_timeout, chat.max_retries), (180, 4))


class FastSupplementTests(unittest.TestCase):
    def test_fast_mode_without_safe_roi_keeps_first_pass_and_skips_work(self):
        image_path, context, _ = _image_fixture(self)
        data = _data()
        context.update(fast_mode=True, ocr_lines=[])
        with (patch("app.core.model_factory.get_ocr") as get_ocr,
              patch("app.core.pipeline.reread_fields") as reread,
              patch("app.core.pipeline.reread_document") as reread_document,
              invocation_trace.capture() as trace):
            supplement.apply(data, "receipt", object(), image_path, mode="image",
                             ocr_text="應付金額", context=context)
        get_ocr.assert_not_called()
        reread.assert_not_called()
        reread_document.assert_not_called()
        self.assertIsNone(data["fields"]["total_amount"])
        self.assertTrue(any(event.get("reason") == "fast_mode_requires_safe_roi" for event in trace))

    def test_fast_mode_uses_at_most_one_roi_ocr_and_one_model_call(self):
        image_path, context, _ = _image_fixture(self, score=.2)
        context["fast_mode"] = True

        class FakeOcr:
            def __init__(self):
                self.recognize = Mock(return_value=[_line("應付金額 100", (50, 50, 220, 80), .99)])

            @staticmethod
            def plain_text(lines):
                return "\n".join(line["text"] for line in lines)

        ocr = FakeOcr()
        data = _data()
        conditions = {"low_text_contrast": False, "skew_degrees": 0}
        with (patch("app.core.settings_store.get_settings", return_value={"ocr": "paddle/pp-ocrv5"}),
              patch("app.core.settings_store.parse_setting", return_value=("paddle", None)),
              patch("app.core.model_factory.get_ocr", return_value=ocr),
              patch("app.core.image_evidence.enhancement", return_value=(None, conditions)),
              patch("app.core.pipeline.reread_fields", return_value={}) as reread,
              patch("app.core.pipeline.reread_document") as reread_document,
              invocation_trace.capture() as trace):
            supplement.apply(data, "receipt", object(), image_path, mode="image",
                             ocr_text="應付金額 新臺幣 100 元", context=context)
        ocr.recognize.assert_called_once()
        reread.assert_called_once()
        reread_document.assert_not_called()
        evidence = next(event for event in trace if event.get("kind") == "supplement_evidence")
        self.assertEqual(evidence["ocr_requests"], 1)
        self.assertEqual(context["supplement_ocr_source"], "native_roi_ocr")

    def test_deadline_exhaustion_preserves_first_pass_result(self):
        image_path, context, _ = _image_fixture(self, score=.2)
        context["fast_mode"] = True
        data = _data(total_amount=90)

        class FakeOcr:
            recognize = Mock(return_value=[_line("應付金額 100", (50, 50, 220, 80), .99)])

            @staticmethod
            def plain_text(lines):
                return "\n".join(line["text"] for line in lines)

        ocr = FakeOcr()
        conditions = {"low_text_contrast": False, "skew_degrees": 0}
        with (patch("app.core.settings_store.get_settings", return_value={"ocr": "paddle/pp-ocrv5"}),
              patch("app.core.settings_store.parse_setting", return_value=("paddle", None)),
              patch("app.core.model_factory.get_ocr", return_value=ocr),
              patch("app.core.image_evidence.enhancement", return_value=(None, conditions)),
              patch("app.core.model_invocation.check_deadline", side_effect=[None, TimeoutError("budget expired"), TimeoutError("budget expired")]),
              patch("app.core.pipeline.reread_fields") as reread,
              invocation_trace.capture()):
            supplement.apply(data, "receipt", object(), image_path, mode="image",
                             ocr_text="應付金額 100", context=context)
        ocr.recognize.assert_called_once()
        reread.assert_not_called()
        self.assertEqual(data["fields"]["total_amount"], 90)
        self.assertIn("保留首讀結果", "".join(data["validation_warnings"]))

    def test_legacy_supplement_does_not_enter_the_new_limit(self):
        image_path, context, _ = _image_fixture(self)
        data = _data()
        conditions = {"low_text_contrast": False, "skew_degrees": 0}
        with (patch("app.core.image_evidence.enhancement", return_value=(None, conditions)),
              patch("app.core.pipeline.reread_fields", return_value={}) as reread,
              patch.object(model_invocation, "limited", side_effect=AssertionError("legacy flow must remain unlimited")),
              invocation_trace.capture()):
            supplement.apply(data, "receipt", object(), image_path, mode="image",
                             ocr_text="應付金額 新臺幣 100 元", context=context)
        reread.assert_called_once()
        self.assertNotIn("supplement_deadline", context)


if __name__ == "__main__":
    unittest.main()
