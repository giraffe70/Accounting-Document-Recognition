from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from ollama import ResponseError

from app.core import pipeline, prompt_evidence, prompts, recognition_checkpoint
from app.models.schemas import Voucher


class SupplementBudgetRecoveryTests(unittest.TestCase):
    TEST_TMP_DIR = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    EXECUTION_VERSION = "test-execution-version"

    def setUp(self) -> None:
        self.TEST_TMP_DIR.mkdir(parents=True, exist_ok=True)

    def _raw_invoice(self, *, seller_name: str | None = "Acme", total_amount: int | None = None) -> dict:
        raw = prompts.extraction_example("invoice", "ocr_text")
        raw["fields"]["seller_name"] = seller_name
        raw["fields"]["total_amount"] = total_amount
        raw["confidence"] = "medium"
        return raw

    def _voucher(self, *, seller_name: str | None = "Acme", total_amount: int | None = None) -> Voucher:
        raw = self._raw_invoice(seller_name=seller_name, total_amount=total_amount)
        return Voucher(
            doc_type="統一發票／電子發票",
            doc_type_key="invoice",
            fields=raw["fields"],
            seller={"name": seller_name or ""},
            total_amount=total_amount,
        )

    def _checkpoint(self, folder: Path, *, seller_name: str | None = "Acme", total_amount: int | None = None):
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / "source.bin"
        image = folder / "input.bin"
        source.write_bytes(b"synthetic source bytes")
        image.write_bytes(b"synthetic prepared bytes")
        voucher = self._voucher(seller_name=seller_name, total_amount=total_amount)
        raw = self._raw_invoice(seller_name=seller_name, total_amount=total_amount)
        with patch("app.core.model_invocation.source_versions", return_value={
            "execution_version_sha256": self.EXECUTION_VERSION,
        }):
            checkpoint = recognition_checkpoint.save(
                image, source, voucher, raw
            )
        recognition_checkpoint.mark_supplement(checkpoint)
        return checkpoint, source

    def test_recover_rejects_version_mismatch_source_mismatch_and_empty_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            folder = Path(temporary)

            checkpoint, source = self._checkpoint(folder / "version", total_amount=0)
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            saved["version"] = 99
            checkpoint.write_text(json.dumps(saved), encoding="utf-8")
            self.assertIsNone(recognition_checkpoint.recover(
                checkpoint, execution_version=self.EXECUTION_VERSION, source_path=source, reason="timeout"
            ))

            checkpoint, source = self._checkpoint(folder / "source", total_amount=0)
            source.write_bytes(b"changed synthetic source")
            self.assertIsNone(recognition_checkpoint.recover(
                checkpoint, execution_version=self.EXECUTION_VERSION, source_path=source, reason="timeout"
            ))

            checkpoint, source = self._checkpoint(folder / "empty", seller_name=None, total_amount=None)
            self.assertIsNone(recognition_checkpoint.recover(
                checkpoint, execution_version=self.EXECUTION_VERSION, source_path=source, reason="timeout"
            ))

    def test_recovery_keeps_valid_zero_and_missing_total_is_insufficient(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            folder = Path(temporary)
            zero_checkpoint, zero_source = self._checkpoint(folder / "zero", total_amount=0)
            recovered_zero = recognition_checkpoint.recover(
                zero_checkpoint, execution_version=self.EXECUTION_VERSION,
                source_path=zero_source, reason="context_limit",
            )
            self.assertIsNotNone(recovered_zero)
            self.assertEqual(recovered_zero["fields"]["total_amount"], 0)
            self.assertEqual(recovered_zero["total_amount"], 0)
            self.assertTrue(any(
                issue["code"] == "supplement_failed"
                for issue in recovered_zero["recognition_diagnostics"]["issues"]
            ))

            missing_checkpoint, missing_source = self._checkpoint(folder / "missing", total_amount=None)
            recovered_missing = recognition_checkpoint.recover(
                missing_checkpoint, execution_version=self.EXECUTION_VERSION,
                source_path=missing_source, reason="context_limit",
            )
            self.assertIsNotNone(recovered_missing)
            self.assertEqual(recovered_missing["quality"]["state"], "insufficient")
            self.assertIn("Acme", recovered_missing["fields"]["seller_name"])

    def test_prompt_evidence_keeps_distinct_same_text_positions_without_confidence(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            image_path = Path(temporary) / "synthetic.png"
            Image.new("RGB", (100, 80), color="white").save(image_path)
            lines = [
                {"text": "same word", "box": [[2, 3], [34, 3], [34, 12], [2, 12]], "score": 0.91},
                {"text": "same word", "box": [[55, 40], [90, 40], [90, 52], [55, 52]], "confidence": 0.78},
            ]

            assembled = prompt_evidence.assemble(
                "same word\nsame word", {"ocr_lines": lines}, str(image_path)
            )

        self.assertNotIn("ocr_text", assembled)
        self.assertEqual([line["text"] for line in assembled["ocr_lines"]], ["same word", "same word"])
        self.assertEqual([line["id"] for line in assembled["ocr_lines"]], [0, 1])
        self.assertTrue(all("score" not in line and "confidence" not in line for line in assembled["ocr_lines"]))

    def _extract_with_supplement_error(self, folder: Path, error: BaseException) -> Voucher:
        folder.mkdir(parents=True, exist_ok=True)
        image_path = folder / "synthetic-source.bin"
        image_path.write_bytes(b"synthetic source bytes")
        raw = self._raw_invoice(seller_name="Acme", total_amount=0)
        classification = {"conflict": False, "protected": False, "adopted": False}
        with ExitStack() as stack:
            stack.enter_context(patch(
                "app.core.classification_evidence.resolve",
                return_value=("invoice", classification),
            ))
            stack.enter_context(patch(
                "app.core.model_invocation.invoke_structured", return_value=raw,
            ))
            stack.enter_context(patch(
                "app.core.model_invocation.source_versions", return_value={
                    "execution_version_sha256": self.EXECUTION_VERSION,
                },
            ))
            stack.enter_context(patch(
                "app.core.settings_store.get_settings", return_value={"ocr": "disabled"},
            ))
            stack.enter_context(patch(
                "app.core.settings_store.parse_setting", return_value=("disabled", ""),
            ))
            stack.enter_context(patch("app.core.supplement.apply", side_effect=error))
            return pipeline.extract_evidence(
                str(image_path), "invoice", object(), [], "", mode="ocr_text"
            )

    def test_first_read_survives_real_ollama_context_400_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            folder = Path(temporary)
            errors = (
                ResponseError("context length exceeded", status_code=400),
                TimeoutError("synthetic timeout"),
            )
            for index, error in enumerate(errors):
                with self.subTest(error=type(error).__name__):
                    result = self._extract_with_supplement_error(folder / str(index), error)
                    self.assertEqual(result.fields["total_amount"], 0.0)
                    self.assertTrue(any(
                        issue["code"] == "supplement_failed"
                        for issue in result.recognition_diagnostics["issues"]
                    ))
                    self.assertTrue(any("補讀失敗" in warning for warning in result.validation_warnings))
                    self.assertEqual(result.quality["state"], "needs_review")

    def test_keyboard_interrupt_from_supplement_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.TEST_TMP_DIR) as temporary:
            with self.assertRaises(KeyboardInterrupt):
                self._extract_with_supplement_error(Path(temporary), KeyboardInterrupt())


if __name__ == "__main__":
    unittest.main()
