"""Offline regressions for the two-stage page routing contract."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw

from app.core import barcode, invocation_trace, page_layout, page_ocr


def _temporary_directory():
    root = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


def _line(text, x, y, *, width=220, height=24):
    return {"text": text, "box": [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]}


def _cheque_lines(count=1):
    lines = []
    for index, x in enumerate((35, 370, 705)[:count]):
        lines.extend((
            _line("支票", x, 100),
            _line(f"支票號碼: {12345678 + index}", x, 180),
            _line("日期 2026/10/11", x, 260),
            _line(f"金額 {1000 + index}", x, 340),
        ))
    return lines


def _page(*, layout_kind="single", doc_type="cheque", confidence="high", **extra):
    return {"type": doc_type, "rotate": 0, "reason": "synthetic page route",
            "content_kind": "document", "layout_kind": layout_kind,
            "confidence": confidence, **extra}


def _region_reply(line_ids, *, doc_type="cheque", rotate=0):
    return {"regions": [{"type": doc_type, "rotate": rotate, "reason": "complete cheque",
                         "confidence": "high", "box": None, "line_ids": ids}
                        for ids in line_ids]}


class PageRoutingV3Tests(unittest.TestCase):
    def _detect(self, image, replies, *, mode="ocr_text", lines=None, text="", settings_ocr="disabled/none"):
        requests = []

        def invoke(_chat, request):
            requests.append(request)
            return json.dumps(replies.pop(0), ensure_ascii=False)

        qr = {"detections": [], "source_size": [1000, 1000]}
        settings = {"structure": "fake/model", "ocr": settings_ocr}
        with (patch("app.core.model_factory.get_chat_model", return_value=object()) as model,
              patch("app.core.pipeline._invoke_text", side_effect=invoke),
              patch("app.core.barcode.decode_diagnostics", return_value=qr),
              patch("app.core.model_coordinates.for_image", return_value={"verified": False, "reason": "test profile"}),
              patch("app.core.settings_store.get_settings", return_value=settings)):
            result = page_layout.detect(str(image), mode=mode, ocr_lines=lines, ocr_text=text,
                                        model_setting="fake/model", native_path=str(image))
        self.assertEqual(model.call_count, 1)
        return result, requests

    def test_single_with_sixteen_first_round_regions_repairs_and_keeps_whole_page(self):
        with _temporary_directory() as temporary:
            image = Path(temporary) / "single.png"
            Image.new("RGB", (1000, 1000), "white").save(image)
            original = hashlib.sha256(image.read_bytes()).hexdigest()
            invalid = _page(layout_kind="single", regions=[{"type": "cheque"}] * 16)
            result, requests = self._detect(image, [invalid, _page()], lines=_cheque_lines(), text="支票 支票號碼 12345678 日期 2026/10/11 金額 1000")

            self.assertEqual(len(requests), 2, "invalid first output must cause the one allowed repair round")
            self.assertEqual(result["regions"][0]["box"], [0, 0, 1000, 1000])
            self.assertEqual(result["regions"][0]["coordinate_source"], "whole_page")
            self.assertTrue(result["auto_split"])
            self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), original)
            self.assertFalse(any("多單據定位" in str(request) for request in requests))

    def test_single_table_frame_never_crops_the_page_header(self):
        with _temporary_directory() as temporary:
            image = Path(temporary) / "table-with-header.png"
            source = Image.new("RGB", (1000, 1000), "white")
            draw = ImageDraw.Draw(source)
            draw.text((45, 28), "公司抬頭 Synthetic Header", fill="black")
            draw.rectangle((75, 245, 925, 895), outline="black", width=4)
            source.save(image)
            lines = [
                _line("公司抬頭 Synthetic Header", 40, 25, width=420),
                _line("支票", 120, 300), _line("支票號碼: 12345678", 120, 390),
                _line("日期 2026/10/11", 120, 480), _line("金額 1000", 120, 570),
            ]
            result, _ = self._detect(image, [_page()], lines=lines,
                                     text="公司抬頭 支票 支票號碼 12345678 日期 2026/10/11 金額 1000")
            self.assertEqual(result["regions"][0]["box"], [0, 0, 1000, 1000])
            crop = Path(temporary) / "whole-page.png"
            page_layout.crop(str(image), result["regions"][0], crop)
            with Image.open(image) as before, Image.open(crop) as after:
                self.assertIsNone(ImageChops.difference(before, after).getbbox())

    def test_numeric_strings_are_rejected_without_rewriting_raw_coordinates(self):
        raw = {"type":"cheque","rotate":0,"reason":"synthetic","confidence":"high",
               "box":["20",20,450,900]}
        layout = {"type":"cheque","rotate":0,"reason":"synthetic","content_kind":"document",
                  "layout_kind":"multiple","regions":[raw, {**raw,"box":[550,20,980,900]}]}
        result = page_layout.validate_layout(layout,evidence={"frames":[],"anchors":[]})
        rejected = next(issue for issue in result["issues"] if isinstance(issue,dict))
        self.assertEqual(rejected["raw"]["box"],["20",20,450,900])
        self.assertFalse(result["auto_split"])
        text_layout = {**layout,"regions":[{**raw,"box":None,"line_ids":["0"]}]}
        result = page_layout.validate_layout(text_layout,mode="ocr_text",ocr_lines=_cheque_lines(),
                                            size=(1000,1000),evidence={"frames":[],"anchors":[]})
        self.assertFalse(result["auto_split"])
        self.assertEqual(result["regions"],[])

    def test_three_cheques_use_complete_ocr_groups_without_uncalibrated_model_boxes(self):
        with _temporary_directory() as temporary:
            image = Path(temporary) / "three-cheques.png"
            Image.new("RGB", (1000, 1000), "white").save(image)
            lines = _cheque_lines(3)
            ids = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]
            page = _page(layout_kind="multiple")
            result, requests = self._detect(image, [page, _region_reply(ids)], lines=lines,
                                            text="支票 支票號碼 日期 金額")

            self.assertTrue(result["auto_split"])
            self.assertEqual(len(result["regions"]), 3)
            self.assertTrue(all(region["coordinate_source"] == "ocr" for region in result["regions"]))
            self.assertTrue(all(region["ocr_region_box"] != region["box"] for region in result["regions"]))
            self.assertEqual([region["line_ids"] for region in result["regions"]], ids)
            evidence_groups = result["source_evidence"]["text_boxes"]
            for group_ids in ids:
                group_text = " ".join(evidence_groups[index]["text"] for index in group_ids)
                self.assertRegex(group_text, r"支票")
                self.assertRegex(group_text, r"支票號碼")
                self.assertRegex(group_text, r"日期")
                self.assertRegex(group_text, r"金額")
            self.assertIn("box=null", str(requests[1:]))
            self.assertFalse(any("image_url" in str(request) for request in requests))

    def test_conflicting_single_page_and_unknown_region_type_each_get_one_repair(self):
        with _temporary_directory() as temporary:
            image = Path(temporary) / "three-cheques.png"
            Image.new("RGB", (1000, 1000), "white").save(image)
            lines = _cheque_lines(3)
            groups = [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]
            first_page = _page(layout_kind="single")
            multiple_page = _page(layout_kind="multiple")
            unknown_type = _region_reply(groups)
            unknown_type["regions"][1]["type"] = "mystery_document"
            result, requests = self._detect(image, [first_page, multiple_page, unknown_type, _region_reply(groups)],
                                            lines=lines, text="支票 支票號碼 日期 金額")
            self.assertEqual(len(requests), 4)
            self.assertTrue(result["auto_split"])
            self.assertEqual(len(result["regions"]), 3)
            self.assertEqual(result["layout_kind"], "multiple")

    def test_single_judgment_conflicting_with_three_complete_groups_is_repaired(self):
        with _temporary_directory() as temporary:
            image = Path(temporary) / "three-cheques.png"
            Image.new("RGB", (1000, 1000), "white").save(image)
            result, requests = self._detect(image, [_page(layout_kind="single"), _page(layout_kind="multiple"),
                                                     _region_reply([[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]])],
                                            lines=_cheque_lines(3), text="支票 支票號碼 日期 金額")
            self.assertEqual(len(requests), 3)
            self.assertEqual(result["layout_kind"], "multiple")
            self.assertEqual(len(result["regions"]), 3)

    def test_page_ocr_stage_records_original_frame_and_direct_vlm_skips_disabled_ocr(self):
        with _temporary_directory() as temporary:
            image = Path(temporary) / "ocr-stage.png"
            Image.new("RGB", (1000, 1000), "white").save(image)
            lines = _cheque_lines()
            ocr_settings = {"structure": "fake/model", "ocr": "fake/ocr"}

            def read_ocr(*_args, **_kwargs):
                invocation_trace.add({"kind": "page_ocr", "rotation": 90, "target_frame": "original_page"})
                return lines, "支票 支票號碼 12345678 日期 2026/10/11 金額 1000", 90, {"rotation": 90}

            with invocation_trace.capture() as trace:
                with (patch("app.core.model_factory.get_chat_model", return_value=object()),
                      patch("app.core.pipeline._invoke_text", return_value=json.dumps(_page())),
                      patch("app.core.barcode.decode_diagnostics", return_value={"detections": [], "source_size": [1000, 1000]}),
                      patch("app.core.model_coordinates.for_image", return_value={"verified":False,"input_path":str(image),"input_size":[1000,1000],"source_size":[1000,1000],"preprocessing":{}}),
                      patch("app.core.settings_store.get_settings", return_value=ocr_settings),
                      patch("app.core.page_ocr.read", side_effect=read_ocr) as reader):
                    result = page_layout.detect(str(image), mode="image", ocr_text="",
                                                model_setting="fake/model", native_path=str(image))
                self.assertEqual(reader.call_count, 1)
                self.assertEqual(result["image_transforms"]["rotation"], 90)
                self.assertTrue(any(event["kind"] == "page_ocr" and event["target_frame"] == "original_page" for event in trace))

            with (patch("app.core.model_factory.get_chat_model", return_value=object()),
                  patch("app.core.pipeline._invoke_text", return_value=json.dumps(_page())),
                  patch("app.core.barcode.decode_diagnostics", return_value={"detections": [], "source_size": [1000, 1000]}),
                  patch("app.core.model_coordinates.for_image", return_value={"verified":False,"input_path":str(image),"input_size":[1000,1000],"source_size":[1000,1000],"preprocessing":{}}),
                  patch("app.core.settings_store.get_settings", return_value={"structure": "fake/model", "ocr": "disabled/none"}),
                  patch("app.core.page_ocr.read") as reader):
                page_layout.detect(str(image), mode="image", ocr_text="", model_setting="fake/model",
                                   native_path=str(image))
                reader.assert_not_called()

    def test_text_routing_requests_never_include_an_image(self):
        with _temporary_directory() as temporary:
            image = Path(temporary) / "text-only.png"
            Image.new("RGB", (1000, 1000), "white").save(image)
            _, requests = self._detect(image, [_page()], lines=_cheque_lines(), text="支票 支票號碼 12345678 日期 金額")
            self.assertTrue(requests)
            self.assertFalse(any("image_url" in str(request) for request in requests))

    def test_page_ocr_cache_maps_each_orientation_back_to_the_source(self):
        with _temporary_directory() as temporary:
            folder = Path(temporary) / "cache"
            folder.mkdir()
            source = Path(temporary) / "source.png"
            Image.new("RGB", (100, 200), "white").save(source)
            setting = "fake/ocr"
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            identity = {"version": page_ocr.VERSION, "source_sha256": source_hash,
                        "ocr": setting, "max_edge": 2400}
            key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            point = [20, 30]
            expected = {
                0: [200.0, 300.0],
                90: [300.0, 1800.0],
                180: [800.0, 1700.0],
                270: [700.0, 200.0],
            }
            for rotation, mapped in expected.items():
                with self.subTest(rotation=rotation):
                    ocr_size = [200, 100] if rotation in (90, 270) else [100, 200]
                    record = {"identity": identity, "rotation": rotation, "size": ocr_size,
                              "lines": [{"text": "mapped", "box": [point, point, point, point]}],
                              "text": "mapped", "path": "cached-native.png"}
                    (folder / f"{key}.json").write_text(json.dumps(record), encoding="utf-8")
                    with invocation_trace.capture() as trace, \
                            patch("app.core.model_factory.get_ocr") as get_ocr:
                        lines, text, actual_rotation, transforms = page_ocr.read(
                            str(source), (1000, 2000), folder=folder, setting=setting)
                    get_ocr.assert_not_called()
                    self.assertEqual(text, "mapped")
                    self.assertEqual(actual_rotation, rotation)
                    self.assertEqual(lines[0]["box"][0], mapped)
                    self.assertEqual(transforms["target_frame"], "original_page")
                    event = next(event for event in trace if event["kind"] == "page_ocr")
                    self.assertEqual(event["rotation"], rotation)
                    self.assertEqual(event["target_frame"], "original_page")


if __name__ == "__main__":
    unittest.main()
