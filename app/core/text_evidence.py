"""Source-coordinate text evidence, shared by routing, extraction and rereads."""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import uuid
from PIL import Image
from app.core import image_evidence, invocation_trace, ocr_cache

VERSION = 1


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(source):
    from app.core.ocr_engine import runtime_signature
    from app.core import settings_store
    return {"version": VERSION, **ocr_cache.pixels(source), "engine": runtime_signature(),
            "ocr_setting":settings_store.get_settings().get("ocr"), "max_edge": 2400}


def _saved(folder, key):
    path = Path(folder) / ("text_" + ocr_cache.digest(key) + ".json")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if ocr_cache.valid(record, key) and isinstance(record.get("lines"), list):
            return record
    except (OSError, ValueError, TypeError):
        pass
    return None


def _store(folder, record):
    record = ocr_cache.seal(record)
    try:
        invocation_trace.atomic_json(Path(folder) / ("text_" + ocr_cache.digest(record["identity"]) + ".json"), record)
    except OSError as exc:
        invocation_trace.add({"kind": "text_evidence_cache_error", "error": type(exc).__name__})
    return record


def pdf_descriptor(source, pdf_path, page_no, folder):
    """Private provenance stays beside the page, not in API-visible origins."""
    if Path(pdf_path).suffix.lower() == ".pdf":
        invocation_trace.atomic_json(Path(folder) / "pdf_source.json", {
            "path": str(Path(pdf_path).resolve()), "sha256": _sha(pdf_path), "page_no": page_no,
            "render_source": str(Path(source).resolve()), "render_identity": ocr_cache.pixels(source)})


def _point(point, transforms, size):
    x, y = point
    w, h = size
    for edit in transforms:
        if edit.get("input_size") != [w, h]:
            raise ValueError("PDF transform input size changed")
        if edit["kind"] == "rotate":
            angle = edit["cw"] % 360
            x, y = {0: lambda: (x, y), 90: lambda: (h-y, x),
                    180: lambda: (w-x, h-y), 270: lambda: (y, w-x)}[angle]()
            if angle in (90, 270):
                w, h = h, w
        elif edit["kind"] == "crop":
            l, t, r, b = edit["pixels"]
            x, y, w, h = x-l, y-t, r-l, b-t
        else:
            raise ValueError("Unsupported PDF transformation")
    return [x, y], [w, h]


def pdf_lines(source, folder, preparation):
    """Reject uncertain/mixed PDFs instead of treating any text layer as OCR."""
    if not preparation.get("pdf_enabled"):
        return None, "not_pdf_or_edited"
    try:
        descriptor = json.loads((Path(folder) / "pdf_source.json").read_text(encoding="utf-8"))
        if _sha(descriptor["path"]) != descriptor["sha256"] or ocr_cache.pixels(descriptor["render_source"]) != descriptor["render_identity"]:
            return None, "pdf_source_changed"
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw
        from app.core.pipeline import _PDF_LOCK
        with Image.open(source) as image:
            target_size = list(image.size)
        transforms = preparation.get("transforms", [])
        # Matching dimensions alone cannot prove that the working image is the
        # rendered PDF. Replay only the supported lossless edits and compare RGB.
        with Image.open(descriptor["render_source"]) as original:
            verified = original.convert("RGB")
        for edit in transforms:
            if list(verified.size) != edit.get("input_size"):
                return None, "transform_size_mismatch"
            if edit.get("kind") == "rotate" and edit.get("cw") in (0,90,180,270):
                operation = {90:Image.Transpose.ROTATE_270,180:Image.Transpose.ROTATE_180,270:Image.Transpose.ROTATE_90}.get(edit["cw"])
                if operation is not None:
                    verified = verified.transpose(operation)
            elif edit.get("kind") == "crop":
                verified = verified.crop(edit["pixels"])
            else:
                return None, "unsupported_image_edit"
        if hashlib.sha256(verified.tobytes()).hexdigest() != ocr_cache.pixels(source)["pixel_sha256"]:
            return None, "working_pixels_do_not_match_pdf"
        characters, invalid, printable, boxes, image_area = [], 0, 0, 0, 0
        base_size = descriptor["render_identity"]["size"]
        with _PDF_LOCK, pdfium.PdfDocument(descriptor["path"]) as pdf:
            page = pdf[descriptor["page_no"]-1]
            bitmap = page.render(scale=200/72)
            try:
                pos = bitmap.get_posconv(page)
                if [bitmap.width, bitmap.height] != base_size:
                    return None, "render_size_changed"
                for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE]):
                    l, b, r, t = obj.get_bounds()
                    points = [pos.to_bitmap(l,b), pos.to_bitmap(r,t)]
                    image_area += max(0,min(base_size[0],max(p[0] for p in points))-max(0,min(p[0] for p in points))) * max(0,min(base_size[1],max(p[1] for p in points))-max(0,min(p[1] for p in points)))
                if image_area > base_size[0]*base_size[1]*.10:
                    return None, "mixed_or_scanned_page"
                with closing(page.get_textpage()) as textpage:
                    for index in range(textpage.count_chars()):
                        code = raw.FPDFText_GetUnicode(textpage, index)
                        ch = chr(code) if 0 <= code <= 0x10ffff else "\ufffd"
                        if ch.isspace():
                            continue
                        printable += 1
                        if not ch.isprintable() or ch == "\ufffd":
                            invalid += 1
                            continue
                        l,b,r,t = textpage.get_charbox(index)
                        points = [pos.to_bitmap(l,b), pos.to_bitmap(r,t)]
                        points = [[min(p[0] for p in points), min(p[1] for p in points)],
                                  [max(p[0] for p in points), max(p[1] for p in points)]]
                        if points[0][0] >= points[1][0] or points[0][1] >= points[1][1]:
                            continue
                        boxes += 1
                        mapped = [_point(p, transforms, base_size) for p in points]
                        if any(size != target_size for _, size in mapped):
                            return None, "transform_size_mismatch"
                        a, z = [p for p, _ in mapped]
                        l,t,r,b = min(a[0],z[0]), min(a[1],z[1]), max(a[0],z[0]), max(a[1],z[1])
                        if r <= 0 or b <= 0 or l >= target_size[0] or t >= target_size[1]:
                            continue
                        if l < 0 or t < 0 or r > target_size[0] or b > target_size[1]:
                            return None, "crop_cuts_text"
                        characters.append({"text": ch, "box": [[l,t],[r,t],[r,b],[l,b]], "source": "pdf_text"})
            finally:
                bitmap.close()
        if printable < 20 or invalid / max(printable,1) > .01 or boxes / max(printable-invalid,1) < .95 or len(characters) < 20:
            return None, "unreliable_text_layer"
        rows = []
        for char in sorted(characters, key=lambda c: (sum(p[1] for p in c["box"])/4, c["box"][0][0])):
            top, bottom = char["box"][0][1], char["box"][2][1]
            center, height = (top+bottom)/2, bottom-top
            match = next((row for row in reversed(rows) if abs(row[0]-center) <= min(row[1],height)*.4), None)
            if match is None:
                rows.append([center, height, [char]])
            else:
                match[2].append(char)
        lines = []
        for _, _, row in rows:
            row.sort(key=lambda c: c["box"][0][0])
            points = [p for c in row for p in c["box"]]
            l,t,r,b = min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)
            text, previous = "", None
            for char in row:
                if previous and char["box"][0][0]-previous["box"][1][0] > max(2,(b-t)*.3):
                    text += " "
                text += char["text"]
                previous = char
            lines.append({"text":text, "box":[[l,t],[r,t],[r,b],[l,b]], "source":"pdf_text"})
        return lines, "usable_text_layer"
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        return None, "pdf_unavailable_" + type(exc).__name__


def read(source, *, folder, preparation=None, purpose="extraction"):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    key = identity(source)
    cached = _saved(folder, key)
    if cached is not None:
        invocation_trace.add({"kind":"text_evidence", "source":cached["source"], "cache_hit":True, "identity":key})
        return cached
    lines, reason = pdf_lines(source, folder, preparation or {})
    kind = "pdf_text"
    if lines is None:
        from app.core import model_factory, ocr_policy, ocr_resolution, settings_store
        provider, _ = settings_store.parse_setting(settings_store.get_settings()["ocr"])
        if provider in ("disabled","none"):
            return ocr_cache.seal({"identity":key,"source":"none","lines":[],"text":"","size":key["size"],"frame":"working_image_pixels"})
        ocr = model_factory.get_ocr(provider)
        target = folder / ("text_" + uuid.uuid4().hex + ".png")
        try:
            with ocr_cache.scope(folder / "ocr_cache"), ocr_policy.isolated_policy({"resolution":"fixed_2400"}):
                lines, input_path = ocr_resolution.read(source, target, ocr, purpose=purpose)
        except (ImportError,RuntimeError) as exc:
            invocation_trace.add({"kind":"text_evidence_failure","source":"ocr","error":type(exc).__name__})
            return ocr_cache.seal({"identity":key,"source":"none","lines":[],"text":"","size":key["size"],"frame":"working_image_pixels"})
        with Image.open(input_path) as image:
            lines = image_evidence.map_lines(lines, image.size, key["size"])
        kind = "ocr"
    record = _store(folder, {"identity":key,"source":kind,"lines":lines,"text":"\n".join(line["text"] for line in lines),
                            "size":key["size"],"frame":"working_image_pixels", "matrix":[[1,0,0],[0,1,0]]})
    invocation_trace.add({"kind":"text_evidence", "source":kind,"cache_hit":False,"reason":reason,"identity":key})
    return record


def validate(record, source):
    return bool(record and ocr_cache.valid(record, identity(source)) and record.get("frame") == "working_image_pixels")


def inherit(parent, child, region, *, folder, target_folder=None):
    """Only complete parent lines authorize reuse in a native, unrotated crop."""
    parent_record = _saved(folder, identity(parent))
    if not parent_record or region.get("rotate",0):
        return False
    sw, sh = parent_record["size"]
    l,t,r,b = [round(region["box"][i]*[sw,sh][i%2]/1000) for i in range(4)]
    key = identity(child)
    lines = []
    for line in parent_record["lines"]:
        xs,ys = [p[0] for p in line["box"]],[p[1] for p in line["box"]]
        if max(xs) <= l or min(xs) >= r or max(ys) <= t or min(ys) >= b:
            continue
        if min(xs) < l or max(xs) > r or min(ys) < t or max(ys) > b:
            return False
        lines.append({**deepcopy(line),"box":[[(p[0]-l)*key["size"][0]/(r-l),(p[1]-t)*key["size"][1]/(b-t)] for p in line["box"]]})
    if not lines:
        return False
    _store(target_folder or folder,{"identity":key,"source":parent_record["source"],"lines":lines,"text":"\n".join(v["text"] for v in lines),
                   "size":key["size"],"frame":"working_image_pixels","matrix":[[1,0,0],[0,1,0]],"inherited_from":parent_record["identity"]})
    return True
