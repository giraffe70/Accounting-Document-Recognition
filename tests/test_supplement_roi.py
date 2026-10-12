"""Offline contracts for conservative supplemental targets and OCR regions."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.core import supplement, supplement_roi, invocation_trace


def _line(text, box, score=.99):
    x0, y0, x1, y1 = box
    return {"text": text, "score": score,
            "box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}


def _data(type_key, fields, *, items=None, entries=None):
    return {"doc_type": type_key, "doc_type_key": type_key, "fields": dict(fields),
            "items": list(items or []), "entries": list(entries or []),
            "validation_warnings": [], "field_evidence": {}, "boxes": {}}


def _test_tempdir():
    root = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


class SupplementTargetPolicyTests(unittest.TestCase):
    def test_normalize_candidates_uses_its_confirmed_paths_without_context(self):
        data = _data("invoice", {"total_amount": "155", "currency": "TWD"})
        data["field_evidence"]["fields.total_amount"] = [{"source": "manual", "selected": True, "value": "155"}]
        supplement.normalize_candidates(data, "invoice", "總計 155", confirmed_paths=["fields.total_amount"])
        self.assertEqual(data["fields"]["total_amount"], "155")

    def test_receipt_nullable_identifiers_do_not_trigger_without_printed_label(self):
        data = _data("receipt", {"date": None, "vendor": None, "tax_id": None,
                                  "voucher_id": None, "total_amount": 100,
                                  "currency": "TWD", "subtotal": None, "tax_amount": None})
        targets, groups, skipped = supplement.select_targets(data, "receipt", "文具", {}, set())
        self.assertEqual(targets, [])
        self.assertEqual(groups, [])
        self.assertEqual(skipped["date"], "no_printed_anchor")
        self.assertEqual(skipped["vendor"], "no_printed_anchor")
        self.assertNotIn("tax_id", targets)
        self.assertNotIn("voucher_id", targets)
        self.assertNotIn("subtotal", targets)
        self.assertNotIn("tax_amount", targets)

    def test_receipt_date_and_vendor_need_matching_printed_labels_and_manual_values_are_protected(self):
        data = _data("receipt", {"date": None, "vendor": None, "total_amount": 100,
                                  "currency": "TWD"})
        targets, _, _ = supplement.select_targets(
            data, "receipt", "收據日期 店家", {}, set())
        self.assertEqual(set(targets), {"date", "vendor"})

        targets, _, skipped = supplement.select_targets(
            data, "receipt", "收據日期 店家", {}, {"date", "vendor"})
        self.assertEqual(targets, [])
        self.assertEqual(skipped["date"], "manual")
        self.assertEqual(skipped["vendor"], "manual")

    def test_invoice_missing_core_fields_trigger_but_manual_fields_do_not(self):
        keys = ("voucher_id", "date", "seller_name", "seller_tax_id", "total_amount")
        data = _data("invoice", {key: None for key in keys})
        targets, _, _ = supplement.select_targets(data, "invoice", "發票", {}, set())
        self.assertEqual(set(targets), set(keys))
        targets, _, skipped = supplement.select_targets(
            data, "invoice", "發票", {}, {"seller_tax_id", "total_amount"})
        self.assertEqual(set(targets), set(keys) - {"seller_tax_id", "total_amount"})
        self.assertEqual(skipped["seller_tax_id"], "manual")
        self.assertEqual(skipped["total_amount"], "manual")

    def test_labeled_total_conflict_triggers_but_generic_sum_does_not(self):
        data = _data("receipt", {"total_amount": 100, "currency": "TWD"})
        generic, _, _ = supplement.select_targets(data, "receipt", "商品合計 90", {}, set())
        labeled, _, _ = supplement.select_targets(data, "receipt", "應付金額 90", {}, set())
        self.assertNotIn("total_amount", generic)
        self.assertIn("total_amount", labeled)

    def test_items_and_journal_groups_require_complete_header_and_data(self):
        invoice = _data("invoice", {"voucher_id": "AB12345678", "date": "2026-10-12",
                                    "seller_name": "店家", "seller_tax_id": "12345678",
                                    "total_amount": 100, "currency": "TWD"})
        cases = [
            ("品名 數量 單價 金額", []),
            ("品名 原子筆 2 10 20", []),
            ("品名 數量 單價 金額\n原子筆 2 10 20", ["items"]),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                _, groups, _ = supplement.select_targets(invoice, "invoice", text, {}, set())
                self.assertEqual(groups, expected)

        voucher = _data("journal_voucher", {"voucher_id": "A911", "date": "2026-10-12",
                                            "debit_total": 100, "credit_total": 100})
        for text, expected in (("借方 辦公用品 100", []),
                               ("借方 貸方", []),
                               ("借方 貸方\n辦公用品 100", ["entries"])):
            with self.subTest(text=text):
                _, groups, _ = supplement.select_targets(voucher, "journal_voucher", text, {}, set())
                self.assertEqual(groups, expected)


class SupplementRoiGeometryTests(unittest.TestCase):
    def test_total_anchor_selects_labeled_total_and_keeps_nearby_money_context(self):
        lines = [
            _line("未稅小計", (100, 100, 230, 120)),
            _line("14,000", (350, 100, 420, 120)),
            _line("營業稅", (100, 130, 200, 150)),
            _line("1,500", (350, 130, 420, 150)),
            _line("含稅總計", (100, 160, 230, 180)),
            _line("新臺幣 15,500 元", (350, 160, 560, 180)),
        ]
        region, fallback = supplement_roi.choose("invoice", ["total_amount"], [], lines, (1000, 800))
        self.assertIsNone(fallback)
        self.assertIsNotNone(region)
        selected = [lines[index]["text"] for index in region["selected_ids"]]
        self.assertIn("含稅總計", selected)
        self.assertIn("14,000", selected)
        self.assertIn("1,500", selected)

    def test_generic_sum_is_not_a_total_anchor(self):
        lines = [_line("商品合計", (100, 100, 240, 125)), _line("100", (300, 100, 350, 125))]
        region, fallback = supplement_roi.choose("receipt", ["total_amount"], [], lines, (1000, 800))
        self.assertIsNone(region)
        self.assertEqual(fallback, "missing_or_ambiguous_anchor")

    def test_seller_roi_excludes_buyer_identity_anchor(self):
        lines = [_line("買方統一編號", (100, 100, 240, 125)),
                 _line("12345678", (280, 100, 380, 125)),
                 _line("賣方統一編號", (100, 200, 240, 225)),
                 _line("87654321", (280, 200, 380, 225))]
        region, fallback = supplement_roi.choose("invoice", ["seller_tax_id"], [], lines, (1000, 800))
        self.assertIsNone(fallback)
        self.assertIsNotNone(region)
        selected = [lines[index]["text"] for index in region["selected_ids"]]
        self.assertIn("賣方統一編號", selected)
        self.assertIn("87654321", selected)
        self.assertNotIn("買方統一編號", selected)

    def test_roi_crop_mapping_and_candidate_box_map_back_to_document(self):
        region = {"box": [100, 200, 500, 600]}
        source_size, crop_size = (1000, 800), (800, 640)
        lines = [_line("總計 100", (200, 240, 300, 260)),
                 _line("partially cut", (490, 400, 520, 420))]
        mapped = supplement_roi.map_existing(lines, source_size, region, crop_size)
        self.assertEqual([row["text"] for row in mapped], ["總計 100"])
        self.assertEqual(mapped[0]["box"], [[200.0, 160.0], [400.0, 160.0],
                                            [400.0, 200.0], [200.0, 200.0]])
        self.assertEqual(supplement_roi.box_to_document([200, 100, 600, 400], region),
                         [180, 240, 340, 360])

    def test_enhanced_box_inverse_maps_to_document(self):
        region = {"box": [100, 200, 500, 600]}
        # Enhanced pixels move right by 10; inverse mapping must remove the shift.
        self.assertEqual(supplement_roi.box_to_document(
            [200, 100, 600, 400], region, image_size=(1000, 1000),
            enhancement_matrix=[[1, 0, 10], [0, 1, 0]]),
            [176, 240, 336, 360])

    def test_rotated_native_image_is_not_a_reusable_document_frame(self):
        with _test_tempdir() as folder:
            image = Image.new("RGB", (200, 120), "white")
            for x in range(30, 70):
                for y in range(20, 45):
                    image.putpixel((x, y), (0, 0, 0))
            display = Path(folder) / "display.png"
            rotated = Path(folder) / "rotated.png"
            image.save(display)
            image.rotate(180).save(rotated)
            self.assertFalse(supplement_roi.same_visual_frame(display, rotated))


class SupplementOcrDecisionTests(unittest.TestCase):
    def _apply_with_readable_roi(self, *, ocr_enabled=True):
        temp = _test_tempdir()
        self.addCleanup(temp.cleanup)
        image_path = Path(temp.name) / "receipt.png"
        Image.new("RGB", (1000, 800), "white").save(image_path)
        # The total label/value are clear and close enough to produce a small ROI.
        lines = [_line("應付金額", (100, 150, 260, 175)),
                 _line("新臺幣 100 元", (330, 150, 520, 175))]
        data = _data("receipt", {"total_amount": None, "currency": "TWD"})
        context = {"ocr_lines": lines, "ocr_enabled": ocr_enabled}
        conditions = {"low_text_contrast": False, "skew_degrees": 0}
        return str(image_path), data, context, conditions

    def test_clear_existing_text_uses_mapped_evidence_without_loading_ocr(self):
        image_path, data, context, conditions = self._apply_with_readable_roi()
        with patch("app.core.image_evidence.enhancement", return_value=(None, conditions)), \
             patch("app.core.model_factory.get_ocr") as get_ocr, \
             patch("app.core.pipeline.reread_fields", return_value={}) as reread, \
             invocation_trace.capture() as trace:
            supplement.apply(data, "receipt", object(), image_path, mode="image",
                             ocr_text="應付金額 新臺幣 100 元", context=context)
        get_ocr.assert_not_called()
        reread.assert_called_once()
        decision = next(event for event in trace if event.get("kind") == "supplement_ocr_decision")
        self.assertFalse(decision["fresh"])
        self.assertEqual(decision["source"], "mapped_first_pass")
        evidence = next(event for event in trace if event.get("kind") == "supplement_evidence")
        self.assertEqual(evidence["ocr_requests"], 0)

    def test_disabled_ocr_does_not_load_engine_even_for_missing_target(self):
        image_path, data, context, conditions = self._apply_with_readable_roi(ocr_enabled=False)
        # Make the existing target text too weak to satisfy the fresh-OCR check.
        context["ocr_lines"] = [_line("應付金額", (100, 150, 260, 175), score=.2)]
        with patch("app.core.image_evidence.enhancement", return_value=(None, conditions)), \
             patch("app.core.model_factory.get_ocr") as get_ocr, \
             patch("app.core.pipeline.reread_fields", return_value={}), \
             invocation_trace.capture() as trace:
            supplement.apply(data, "receipt", object(), image_path, mode="image",
                             ocr_text="應付金額", context=context)
        get_ocr.assert_not_called()
        decision = next(event for event in trace if event.get("kind") == "supplement_ocr_decision")
        self.assertFalse(decision["fresh"])
        self.assertEqual(decision["reason"], "ocr_disabled")

    def test_enhanced_copy_conflict_blocks_original_supported_candidate(self):
        image_path, data, context, _ = self._apply_with_readable_roi()
        context["ocr_lines"] = [_line("應付金額 100", (100, 150, 330, 175), score=.2)]

        class FakeOcr:
            outputs = [[_line("應付金額 100", (20, 20, 230, 50), score=.2)],
                       [_line("應付金額 200", (20, 20, 230, 50), score=.9)]]

            def recognize(self, _path):
                return self.outputs.pop(0)

            def plain_text(self, lines):
                return "\n".join(line["text"] for line in lines)

        def enhance(source, lines, target):
            if target is None:
                return None, {"low_text_contrast": False, "skew_degrees": 0, "text_contrast": 30}
            return str(target), {"low_text_contrast": True, "skew_degrees": 2,
                                 "text_contrast": 20, "matrix": [[1, 0, 0], [0, 1, 0]]}

        with patch("app.core.settings_store.get_settings", return_value={"ocr": "paddle/pp-ocrv5"}), \
             patch("app.core.model_factory.get_ocr", return_value=FakeOcr()), \
             patch("app.core.image_evidence.enhancement", side_effect=enhance), \
             patch("app.core.pipeline.reread_fields", return_value={"total_amount": 100}), \
             invocation_trace.capture() as trace:
            supplement.apply(data, "receipt", object(), image_path, mode="image",
                             ocr_text="應付金額 100", context=context)
        self.assertTrue(context["ocr_copy_conflict"])
        self.assertIsNone(data["fields"]["total_amount"])
        self.assertEqual(next(record for record in trace if record.get("kind") == "supplement_result")
                         ["rejected"]["total_amount"], "copy_conflict")


if __name__ == "__main__":
    unittest.main()
