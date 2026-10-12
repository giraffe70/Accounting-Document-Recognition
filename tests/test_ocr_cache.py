"""Offline contract tests for work-scoped OCR result caching."""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
from contextvars import copy_context
import unittest
from unittest.mock import patch

from PIL import Image

from app.core import invocation_trace, ocr_cache, ocr_engine


def _spawn_probe(cache_dir: str, image_path: str, result_queue) -> None:
    """Spawn target: a cache hit must happen before this fake loader is called."""
    from unittest.mock import patch
    from app.core import ocr_cache, ocr_engine

    def forbidden_loader(lang):
        raise AssertionError("Paddle loader ran in child despite shared disk cache")

    try:
        with patch.object(ocr_engine, "runtime_signature",
                          return_value={"fake": "spawn", "lang": "chinese_cht"}), \
                patch.object(ocr_engine, "_get_paddle", side_effect=forbidden_loader), \
                ocr_cache.scope(cache_dir):
            result = ocr_engine.PaddleOCREngine().recognize(image_path)
        result_queue.put(("ok", result))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _spawn_incomplete_writer(lock_path, temporary_path, ready):
    with ocr_cache._key_lock(Path(lock_path)):
        Path(temporary_path).write_text('{"identity":', encoding="utf-8")
        ready.set()
        while True:
            time.sleep(1)


class _FakeOCR:
    def __init__(self, payload=None, error=None, delay=0):
        self.payload = payload if payload is not None else [{
            "rec_texts": ["離線辨識"],
            "dt_polys": [[[1, 2], [11, 2], [11, 12], [1, 12]]],
            "rec_scores": [0.99],
        }]
        self.error = error
        self.delay = delay
        self.calls = 0
        self.guard = threading.Lock()

    def predict(self, _image_path):
        with self.guard:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.payload


class OCRCacheContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temporary.name)
        self.image = self.root / "source.png"
        Image.new("RGB", (24, 18), (240, 240, 240)).save(self.image)
        self.signature = {"provider": "fake", "lang": "chinese_cht", "revision": 1}
        self.fake = _FakeOCR()
        self.signature_patcher = patch.object(ocr_engine, "runtime_signature")
        self.loader_patcher = patch.object(ocr_engine, "_get_paddle")
        self.signature_mock = self.signature_patcher.start()
        self.loader_mock = self.loader_patcher.start()
        self.signature_mock.side_effect = lambda lang="chinese_cht": {
            **self.signature, "lang": lang,
        }
        self.loader_mock.side_effect = lambda _lang: self.fake
        self.addCleanup(self.signature_patcher.stop)
        self.addCleanup(self.loader_patcher.stop)
        self.addCleanup(self.temporary.cleanup)

    def recognize(self, image=None, lang="chinese_cht"):
        return ocr_engine.PaddleOCREngine(lang).recognize(str(image or self.image))

    def loader_calls(self):
        return self.loader_mock.call_count

    def test_encoded_files_with_same_pixels_hit_and_results_are_deep_copied(self):
        duplicate = self.root / "different-encoding.png"
        pixels = Image.open(self.image).convert("RGB")
        pixels.save(duplicate, optimize=True)
        self.assertNotEqual(self.image.read_bytes(), duplicate.read_bytes())
        with ocr_cache.scope():
            first = self.recognize()
            first[0]["box"][0][0] = -1
            second = self.recognize(duplicate)
        self.assertEqual(second[0]["box"][0][0], 1)
        self.assertEqual(self.fake.calls, 1)
        self.assertEqual(self.loader_calls(), 1)

    def test_pixel_dimensions_language_signature_crop_and_augmentation_are_identity(self):
        variants = []
        changed_pixel = Image.new("RGB", (24, 18), (239, 240, 240))
        changed_size = Image.new("RGB", (25, 18), (240, 240, 240))
        cropped = Image.open(self.image).crop((0, 0, 20, 18))
        enhanced = Image.open(self.image).point(lambda value: min(255, value + 1))
        for name, picture in (("pixel", changed_pixel), ("size", changed_size),
                              ("crop", cropped), ("enhanced", enhanced)):
            path = self.root / f"{name}.png"
            picture.save(path)
            variants.append(path)
        with ocr_cache.scope():
            self.recognize()
            for path in variants:
                self.recognize(path)
            self.recognize(lang="english")
            self.signature["revision"] = 2
            self.recognize()
        self.assertEqual(self.fake.calls, 7)

    def test_empty_ocr_result_is_cacheable(self):
        self.fake.payload = [{"rec_texts": [], "dt_polys": [], "rec_scores": []}]
        with ocr_cache.scope():
            self.assertEqual(self.recognize(), [])
            self.assertEqual(self.recognize(), [])
        self.assertEqual(self.fake.calls, 1)

    def test_memory_scopes_are_isolated_and_nested_scope_inherits_parent(self):
        with ocr_cache.scope():
            self.recognize()
            with ocr_cache.scope():
                self.recognize()
        with ocr_cache.scope():
            self.recognize()
        self.assertEqual(self.fake.calls, 2)

    def test_shared_memory_scope_serializes_concurrent_contexts(self):
        self.fake.delay = .05
        results, errors = [], []

        def run():
            try:
                results.append(self.recognize())
            except BaseException as exc:
                errors.append(exc)

        with ocr_cache.scope():
            contexts = [copy_context(), copy_context()]
            threads = [threading.Thread(target=context.run, args=(run,)) for context in contexts]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(self.fake.calls, 1)

    def test_terminated_writer_releases_os_lock_and_partial_record_is_ignored(self):
        directory = self.root / "interrupted-cache"
        signature = {**self.signature, "lang": "chinese_cht"}
        key = ocr_cache.digest({"version": ocr_cache.VERSION, **ocr_cache.pixels(self.image), "engine": signature})
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        child = context.Process(target=_spawn_incomplete_writer,
                                args=(str(directory / (key + ".lock")),
                                      str(directory / (key + ".json.interrupted.tmp")), ready))
        child.start()
        try:
            self.assertTrue(ready.wait(10))
        finally:
            child.terminate()
            child.join(10)
        self.assertFalse(child.is_alive())
        with ocr_cache.scope(directory):
            lines = self.recognize()
        self.assertEqual(lines[0]["text"], "離線辨識")
        self.assertEqual(self.fake.calls, 1)

    def test_disk_cache_survives_closed_scope_and_loader_is_not_called(self):
        folder = self.root / "disk-cache"
        with ocr_cache.scope(folder):
            self.recognize()
        self.loader_mock.reset_mock()
        with ocr_cache.scope(folder):
            result = self.recognize()
        self.assertEqual(result[0]["text"], "離線辨識")
        self.assertEqual(self.loader_calls(), 0)

    def test_spawned_process_reuses_disk_cache(self):
        folder = self.root / "spawn-cache"
        self.signature = {"fake": "spawn"}
        with ocr_cache.scope(folder):
            self.recognize()
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process = context.Process(target=_spawn_probe,
                                  args=(str(folder), str(self.image), result_queue))
        process.start()
        process.join(30)
        if process.is_alive():
            process.terminate()
            process.join()
            self.fail("spawned cache probe timed out")
        self.assertEqual(process.exitcode, 0)
        status, result = result_queue.get(timeout=5)
        self.assertEqual(status, "ok", result)
        self.assertEqual(result[0]["text"], "離線辨識")

    def test_concurrent_disk_scope_computes_once(self):
        folder = self.root / "thread-cache"
        self.fake.delay = 0.08
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run():
            try:
                with ocr_cache.scope(folder):
                    barrier.wait(timeout=5)
                    results.append(self.recognize())
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(self.fake.calls, 1)

    def test_truncated_invalid_and_checksum_mismatch_records_recompute(self):
        for mutation in ("truncated", "identity", "checksum"):
            with self.subTest(mutation=mutation):
                folder = self.root / f"corrupt-{mutation}"
                with ocr_cache.scope(folder):
                    self.recognize()
                record_path = next(folder.glob("*.json"))
                if mutation == "truncated":
                    record_path.write_text('{"identity":', encoding="utf-8")
                else:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    if mutation == "identity":
                        record["identity"]["pixel_sha256"] = "bad"
                    else:
                        record["lines"][0]["text"] = "tampered"
                    record_path.write_text(json.dumps(record), encoding="utf-8")
                before = self.fake.calls
                with ocr_cache.scope(folder):
                    self.recognize()
                self.assertEqual(self.fake.calls, before + 1)

    def test_orphan_tmp_file_does_not_create_a_hit(self):
        folder = self.root / "tmp-cache"
        with ocr_cache.scope(folder):
            self.recognize()
        record_path = next(folder.glob("*.json"))
        record_path.unlink()
        (folder / (record_path.name + ".abandoned.tmp")).write_text("{}", encoding="utf-8")
        before = self.fake.calls
        with ocr_cache.scope(folder):
            self.recognize()
        self.assertEqual(self.fake.calls, before + 1)

    def test_ocr_exception_is_not_cached_or_retried(self):
        self.fake.error = RuntimeError("fake recognition failure")
        with ocr_cache.scope():
            with self.assertRaisesRegex(RuntimeError, "fake recognition failure"):
                self.recognize()
            self.assertEqual(self.fake.calls, 1)
            self.fake.error = None
            self.recognize()
        self.assertEqual(self.fake.calls, 2)

    def test_cache_io_oserrors_fall_back_to_one_recognition(self):
        # A read error disables the disk read for this call; a lock error falls
        # back to memory, and a write error leaves the returned OCR intact.
        cases = (
            ("read", patch.object(Path, "read_text", side_effect=PermissionError("read denied"))),
            ("lock", patch.object(ocr_cache, "_key_lock", side_effect=OSError("lock denied"))),
            ("write", patch.object(invocation_trace, "atomic_json", side_effect=OSError("write denied"))),
        )
        for name, failing_patch in cases:
            with self.subTest(operation=name):
                folder = self.root / f"io-{name}"
                if name == "read":
                    with ocr_cache.scope(folder):
                        self.recognize()
                before = self.fake.calls
                with failing_patch, ocr_cache.scope(folder):
                    lines = self.recognize()
                self.assertEqual(lines[0]["text"], "離線辨識")
                self.assertEqual(self.fake.calls, before + 1)

    def test_trace_reports_hit_zero_execution_original_source_and_purpose(self):
        with invocation_trace.capture() as trace:
            with ocr_cache.scope():
                with ocr_cache.purpose("cropped-region"):
                    first = self.recognize()
                with ocr_cache.purpose("second-use"):
                    second = self.recognize()
        self.assertEqual(first, second)
        result_events = [entry for entry in trace if entry.get("kind") == "ocr_result"]
        self.assertEqual(len(result_events), 2)
        miss, hit = result_events
        self.assertFalse(miss["cache_hit"])
        self.assertTrue(hit["cache_hit"])
        self.assertEqual(hit["seconds"], 0)
        self.assertEqual(hit["result_source"]["image"], str(self.image))
        self.assertEqual(hit["result_source"]["purpose"], "cropped-region")
        self.assertEqual(hit["purpose"], "second-use")


if __name__ == "__main__":
    unittest.main()
