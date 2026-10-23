from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import config
from app.core import model_coordinates, model_invocation, request_budget


class _OllamaChat:
    __module__ = "langchain_ollama.chat_models"

    def __init__(self, *, num_ctx: int = 8192) -> None:
        self.model = "qwen2.5vl:7b"
        self.base_url = "http://offline.test"
        self.num_ctx = num_ctx

    def model_copy(self, update=None):
        copied = _OllamaChat(num_ctx=self.num_ctx)
        copied.__dict__.update(self.__dict__)
        copied.__dict__.update(update or {})
        return copied


class BudgetProfileTests(unittest.TestCase):
    TEST_TMP_DIR = config.DATA_DIR / "test-tmp-plan"

    def setUp(self) -> None:
        self.TEST_TMP_DIR.mkdir(parents=True, exist_ok=True)
        request_budget._tokenizers.clear()

    def _identity(self, server_version="0.35.0") -> dict:
        return {
            "provider": "ollama",
            "backend_url": "http://offline.test",
            "server_version": server_version,
            "model": "qwen2.5vl:7b",
            "model_digest": "sha256:synthetic-model-digest",
        }

    def _show(self) -> dict:
        return {"template": request_budget.QWEN_TEMPLATE, "system": "synthetic system"}

    def _write_manifest(self, folder: Path) -> dict:
        folder.mkdir(parents=True, exist_ok=True)
        assets = {
            "tokenizer.json": b'{"model":{"vocab":{"a":0},"merges":[]},"added_tokens":[]}',
            "tokenizer_config.json": b'{"synthetic":true}',
        }
        for name, payload in assets.items():
            (folder / name).write_bytes(payload)
        show = self._show()
        profile = {
            **self._identity(),
            "version": request_budget.PROFILE_VERSION,
            "tokenizers_version": request_budget.TOKENIZERS_VERSION,
            "repository": request_budget.REPOSITORY,
            "revision": request_budget.REVISION,
            "files": {name: request_budget._sha(payload) for name, payload in assets.items()},
            "vocabulary_proof": {"verified_ids": 1, "backend_ids": 1, "merges": 0},
            "template": show["template"],
            "template_sha256": request_budget._sha(show["template"]),
            "default_system": show["system"],
            "processor": model_coordinates.CHECKED_PROCESSORS["0.35.0"],
            "input_preprocessing":model_coordinates.INPUT_PREPROCESSING,
            "template_extra_tokens": 64,
            "image_special_tokens": 16,
        }
        profile["profile_id"] = request_budget._sha(json.dumps(profile, sort_keys=True))
        (folder / "manifest.json").write_text(json.dumps(profile), encoding="utf-8")
        return profile

    def _runtime(self, folder: Path, *, identity=None, show=None):
        stack = patch.object(request_budget, "asset_directory", return_value=folder)
        stack.start()
        self.addCleanup(stack.stop)
        metadata = patch.object(
            request_budget, "_metadata",
            return_value=(identity or self._identity(), show or self._show()),
        )
        metadata.start()
        self.addCleanup(metadata.stop)
        tokenizer = patch("tokenizers.Tokenizer.from_file", return_value=object())
        tokenizer_mock = tokenizer.start()
        self.addCleanup(tokenizer.stop)
        return tokenizer_mock

    def _assert_not_ready(self, folder: Path, *, identity=None, show=None) -> None:
        self._runtime(folder, identity=identity, show=show)
        with self.assertRaises(request_budget.BudgetError) as caught:
            request_budget.runtime_profile(_OllamaChat())
        self.assertEqual(caught.exception.code, "budget_profile_not_ready")

    def test_runtime_rejects_missing_manifest_asset_hash_and_fixed_revision(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            root = Path(temporary)

            missing = root / "missing"
            missing.mkdir()
            self._assert_not_ready(missing)

            bad_hash = root / "bad-hash"
            self._write_manifest(bad_hash)
            (bad_hash / "tokenizer.json").write_bytes(b"changed bytes")
            self._assert_not_ready(bad_hash)

            wrong_revision = root / "wrong-revision"
            profile = self._write_manifest(wrong_revision)
            profile["revision"] = "main"
            profile["profile_id"] = request_budget._sha(json.dumps(
                {key: value for key, value in profile.items() if key != "profile_id"}, sort_keys=True
            ))
            (wrong_revision / "manifest.json").write_text(json.dumps(profile), encoding="utf-8")
            self._assert_not_ready(wrong_revision)

    def test_runtime_rejects_live_digest_server_and_template_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            folder = Path(temporary)
            self._write_manifest(folder)

            wrong_digest = self._identity()
            wrong_digest["model_digest"] = "sha256:changed"
            self._assert_not_ready(folder, identity=wrong_digest)

            wrong_version = self._identity()
            wrong_version["server_version"] = "0.40.4"
            self._assert_not_ready(folder, identity=wrong_version)

            wrong_template = self._show()
            wrong_template["template"] = "{{ changed template }}"
            self._assert_not_ready(folder, show=wrong_template)

    def test_valid_manifest_uses_sorted_profile_hash_and_loads_offline_tokenizer(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            folder = Path(temporary)
            expected = self._write_manifest(folder)
            load = self._runtime(folder)

            profile, tokenizer = request_budget.runtime_profile(_OllamaChat())

        self.assertEqual(profile["profile_id"], expected["profile_id"])
        load.assert_called_once_with(str(folder / "tokenizer.json"))
        self.assertIsNotNone(tokenizer)

    def test_verify_vocabulary_rejects_token_id_and_merge_mismatches(self) -> None:
        tokenizer = {
            "model": {"vocab": {"a": 0}, "merges": [["a", "b"]]},
            "added_tokens": [],
        }
        correct = {
            "tokenizer.ggml.pre": "qwen2",
            "tokenizer.ggml.tokens": ["a"],
            "tokenizer.ggml.merges": ["a b"],
        }
        self.assertEqual(request_budget.verify_vocabulary(tokenizer, correct)["verified_ids"], 1)

        with self.assertRaises(request_budget.BudgetError) as token_error:
            request_budget.verify_vocabulary(tokenizer, {**correct, "tokenizer.ggml.tokens": ["wrong"]})
        self.assertEqual(token_error.exception.code, "budget_profile_not_ready")

        with self.assertRaises(request_budget.BudgetError) as merge_error:
            request_budget.verify_vocabulary(tokenizer, {**correct, "tokenizer.ggml.merges": ["wrong"]})
        self.assertEqual(merge_error.exception.code, "budget_profile_not_ready")

    def test_prepare_fetches_only_two_files_from_the_pinned_revision(self) -> None:
        tokenizer_json = json.dumps({
            "model": {"vocab": {"a": 0}, "merges": []}, "added_tokens": [],
        }).encode()
        config_json = b'{"synthetic":true}'
        responses = [SimpleNamespace(content=tokenizer_json), SimpleNamespace(content=config_json)]
        for response in responses:
            response.raise_for_status = Mock()
        identity = self._identity()
        info = {
            "general.architecture": "qwen25vl",
            "qwen25vl.vision.patch_size": 14,
            "qwen25vl.vision.spatial_merge_size": 2,
            "tokenizer.ggml.pre": "qwen2",
            "tokenizer.ggml.tokens": ["a"],
            "tokenizer.ggml.merges": [],
        }
        show = {
            **self._show(),
            "model_info": info,
        }

        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            folder = Path(temporary)
            with (
                patch("requests.get", side_effect=responses) as get,
                patch.object(request_budget, "_metadata", return_value=(identity, show)) as metadata,
            ):
                profile = request_budget.prepare_profile(
                    "qwen2.5vl:7b", backend_url="http://offline.test", asset_dir=folder
                )

            prefix = f"https://huggingface.co/{request_budget.REPOSITORY}/resolve/{request_budget.REVISION}/"
            self.assertEqual(
                [call.args[0] for call in get.call_args_list],
                [prefix + "tokenizer.json", prefix + "tokenizer_config.json"],
            )
            self.assertEqual(get.call_count, 2)
            self.assertTrue(all("weights" not in call.args[0] for call in get.call_args_list))
            metadata.assert_called_once_with("qwen2.5vl:7b", "http://offline.test", verbose=True)
            profile_without_id = {key: value for key, value in profile.items() if key != "profile_id"}
            self.assertEqual(
                profile["profile_id"],
                request_budget._sha(json.dumps(profile_without_id, sort_keys=True)),
            )
            self.assertTrue((folder / "manifest.json").is_file())

    def test_0404_prepare_and_runtime_roundtrip_uses_checked_processor(self) -> None:
        tokenizer_json = json.dumps({
            "model": {"vocab": {"a": 0}, "merges": []}, "added_tokens": [],
        }).encode()
        config_json = b'{"synthetic":true}'
        responses = [SimpleNamespace(content=tokenizer_json), SimpleNamespace(content=config_json)]
        for response in responses:
            response.raise_for_status = Mock()
        identity = self._identity("0.40.4")
        info = {
            "general.architecture": "qwen25vl",
            "qwen25vl.vision.patch_size": 14,
            "qwen25vl.vision.spatial_merge_size": 2,
            "tokenizer.ggml.pre": "qwen2",
            "tokenizer.ggml.tokens": ["a"],
            "tokenizer.ggml.merges": [],
        }
        show = {**self._show(), "model_info": info}

        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            folder = Path(temporary)
            with (
                patch("requests.get", side_effect=responses) as get,
                patch.object(request_budget, "_metadata", return_value=(identity, show)),
            ):
                prepared = request_budget.prepare_profile(
                    "qwen2.5vl:7b", backend_url="http://offline.test", asset_dir=folder
                )

            self.assertEqual(get.call_count, 2)
            self.assertEqual(prepared["server_version"], "0.40.4")
            self.assertEqual(prepared["processor"], model_coordinates.CHECKED_PROCESSORS["0.40.4"])
            prepared_without_id = {key: value for key, value in prepared.items() if key != "profile_id"}
            self.assertEqual(
                prepared["profile_id"],
                request_budget._sha(json.dumps(prepared_without_id, sort_keys=True)),
            )

            self._runtime(folder, identity=identity, show=show)
            loaded, tokenizer = request_budget.runtime_profile(_OllamaChat())

        self.assertEqual(loaded["server_version"], "0.40.4")
        self.assertEqual(loaded["profile_id"], prepared["profile_id"])
        self.assertEqual(loaded["processor"], model_coordinates.CHECKED_PROCESSORS["0.40.4"])
        self.assertIsNotNone(tokenizer)

    def test_unknown_0405_prepare_preserves_existing_manifest_and_cached_assets(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            folder = Path(temporary)
            original_profile = self._write_manifest(folder)
            original_files = {path.name: path.read_bytes() for path in folder.iterdir()}
            identity = self._identity("0.40.5")
            info = {
                "general.architecture": "qwen25vl",
                "qwen25vl.vision.patch_size": 14,
                "qwen25vl.vision.spatial_merge_size": 2,
                "tokenizer.ggml.pre": "qwen2",
                "tokenizer.ggml.tokens": ["a"],
                "tokenizer.ggml.merges": [],
            }
            show = {**self._show(), "model_info": info}
            with (
                patch("requests.get", side_effect=AssertionError("cached assets should be reused")) as get,
                patch.object(request_budget, "_metadata", return_value=(identity, show)),
                self.assertRaises(request_budget.BudgetError) as caught,
            ):
                request_budget.prepare_profile(
                    "qwen2.5vl:7b", backend_url="http://offline.test", asset_dir=folder
                )

            self.assertEqual(caught.exception.code, "budget_profile_not_ready")
            get.assert_not_called()
            self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original_files)
            self.assertEqual(json.loads((folder / "manifest.json").read_text(encoding="utf-8")), original_profile)

    def test_preflight_rejects_1024_context_without_downgrading(self) -> None:
        profile = {
            "profile_id": "offline-profile",
            "processor": model_coordinates.CHECKED_PROCESSORS["0.35.0"],
        }
        tokenizer = SimpleNamespace(encode=lambda text, **kwargs: SimpleNamespace(ids=list(text)))
        with patch.object(request_budget, "runtime_profile", return_value=(profile, tokenizer)):
            with self.assertRaises(request_budget.BudgetError) as caught:
                request_budget.preflight(
                    _OllamaChat(num_ctx=1024), [{"role": "user", "content": "synthetic"}], None, 2048
                )
        self.assertEqual(caught.exception.code, "budget_profile_not_ready")

    def test_version_one_assets_remain_unchanged_across_context_policy_upgrade(self):
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            folder = Path(temporary)
            old = self._write_manifest(folder)
            original = {path.name:path.read_bytes() for path in folder.iterdir()}
            self._runtime(folder)
            self.assertEqual(old["version"], 1)
            self.assertEqual(request_budget.PROFILE_VERSION, 1)
            self.assertEqual(request_budget.VERSION, 2)
            with patch("requests.get", side_effect=AssertionError("must not download")) as download:
                for context in config.SUPPORTED_OLLAMA_CONTEXTS:
                    with self.subTest(context=context), patch.object(config, "OLLAMA_NUM_CTX", context):
                        chat = _OllamaChat(num_ctx=context)
                        self.assertEqual(request_budget.resolve_context(chat), context)
                        loaded, _ = request_budget.runtime_profile(chat)
                        self.assertEqual(loaded["profile_id"], old["profile_id"])
                download.assert_not_called()
            self.assertEqual({path.name:path.read_bytes() for path in folder.iterdir()}, original)

    def test_oversized_second_round_repair_fragment_is_omitted_but_original_stays(self) -> None:
        chat = _OllamaChat()
        original = "ORIGINAL DOCUMENT EVIDENCE: preserve this text"
        first_output = '{"bad":"' + ("x" * 5000) + '"}'
        requests = []
        budgets = []

        def preflight(_chat, request, _schema, _limit, **_kwargs):
            requests.append(deepcopy(request))
            if len(requests) == 2:
                raise request_budget.BudgetError("context_budget_exhausted", "synthetic overflow")
            return request, {"fits": True, "input_sha256": str(len(requests))}

        def invoke(_chat, _request):
            return first_output if len(budgets) == 0 else '{"ok":true}'

        def validate(value):
            budgets.append(value)
            if value.get("ok") is not True:
                raise ValueError("synthetic first response invalid")
            return value

        from app.core import invocation_trace
        with patch.object(request_budget, "preflight", side_effect=preflight), invocation_trace.capture() as trace:
            result = model_invocation.invoke_structured(
                chat, [{"role": "user", "content": original}], validate,
                invoke=invoke, schema={"type": "object"},
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(requests), 3)
        repair = json.loads(requests[2][-1]["content"])
        self.assertEqual(repair["previous_output_raw"], "")
        self.assertTrue(repair["previous_output_truncated"])
        self.assertTrue(any(original in str(message) for message in requests[2]))
        self.assertTrue(any(record.get("kind") == "repair_fragment_omitted" for record in trace))


if __name__ == "__main__":
    unittest.main()
