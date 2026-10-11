import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import validation
from app.core import model_factory, settings_store, prompts


class _Response:
    def __init__(self, content): self.content = content


class _Chat:
    def invoke(self, messages):
        text_parts = []
        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                text_parts.extend(part.get("text", "") for part in content if part.get("type") == "text")
        text = "\n".join(text_parts)
        if prompts.ROUTE_TEMPLATE in text:
            return _Response('{"type":"receipt","rotate":0,"reason":"synthetic single receipt","content_kind":"document","layout_kind":"single","confidence":"high"}')
        mode = "ocr_text" if "沒有圖片" in text else "image"
        data = prompts.extraction_example("receipt", mode)
        data["fields"].update(date="2026-09-22", vendor="測試商店", tax_id="12345678", total_amount=100)
        data.update(items=[{"description": "文具", "quantity": 1, "unit_price": 100, "amount": 100}], confidence="high")
        if mode == "image":
            data["boxes"] = {"total_amount": [100, 700, 400, 800]}
        return _Response(json.dumps(data, ensure_ascii=False))


class _OCR:
    def recognize(self, image_path):
        return [
            {"text":"測試商店", "score":.99, "box":[[10,10],[100,10],[100,30],[10,30]]},
            {"text":"12345678", "score":.99, "box":[[10,40],[100,40],[100,60],[10,60]]},
            {"text":"100", "score":.99, "box":[[10,70],[100,70],[100,90],[10,90]]},
        ]
    def plain_text(self, lines): return "\n".join(x["text"] for x in lines)


class ValidationTests(unittest.TestCase):
    def test_terminate_preserves_finished_results_and_cannot_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "validation.db"

            class ClosingConnection(sqlite3.Connection):
                def __exit__(self, *args):
                    try:
                        return super().__exit__(*args)
                    finally:
                        self.close()

            def connect():
                con = sqlite3.connect(db_path, factory=ClosingConnection)
                con.row_factory = sqlite3.Row
                return con

            with connect() as con:
                con.executescript("""
                  CREATE TABLE runs (id TEXT PRIMARY KEY,status TEXT,cancel_requested INTEGER,
                    finished_at TEXT,current TEXT,done INTEGER,succeeded INTEGER,failed INTEGER);
                  CREATE TABLE results (id TEXT PRIMARY KEY,run_id TEXT,status TEXT,finished_at TEXT);
                  INSERT INTO runs VALUES ('batch','interrupted',0,NULL,'',0,0,0);
                  INSERT INTO results VALUES ('saved','batch','succeeded','done');
                  INSERT INTO results VALUES ('working','batch','running',NULL);
                  INSERT INTO results VALUES ('pending','batch','queued',NULL);
                """)
            with patch("app.validation._connect", side_effect=connect), \
                 patch("app.validation.get_run", side_effect=lambda _: {"status": "terminated"}):
                self.assertEqual(validation.terminate_run("batch"), {"status": "terminated"})
                with connect() as con:
                    run = con.execute("SELECT * FROM runs WHERE id='batch'").fetchone()
                    results = dict(con.execute("SELECT id,status FROM results WHERE run_id='batch'"))
                self.assertEqual(run["status"], "terminated")
                self.assertEqual(run["succeeded"], 1)
                self.assertEqual(run["done"], 1)
                self.assertEqual(results, {"saved": "succeeded", "working": "terminated",
                                           "pending": "terminated"})
                validation._save_response(
                    {"run_id": "batch", "id": "working", "queued_at": None, "started_at": None},
                    {"ok": True, "data": {"voucher": {"fields": {"total": 100}}}})
                with connect() as con:
                    self.assertEqual(con.execute("SELECT status FROM results WHERE id='working'").fetchone()[0],
                                     "terminated")
                self.assertEqual(validation.resume_run("batch").status_code, 409)
                self.assertEqual(validation.retry_run("batch").status_code, 409)

    def test_model_capabilities_filter_embedding_and_text_models(self):
        with patch("app.core.model_factory._ollama_models", return_value=[
            "ollama/bge-m3:latest", "ollama/qwen2.5:7b", "ollama/qwen2.5vl:7b"]), \
             patch("app.core.model_factory._h200_models", return_value=[
                 "h200/qwen3-32b", "h200/qwen2.5-vl-7b"]), \
             patch("app.core.model_factory.sysconfig.get_section", return_value={}):
            self.assertEqual(model_factory.list_models("vlm"), [
                "h200/qwen2.5-vl-7b", "ollama/qwen2.5vl:7b"])
            self.assertEqual(model_factory.list_models("text"), [
                "h200/qwen3-32b", "ollama/qwen2.5:7b"])
            self.assertNotIn("ollama/bge-m3:latest", model_factory.list_models("llm"))

    def test_ollama_declared_vision_capability_handles_custom_model_names(self):
        with patch("app.core.model_factory._ollama_model_capabilities",
                   return_value={"completion", "vision", "audio"}):
            self.assertTrue(model_factory.is_vision_model("ollama/gemma4:e4b"))

    def test_context_settings_do_not_persist(self):
        before = settings_store.get_settings()
        with settings_store.isolated_settings({"ocr":"disabled/none","structure":"x/y","classify":"x/z"}):
            self.assertEqual(settings_store.get_settings()["structure"], "x/y")
        self.assertEqual(settings_store.get_settings(), before)

    def test_zip_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zf: zf.writestr("../escape.jpg", b"x")
            with self.assertRaises(ValueError): validation._expand_zip(archive, Path(tmp) / "out")

    def test_all_three_recognition_modes_and_text_mode_has_no_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); image = root / "source.jpg"
            Image.new("RGB", (300, 200), "white").save(image)
            old_runs = validation.RUNS; validation.RUNS = root / "runs"
            try:
                for mode in ("vlm_direct", "ocr_vlm", "ocr_llm"):
                    payload = {"run_id":"run", "id":mode, "page_path":str(image),
                      "relative_path":"nested/source.jpg", "page_no":1, "render_seconds":0,
                      "max_edge":1180, "combo":{"id":mode,"name":mode,"mode":mode,
                      "structure_model":"fake/model","classify_model":"fake/model"}}
                    def get_ocr(provider="paddle"):
                        if provider in ("disabled", "none"): raise RuntimeError("disabled")
                        return _OCR()
                    with patch("app.core.model_factory.get_chat_model", return_value=_Chat()), \
                         patch("app.core.model_factory.get_ocr", side_effect=get_ocr), \
                         patch("app.core.barcode.decode", return_value=[]):
                        result = validation._recognize(payload)
                    self.assertEqual(result["voucher"]["total_amount"], 100)
                    self.assertTrue(Path(result["artifacts"]["annotated"]["path"]).is_file())
                    if mode == "ocr_llm":
                        calls = [x for x in result["trace"] if x.get("kind") == "model_call"]
                        self.assertTrue(calls)
                        self.assertNotIn("embedded image", json.dumps(calls))
            finally:
                validation.RUNS = old_runs


if __name__ == "__main__": unittest.main()
