"""Offline contracts for cached OCR and PDF text evidence."""
from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.core import text_evidence


# Synthetic PDF generated with reportlab UnicodeCIDFont("MSung-Light"). The
# built-in Adobe-GB1 CID font keeps the fixture small and avoids system-font
# or reportlab dependencies in the pdfscan312 test environment.
_CJK_PDF = "".join((
    "JVBERi0xLjMKJZOMi54gUmVwb3J0TGFiIEdlbmVyYXRlZCBQREYgZG9jdW1lbnQgKG9wZW5zb3VyY2UpCjEgMCBvYmoKPDwKL0Yx",
    "IDIgMCBSIC9GMiAzIDAgUgo+PgplbmRvYmoKMiAwIG9iago8PAovQmFzZUZvbnQgL0hlbHZldGljYSAvRW5jb2RpbmcgL1dpbkFu",
    "c2lFbmNvZGluZyAvTmFtZSAvRjEgL1N1YnR5cGUgL1R5cGUxIC9UeXBlIC9Gb250Cj4+CmVuZG9iagozIDAgb2JqCjw8Ci9CYXNl",
    "Rm9udCAvTVN1bmctTGlnaHQgL0Rlc2NlbmRhbnRGb250cyBbIDw8Ci9CYXNlRm9udCAvTVN1bmctTGlnaHQgL0NJRFN5c3RlbUlu",
    "Zm8gPDwKL09yZGVyaW5nIChDTlMxKSAvUmVnaXN0cnkgKEFkb2JlKSAvU3VwcGxlbWVudCAxCj4+IC9EVyAxMDAwIC9Gb250RGVz",
    "Y3JpcHRvciA8PAovQXNjZW50IDc1MiAvQ2FwSGVpZ2h0IDczNyAvRGVzY2VudCAtMjcxIC9GbGFncyA2IC9Gb250QkJveCBbIC0x",
    "NjAgLTI0OSAxMDE1IDg4OCBdIC9Gb250TmFtZSAvTVN1bmctTGlnaHQgCiAgL0l0YWxpY0FuZ2xlIDAgL0xlYWRpbmcgMTQ4IC9N",
    "YXhXaWR0aCAxMDAwIC9NaXNzaW5nV2lkdGggNTAwIC9TdGVtSCA0NSAvU3RlbVYgNTggCiAgL1R5cGUgL0ZvbnREZXNjcmlwdG9y",
    "IC9YSGVpZ2h0IDU1Mwo+PiAvU3VidHlwZSAvQ0lERm9udFR5cGUwIC9UeXBlIC9Gb250IAogIC9XIFsgMSAyIDI1MCAzIFsgNDA4",
    "IDY2OCA0OTAgODc1IDY5OCAyNTAgMjQwIF0gMTAgWyAyNDAgNDE3IDY2NyAyNTAgMzEzIDI1MCA1MjAgNTAwIF0gMTggMjYgNTAw",
    "IAogIDI3IDI4IDI1MCAyOSAzMSA2NjcgMzIgWyAzOTYgOTIxIDY3NyA2MTUgNzE5IDc2MCA2MjUgNTUyIDc3MSA4MDIgCiAgMzU0",
    "IF0gNDMgWyAzNTQgNzgxIDYwNCA5MjcgNzUwIDgyMyA1NjMgODIzIDcyOSA1NDIgCiAgNjk4IDc3MSA3MjkgOTQ4IDc3MSA2Nzcg",
    "NjM1IDM0NCA1MjAgMzQ0IAogIDQ2OSA1MDAgMjUwIDQ2OSA1MjEgNDI3IDUyMSA0MzggMjcxIDQ2OSAKICA1MzEgMjUwIF0gCiAg",
    "NzUgWyAyNTAgNDU4IDI0MCA4MDIgNTMxIDUwMCA1MjEgXSA4MiBbIDUyMSAzNjUgMzMzIDI5MiA1MjEgNDU4IDY3NyA0NzkgNDU4",
    "IDQyNyAKICA0ODAgNDk2IDQ4MCA2NjcgXSBdCj4+IF0gL0VuY29kaW5nIC9VbmlHQi1VQ1MyLUggL05hbWUgL0YyIC9TdWJ0eXBl",
    "IC9UeXBlMCAvVHlwZSAvRm9udAo+PgplbmRvYmoKNCAwIG9iago8PAovQ29udGVudHMgOCAwIFIgL01lZGlhQm94IFsgMCAwIDM2",
    "MCAzNjAgXSAvUGFyZW50IDcgMCBSIC9SZXNvdXJjZXMgPDwKL0ZvbnQgMSAwIFIgL1Byb2NTZXQgWyAvUERGIC9UZXh0IC9JbWFn",
    "ZUIgL0ltYWdlQyAvSW1hZ2VJIF0KPj4gL1JvdGF0ZSAwIC9UcmFucyA8PAoKPj4gCiAgL1R5cGUgL1BhZ2UKPj4KZW5kb2JqCjUg",
    "MCBvYmoKPDwKL1BhZ2VNb2RlIC9Vc2VOb25lIC9QYWdlcyA3IDAgUiAvVHlwZSAvQ2F0YWxvZwo+PgplbmRvYmoKNiAwIG9iago8",
    "PAovQXV0aG9yIChhbm9ueW1vdXMpIC9DcmVhdGlvbkRhdGUgKEQ6MjAyNjEwMjYxMjQ0MTUrMDgnMDAnKSAvQ3JlYXRvciAoYW5v",
    "bnltb3VzKSAvS2V5d29yZHMgKCkgL01vZERhdGUgKEQ6MjAyNjEwMjYxMjQ0MTUrMDgnMDAnKSAvUHJvZHVjZXIgKFJlcG9ydExh",
    "YiBQREYgTGlicmFyeSAtIFwob3BlbnNvdXJjZVwpKSAKICAvU3ViamVjdCAodW5zcGVjaWZpZWQpIC9UaXRsZSAodW50aXRsZWQp",
    "IC9UcmFwcGVkIC9GYWxzZQo+PgplbmRvYmoKNyAwIG9iago8PAovQ291bnQgMSAvS2lkcyBbIDQgMCBSIF0gL1R5cGUgL1BhZ2Vz",
    "Cj4+CmVuZG9iago4IDAgb2JqCjw8Ci9GaWx0ZXIgWyAvQVNDSUk4NURlY29kZSAvRmxhdGVEZWNvZGUgXSAvTGVuZ3RoIDE4Nwo+",
    "PgpzdHJlYW0KR2FyVzIzdCZvJSUjK0hUTURNOi1hUSdeUT0qMmxCT01OKGc7SGNNZmkkJkkjXk8nMCk5MVQra2M4Z1BEV0hhZTEk",
    "YGpmPCpKNE9ISkFYLlhwPU5QLEUjSWQ4TyxzPmZLIWNpRzhcKGQqLikqSGddOE5uL0MvSSVBWHMoUD9DJF0vc0NKSUgkPmN0NlJW",
    "NW1MNydnNTNNLi1LbFtlbzZHY2JzMWJaR0A+M19ZUW8hcXMrS19EKzcoYj5+PmVuZHN0cmVhbQplbmRvYmoKeHJlZgowIDkKMDAw",
    "MDAwMDAwMCA2NTUzNSBmIAowMDAwMDAwMDYxIDAwMDAwIG4gCjAwMDAwMDAxMDIgMDAwMDAgbiAKMDAwMDAwMDIwOSAwMDAwMCBu",
    "IAowMDAwMDAxMTU1IDAwMDAwIG4gCjAwMDAwMDEzNDggMDAwMDAgbiAKMDAwMDAwMTQxNiAwMDAwMCBuIAowMDAwMDAxNjc3IDAw",
    "MDAwIG4gCjAwMDAwMDE3MzYgMDAwMDAgbiAKdHJhaWxlcgo8PAovSUQgCls8ZTEwM2FhZDJjNDdlZjIxMGEzYzYyMmRjMmJhNDIx",
    "M2I+PGUxMDNhYWQyYzQ3ZWYyMTBhM2M2MjJkYzJiYTQyMTNiPl0KJSBSZXBvcnRMYWIgZ2VuZXJhdGVkIFBERiBkb2N1bWVudCAt",
    "LSBkaWdlc3QgKG9wZW5zb3VyY2UpCgovSW5mbyA2IDAgUgovUm9vdCA1IDAgUgovU2l6ZSA5Cj4+CnN0YXJ0eHJlZgoyMDEzCiUl",
    "RU9GCg==",
))


def _tempdir():
    root = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


def _pdf(objects, root=1):
    """Build a tiny standards-compliant PDF without external writer packages."""
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode())
        body.extend(value if isinstance(value, bytes) else value.encode("ascii"))
        body.extend(b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(f"trailer\n<< /Size {len(objects)+1} /Root {root} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(body)


def _text_pdf(text, *, rotate=0, crop=None, include_image=False):
    content = f"BT /F1 8 Tf 24 300 Td ({text}) Tj ET\n".encode("ascii")
    if include_image:
        content += b"q 120 0 0 120 160 60 cm /Im1 Do Q\n"
    resources = "/Font << /F1 4 0 R >>"
    if include_image:
        resources += "/XObject << /Im1 6 0 R >>"
    page = f"/Type /Page /Parent 2 0 R /MediaBox [0 0 360 360] /Resources << {resources} >> /Contents 5 0 R"
    if rotate:
        page += f" /Rotate {rotate}"
    if crop:
        page += f" /CropBox [{crop[0]} {crop[1]} {crop[2]} {crop[3]}]"
    stream = b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< {page} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        stream,
    ]
    if include_image:
        objects.append(b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\xff\nendstream")
    return _pdf(objects)


def _render_pdf(pdf_path, image_path, page_no=1):
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(pdf_path)) as document:
        bitmap = document[page_no - 1].render(scale=200 / 72)
        try:
            bitmap.to_pil().save(image_path)
        finally:
            bitmap.close()


class TextEvidenceCacheTests(unittest.TestCase):
    def test_same_pixels_are_recognized_once_and_pixel_or_engine_change_invalidates(self):
        with _tempdir() as temporary:
            folder = Path(temporary)
            source = folder / "source.png"
            Image.new("RGB", (80, 50), "white").save(source)
            calls = []

            def fake_ocr(_source, target, _ocr, *, purpose):
                calls.append(purpose)
                Image.open(_source).save(target)
                return ([{"text": "offline result", "box": [[2, 3], [40, 3], [40, 12], [2, 12]]}], str(target))

            with (patch.object(text_evidence, "pdf_lines", return_value=(None, "not_pdf_or_edited")),
                  patch("app.core.ocr_engine.runtime_signature", return_value="engine-a"),
                  patch("app.core.model_factory.get_ocr", return_value=object()),
                  patch("app.core.ocr_resolution.read", side_effect=fake_ocr)):
                first = text_evidence.read(source, folder=folder)
                second = text_evidence.read(source, folder=folder)
                self.assertEqual(len(calls), 1)
                self.assertTrue(text_evidence.validate(second, source))

                changed = Image.new("RGB", (80, 50), "white")
                changed.putpixel((0, 0), (0, 0, 0))
                changed.save(source)
                text_evidence.read(source, folder=folder)
                self.assertEqual(len(calls), 2)

                with patch("app.core.ocr_engine.runtime_signature", return_value="engine-b"):
                    text_evidence.read(source, folder=folder)
                self.assertEqual(len(calls), 3)
            self.assertEqual(first["source"], "ocr")

    def test_complete_crop_lines_inherit_but_cut_through_lines_do_not(self):
        with _tempdir() as temporary:
            folder = Path(temporary)
            parent, child = folder / "parent.png", folder / "child.png"
            Image.new("RGB", (100, 100), "white").save(parent)
            Image.new("RGB", (50, 50), "white").save(child)
            lines = [
                {"text": "complete", "box": [[20, 20], [40, 20], [40, 30], [20, 30]]},
                {"text": "crosses edge", "box": [[45, 20], [65, 20], [65, 30], [45, 30]]},
            ]
            with (patch.object(text_evidence, "pdf_lines", return_value=(None, "not_pdf_or_edited")),
                  patch("app.core.ocr_engine.runtime_signature", return_value="engine-a"),
                  patch("app.core.model_factory.get_ocr", return_value=object()),
                  patch("app.core.ocr_resolution.read", return_value=(lines, str(parent)))):
                text_evidence.read(parent, folder=folder)
                region = {"box": [100, 100, 600, 600], "rotate": 0}
                self.assertFalse(text_evidence.inherit(parent, child, region, folder=folder))

            # A parent with only fully enclosed text safely reuses that line.
            parent2, child2 = folder / "parent2.png", folder / "child2.png"
            distinct_parent = Image.new("RGB", (100, 100), "white")
            distinct_parent.putpixel((0, 0), (0, 0, 0))
            distinct_parent.save(parent2)
            Image.new("RGB", (50, 50), "white").save(child2)
            lines[:] = [lines[0]]
            with (patch.object(text_evidence, "pdf_lines", return_value=(None, "not_pdf_or_edited")),
                  patch("app.core.ocr_engine.runtime_signature", return_value="engine-a"),
                  patch("app.core.model_factory.get_ocr", return_value=object()),
                  patch("app.core.ocr_resolution.read", return_value=(lines, str(parent)))):
                text_evidence.read(parent2, folder=folder)
                self.assertTrue(text_evidence.inherit(parent2, child2, region, folder=folder))
                inherited = text_evidence.read(child2, folder=folder)
                self.assertEqual(inherited["lines"][0]["text"], "complete")
                self.assertEqual(inherited["lines"][0]["box"][0], [10.0, 10.0])


@unittest.skipUnless(__import__("importlib").util.find_spec("pypdfium2"), "pypdfium2 is required for PDF evidence")
class PdfTextEvidenceTests(unittest.TestCase):
    def _fixture(self, folder, *, text, rotate=0, crop=None, include_image=False):
        pdf_path, source = folder / "sample.pdf", folder / "render.png"
        pdf_path.write_bytes(_text_pdf(text, rotate=rotate, crop=crop, include_image=include_image))
        _render_pdf(pdf_path, source)
        text_evidence.pdf_descriptor(source, pdf_path, 1, folder)
        return source

    def test_reliable_text_layer_returns_source_pixel_boxes_without_ocr(self):
        with _tempdir() as temporary:
            folder = Path(temporary)
            text = "RELIABLE OFFLINE PDF TEXT LAYER WITH ENOUGH PRINTABLE CHARACTERS"
            source = self._fixture(folder, text=text)
            with patch("app.core.model_factory.get_ocr", side_effect=AssertionError("OCR must not run")):
                lines, reason = text_evidence.pdf_lines(source, folder, {"pdf_enabled": True, "transforms": []})
            self.assertEqual(reason, "usable_text_layer")
            self.assertEqual("".join("".join(line["text"].split()) for line in lines), "".join(text.split()))
            self.assertTrue(all(line["source"] == "pdf_text" for line in lines))
            with Image.open(source) as image:
                width, height = image.size
            self.assertTrue(all(0 <= x <= width and 0 <= y <= height
                                for line in lines for x, y in line["box"]))

    def test_short_mixed_and_nonprintable_layers_are_rejected(self):
        cases = [
            ("SHORT", False, "unreliable_text_layer"),
            ("LONG ENOUGH TEXT FOR RELIABILITY BUT MIXED WITH A LARGE IMAGE", True, "mixed_or_scanned_page"),
            ("\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000\\000", False, "unreliable_text_layer"),
        ]
        with _tempdir() as temporary:
            folder = Path(temporary)
            for index, (text, include_image, expected) in enumerate(cases):
                with self.subTest(expected=expected, index=index):
                    case_folder = folder / str(index)
                    case_folder.mkdir()
                    source = self._fixture(case_folder, text=text, include_image=include_image)
                    lines, reason = text_evidence.pdf_lines(source, case_folder, {"pdf_enabled": True, "transforms": []})
                    self.assertIsNone(lines)
                    self.assertEqual(reason, expected)
                    fallback_lines = [{"text": "fallback OCR", "box": [[5, 5], [30, 5], [30, 12], [5, 12]]}]
                    with (patch("app.core.model_factory.get_ocr", return_value=object()),
                          patch("app.core.ocr_resolution.read", return_value=(fallback_lines, str(source))) as ocr_read):
                        record = text_evidence.read(source, folder=case_folder,
                                                    preparation={"pdf_enabled": True, "transforms": []})
                    self.assertEqual(record["source"], "ocr")
                    self.assertEqual(record["lines"][0]["text"], "fallback OCR")
                    ocr_read.assert_called_once()

    def test_page_rotations_and_cropbox_keep_boxes_inside_rendered_bitmap(self):
        with _tempdir() as temporary:
            root = Path(temporary)
            text = "ROTATED PDF TEXT BOXES MUST FOLLOW THE RENDERED PAGE COORDINATE SYSTEM"
            for rotate in (0, 90, 180, 270):
                with self.subTest(rotate=rotate):
                    folder = root / str(rotate)
                    folder.mkdir()
                    source = self._fixture(folder, text=text, rotate=rotate, crop=(20, 30, 340, 330))
                    lines, reason = text_evidence.pdf_lines(source, folder, {"pdf_enabled": True, "transforms": []})
                    self.assertEqual(reason, "usable_text_layer")
                    with Image.open(source) as image:
                        width, height = image.size
                    self.assertTrue(all(0 <= x <= width and 0 <= y <= height
                                        for line in lines for x, y in line["box"]))

    def test_preparation_rotation_and_crop_map_pdf_boxes_to_working_bitmap(self):
        with _tempdir() as temporary:
            folder = Path(temporary)
            text = "TRANSFORMED PDF TEXT BOXES MUST MATCH ROTATED AND CROPPED PIXELS"
            raw_source = self._fixture(folder, text=text)
            raw = Image.open(raw_source).copy()
            width, height = raw.size
            rotated = raw.transpose(Image.Transpose.ROTATE_270)
            rotated_path = folder / "rotated.png"
            rotated.save(rotated_path)
            rw, rh = rotated.size
            crop = (20, 30, rw - 20, rh - 30)
            cropped_path = folder / "rotated-cropped.png"
            rotated.crop(crop).save(cropped_path)
            preparation = {"pdf_enabled": True, "transforms": [
                {"kind": "rotate", "cw": 90, "input_size": [width, height]},
                {"kind": "crop", "pixels": list(crop), "input_size": [rw, rh]},
            ]}
            lines, reason = text_evidence.pdf_lines(cropped_path, folder, preparation)
            self.assertEqual(reason, "usable_text_layer")
            with Image.open(cropped_path) as image:
                out_w, out_h = image.size
            self.assertTrue(all(0 <= x <= out_w and 0 <= y <= out_h
                                for line in lines for x, y in line["box"]))

    def test_same_size_external_image_does_not_reuse_pdf_text(self):
        with _tempdir() as temporary:
            folder = Path(temporary)
            original = self._fixture(
                folder, text="SAME SIZE REPLACEMENT IMAGE MUST NOT INHERIT PDF TEXT EVIDENCE")
            replacement = folder / "external-replacement.png"
            with Image.open(original) as image:
                size = image.size
            external = Image.new("RGB", size, "white")
            external.putpixel((size[0] // 2, size[1] // 2), (0, 0, 0))
            external.save(replacement)

            with Image.open(replacement) as image:
                self.assertEqual(image.size, size)
            lines, reason = text_evidence.pdf_lines(
                replacement, folder, {"pdf_enabled": True, "transforms": []})
            self.assertIsNone(lines)
            self.assertEqual(reason, "working_pixels_do_not_match_pdf")

    def test_chinese_text_layer_when_a_usable_font_is_available(self):
        with _tempdir() as temporary:
            folder = Path(temporary)
            pdf_path, source = folder / "chinese.pdf", folder / "chinese.png"
            pdf_path.write_bytes(base64.b64decode(_CJK_PDF))
            _render_pdf(pdf_path, source)
            text_evidence.pdf_descriptor(source, pdf_path, 1, folder)
            lines, reason = text_evidence.pdf_lines(source, folder, {"pdf_enabled": True, "transforms": []})
            self.assertEqual(reason, "usable_text_layer")
            extracted = "".join("".join(line["text"].split()) for line in lines)
            expected = "中文發票金額日期統編號應付新臺幣壹仟元中文測試"
            self.assertEqual(extracted, expected)
            self.assertGreaterEqual(len(extracted), 20)
            self.assertIn("應付", extracted)
            self.assertIn("新臺幣壹仟元", extracted)
            self.assertTrue(all(line["source"] == "pdf_text" for line in lines))
            with Image.open(source) as image:
                width, height = image.size
            self.assertTrue(all(0 <= x <= width and 0 <= y <= height
                                for line in lines for x, y in line["box"]))


if __name__ == "__main__":
    unittest.main()
