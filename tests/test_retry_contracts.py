"""Offline regressions for structured retries and latest validation attempts."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core import invocation_trace, model_invocation
from app import validation


class StructuredRetryContractTests(unittest.TestCase):
    def test_large_first_response_is_bounded_in_repair_and_kept_in_trace(self):
        requests = []
        oversized = '{"bad":"' + ("x" * 8000) + '"}'

        def invoke(_chat, request):
            requests.append(request)
            return oversized if len(requests) == 1 else '{"ok":true}'

        def validate(value):
            if value.get("ok") is not True:
                raise ValueError("ok must be true")
            return value

        with invocation_trace.capture() as trace:
            result = model_invocation.invoke_structured(object(), "synthetic", validate, invoke=invoke)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(requests), 2)
        repair = json.loads(requests[1][-1]["content"])
        self.assertEqual(len(repair["previous_output_raw"]), 1024)
        self.assertTrue(repair["previous_output_truncated"])
        full_response = next(record for record in trace
                             if record.get("kind") == "structured_response" and record.get("round") == 1)
        self.assertEqual(full_response["raw"], oversized)

    def test_context_http_400_is_not_retried(self):
        class ContextError(Exception):
            status_code = 400

        chat = SimpleNamespace(invoke=unittest.mock.Mock(
            side_effect=ContextError("request exceeds the available context size")))
        with patch("time.sleep") as sleep:
            with self.assertRaisesRegex(ContextError, "available context size"):
                model_invocation._invoke_text(chat, "synthetic", tries=3)
        self.assertEqual(chat.invoke.call_count, 1)
        sleep.assert_not_called()

    def test_ollama_tasks_bind_output_limit_schema_and_record_usage(self):
        from langchain_ollama import ChatOllama
        from ollama import ChatResponse, Message
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        chat = ChatOllama(model="offline-test", base_url="http://127.0.0.1:1", temperature=0,
                          num_ctx=8192, seed=42, num_predict=128, top_p=.8, repeat_penalty=1.1)
        response = ChatResponse(model="offline-test", message=Message(role="assistant",content='{"ok":true}'),
                                done=True, done_reason="stop", prompt_eval_count=12, eval_count=4)
        # Keep the installed SDK's method signature while replacing only I/O.
        # This catches unsupported top-level kwargs that permissive model fakes miss.
        for task, expected_limit in (("layout", 2048), ("route", 2048), ("extract", 4096), ("supplement", 2048)):
            with self.subTest(task=task):
                with patch.object(chat._client, "chat", autospec=True, return_value=iter([response])) as client_chat, \
                     invocation_trace.capture() as trace:
                    result = model_invocation.invoke_structured(
                        chat, "synthetic", lambda value: value, task=task, schema=schema)
                self.assertEqual(result, {"ok": True})
                sent = client_chat.call_args.kwargs
                self.assertEqual(sent["format"],schema)
                self.assertNotIn("num_predict",sent)
                self.assertEqual(sent["options"]["num_predict"],expected_limit)
                self.assertEqual(sent["options"]["num_ctx"],8192)
                self.assertEqual(sent["options"]["seed"],42)
                self.assertEqual(sent["options"]["temperature"],0)
                self.assertEqual(sent["options"]["top_p"],.8)
                self.assertEqual(sent["options"]["repeat_penalty"],1.1)
                self.assertEqual(chat.num_predict,128)
                call = next(record for record in trace if record.get("kind") == "model_call")
                self.assertEqual(call["parameters"]["format"], schema)
                self.assertEqual(call["parameters"]["num_predict"], expected_limit)
                self.assertEqual(call["response_metadata"]["done_reason"], "stop")
                self.assertEqual(call["usage_metadata"]["output_tokens"], 4)

    def test_output_limit_preserves_existing_binding_and_explicit_options(self):
        from langchain_ollama import ChatOllama
        from ollama import Options
        chat = ChatOllama(model="offline-test", base_url="http://127.0.0.1:1", num_ctx=8192, num_predict=128)
        for options in ({"seed":42,"temperature":0,"num_predict":99}, Options(seed=42, temperature=0, num_predict=99)):
            with self.subTest(options=type(options).__name__):
                original = chat.bind(format="json", options=options).with_config({"tags":["offline"]})
                bounded = model_invocation._output_bound(original,2048)
                self.assertIsNot(bounded,original)
                self.assertIsNot(bounded.bound,chat)
                self.assertEqual(bounded.config,original.config)
                self.assertEqual(bounded.kwargs["format"],"json")
                self.assertEqual(bounded.kwargs["options"],{"seed":42,"temperature":0,"num_predict":2048})
                self.assertEqual(bounded.bound.num_ctx,8192)
                self.assertEqual(chat.num_predict,128)
                self.assertEqual(original.kwargs["options"],options)
                self.assertEqual(options["num_predict"] if isinstance(options,dict) else options.num_predict,99)

    def test_unbound_ollama_output_limit_does_not_mutate_model(self):
        from langchain_ollama import ChatOllama
        chat = ChatOllama(model="offline-test", base_url="http://127.0.0.1:1", num_ctx=8192)
        bounded = model_invocation._output_bound(chat,4096)
        self.assertIsNot(bounded,chat)
        self.assertEqual(bounded.num_predict,4096)
        self.assertEqual(bounded.num_ctx,8192)
        self.assertIsNone(chat.num_predict)


class LatestValidationRowsTests(unittest.TestCase):
    def test_excludes_warmup_selects_attempt_and_rowid_tie_per_combo_repeat(self):
        rows = [
            {"page_id": "p1", "combo_id": "a", "repeat_no": 1, "attempt": 1,
             "_rowid": 1, "warmup": 0, "id": "first"},
            {"page_id": "p1", "combo_id": "a", "repeat_no": 1, "attempt": 2,
             "_rowid": 2, "warmup": 0, "id": "tie-lower"},
            {"page_id": "p1", "combo_id": "a", "repeat_no": 1, "attempt": 2,
             "_rowid": 3, "warmup": 0, "id": "tie-winner"},
            {"page_id": "p1", "combo_id": "b", "repeat_no": 1, "attempt": 1,
             "_rowid": 4, "warmup": 0, "id": "other-combo"},
            {"page_id": "p1", "combo_id": "a", "repeat_no": 2, "attempt": 1,
             "_rowid": 5, "warmup": 0, "id": "other-repeat"},
            {"page_id": "p1", "combo_id": "a", "repeat_no": 1, "attempt": 9,
             "_rowid": 6, "warmup": 1, "id": "warmup"},
        ]

        selected = validation._latest_rows(rows)
        by_key = {(row["page_id"], row["combo_id"], row["repeat_no"]): row["id"] for row in selected}
        self.assertEqual(by_key, {
            ("p1", "a", 1): "tie-winner",
            ("p1", "b", 1): "other-combo",
            ("p1", "a", 2): "other-repeat",
        })


if __name__ == "__main__":
    unittest.main()
