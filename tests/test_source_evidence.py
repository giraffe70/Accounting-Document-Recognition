"""Offline source evidence and candidate parsing regressions."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw

from app.core import barcode, image_evidence, normalization, page_layout, supplement


def _tempdir():
    root = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


def _route(regions, *, layout_kind="multiple", route_type="invoice"):
    return {"type": route_type, "rotate": 0, "reason": "synthetic layout",
            "content_kind": "document", "layout_kind": layout_kind, "regions": regions}


def _region(box, doc_type="invoice"):
    return {"type": doc_type, "rotate": 0, "reason": "document", "confidence": "high", "box": box}


class PageSourceEvidenceTests(unittest.TestCase):
    def test_real_source_image_finds_three_document_boundaries_and_routes_them(self):
        with _tempdir() as temporary:
            source = Path(temporary) / "three-documents.png"
            image = Image.new("RGB", (1200, 900), "white")
            draw = ImageDraw.Draw(image)
            for left in (35, 435, 835):
                right, top, bottom = left + 330, 110, 790
                draw.rectangle((left, top, right, bottom), outline="black", width=4)
                # At least three separate content rows inside each complete source frame.
                for row in range(4):
                    y = top + 100 + row * 120
                    draw.line((left + 35, y, right - 35 - (row % 2) * 30, y), fill="black", width=5)
            image.save(source)

            evidence = image_evidence.layout_evidence(source)
            self.assertEqual(len(evidence["frames"]), 3, evidence["frames"])
            lines = []
            ids = []
            for left in (35, 435, 835):
                start = len(lines)
                lines.extend([
                    {"text": "支票", "box": [[left+35, 220], [left+255, 220], [left+255, 244], [left+35, 244]]},
                    {"text": f"支票號碼 {12345678 + len(ids)}", "box": [[left+35, 300], [left+255, 300], [left+255, 324], [left+35, 324]]},
                    {"text": "日期 2026/10/11", "box": [[left+35, 380], [left+255, 380], [left+255, 404], [left+35, 404]]},
                    {"text": "金額 1000", "box": [[left+35, 460], [left+255, 460], [left+255, 484], [left+35, 484]]},
                ])
                ids.append(list(range(start, start + 4)))
            page = {"type": "cheque", "rotate": 0, "reason": "three separate cheques",
                    "content_kind": "document", "layout_kind": "multiple", "confidence": "high"}
            region_reply = {"regions": [_region(None, "cheque") | {"line_ids": group} for group in ids]}
            replies = iter((page, region_reply))
            requests = []

            def invoke(_chat, request):
                requests.append(request)
                return json.dumps(next(replies), ensure_ascii=False)

            with (patch("app.core.model_factory.get_chat_model", return_value=object()),
                  patch("app.core.pipeline._invoke_text", side_effect=invoke),
                  patch("app.core.barcode.decode_diagnostics", return_value={"detections": [], "source_size": [1200, 900]}),
                  patch("app.core.model_coordinates.for_image", return_value={"verified": False}),
                  patch("app.core.settings_store.get_settings", return_value={"structure": "fake/model", "ocr": "disabled/none"})):
                result = page_layout.detect(str(source), mode="ocr_text", ocr_lines=lines,
                                            ocr_text="支票 支票號碼 日期 金額", model_setting="fake/model",
                                            native_path=str(source))
            self.assertEqual(len(requests), 2)  # page-only route followed by regions-only routing
            self.assertIn("禁止輸出regions", str(requests[0]))
            self.assertIn("僅輸出regions", str(requests[1]))
            self.assertTrue(result["auto_split"])
            self.assertEqual(len(result["regions"]), 3)

    def test_real_source_image_ignores_table_cells_empty_frames_and_single_line_cells(self):
        with _tempdir() as temporary:
            table_path = Path(temporary) / "table.png"
            table = Image.new("RGB", (1200, 900), "white")
            draw = ImageDraw.Draw(table)
            left, top, right, bottom = 80, 100, 1120, 800
            draw.rectangle((left, top, right, bottom), outline="black", width=4)
            row_height = (bottom-top) // 5
            for row in range(5):
                row_top, row_bottom = top + row*row_height, top + (row+1)*row_height
                for column in range(1, 5):
                    x = left + column * (right-left) // 5
                    draw.line((x, row_top+12, x, row_bottom-12), fill="black", width=3)
                if row:
                    y = row_top
                    draw.line((left+12, y, right-12, y), fill="black", width=3)
                # Simulated text stroke in each one-line cell; keep it away from
                # the cell walls so it cannot create another closed boundary.
                for column in range(5):
                    cell_left = left + column*(right-left)//5
                    y = row_top + row_height//2
                    draw.line((cell_left+18, y, cell_left+(right-left)//5-18, y), fill="black", width=2)
            table.save(table_path)
            table_evidence = image_evidence.layout_evidence(table_path)
            self.assertEqual(len(table_evidence["frames"]), 1, table_evidence["frames"])

            for name, content in (("empty", "empty"), ("single-line", "single-line")):
                with self.subTest(name=name):
                    path = Path(temporary) / f"{name}.png"
                    sample = Image.new("RGB", (1200, 900), "white")
                    sample_draw = ImageDraw.Draw(sample)
                    sample_draw.rectangle((300, 200, 900, 700), outline="black", width=4)
                    if content == "single-line":
                        sample_draw.line((350, 350, 850, 350), fill="black", width=4)
                    sample.save(path)
                    evidence = image_evidence.layout_evidence(path)
                    self.assertEqual(evidence["frames"], [])
                    self.assertIsNone(image_evidence.single_document(evidence))

    def test_sixteen_text_strips_are_rejected_while_complete_page_text_supports_only_whole_page(self):
        strips = [_region([0, i * 60, 1000, i * 60 + 30]) for i in range(16)]
        lines = [
            {"text": "收據", "box": [[100, 30], [250, 30], [250, 54], [100, 54]]},
            {"text": "日期 2026/10/11", "box": [[100, 65], [350, 65], [350, 89], [100, 89]]},
            {"text": "總計 1000", "box": [[100, 100], [300, 100], [300, 124], [100, 124]]},
        ]
        evidence = image_evidence.layout_evidence(lines=lines, size=(1000, 1000))
        evidence["frames"] = [[80, 200, 920, 960]]  # frame is a proposal, and contains none of the proof text
        self.assertEqual(image_evidence.single_document(evidence), [0, 0, 1000, 1000])
        self.assertFalse(image_evidence.complete_region({"box": evidence["frames"][0]}, evidence)["complete"])
        result = page_layout.validate_layout(_route(strips), evidence=evidence)
        self.assertFalse(result["auto_split"])
        self.assertIsNone(result["fallback_reason"])
        self.assertTrue(result["issues"])
        self.assertNotEqual([region["box"] for region in result["regions"]], [evidence["frames"][0]])

    def test_strips_without_frame_or_identity_amount_anchor_remain_partial(self):
        strips = [_region([0, i * 60, 1000, i * 60 + 30]) for i in range(16)]
        result = page_layout.validate_layout(_route(strips), evidence={"frames": [], "anchors": []})
        self.assertFalse(result["auto_split"])
        self.assertIsNone(result["fallback_reason"])
        self.assertEqual(len(result["regions"]), 16)

    def test_text_header_types_and_duplicate_identity_anchor_reject_autosplit(self):
        invalid = [_region([0, 0, 450, 900], "text"), _region([550, 0, 1000, 900], "header")]
        result = page_layout.validate_layout(_route(invalid), evidence={"frames": [], "anchors": []})
        self.assertFalse(result["auto_split"])
        self.assertEqual(result["regions"], [])
        self.assertEqual(len(result["issues"]), 2)

        regions = [_region([0, 0, 450, 900]), _region([550, 0, 1000, 900])]
        evidence = {"frames": [], "anchors": [
            {"ids": ["AB12345678"], "amount": True, "center": [250, 400]},
            {"ids": ["AB12345678"], "amount": True, "center": [750, 400]},
        ]}
        result = page_layout.validate_layout(_route(regions), evidence=evidence)
        self.assertFalse(result["auto_split"])
        self.assertTrue(any("repeated identity anchor" in issue for issue in result["issues"]))

    def test_three_distinct_source_frames_support_high_confidence_split(self):
        regions = [_region([0, 0, 300, 500]), _region([350, 0, 650, 500], "cheque"),
                   _region([700, 0, 1000, 500], "cheque")]
        frames = [[0, 0, 300, 500], [350, 0, 650, 500], [700, 0, 1000, 500]]
        text_boxes, anchors = [], []
        for index, left in enumerate((0, 350, 700)):
            text_boxes.extend([
                {"id": len(text_boxes), "text": "支票", "box": [left+30, 60, left+250, 84]},
                {"id": len(text_boxes)+1, "text": f"支票號碼 {12345678+index}", "box": [left+30, 120, left+250, 144]},
                {"id": len(text_boxes)+2, "text": "日期 2026/10/11", "box": [left+30, 200, left+250, 224]},
                {"id": len(text_boxes)+3, "text": "金額 1000", "box": [left+30, 300, left+250, 324]},
            ])
            anchors.append({"ids": [f"{12345678+index}"], "amount": True,
                            "center": [left+140, 132]})
        evidence = {"frames": frames, "anchors": anchors, "text_boxes": text_boxes}
        result = page_layout.validate_layout(_route(regions), evidence=evidence)
        self.assertTrue(result["auto_split"])
        self.assertEqual(len(result["regions"]), 3)

    def test_multi_crop_must_include_its_header_and_detail_content_group(self):
        lines = []
        for index,left in enumerate((50,550)):
            for y,text in ((40,"公司抬頭"),(150,"支票"),(250,f"支票號碼 {12345678+index}"),
                           (350,"日期 2026/10/11"),(450,"金額 1000"),(550,"付款用途 辦公用品")):
                lines.append({"text":text,"box":[[left,y],[left+250,y],[left+250,y+24],[left,y+24]]})
        evidence = image_evidence.layout_evidence(lines=lines,size=(1000,1000))
        cropped = [_region([40,140,320,480],"cheque"),_region([540,140,820,480],"cheque")]
        self.assertTrue(all(image_evidence.complete_region(r,evidence)["complete"] for r in cropped))
        issues = image_evidence.verify_regions(cropped,evidence)
        self.assertTrue(any("complete OCR content group" in issue for issue in issues))
        whole = [_region([40,30,320,590],"cheque"),_region([540,30,820,590],"cheque")]
        self.assertEqual(image_evidence.verify_regions(whole,evidence),[])


class NativeImageEvidenceTests(unittest.TestCase):
    def test_native_ocr_preserves_source_caps_at_2400_without_upscaling_and_maps_lines(self):
        with _tempdir() as temporary:
            source = Path(temporary) / "source.png"
            target = Path(temporary) / "native.png"
            Image.new("RGB", (4000, 2000), "white").save(source)
            original = source.read_bytes()
            image_evidence.native_ocr(source, target)
            self.assertEqual(source.read_bytes(), original)
            with Image.open(target) as image:
                self.assertEqual(image.size, (2400, 1200))
            lines = [{"text": "line", "box": [[100, 50], [200, 50], [200, 80], [100, 80]]}]
            mapped = image_evidence.map_lines(lines, (4000, 2000), (2400, 1200))
            self.assertEqual(mapped[0]["box"], [[60.0, 30.0], [120.0, 30.0], [120.0, 48.0], [60.0, 48.0]])

            small = Path(temporary) / "small.png"
            small_target = Path(temporary) / "small-native.png"
            Image.new("RGB", (1000, 500), "white").save(small)
            image_evidence.native_ocr(small, small_target)
            with Image.open(small_target) as image:
                self.assertEqual(image.size, (1000, 500))

    def test_enhancement_skips_normal_image_and_copies_reliable_three_line_skew(self):
        with _tempdir() as temporary:
            source = Path(temporary) / "page.png"
            normal_target = Path(temporary) / "normal-enhanced.png"
            Image.new("RGB", (1000, 500), "white").save(source)
            # Put high-contrast marks inside the OCR boxes so only the measured skew triggers enhancement.
            lines = []
            with Image.open(source) as image:
                draw = ImageDraw.Draw(image)
                for index, y in enumerate((100, 200, 300)):
                    draw.rectangle((100, y, 500, y + 12), fill="black")
                    lines.append({"text": f"RELIABLE LINE {index}", "score": .99,
                                  "box": [[100, y], [500, y], [500, y + 20], [100, y + 20]]})
                image.save(source)
            before = source.read_bytes()
            no_copy, info = image_evidence.enhancement(source, lines, normal_target)
            self.assertIsNone(no_copy)
            self.assertFalse(info["applied"])
            self.assertFalse(normal_target.exists())

            tilted_lines = []
            for index, y in enumerate((100, 200, 300)):
                rise = 21
                tilted_lines.append({"text": f"RELIABLE LINE {index}", "score": .99,
                    "box": [[100, y], [500, y + rise], [500, y + rise + 20], [100, y + 20]]})
            target = Path(temporary) / "tilted-copy.png"
            enhanced, skew_info = image_evidence.enhancement(source, tilted_lines, target)
            self.assertEqual(enhanced, str(target))
            self.assertTrue(skew_info["applied"])
            self.assertAlmostEqual(skew_info["skew_degrees"], 3, delta=.2)
            self.assertTrue(target.exists())
            self.assertEqual(source.read_bytes(), before)

    def test_low_contrast_text_triggers_clahe_copy_without_changing_original(self):
        with _tempdir() as temporary:
            source = Path(temporary) / "low-contrast.png"
            target = Path(temporary) / "low-contrast-copy.png"
            image = Image.new("RGB", (800, 400), (255, 255, 255))
            draw = ImageDraw.Draw(image)
            lines = []
            for index, y in enumerate((90, 180, 270)):
                draw.text((120, y), f"FAINT TEXT ROW {index}", fill=(222, 222, 222))
                lines.append({"text": f"FAINT TEXT ROW {index}", "score": .98,
                    "box": [[110, y-2], [500, y-2], [500, y+24], [110, y+24]]})
            image.save(source)
            original = source.read_bytes()
            enhanced, info = image_evidence.enhancement(source, lines, target)
            self.assertEqual(enhanced, str(target))
            self.assertTrue(info["low_text_contrast"])
            self.assertTrue(info["applied"])
            self.assertTrue(target.exists())
            self.assertEqual(source.read_bytes(), original)
            with Image.open(source) as original_image, Image.open(target) as enhanced_image:
                difference = ImageChops.difference(original_image.convert("L"), enhanced_image.convert("L"))
                self.assertIsNotNone(difference.getbbox())

    def test_same_invoice_id_from_qr_and_ocr_is_one_identity(self):
        code = "AB12345678"
        lines = [{"text": f"發票號碼 {code}", "box": [[100, 100], [300, 100], [300, 130], [100, 130]]},
                 {"text": "總計 500", "box": [[100, 300], [300, 300], [300, 330], [100, 330]]}]
        qr = {"source_size": [1000, 1000], "detections": [
            {"box": [90, 90, 310, 140], "candidates": [{"voucher_id": code}]}
        ]}
        evidence = image_evidence.layout_evidence(lines=lines, size=(1000, 1000), qr=qr)
        self.assertEqual(len(evidence["anchors"]), 3)
        self.assertEqual(image_evidence.single_document(evidence), [0, 0, 1000, 1000])


class AmountCandidateTests(unittest.TestCase):
    def test_known_payment_and_tax_annotations_parse_but_unknown_or_ambiguous_do_not(self):
        for value, amount in (("500元(現金)", 500), ("500 TX", 500)):
            with self.subTest(value=value):
                self.assertEqual(normalization.amount_annotation(value)["value"], amount)
        for value in ("500 UNKNOWN", "500 TX 600", "500 USD"):
            with self.subTest(value=value):
                self.assertIsNone(normalization.amount_annotation(value))

    def test_normalization_candidate_requires_ocr_total_and_twd_and_respects_manual_value(self):
        def data(value="500元(現金)"):
            return {"fields": {"currency": "TWD", "total_amount": value}, "items": [], "field_evidence": {}}

        supported = data()
        supplement.normalize_candidates(supported, "invoice", "總計 500")
        self.assertEqual(supported["fields"]["total_amount"], 500)
        evidence = supported["field_evidence"]["fields.total_amount"]
        self.assertTrue(any(item["source"] == "normalization_verified" and item["selected"] for item in evidence))

        no_evidence = data()
        supplement.normalize_candidates(no_evidence, "invoice", "品名 500")
        self.assertEqual(no_evidence["fields"]["total_amount"], "500元(現金)")

        manual = data()
        supplement.normalize_candidates(manual, "invoice", "總計 500", confirmed_fields={"total_amount": True})
        self.assertEqual(manual["fields"]["total_amount"], "500元(現金)")

        foreign = data()
        supplement.normalize_candidates(foreign, "invoice", "總計 500 USD")
        self.assertEqual(foreign["fields"]["total_amount"], "500元(現金)")


class BarcodeSummaryTests(unittest.TestCase):
    @staticmethod
    def _valid_code(invoice="AB12345678"):
        return invoice + "1150604" + "0000" + f"{100:08X}" + f"{105:08X}" + "12345678" + "87654321"

    def test_summary_classifies_all_qr_diagnostic_states(self):
        size = [1200, 800]
        self.assertEqual(barcode.summarize([], [], size)["status"], "not_detected")
        self.assertEqual(barcode.summarize([], [[10, 10, 40, 40]], size)["status"], "detected_undecoded")
        self.assertEqual(barcode.summarize(["not invoice QR"], [[10, 10, 40, 40]], size)["status"], "invalid")
        valid = self._valid_code()
        self.assertEqual(barcode.summarize([valid], [[10, 10, 40, 40]], size)["status"], "valid")
        conflict = barcode.summarize([valid, self._valid_code("CD87654321")], [[10, 10, 40, 40]], size)
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(len(conflict["candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
