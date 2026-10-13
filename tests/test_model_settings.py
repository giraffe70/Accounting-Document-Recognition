"""Offline model-list and /api/models contract tests for local-only settings."""
from __future__ import annotations

import ast
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from app.core import model_factory


ROOT = Path(__file__).resolve().parents[1]
APP_MAIN = ROOT / "app" / "main.py"


class LocalModelListTests(unittest.TestCase):
    def test_local_only_filters_each_capability_and_never_queries_remote_sources(self):
        ollama = [
            "ollama/local-vlm", "ollama/local-text", "ollama/embedded",
            "ollama/local-text",
        ]
        capabilities = {
            "ollama/local-vlm": {"vision", "completion"},
            "ollama/local-text": {"completion"},
            "ollama/embedded": {"embedding"},
        }
        with (
            patch.object(model_factory, "_ollama_models", return_value=ollama),
            patch.object(model_factory, "_ollama_model_capabilities", side_effect=lambda model: capabilities.get(model, set())),
            patch.object(model_factory, "_h200_models") as h200,
            patch.object(model_factory.sysconfig, "get_section") as get_section,
        ):
            self.assertEqual(model_factory.list_models(kind="vlm", local_only=True), ["ollama/local-vlm"])
            self.assertEqual(model_factory.list_models(kind="llm", local_only=True), ["ollama/local-text", "ollama/local-vlm"])
            self.assertEqual(model_factory.list_models(kind="text", local_only=True), ["ollama/local-text"])

        h200.assert_not_called()
        get_section.assert_not_called()

    def test_default_model_list_still_combines_ollama_h200_and_configured_cloud(self):
        ollama = ["ollama/local-text"]
        with (
            patch.object(model_factory, "_ollama_models", return_value=ollama),
            patch.object(model_factory, "_ollama_model_capabilities", return_value={"completion"}),
            patch.object(model_factory, "_h200_models", return_value=["h200/qwen3-32b"]) as h200,
            patch.object(model_factory.sysconfig, "get_section", return_value={"google_api_key": "configured"}) as get_section,
            patch.object(model_factory.config, "CLOUD_MODELS", {"google": {"llm": ["google/gemini"]}}),
        ):
            actual = model_factory.list_models(kind="llm")

        self.assertEqual(actual, ["google/gemini", "h200/qwen3-32b", "ollama/local-text"])
        h200.assert_called_once_with()
        get_section.assert_called_once_with("llm")


class ApiModelsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse(APP_MAIN.read_text(encoding="utf-8"), filename=str(APP_MAIN))
        node = next(
            (copy.deepcopy(item) for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "api_models"),
            None,
        )
        if node is None:
            raise AssertionError("could not locate api_models function")
        node.decorator_list = []  # Extract only the function; do not import app.main or initialize the app/database.
        ast.fix_missing_locations(node)
        factory = SimpleNamespace(
            list_ocr_providers=lambda: ["paddle/pp-ocrv5"],
            ocr_status=lambda: {"available": True},
            list_models=lambda **kwargs: {"vlm": ["vlm-list"], "llm": ["llm-list"], "text": ["text-list"]}[kwargs["kind"]],
        )
        namespace = {"model_factory": factory}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(APP_MAIN), "exec"), namespace)
        cls.api_models = staticmethod(namespace["api_models"])
        cls.factory = factory

    def test_local_query_is_forwarded_and_response_shape_stays_stable(self):
        with patch.object(self.factory, "list_models", side_effect=lambda **kwargs: [f"{kwargs['kind']}:{kwargs['local_only']} "]) as list_models:
            response = self.api_models(local_only=True)

        self.assertEqual(set(response), {"ocr", "ocr_status", "structure", "classify", "text"})
        self.assertEqual(response, {
            "ocr": ["paddle/pp-ocrv5"], "ocr_status": {"available": True},
            "structure": ["vlm:True "], "classify": ["llm:True "], "text": ["text:True "],
        })
        self.assertEqual(list_models.call_args_list, [
            call(kind="vlm", local_only=True),
            call(kind="llm", local_only=True),
            call(kind="text", local_only=True),
        ])

    def test_omitted_query_keeps_default_full_model_listing(self):
        with patch.object(self.factory, "list_models", side_effect=lambda **kwargs: [f"{kwargs['kind']}:{kwargs['local_only']}"]) as list_models:
            response = self.api_models()

        self.assertEqual(response["structure"], ["vlm:False"])
        self.assertEqual(response["classify"], ["llm:False"])
        self.assertEqual(response["text"], ["text:False"])
        self.assertEqual(list_models.call_args_list, [
            call(kind="vlm", local_only=False),
            call(kind="llm", local_only=False),
            call(kind="text", local_only=False),
        ])


if __name__ == "__main__":
    unittest.main()
