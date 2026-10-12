"""Offline regressions for configured Ollama context upgrades."""
from __future__ import annotations

import unittest
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from app import config
from app.core import model_coordinates, model_invocation, request_budget
from PIL import Image


class _Encoding:
    def __init__(self, text: str) -> None:
        self.ids = list(range(len(text)))


class _Tokenizer:
    def encode(self, text: str, add_special_tokens: bool = False) -> _Encoding:
        return _Encoding(text)


class _ChatOllama:
    __module__ = "langchain_ollama.chat_models"

    def __init__(self, context: int, options=None) -> None:
        self.model = "offline-test"
        self.base_url = "http://offline.test"
        self.num_ctx = context
        self.kwargs = {"options": options} if options is not None else {}


class ContextUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = {
            "profile_id": "synthetic-profile",
            "default_system": "",
            "template_extra_tokens": 64,
            "image_special_tokens": 16,
            "processor": model_coordinates.CHECKED_PROCESSORS["0.35.0"],
        }
        self.tokenizer = _Tokenizer()

    def runtime(self):
        return patch.object(request_budget, "runtime_profile", return_value=(self.profile, self.tokenizer))

    def test_supported_contexts_and_strict_validation(self):
        self.assertEqual(config.SUPPORTED_OLLAMA_CONTEXTS, (8192, 12288, 16384))
        for valid in config.SUPPORTED_OLLAMA_CONTEXTS:
            with self.subTest(value=valid):
                self.assertEqual(config.validate_ollama_context(valid), valid)
        for invalid in (0, -1, 8191, 12289, 16384.0, "16384", True, None):
            with self.subTest(value=invalid), self.assertRaises((ValueError, TypeError)):
                config.validate_ollama_context(invalid)
        self.assertEqual(config.get_ollama_context(), config.OLLAMA_NUM_CTX)

    def test_count_observes_all_contexts_and_both_output_boundaries(self):
        for context in config.SUPPORTED_OLLAMA_CONTEXTS:
            for output_limit in (2048, 4096):
                with self.subTest(context=context, output_limit=output_limit):
                    target = context - output_limit - request_budget.SAFETY
                    message = {"role": "user", "content": ""}
                    baseline = request_budget.count([message], None, [], self.profile, self.tokenizer,
                                                    output_limit, context)
                    # The synthetic tokenizer is one token per character, so this
                    # adjustment places the estimated input exactly on the limit.
                    message["content"] = "x" * (target - baseline["estimated_input_tokens"])
                    exact = request_budget.count([message], None, [], self.profile, self.tokenizer,
                                                 output_limit, context)
                    self.assertEqual(exact["estimated_input_tokens"] + output_limit + request_budget.SAFETY, context)
                    self.assertTrue(exact["fits"])
                    message["content"] += "x"
                    over = request_budget.count([message], None, [], self.profile, self.tokenizer,
                                                output_limit, context)
                    self.assertFalse(over["fits"])

    def test_saved_input_shapes_reject_8192_and_fit_larger_contexts(self):
        # These are synthetic one-token-per-character saved input shapes. Output
        # and safety reserves are both 4096 and 512 respectively.
        for saved_tokens in (3671, 3791, 4500):
            for context, expected in ((8192, False), (12288, True), (16384, True)):
                with self.subTest(saved_tokens=saved_tokens, context=context):
                    message = {"role": "user", "content": "x" * (saved_tokens - 64 - len("<|im_start|>user\n<|im_end|>\n<|im_start|>assistant\n"))}
                    counted = request_budget.count([message], None, [], self.profile, self.tokenizer,
                                                   4096, context)
                    self.assertEqual(counted["estimated_input_tokens"], saved_tokens)
                    self.assertEqual(counted["fits"], expected)

    def test_preflight_rejects_unsupported_and_unbound_or_conflicting_context_before_profile(self):
        messages = [{"role": "user", "content": "synthetic"}]
        for configured, model_context, options_context in (
            (10000, 10000, None),
            (12288, 8192, None),
            (12288, 12288, 8192),
            (12288, 12288, 16384),
        ):
            with self.subTest(configured=configured, model=model_context, options=options_context):
                chat = _ChatOllama(model_context, {"num_ctx": options_context} if options_context else None)
                with patch.object(config, "OLLAMA_NUM_CTX", configured), self.runtime() as runtime, \
                     self.assertRaises(request_budget.BudgetError) as caught:
                    request_budget.preflight(chat, messages, None, 2048)
                self.assertEqual(caught.exception.code, "budget_profile_not_ready")
                runtime.assert_not_called()

    def test_profile_resolver_requires_both_model_and_binding_context(self):
        resolver = request_budget.resolve_context
        configured = 16384
        with patch.object(config, "OLLAMA_NUM_CTX", configured):
            self.assertEqual(resolver(_ChatOllama(configured)), configured)
            for chat in (_ChatOllama(8192), _ChatOllama(configured, {"num_ctx": 8192}),
                         _ChatOllama(configured, {"num_ctx": 12288})):
                with self.subTest(chat=chat.__dict__), self.assertRaises(request_budget.BudgetError) as caught:
                    resolver(chat)
                self.assertEqual(caught.exception.code, "budget_profile_not_ready")

    def test_status_reports_policy_but_does_not_claim_hardware_verification(self):
        with patch.object(request_budget, "runtime_profile", return_value=(self.profile, self.tokenizer)):
            status = request_budget.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["configured_context"], config.get_ollama_context())
        self.assertEqual(tuple(status["supported_contexts"]), config.SUPPORTED_OLLAMA_CONTEXTS)
        self.assertIn("input_limits", status)
        self.assertEqual(status["policy_version"], request_budget.VERSION)
        self.assertFalse(status["hardware_verified"])

    def test_context_and_output_options_at_sdk_top_level_are_rejected_before_profile(self):
        for key in ("num_ctx", "num_predict"):
            with self.subTest(key=key), patch.object(config, "OLLAMA_NUM_CTX", 16384):
                chat = _ChatOllama(16384)
                chat.kwargs = {key:16384}
                with self.runtime() as runtime, self.assertRaises(request_budget.BudgetError):
                    request_budget.preflight(chat, [{"role":"user","content":"synthetic"}],None,2048)
                runtime.assert_not_called()

    def test_context_change_changes_execution_version_hash(self):
        from app.core import model_invocation as invocation
        from app import validation

        current = config.get_ollama_context()
        alternate = next(value for value in config.SUPPORTED_OLLAMA_CONTEXTS if value != current)
        with patch.object(config, "OLLAMA_NUM_CTX", current):
            saved = invocation.source_versions()
        with patch.object(config, "OLLAMA_NUM_CTX", alternate):
            changed = invocation.source_versions()
            self.assertNotEqual(saved["execution_version_sha256"], changed["execution_version_sha256"])
            self.assertFalse(invocation.same_execution_version(saved))
            payload = {"worker_protocol": validation.WORKER_PROTOCOL,
                       "parent_build": validation.LOADED_BUILD,
                       "execution_version_sha256": saved["execution_version_sha256"]}
            with self.assertRaisesRegex(ValueError, "批次版本不一致"):
                validation._handshake(payload)

    def test_schema_fallback_and_second_round_keep_configured_context_options(self):
        from langchain_ollama import ChatOllama
        from ollama import ChatResponse, Message

        response = lambda value: ChatResponse(model="offline-test", message=Message(role="assistant", content=value), done=True)

        def validate(data):
            if data.get("ok") is not True:
                raise ValueError("ok must be true")
            return data

        for context in config.SUPPORTED_OLLAMA_CONTEXTS:
            with self.subTest(context=context):
                chat = ChatOllama(model="offline-test", base_url="http://127.0.0.1:1", num_ctx=context)
                requests = [
                    ValueError("unsupported schema"),
                    iter([response('{"ok":false}')]),
                    iter([response('{"ok":true}')]),
                ]
                with patch.object(config, "OLLAMA_NUM_CTX", context), \
                     patch.object(request_budget, "runtime_profile", return_value=(self.profile, self.tokenizer)), \
                     patch.object(chat._client, "chat", autospec=True, side_effect=requests) as client_chat:
                    result = model_invocation.invoke_structured(
                        chat, "synthetic", validate, schema={"type": "object"}, task="layout")
                self.assertEqual(result, {"ok": True})
                self.assertEqual(client_chat.call_count, 3)
                for call in client_chat.call_args_list:
                    sent = call.kwargs
                    self.assertNotIn("num_ctx", sent)
                    self.assertEqual(sent["options"]["num_ctx"], context)
                    self.assertEqual(sent["options"]["num_predict"], model_invocation.OUTPUT_LIMITS["layout"])

    def test_each_structured_task_sends_context_inside_sdk_options(self):
        from langchain_ollama import ChatOllama
        from ollama import ChatResponse, Message

        response = ChatResponse(model="offline-test", message=Message(role="assistant", content='{"ok":true}'), done=True)
        for context in config.SUPPORTED_OLLAMA_CONTEXTS:
            with self.subTest(context=context), patch.object(config, "OLLAMA_NUM_CTX", context), \
                 patch.object(request_budget, "runtime_profile", return_value=(self.profile, self.tokenizer)):
                for task, output_limit in model_invocation.OUTPUT_LIMITS.items():
                    with self.subTest(task=task):
                        chat = ChatOllama(model="offline-test", base_url="http://127.0.0.1:1", num_ctx=context)
                        with patch.object(chat._client, "chat", autospec=True, return_value=iter([response])) as client_chat:
                            result = model_invocation.invoke_structured(
                                chat, "synthetic", lambda data: data, schema={"type": "object"}, task=task)
                        self.assertEqual(result, {"ok": True})
                        sent = client_chat.call_args.kwargs
                        self.assertNotIn("num_ctx", sent)
                        self.assertEqual(sent["options"]["num_ctx"], context)
                        self.assertEqual(sent["options"]["num_predict"], output_limit)

    def test_16384_context_keeps_synthetic_1653x2339_image_at_original_size(self):
        context = 16384
        temporary_root = config.DATA_DIR / "test-tmp-plan"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            source = Path(directory) / "synthetic-source.png"
            prepared = Path(directory) / "synthetic-input.png"
            Image.new("RGB", (1653, 2339), color=(91, 117, 143)).save(source, format="PNG")
            original_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            prepared.write_bytes(source.read_bytes())
            metadata = {
                "input_path": str(prepared),
                "budget_source_path": str(source),
                "input_size": [1653, 2339],
                "source_size": [1653, 2339],
                "preprocessing": {"source_to_input": [[1, 0, 0], [0, 1, 0]]},
            }
            messages = [{"role": "user", "content": [
                {"type": "text", "text": "Synthetic page for budget accounting."},
                request_budget.image_part(metadata),
            ]}]
            with patch.object(config, "OLLAMA_NUM_CTX", context), \
                 patch.object(request_budget, "runtime_profile", return_value=(self.profile, self.tokenizer)):
                _, budget = request_budget.preflight(_ChatOllama(context), messages, None, 4096)

            self.assertTrue(budget["fits"])
            self.assertFalse(budget["downscaled"])
            self.assertEqual(budget["input_sizes"], [[1653, 2339]])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_digest)


if __name__ == "__main__":
    unittest.main()
