from __future__ import annotations

import contextlib
import io
import sys
import types
import unittest
from unittest.mock import Mock, patch

from scripts import prepare_token_budget


class PrepareTokenBudgetCliTests(unittest.TestCase):
    def _fake_request_budget(self, prepare_profile: Mock) -> types.ModuleType:
        module = types.ModuleType("app.core.request_budget")
        module.prepare_profile = prepare_profile  # type: ignore[attr-defined]
        return module

    def test_uses_defaults_and_prints_compact_ready_line(self) -> None:
        prepare_profile = Mock(return_value={"profile_id": "profile-123", "large": "metadata"})
        output = io.StringIO()

        with patch.dict(sys.modules, {"app.core.request_budget": self._fake_request_budget(prepare_profile)}):
            with contextlib.redirect_stdout(output):
                result = prepare_token_budget.main([])

        self.assertEqual(result, 0)
        prepare_profile.assert_called_once_with("qwen2.5vl:7b", backend_url=None, asset_dir=None)
        self.assertEqual(output.getvalue(), "ready model=qwen2.5vl:7b profile=profile-123\n")
        self.assertNotIn("metadata", output.getvalue())

    def test_passes_overrides(self) -> None:
        prepare_profile = Mock(return_value={"profile_id": "p"})

        with patch.dict(sys.modules, {"app.core.request_budget": self._fake_request_budget(prepare_profile)}):
            result = prepare_token_budget.main(
                ["--model", "qwen-test", "--backend-url", "http://ollama", "--asset-dir", "assets"]
            )

        self.assertEqual(result, 0)
        prepare_profile.assert_called_once_with("qwen-test", backend_url="http://ollama", asset_dir="assets")

    def test_failure_is_compact_and_returns_one(self) -> None:
        prepare_profile = Mock(side_effect=RuntimeError("very large backend metadata"))
        errors = io.StringIO()

        with patch.dict(sys.modules, {"app.core.request_budget": self._fake_request_budget(prepare_profile)}):
            with contextlib.redirect_stderr(errors):
                result = prepare_token_budget.main([])

        self.assertEqual(result, 1)
        self.assertEqual(errors.getvalue(), "ERROR: profile preparation failed (RuntimeError)\n")
        self.assertNotIn("very large", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
