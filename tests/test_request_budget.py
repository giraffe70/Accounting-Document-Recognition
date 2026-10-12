from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import config
from app.core import model_coordinates, request_budget


class _Encoding:
    def __init__(self, text: str) -> None:
        # Deliberately simple offline stand-in: one token per character.
        self.ids = list(range(len(text)))


class _Tokenizer:
    def encode(self, text: str, add_special_tokens: bool = False) -> _Encoding:
        return _Encoding(text)


class _ChatOllama:
    __module__ = "langchain_ollama.chat_models"

    def __init__(self, num_ctx: int = 8192) -> None:
        self.model = "qwen2.5vl:7b"
        self.base_url = "http://offline.test"
        self.num_ctx = num_ctx


class RequestBudgetTests(unittest.TestCase):
    TEST_TMP_DIR = config.DATA_DIR / "test-tmp-plan"

    def setUp(self) -> None:
        context = patch.object(config, "OLLAMA_NUM_CTX", 8192)
        context.start()
        self.addCleanup(context.stop)
        self.TEST_TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.profile = {
            "profile_id": "offline-profile",
            "default_system": "",
            "template_extra_tokens": 64,
            "image_special_tokens": 16,
            "processor": model_coordinates.CHECKED_PROCESSORS["0.35.0"],
        }
        self.tokenizer = _Tokenizer()

    def _runtime(self):
        return patch.object(request_budget, "runtime_profile", return_value=(self.profile, self.tokenizer))

    def _image_messages(self, directory: Path, size: tuple[int, int]):
        source = directory / "synthetic-original.png"
        prepared = directory / "prepared-input.png"
        Image.new("RGB", size, color=(91, 117, 143)).save(source, format="PNG")
        source_bytes = source.read_bytes()
        prepared.write_bytes(source_bytes)
        context = {
            "input_path": str(prepared),
            "budget_source_path": str(source),
            "input_size": list(size),
            "source_size": list(size),
            "preprocessing": {"source_to_input": [[1, 0, 0], [0, 1, 0]]},
        }
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "Review this synthetic page."},
            request_budget.image_part(context),
        ]}]
        return messages, source, prepared, source_bytes

    def test_visual_frame_accounts_for_checked_qwen_grid(self) -> None:
        frame = model_coordinates.visual_frame(
            (1653, 2339), self.profile["processor"]
        )

        self.assertEqual(frame["visual_size"], [1484, 2128])
        self.assertEqual(frame["visual_size"][0] * frame["visual_size"][1] // 28**2, 4028)

    def test_preflight_reserves_output_and_safety_for_both_output_limits(self) -> None:
        chat = _ChatOllama()
        messages = [{"role": "user", "content": "A short synthetic request."}]

        with self._runtime():
            for output_limit in (2048, 4096):
                with self.subTest(output_limit=output_limit):
                    _, budget = request_budget.preflight(chat, messages, None, output_limit)
                    self.assertEqual(budget["context_limit"], 8192)
                    self.assertEqual(budget["output_limit"], output_limit)
                    self.assertEqual(budget["safety_tokens"], 512)
                    self.assertEqual(
                        budget["fits"],
                        budget["estimated_input_tokens"] + output_limit + 512 <= 8192,
                    )

    def test_automatic_thumbnail_preserves_original_and_matches_artifact_hash(self) -> None:
        chat = _ChatOllama()
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            messages, source, prepared, original_bytes = self._image_messages(
                Path(temporary), (1653, 2339)
            )
            with self._runtime():
                result, budget = request_budget.preflight(chat, messages, None, 4096)

            self.assertTrue(budget["downscaled"])
            self.assertEqual(budget["original_input_sizes"], [[1653, 2339]])
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(prepared.read_bytes(), original_bytes)
            sent_path = Path(messages[0]["content"][1]["_budget_image"]["input_path"])
            image_part = next(
                part for part in result[0]["content"] if part.get("type") == "image_url"
            )
            encoded = image_part["image_url"]["url"].split(",", 1)[1]
            sent_bytes = base64.b64decode(encoded)
            digest = hashlib.sha256(sent_bytes).hexdigest()
            self.assertEqual(sent_path.read_bytes(), sent_bytes)
            self.assertEqual(hashlib.sha256(sent_path.read_bytes()).hexdigest(), digest)
            self.assertEqual(budget["image_sha256"], [digest])

    def test_minimum_thumbnail_grid_overflow_raises_budget_error(self) -> None:
        chat = _ChatOllama()
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            messages, _, _, _ = self._image_messages(Path(temporary), (1200, 1000))
            messages[0]["content"][0]["text"] = "x" * 9000

            with self._runtime(), self.assertRaises(request_budget.BudgetError) as caught:
                request_budget.preflight(chat, messages, None, 4096)

        self.assertEqual(caught.exception.code, "context_budget_exhausted")
        self.assertEqual(caught.exception.details["candidates"][-1]["max_edge"], 896)
        self.assertFalse(caught.exception.details["candidates"][-1]["fits"])

    def test_repair_thumbnail_retains_each_previously_transmitted_artifact(self):
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            messages, source, _, original = self._image_messages(Path(temporary),(1653,2339))
            messages[0]["content"][0]["text"] = "x" * 900
            with self._runtime():
                _, first = request_budget.preflight(_ChatOllama(),messages,None,2048)
                first_path = Path(first["image_artifacts"][0]["input_path"])
                first_bytes = first_path.read_bytes()
                messages.append({"role":"user","content":"y"*1800})
                _, repaired = request_budget.preflight(_ChatOllama(),messages,None,2048)
            second_path = Path(repaired["image_artifacts"][0]["input_path"])
            self.assertNotEqual(first_path,second_path)
            self.assertEqual(first_path.read_bytes(),first_bytes)
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual(hashlib.sha256(second_path.read_bytes()).hexdigest(),repaired["image_sha256"][0])

    def test_plain_text_and_same_word_at_distinct_positions_are_preserved(self) -> None:
        chat = _ChatOllama()
        messages = [
            {"role": "system", "content": "ledger"},
            {"role": "user", "content": "ledger"},
        ]
        original = [dict(message) for message in messages]

        with self._runtime():
            result, budget = request_budget.preflight(chat, messages, None, 2048)

        self.assertEqual(result, original)
        rendered = "<|im_start|>system\nledger<|im_end|>\n<|im_start|>user\nledger<|im_end|>\n<|im_start|>assistant\n"
        self.assertEqual(budget["text_tokens"], len(rendered))

    def test_context_400_fails_profile_check_without_downgrade(self) -> None:
        chat = _ChatOllama(num_ctx=400)
        messages = [{"role": "user", "content": "Do not downgrade context."}]

        with self._runtime() as runtime_profile, self.assertRaises(request_budget.BudgetError) as caught:
            request_budget.preflight(chat, messages, None, 2048)

        runtime_profile.assert_not_called()
        self.assertEqual(caught.exception.code, "budget_profile_not_ready")


if __name__ == "__main__":
    unittest.main()
