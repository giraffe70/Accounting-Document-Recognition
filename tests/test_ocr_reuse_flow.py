"""Regression checks for scoped OCR reuse and recognition timing exports."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import shutil
from unittest.mock import patch

from PIL import Image

from app import config, validation
from app.core import recognition_timing, pipeline, ocr_cache, invocation_trace, settings_store, page_ocr, supplement
from app.services import review


def _test_tempdir():
    root = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


def _trace(seconds, purpose="full", *, hit=False, cache=0.0, init=0.0):
    return {"kind": "ocr_result", "seconds": seconds, "purpose": purpose,
            "cache_hit": hit, "cache_enabled": True, "cache_seconds": cache,
            "engine_init_seconds": init}


def _layout(regions):
    return {"content_kind": "document", "auto_split": True, "regions": regions}


def _region(type_key, left, right):
    return {"type": type_key, "rotate": 0, "reason": "synthetic", "confidence": "high",
            "box": [left, 0, right, 1000]}


class RecognitionTimingTests(unittest.TestCase):
    def test_only_leaf_calls_are_counted_and_initialization_is_a_subset(self):
        trace = [
            {"kind": "stage", "name": "ocr", "seconds": 91},
            {"kind": "ocr_enhancement", "seconds": 17},
            _trace(1.25, "orientation", init=.3),
            _trace(2.5, "full", cache=.08),
            _trace(0, "supplement_candidate", hit=True, cache=.04),
            _trace(.75, "supplement", cache=.2, init=.5),
            {"kind": "structured_round", "task": "layout"},
            {"kind": "model_call", "seconds": 3},
            {"kind": "structured_round", "task": "extract"},
            {"kind": "model_call", "seconds": 4},
        ]
        timings = recognition_timing.from_trace(trace)
        self.assertEqual(timings["orientation_ocr_seconds"], 1.25)
        self.assertEqual(timings["full_ocr_seconds"], 2.5)
        self.assertEqual(timings["supplement_ocr_seconds"], .75)
        self.assertEqual(timings["ocr_seconds"], 4.5)
        self.assertEqual(timings["ocr_init_seconds"], .8)
        self.assertEqual(timings["ocr_cache_hits"], 1)
        self.assertEqual(timings["ocr_cache_misses"], 3)
        self.assertEqual(timings["model_seconds"], 7)
        self.assertEqual(timings["classification_seconds"], 3)
        self.assertEqual(timings["extraction_seconds"], 4)

    def test_legacy_csv_timings_are_blank_for_new_fields(self):
        values = recognition_timing.csv_values({"ocr_seconds": 9, "complete_seconds": 10})
        self.assertTrue(all(value is None for value in values.values()))

    def test_interrupted_calls_report_incomplete_measurements(self):
        incomplete = recognition_timing.from_trace([{"kind": "ocr_call_start"}])
        self.assertTrue(incomplete["incomplete"])
        self.assertTrue(recognition_timing.merge(incomplete, recognition_timing.from_trace([]))["incomplete"])


class PipelineReuseIntegrationTests(unittest.TestCase):
    class FakePaddle:
        def __init__(self):
            self.sizes = []

        def predict(self, path):
            with Image.open(path) as image:
                self.sizes.append(image.size)
            texts = ["收據 測試商店 統一編號 12345678", "日期 民國115年09月22日",
                     "品名 文具 數量 1 單價 100", "合計 新臺幣 100 元"]
            return [{"rec_texts": texts, "rec_scores": [.99] * len(texts),
                     "dt_polys": [[[10, y], [300, y], [300, y+20], [10, y+20]]
                                  for y in (10, 50, 90, 140)]}]

    def test_upload_review_copy_reuses_layout_ocr_for_invalid_tax_id_target(self):
        # Exercise the real upload/preparation and extraction pipeline; only the
        # Paddle runtime and chat are fakes. Human review would copy the PNG in
        # save_voucher, so this test performs the identical byte-preserving copy.
        from tests.test_validation import _Chat
        with _test_tempdir() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            Image.new("RGB", (1000, 800), "white").save(source)
            fake = self.FakePaddle()
            conditions = {"low_text_contrast": False, "skew_degrees": 0, "blur_warning": False}
            with patch.object(config, "UPLOAD_DIR", root / "uploads"), \
                 patch("app.core.ocr_engine._get_paddle", return_value=fake) as loader, \
                 patch("app.core.model_factory.get_chat_model", return_value=_Chat()), \
                 patch("app.core.barcode.decode", return_value=[]), \
                 patch("app.core.image_evidence.enhancement", return_value=(None, conditions)), \
                 patch("app.core.pipeline.reread_fields", return_value={}) as reread, \
                 settings_store.isolated_settings({"ocr": "paddle/pp-ocrv5", "structure": "fake/model", "classify": "fake/model"}):
                prepared = pipeline.prepare_pages(str(source))
                child = next(item for item in prepared if item.origin.get("record_role") != "source_page")
                copied = root / "review-copy.png"
                shutil.copyfile(child.source_image, copied)
                rec = SimpleNamespace(current_json=child.model_dump_json(), source_parent_id=1, id=2)
                loader.reset_mock()
                with invocation_trace.capture() as trace, ocr_cache.scope(review._ocr_cache_directory(rec)):
                    result = pipeline.extract_for_type(str(copied), child.doc_type_key, confirmed_type=True)
                loader.assert_not_called()
            self.assertEqual(fake.sizes.count((1000, 800)), 1)
            self.assertEqual(fake.sizes.count((720, 576)), 1)
            reread.assert_called_once()
            self.assertEqual(reread.call_args.args[3], ["tax_id"])
            events = [event for event in trace if event.get("kind") == "ocr_result"]
            self.assertEqual([event["purpose"] for event in events], ["extraction"])
            self.assertTrue(events[0]["cache_hit"])
            self.assertEqual(result.total_amount, 100)

    def test_real_batch_phases_share_raw_ocr_but_new_attempt_is_cold(self):
        from tests.test_validation import _Chat
        with _test_tempdir() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            Image.new("RGB", (1000, 800), "white").save(source)
            fake = self.FakePaddle()
            payload = {"run_id": "run", "id": "attempt-1", "page_path": str(source),
                       "relative_path": "synthetic.png", "page_no": 1, "render_seconds": 0, "max_edge": 1180,
                       "combo": {"mode": "ocr_vlm", "structure_model": "fake/model", "classify_model": "fake/model"}}
            conditions = {"low_text_contrast": False, "skew_degrees": 0, "blur_warning": False}
            with patch.object(validation, "RUNS", root / "runs"), \
                 patch("app.core.ocr_engine._get_paddle", return_value=fake), \
                 patch("app.core.model_factory.get_chat_model", return_value=_Chat()), \
                 patch("app.core.barcode.decode", return_value=[]), \
                 patch("app.core.image_evidence.enhancement", return_value=(None, conditions)), \
                 patch("app.core.pipeline.reread_fields", return_value={}):
                result = validation._recognize(payload)
                validation._recognize({**payload, "id": "attempt-2"})
            self.assertEqual(fake.sizes.count((1000, 800)), 2)
            self.assertEqual(fake.sizes.count((720, 576)), 2)
            self.assertEqual(result["timings"]["ocr_cache_hits"], 1)
            self.assertEqual(result["timings"]["ocr_cache_misses"], 2)
            self.assertEqual(result["timings"]["supplement_ocr_seconds"], 0)

    def test_manual_split_keeps_appearance_scope_after_image_copy(self):
        from tests.test_validation import _Chat
        with _test_tempdir() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            copied = root / "review-copy.png"
            Image.new("RGB", (1000, 800), "white").save(source)
            parent = SimpleNamespace(id=1, source_parent_id=None, record_role="source_page",
                                     current_json="{}", company="", source_file="synthetic.png")
            fake = self.FakePaddle()

            def prepare(path):
                page_ocr.read(path, (1000, 800), folder=root / "appearance")
                return "receipt"

            def save(voucher, **kwargs):
                shutil.copyfile(voucher.source_image, copied)
                return SimpleNamespace(id=2, source_parent_id=1, current_json=voucher.model_dump_json())

            with patch.object(config, "UPLOAD_DIR", root / "uploads"), \
                 patch.object(review, "get_voucher", return_value=parent), \
                 patch.object(review, "_image_path_of", return_value=source), \
                 patch.object(review, "save_voucher", side_effect=save), \
                 patch.object(pipeline, "prepare_image", side_effect=prepare), \
                 patch("app.core.ocr_engine._get_paddle", return_value=fake) as loader, \
                 patch("app.core.model_factory.get_chat_model", return_value=_Chat()), \
                 patch("app.core.barcode.decode", return_value=[]), \
                 patch("app.core.image_evidence.enhancement", return_value=(None, {})), \
                 patch("app.core.pipeline.reread_fields", return_value={}), \
                 settings_store.isolated_settings({"ocr": "paddle/pp-ocrv5", "structure": "fake/model", "classify": "fake/model"}):
                record = review.split_voucher(1, [0, 0, 1, 1])
                self.assertEqual(len(json.loads(record.current_json)["origin"]["source_token"]), 32)
                loader.reset_mock()
                with ocr_cache.scope(review._ocr_cache_directory(record)):
                    pipeline.extract_for_type(str(copied), "receipt", confirmed_type=True)
                loader.assert_not_called()
            self.assertEqual(fake.sizes.count((1000, 800)), 1)

    def test_generic_sum_and_page_skew_do_not_trigger_low_yield_reocr(self):
        from app.core.ocr_engine import PaddleOCREngine
        with _test_tempdir() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            Image.new("RGB", (1000, 800), "white").save(source)
            fake = self.FakePaddle()
            data = {"fields": {"tax_id": None, "total_amount": 50, "currency": "TWD"},
                    "items": [{"description": "文具"}], "field_evidence": {}, "validation_warnings": []}
            context = {"ocr_enabled": True, "image_conditions": {"skew_degrees": 1}}
            with patch("app.core.ocr_engine._get_paddle", return_value=fake), \
                 patch("app.core.image_evidence.enhancement") as enhance, \
                 patch("app.core.pipeline.reread_fields", return_value={"total_amount": 100}), \
                 settings_store.isolated_settings({"ocr": "paddle/pp-ocrv5"}), \
                 ocr_cache.scope(root / "cache"), invocation_trace.capture() as trace:
                lines = PaddleOCREngine().recognize(str(source))
                supplement.apply(data, "receipt", object(), str(source), mode="image",
                                 ocr_text=PaddleOCREngine.plain_text(lines), context=context)
            events = [event for event in trace if event.get("kind") == "ocr_result"]
            self.assertEqual([event["cache_hit"] for event in events], [False])
            enhance.assert_not_called()
            self.assertNotIn("ocr_copy_conflict", context)
            self.assertEqual(data["fields"]["total_amount"], 50)


class PageChildTimingAndScopeTests(unittest.TestCase):
    def test_timing_sidecar_storage_failure_does_not_block_documents(self):
        with _test_tempdir() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            Image.new("RGB", (1000, 800), "white").save(source)
            phases = []

            def runner(payload, _timeout):
                phases.append(payload["phase"])
                if payload["phase"] == "layout":
                    return {"ok": True, "data": {"layout": _layout([_region("receipt", 0, 1000)])}}
                return {"ok": True, "data": {"voucher": {"voucher_id": "synthetic"}}}

            write = invocation_trace.atomic_json

            def limited_write(path, value):
                if Path(path).name == "layout_timing.json":
                    raise OSError("synthetic storage failure")
                return write(path, value)

            payload = {"run_id": "run", "id": "page", "page_no": 1,
                       "page_path": str(source), "combo": {"mode": "ocr_vlm"}}
            with patch.object(validation, "RUNS", root / "runs"), \
                 patch.object(invocation_trace, "atomic_json", side_effect=limited_write):
                result = validation._run_page_child(payload, 10, runner)
            self.assertTrue(result["ok"])
            self.assertEqual(phases, ["layout", "document"])

    def test_layout_and_all_document_timings_merge_without_single_document_overwrite(self):
        with _test_tempdir() as temp:
            root = Path(temp)
            image = root / "page.png"
            Image.new("RGB", (320, 200), "white").save(image)
            calls = []

            def runner(payload, _timeout):
                calls.append(payload)
                if payload["phase"] == "layout":
                    return {"ok": True, "wall_seconds": .2,
                            "data": {"layout": _layout([_region("receipt", 0, 500),
                                                         _region("invoice", 500, 1000)]),
                                     "trace": [_trace(.1, "orientation"),
                                               {"kind": "structured_round", "task": "layout"},
                                               {"kind": "model_call", "seconds": .2}]}}
                trace = [_trace(1 if payload["type_key"] == "receipt" else 2),
                         {"kind": "structured_round", "task": "extract"},
                         {"kind": "model_call", "seconds": 3 if payload["type_key"] == "receipt" else 4}]
                return {"ok": True, "wall_seconds": 1,
                        "data": {"voucher": {"voucher_id": payload["type_key"]}, "trace": trace}}

            payload = {"run_id": "run", "id": "page-combo-repeat-attempt", "page_no": 1,
                       "page_path": str(image), "combo": {"mode": "image"}}
            with patch.object(validation, "RUNS", root / "runs"):
                result = validation._run_page_child(payload, 10, runner=runner)["data"]

            self.assertEqual(result["timings"]["ocr_seconds"], 3.1)
            self.assertEqual(result["timings"]["model_seconds"], 7.2)
            self.assertEqual(result["timings"]["classification_seconds"], .2)
            self.assertEqual(result["timings"]["extraction_seconds"], 7)
            self.assertEqual([call["ocr_cache_dir"] for call in calls],
                             [calls[0]["ocr_cache_dir"]] * 3)

            def single_runner(payload, _timeout):
                if payload["phase"] == "layout":
                    return {"ok": True, "wall_seconds": .1,
                            "data": {"layout": _layout([_region("receipt", 0, 1000)]),
                                     "trace": [{"kind": "structured_round", "task": "layout"},
                                               {"kind": "model_call", "seconds": 2}, _trace(.4)]}}
                return {"ok": True, "wall_seconds": .3,
                        "data": {"voucher": {"voucher_id": "single"},
                                 "trace": [{"kind": "structured_round", "task": "extract"},
                                           {"kind": "model_call", "seconds": 5}, _trace(.6)]}}

            with patch.object(validation, "RUNS", root / "runs-single"):
                single = validation._run_page_child({**payload, "id": "single-page"}, 10,
                                                    runner=single_runner)["data"]
            self.assertEqual(single["timings"]["classification_seconds"], 2)
            self.assertEqual(single["timings"]["extraction_seconds"], 5)
            self.assertEqual(single["timings"]["ocr_seconds"], 1)

    def test_cache_scope_is_shared_by_phases_and_isolated_by_result_identity(self):
        with _test_tempdir() as temp:
            root = Path(temp)
            image = root / "page.png"
            Image.new("RGB", (320, 200), "white").save(image)
            observed = []

            def runner(payload, _timeout):
                observed.append((payload.get("parent_result_id") or payload["id"],
                                 payload["phase"], payload["ocr_cache_dir"]))
                if payload["phase"] == "layout":
                    return {"ok": True, "data": {"layout": _layout([_region("receipt", 0, 1000)])}}
                return {"ok": True, "data": {"voucher": {"voucher_id": "R"}}}

            payloads = [
                {"run_id": "run", "id": "combo-a-repeat-1-attempt-1", "page_no": 1,
                 "page_path": str(image), "combo": {"mode": "image"}, "combo_name": "a", "repeat_no": 1, "attempt": 1},
                {"run_id": "run", "id": "combo-b-repeat-1-attempt-1", "page_no": 1,
                 "page_path": str(image), "combo": {"mode": "image"}, "combo_name": "b", "repeat_no": 1, "attempt": 1},
                {"run_id": "run", "id": "combo-a-repeat-2-attempt-3", "page_no": 1,
                 "page_path": str(image), "combo": {"mode": "image"}, "combo_name": "a", "repeat_no": 2, "attempt": 3},
            ]
            with patch.object(validation, "RUNS", root / "runs"):
                for payload in payloads:
                    validation._run_page_child(payload, 10, runner=runner)

            scopes = {}
            for result_id, _phase, directory in observed:
                scopes.setdefault(result_id, set()).add(directory)
            self.assertEqual(len(scopes), 3)
            self.assertTrue(all(len(directories) == 1 for directories in scopes.values()))
            self.assertEqual(len({next(iter(directories)) for directories in scopes.values()}), 3)


class CsvTimingOwnershipTests(unittest.TestCase):
    def test_page_voucher_and_legacy_csv_timing_ownership(self):
        data = {"results": [{"id": "page", "relative_path": "set/page.png", "page_no": 1,
                             "status": "succeeded", "combo_name": "combo", "mode": "image",
                             "actual_path": "image", "repeat_no": 1, "attempt": 2, "error": "",
                             "timings": {"timings_version": 2, "ocr_seconds": 8, "queue_seconds": 1,
                                         "complete_seconds": 10},
                             "documents": [{"id": "doc", "status": "succeeded", "type_key": "receipt",
                                            "voucher": {"voucher_id": "R1", "total_amount": 10},
                                            "timings": {"timings_version": 2, "ocr_seconds": 3,
                                                        "queue_seconds": 99, "complete_seconds": 4}}]},
                            {"id": "legacy", "relative_path": "set/old.png", "page_no": 2,
                             "status": "succeeded", "combo_name": "combo", "mode": "image",
                             "actual_path": "image", "repeat_no": 1, "attempt": 1, "error": "",
                             "timings": {"complete_seconds": 2}, "documents": []}], "artifacts": []}
        files = validation._csv_files(data)
        pages = list(csv.DictReader(files["pages.csv"].lstrip("\ufeff").splitlines()))
        vouchers = list(csv.DictReader(files["vouchers.csv"].lstrip("\ufeff").splitlines()))
        self.assertIn("ocr_seconds", pages[0])
        self.assertEqual(pages[0]["ocr_seconds"], "8")
        self.assertEqual(pages[0]["queue_seconds"], "1")
        self.assertEqual(vouchers[0]["ocr_seconds"], "3")
        self.assertEqual(vouchers[0]["queue_seconds"], "99")
        legacy = next(row for row in pages if row["result_id"] == "legacy")
        self.assertEqual(legacy["timings_version"], "")
        self.assertEqual(legacy["ocr_seconds"], "")


class ReviewCacheScopeTests(unittest.TestCase):
    def test_source_token_and_record_fallback_scopes(self):
        token = "a" * 32
        rec = SimpleNamespace(current_json=json.dumps({"origin": {"source_token": token}}),
                              source_parent_id="parent", id="record")
        with patch.object(config, "UPLOAD_DIR", Path("uploads")), patch.object(config, "DATA_DIR", Path("data")):
            self.assertEqual(review._ocr_cache_directory(rec), Path("uploads/_pages") / token / "ocr_cache")
            rec.current_json = json.dumps({"origin": {"source_token": "invalid"}})
            self.assertEqual(review._ocr_cache_directory(rec), Path("data/recognition/parent/ocr_cache"))
            rec.source_parent_id = None
            self.assertEqual(review._ocr_cache_directory(rec), Path("data/recognition/record/ocr_cache"))


if __name__ == "__main__":
    unittest.main()
