"""Offline tests for processor-backed coordinate calibration."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.core import geometry, model_coordinates
from scripts import calibrate_model_coordinates as calibration


class CoordinateCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=".coordinate-calibration-test-", dir=Path(__file__).resolve().parents[1]))
        self.identity = {"provider": "ollama", "backend_url": "http://localhost:11434",
                         "server_version": "test-processor-v1", "model": "qwen2.5vl:7b",
                         "model_digest": "a" * 64}
        self.source_contents = {
            "server.go": b"checked server source fixture",
            "pin.txt": b"b11081\n",
            "clip.cpp": b"checked clip source fixture",
            "clip-model.h": b"checked clip model source fixture",
            "mtmd-image.cpp": b"checked mtmd image source fixture",
        }
        base = "https://github.com/ollama/ollama/blob/b11081/app/"
        self.provenance = {
            base + "server.go": hashlib.sha256(self.source_contents["server.go"]).hexdigest(),
            base + "pin.txt": hashlib.sha256(self.source_contents["pin.txt"]).hexdigest(),
            "https://github.com/ggerganov/llama.cpp/blob/b11081/tools/mtmd/clip.cpp": hashlib.sha256(self.source_contents["clip.cpp"]).hexdigest(),
            "https://github.com/ggerganov/llama.cpp/blob/b11081/tools/mtmd/clip-model.h": hashlib.sha256(self.source_contents["clip-model.h"]).hexdigest(),
            "https://github.com/ggerganov/llama.cpp/blob/b11081/tools/mtmd/mtmd-image.cpp": hashlib.sha256(self.source_contents["mtmd-image.cpp"]).hexdigest(),
        }
        self.processor = {"kind": "qwen_smart_resize", "server_version": "test-processor-v1",
                          "factor": 28, "min_pixels": 802816, "max_pixels": 3211264,
                          "padding_mode": "ceil_center",
                          "source_url": "https://github.com/ggerganov/llama.cpp/blob/b11081/tools/mtmd/mtmd-image.cpp",
                          "source_sha256": hashlib.sha256(self.source_contents["mtmd-image.cpp"]).hexdigest(),
                          "provenance": self.provenance}
        self.spec = {"version": 1, **self.identity, "processor": self.processor}
        self.spec_path = self.root / "processor.json"
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        content_by_url = dict(zip(self.provenance, self.source_contents.values()))

        def get(url, timeout):
            self.assertEqual(timeout, 20)
            return SimpleNamespace(content=content_by_url[url], raise_for_status=lambda: None)
        self.get = get

    def tearDown(self):
        shutil.rmtree(self.root)

    def _frame(self, size, _processor):
        width, height = size
        pad = 14
        return {"visual_size": [width + pad * 2, height + pad * 2],
                "content_size": [width, height], "padding": [pad, pad, pad, pad],
                "transform": [[1000 / width, 0, -1000 * pad / width],
                              [0, 1000 / height, -1000 * pad / height]]}

    def _checked_registry(self):
        return patch.dict(model_coordinates.CHECKED_PROCESSORS,
                          {self.identity["server_version"]: self.processor})

    def _chat(self, unit, fail_at=None):
        calls = []

        def fake_chat(_base, _model, image, _prompt):
            index = next(i for i, (w, h, *_rest) in enumerate(calibration.TARGETS)
                         if (w, h) == image.size)
            calls.append(index)
            if fail_at is not None and len(calls) == fail_at:
                raise calibration.ChatCallError("mock request failed", {
                    "http_status": 503, "raw_reply": {"error": "unavailable"},
                    "backend": {"eval_count": 17, "done_reason": "error"}})
            _, _, rect, _ = calibration.TARGETS[index]
            pad = 14
            box = [rect[0] + pad, rect[1] + pad, rect[2] + pad, rect[3] + pad]
            if unit == "normalized_1000":
                fw, fh = image.width + 2 * pad, image.height + 2 * pad
                box = [round(box[0] * 1000 / fw), round(box[1] * 1000 / fh),
                       round(box[2] * 1000 / fw), round(box[3] * 1000 / fh)]
            return {"parsed": {"targets": {f"target_{index + 1}": box}},
                    "raw_reply": {"message": {"content": "mock"}, "eval_count": 12, "done_reason": "stop"},
                    "backend": {"eval_count": 12, "done_reason": "stop"}}
        return fake_chat, calls

    def _run(self, output, chat, identity_fn=None):
        identity_fn = identity_fn or (lambda *_: self.identity)
        with self._checked_registry():
            return calibration.run_calibration(output_dir=output, spec_path=self.spec_path,
                model=self.identity["model"], base_url=self.identity["backend_url"],
                identity_fn=identity_fn, chat_fn=chat, get=self.get)

    def test_success_saves_twelve_raw_calls_and_atomically_registers(self):
        fake_chat, calls = self._chat("pixels")
        registry_path = self.root / "registry.json"
        registry_path.write_text('{"version":1,"profiles":[]}', encoding="utf-8")
        with (self._checked_registry(),
              patch.object(model_coordinates, "visual_frame", side_effect=self._frame, create=True),
              patch.object(model_coordinates, "profile_path", return_value=registry_path)):
            result = self._run(self.root / "run", fake_chat)
        self.assertTrue(result["verified"], result["reason"])
        self.assertEqual(result["calls"], 12)
        self.assertEqual(len(calls), 12)
        self.assertEqual(result["per_unit_iou"]["pixels"]["count"], 12)
        call_file = json.loads((self.root / "run" / "calls" / "call-01.json").read_text(encoding="utf-8"))
        self.assertEqual(call_file["backend"]["eval_count"], 12)
        self.assertNotIn("images", json.dumps(call_file))
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(registry["profiles"][0]["unit"], "pixels")
        self.assertEqual(registry["profiles"][0]["input_preprocessing"],model_coordinates.INPUT_PREPROCESSING)

    def test_enabled_profile_requires_matching_input_preprocessing_rule(self):
        profile = {**self.identity,"verified":True,"unit":"pixels","processor":self.processor,
                   "input_preprocessing":dict(model_coordinates.INPUT_PREPROCESSING),
                   "calibration":{"version":1,"targets":12,"min_iou":.95}}
        with self._checked_registry():
            context = model_coordinates.from_profile(profile,[640,480],identity=self.identity)
            self.assertTrue(context["verified"])
            profile["input_preprocessing"]["version"] = 0
            context = model_coordinates.from_profile(profile,[640,480],identity=self.identity)
            self.assertFalse(context["verified"])
            profile.pop("input_preprocessing")
            self.assertFalse(model_coordinates.from_profile(profile,[640,480],identity=self.identity)["verified"])

    def test_padding_transform_maps_visual_box_to_original_normalized_frame(self):
        frame = {"visual_size": [120, 120], "content_size": [100, 100],
                 "padding": [10, 10, 10, 10],
                 "transform": [[10, 0, -100], [0, 10, -100]]}
        self.assertEqual(calibration._to_system_box([20, 30, 60, 80], "pixels", frame),
                         [100, 200, 500, 700])
        normalized_visual_box = [167, 250, 500, 667]
        mapped = calibration._to_system_box(normalized_visual_box, "normalized_1000", frame)
        self.assertTrue(geometry.iou(mapped, [100, 200, 500, 700]) >= .99)

    def test_changed_processor_parameters_are_rejected_before_model_calls(self):
        mutated = json.loads(json.dumps(self.spec))
        mutated["processor"]["min_pixels"] = 3136
        self.spec_path.write_text(json.dumps(mutated), encoding="utf-8")
        fake_chat, calls = self._chat("pixels")
        with self._checked_registry():
            result = self._run(self.root / "bad-processor", fake_chat)
        self.assertFalse(result["verified"])
        self.assertEqual(result["calls"], 0)
        self.assertEqual(calls, [])

    def test_source_download_hash_mismatch_is_rejected_before_model_calls(self):
        fake_chat, calls = self._chat("pixels")
        def bad_get(url, timeout):
            return SimpleNamespace(content=b"different bytes", raise_for_status=lambda: None)
        with self._checked_registry():
            result = calibration.run_calibration(output_dir=self.root / "bad-source", spec_path=self.spec_path,
                model=self.identity["model"], base_url=self.identity["backend_url"],
                identity_fn=lambda *_: self.identity, chat_fn=fake_chat, get=bad_get)
        self.assertFalse(result["verified"])
        self.assertEqual(result["calls"], 0)
        self.assertEqual(calls, [])

    def test_normalized_unit_scores_portrait_and_landscape_frames(self):
        responses = []
        pad = 14
        for index, (width, height, rect, _) in enumerate(calibration.TARGETS):
            fw, fh = width + 2 * pad, height + 2 * pad
            box = [rect[0] + pad, rect[1] + pad, rect[2] + pad, rect[3] + pad]
            normalized = [round(box[0] * 1000 / fw), round(box[1] * 1000 / fh),
                          round(box[2] * 1000 / fw), round(box[3] * 1000 / fh)]
            responses.extend([(index, {"targets": {f"target_{index + 1}": normalized}})] * 2)
        with patch.object(model_coordinates, "visual_frame", side_effect=self._frame, create=True):
            scored = calibration.score_responses(responses, self.processor)
        self.assertEqual(scored["unit"], "normalized_1000")
        self.assertEqual(scored["per_unit_iou"]["normalized_1000"]["count"], 12)
        self.assertEqual(scored["observations"][4]["input_size"], [400, 800])
        self.assertEqual(scored["observations"][8]["input_size"], [503, 713])

    def test_dry_run_and_unsupported_processor_make_zero_model_calls(self):
        fake_chat, calls = self._chat("pixels")
        with self._checked_registry():
            result = calibration.run_calibration(output_dir=self.root / "dry", spec_path=self.spec_path,
                model=self.identity["model"], base_url=self.identity["backend_url"],
                identity_fn=lambda *_: self.identity, chat_fn=fake_chat, get=self.get, dry_run=True)
        self.assertFalse(result["verified"])
        self.assertEqual(result["calls"], 0)
        self.assertTrue((self.root / "dry" / "target_1.png").is_file())
        self.assertEqual(calls, [])

        mutated = json.loads(json.dumps(self.spec))
        mutated["processor"]["kind"] = "unknown_processor"
        self.spec_path.write_text(json.dumps(mutated), encoding="utf-8")
        with self._checked_registry():
            result = self._run(self.root / "unsupported", fake_chat)
        self.assertFalse(result["verified"])
        self.assertEqual(result["calls"], 0)
        self.assertEqual(calls, [])

    def test_reversed_and_out_of_frame_boxes_are_rejected(self):
        frame = self._frame((100, 100), self.processor)
        for unit, box in (("pixels", [80, 80, 20, 20]), ("pixels", [0, 0, 121, 90]),
                          ("normalized_1000", [800, 800, 200, 200]),
                          ("normalized_1000", [0, 0, 1001, 900])):
            with self.assertRaises((calibration.CalibrationError, ValueError)):
                calibration._to_system_box(box, unit, frame)

    def test_for_image_caps_default_size_and_disables_mismatched_profile(self):
        image_path = self.root / "large.png"
        Image.new("RGB", (2360, 1180), "white").save(image_path)
        registry_path = self.root / "registry.json"
        profile = {**self.identity, "id": "saved", "unit": "pixels", "verified": True,
                   "processor": self.processor,
                   "calibration": {"version": 1, "targets": 12, "min_iou": .95}}
        registry_path.write_text(json.dumps({"version": 1, "profiles": [profile]}), encoding="utf-8")
        changed_identity = {**self.identity, "model_digest": "c" * 64}
        with (patch.object(model_coordinates, "profile_path", return_value=registry_path),
              patch("app.core.settings_store.parse_setting", return_value=("ollama", self.identity["model"])),
              patch.object(model_coordinates, "ollama_identity", return_value=changed_identity),
              patch.object(model_coordinates.invocation_trace, "add")):
            context = model_coordinates.for_image(image_path, "ollama:qwen2.5vl:7b")
        self.assertEqual(context["source_size"], [2360, 1180])
        self.assertEqual(context["input_size"], [1180, 590])
        self.assertFalse(context["verified"])
        self.assertEqual(model_coordinates.box_schema(context),
                         {"type": "object", "properties": {}, "additionalProperties": False})

    def test_checked_processor_metadata_does_not_enable_uncalibrated_boxes(self):
        image_path = self.root / "large.png"
        Image.new("RGB", (2360, 1180), "white").save(image_path)
        original = image_path.read_bytes()
        registry_path = self.root / "registry.json"
        registry_path.write_text('{"version":1,"profiles":[]}', encoding="utf-8")
        with (self._checked_registry(),
              patch.object(model_coordinates, "profile_path", return_value=registry_path),
              patch.object(model_coordinates, "ollama_identity", return_value=self.identity)):
            context = model_coordinates.for_image(image_path,"ollama/qwen2.5vl:7b")
        self.assertFalse(context["verified"])
        self.assertTrue(context["processor_source_checked"])
        self.assertEqual(context["model_digest"],self.identity["model_digest"])
        self.assertEqual(context["visual_size"],model_coordinates.visual_frame([1180,590],self.processor)["visual_size"])
        sent = Path(context["input_path"])
        self.assertEqual(hashlib.sha256(sent.read_bytes()).hexdigest(),context["sent_sha256"])
        import base64
        from app.core.pipeline import _data_uri
        self.assertEqual(base64.b64decode(_data_uri(str(image_path)).split(",",1)[1]),sent.read_bytes())
        with Image.open(sent) as image:
            self.assertEqual(image.size,(1180,590))
        self.assertEqual(image_path.read_bytes(),original)
        with self.assertRaisesRegex(ValueError,"unverified"):
            model_coordinates.convert([0,0,100,100],context)

    def test_registry_digest_changes_when_registry_changes(self):
        registry_path = self.root / "registry.json"
        with patch.object(model_coordinates, "profile_path", return_value=registry_path):
            registry_path.write_text('{"version":1,"profiles":[]}', encoding="utf-8")
            before = model_coordinates.registry_digest()
            registry_path.write_text(json.dumps({"version": 1, "profiles": [self.identity]}), encoding="utf-8")
            after = model_coordinates.registry_digest()
        self.assertNotEqual(before, after)

    def test_mid_call_failure_saves_diagnostics_and_preserves_registry(self):
        fake_chat, calls = self._chat("pixels", fail_at=7)
        registry_path = self.root / "registry.json"
        original = '{"version":1,"profiles":[{"id":"prior","verified":true}]}'
        registry_path.write_text(original, encoding="utf-8")
        with (self._checked_registry(),
              patch.object(model_coordinates, "visual_frame", side_effect=self._frame, create=True),
              patch.object(model_coordinates, "profile_path", return_value=registry_path)):
            result = self._run(self.root / "failed", fake_chat)
        self.assertFalse(result["verified"])
        self.assertEqual(result["calls"], 7)
        self.assertLessEqual(result["calls"], 12)
        failed = json.loads((self.root / "failed" / "calls" / "call-07.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["backend"]["eval_count"], 17)
        report = json.loads((self.root / "failed" / "calibration_report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["call_evidence"]), 7)
        self.assertEqual(registry_path.read_text(encoding="utf-8"), original)

    def test_runtime_change_after_calibration_blocks_registration(self):
        fake_chat, _ = self._chat("pixels")
        registry_path = self.root / "registry.json"
        original = '{"version":1,"profiles":[]}'
        registry_path.write_text(original, encoding="utf-8")
        identities = iter([self.identity, {**self.identity, "model_digest": "c" * 64}])
        with (self._checked_registry(),
              patch.object(model_coordinates, "visual_frame", side_effect=self._frame, create=True),
              patch.object(model_coordinates, "profile_path", return_value=registry_path)):
            result = self._run(self.root / "changed-runtime", fake_chat, lambda *_: next(identities))
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "runtime identity changed during calibration")
        self.assertEqual(registry_path.read_text(encoding="utf-8"), original)

    def test_ollama_chat_sets_bounded_deterministic_options_and_keeps_raw_reply(self):
        raw = {"message": {"content": '{"targets": {}}'}, "prompt_eval_count": 40,
               "eval_count": 9, "done_reason": "stop"}
        captured = {}

        class Response:
            status_code = 200
            text = json.dumps(raw)
            def json(self): return raw
            def raise_for_status(self): return None

        def post(url, *, json, timeout):
            captured.update(url=url, payload=json, timeout=timeout)
            return Response()
        image = Image.new("RGB", (32, 24), "white")
        result = calibration.ollama_chat("http://localhost:11434", "qwen2.5vl:7b", image, "find box", post=post)
        self.assertEqual(captured["payload"]["options"], {"num_predict": 512, "temperature": 0})
        self.assertEqual(result["raw_reply"], raw)
        self.assertEqual(result["backend"], {"prompt_eval_count": 40, "eval_count": 9, "done_reason": "stop"})
        self.assertIn("images", captured["payload"]["messages"][0])

    def test_identity_requires_matching_vision_architecture_metadata(self):
        calls = []
        class Response:
            def __init__(self, value): self.value = value
            def json(self): return self.value
            def raise_for_status(self): return None
        def get(url, timeout):
            calls.append(url)
            if url.endswith("/api/version"):
                return Response({"version": "0.35.0"})
            return Response({"models": [{"name": self.identity["model"], "digest": "a" * 64}]})
        def post(url, *, json, timeout):
            calls.append((url, json))
            return Response({"model_info": {"general.architecture": "qwen25vl",
                "qwen25vl.vision.patch_size": 14, "qwen25vl.vision.spatial_merge_size": 2}})
        identity = calibration.ollama_identity_at(self.identity["backend_url"], self.identity["model"], get=get, post=post)
        self.assertEqual(identity["server_version"], "0.35.0")
        self.assertTrue(any(isinstance(call, tuple) and call[1] == {"model": self.identity["model"]} for call in calls))

        def wrong_post(url, *, json, timeout):
            return Response({"model_info": {"general.architecture": "qwen2vl",
                "qwen25vl.vision.patch_size": 14, "qwen25vl.vision.spatial_merge_size": 2}})
        with self.assertRaises(calibration.CalibrationError):
            calibration.ollama_identity_at(self.identity["backend_url"], self.identity["model"], get=get, post=wrong_post)

    def test_ollama_http_and_parse_failures_retain_response_evidence(self):
        image = Image.new("RGB", (32, 24), "white")
        class Response:
            status_code = 503
            text = '{"error":"offline"}'
            def json(self): return {"error": "offline"}
            def raise_for_status(self): raise RuntimeError("HTTP 503")
        with self.assertRaises(calibration.ChatCallError) as caught:
            calibration.ollama_chat("http://localhost:11434", "qwen2.5vl:7b", image, "find", post=lambda *_a, **_kw: Response())
        self.assertEqual(caught.exception.evidence["raw_reply"], {"error": "offline"})
        self.assertEqual(caught.exception.evidence["http_status"], 503)

        class BadJSONResponse(Response):
            status_code = 200
            text = '{"message":{"content":"not-json"},"eval_count":3}'
            def json(self): return {"message": {"content": "not-json"}, "eval_count": 3}
            def raise_for_status(self): return None
        with self.assertRaises(calibration.ChatCallError) as parse_error:
            calibration.ollama_chat("http://localhost:11434", "qwen2.5vl:7b", image, "find", post=lambda *_a, **_kw: BadJSONResponse())
        self.assertEqual(parse_error.exception.evidence["backend"]["eval_count"], 3)
        self.assertEqual(parse_error.exception.evidence["raw_reply"]["message"]["content"], "not-json")


if __name__ == "__main__":
    unittest.main()
