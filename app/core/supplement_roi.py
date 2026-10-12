"""Conservative, single-frame regions for one supplemental read."""
from __future__ import annotations

import re

from app.core import image_evidence

VERSION = 1
MAX_AREA = .60
LABELS = {
    "invoice": {
        "voucher_id": r"發票(?:號碼|編號)|字軌號碼",
        "date": r"發票日期|開立日期|日期",
        "seller_name": r"賣方|銷售人|營業人|店家|開立人",
        "seller_tax_id": r"賣方|銷售人|營業人|統一編號|統編",
        "buyer_name": r"買方|買受人|客戶",
        "buyer_tax_id": r"買方|買受人|客戶|統一編號|統編",
        "subtotal": r"未稅|銷售額|小計",
        "tax_amount": r"營業稅|稅額",
        "total_amount": r"含稅|總計|應付金額|實付金額",
    },
    "receipt": {
        "date": r"收據日期|開立日期|日期",
        "vendor": r"廠商|收款人|店家|營業人",
        "tax_id": r"統一編號|統編",
        "subtotal": r"未稅|小計",
        "tax_amount": r"營業稅|稅額",
        "total_amount": r"應付金額|實收金額|總計|金額",
    },
}
MONEY_CONTEXT = re.compile(r"幣別|NT\$|TWD|USD|EUR|JPY|人民幣|未稅|小計|營業稅|稅額", re.I)
OTHER_DOCUMENT = re.compile(r"發票|收據|傳票|支票|報單|對帳單")


def _box(line, size):
    points = line.get("box") or []
    if not points or len(points) != 4:
        return None
    w, h = size
    if min(w, h) <= 0:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [min(xs) * 1000 / w, min(ys) * 1000 / h,
            max(xs) * 1000 / w, max(ys) * 1000 / h]


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _intersects(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _contains(a, b):
    return a[0] <= b[0] and a[1] <= b[1] and a[2] >= b[2] and a[3] >= b[3]


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1]) / 1_000_000


def same_visual_frame(display_path, source_path):
    """Reject a rotated or unrelated native image before reusing mapped OCR."""
    from PIL import Image, ImageChops, ImageStat
    with Image.open(display_path) as display, Image.open(source_path) as source:
        dw, dh = display.size
        sw, sh = source.size
        if min(dw, dh, sw, sh) <= 0 or abs(dw / dh - sw / sh) >= .02:
            return False
        display = display.convert("L").resize((128, 128), Image.Resampling.LANCZOS)
        source = source.convert("L").resize((128, 128), Image.Resampling.LANCZOS)
    return ImageStat.Stat(ImageChops.difference(display, source)).mean[0] <= 22


def _table_ids(kind, rows):
    if kind == "entries":
        headers = [r for r in rows if "借方" in r["text"] or "貸方" in r["text"]]
        if not any("借方" in r["text"] for r in headers) or not any("貸方" in r["text"] for r in headers):
            return set()
    else:
        headers = [r for r in rows if re.search(r"品名|項目|明細", r["text"])]
        if not headers or not re.search(r"數量|單價|金額", " ".join(r["text"] for r in rows)):
            return set()
    top = min(r["box"][1] for r in headers)
    end = min((r["box"][1] for r in rows if r["box"][1] > top + 15 and
               re.search(r"含稅|總計|應付|實付", r["text"])), default=1000)
    body = [r for r in rows if top <= r["box"][1] < end and
            not (OTHER_DOCUMENT.search(r["text"]) and r not in headers)]
    if not any(r not in headers and re.search(r"\d", r["text"]) for r in body):
        return set()
    return {r["id"] for r in body}


def choose(type_key, requested, groups, lines, size):
    """Return a normalized document ROI or a reason to use the full document."""
    rows = [{"id": i, "text": str(line.get("text") or ""), "box": box}
            for i, line in enumerate(lines or []) if (box := _box(line, size))]
    if not rows:
        return None, "no_positioned_ocr"
    selected = set()
    rules = LABELS.get(type_key, {})
    for key in requested:
        pattern = rules.get(key)
        if not pattern:
            return None, "unsupported_target"
        anchors = [r for r in rows if re.search(pattern, r["text"], re.I)]
        if key == "total_amount":
            anchors = [r for r in anchors if not re.search(r"未稅|小計|稅額|營業稅", r["text"])]
        if key.startswith("seller"):
            anchors = [r for r in anchors if not re.search(r"買方|買受人|客戶", r["text"])]
        if key.startswith("buyer"):
            anchors = [r for r in anchors if not re.search(r"賣方|銷售人|營業人", r["text"])]
        if len(anchors) != 1:
            return None, "missing_or_ambiguous_anchor"
        anchor = anchors[0]
        selected.add(anchor["id"])
        ay = (anchor["box"][1] + anchor["box"][3]) / 2
        # Keep a label's value on the same row or immediately below it.
        nearby = [r for r in rows if r["id"] != anchor["id"] and
                  -12 <= (r["box"][1] + r["box"][3]) / 2 - ay <= 45 and
                  not OTHER_DOCUMENT.search(r["text"]) and
                  not (key.startswith("seller") and re.search(r"買方|買受人|客戶", r["text"])) and
                  not (key.startswith("buyer") and re.search(r"賣方|銷售人|營業人", r["text"]))]
        selected.update(r["id"] for r in nearby)
        if key in ("subtotal", "tax_amount", "total_amount"):
            related = [r for r in rows if abs((r["box"][1] + r["box"][3]) / 2 - ay) <= 110
                       and MONEY_CONTEXT.search(r["text"])]
            selected.update(r["id"] for r in related)
            for label in related:
                ly = (label["box"][1] + label["box"][3]) / 2
                selected.update(r["id"] for r in rows if r["id"] != label["id"] and
                                -12 <= (r["box"][1] + r["box"][3]) / 2 - ly <= 45 and
                                re.search(r"\d", r["text"]) and not OTHER_DOCUMENT.search(r["text"]))
    for group in groups:
        ids = _table_ids(group, rows)
        if not ids:
            return None, "incomplete_table_anchor"
        selected.update(ids)
    if not selected:
        return None, "no_target"
    boxes = [r["box"] for r in rows if r["id"] in selected]
    evidence = {"text_boxes": rows}
    try:
        box = image_evidence.safe_margin([round(v) for v in _union(boxes)], evidence)
    except ValueError:
        return None, "unsafe_text_boundary"
    if _area(box) >= MAX_AREA:
        return None, "region_near_full_page"
    if any(OTHER_DOCUMENT.search(r["text"]) and r["id"] not in selected and
           _intersects(box, r["box"]) for r in rows):
        return None, "possible_other_document"
    return {"box": box, "rotate": 0, "selected_ids": sorted(selected), "source": "ocr_anchors"}, None


def map_existing(lines, source_size, region, output_size):
    """Map first-pass document pixels into output crop pixels; omit partial lines."""
    result = []
    sx0, sy0, sx1, sy1 = region["box"]
    ow, oh = output_size
    for line in lines or []:
        box = _box(line, source_size)
        if not box or not _contains([sx0, sy0, sx1, sy1], box):
            continue
        mapped = {**line, "box": [[(p[0] * 1000 / source_size[0] - sx0) * ow / (sx1 - sx0),
                                  (p[1] * 1000 / source_size[1] - sy0) * oh / (sy1 - sy0)]
                                 for p in line["box"]]}
        result.append(mapped)
    return result


def box_to_document(box, region, *, image_size=None, enhancement_matrix=None):
    """Compose optional enhanced pixels, crop pixels, then document coordinates."""
    if enhancement_matrix is not None:
        if image_size is None:
            raise ValueError("enhanced box requires image size")
        a, b, tx = enhancement_matrix[0]
        c, d, ty = enhancement_matrix[1]
        det = a * d - b * c
        if abs(det) < 1e-9:
            raise ValueError("enhancement matrix is singular")
        w, h = image_size
        corners = [(box[0] * w / 1000, box[1] * h / 1000),
                   (box[2] * w / 1000, box[1] * h / 1000),
                   (box[2] * w / 1000, box[3] * h / 1000),
                   (box[0] * w / 1000, box[3] * h / 1000)]
        original = [((d * (x - tx) - b * (y - ty)) / det,
                     (-c * (x - tx) + a * (y - ty)) / det) for x, y in corners]
        box = [max(0, min(x for x, _ in original) * 1000 / w),
               max(0, min(y for _, y in original) * 1000 / h),
               min(1000, max(x for x, _ in original) * 1000 / w),
               min(1000, max(y for _, y in original) * 1000 / h)]
    x0, y0, x1, y1 = region["box"]
    return [round(x0 + box[0] * (x1 - x0) / 1000),
            round(y0 + box[1] * (y1 - y0) / 1000),
            round(x0 + box[2] * (x1 - x0) / 1000),
            round(y0 + box[3] * (y1 - y0) / 1000)]


def needs_fresh_ocr(type_key, requested, groups, lines, region, *, source_size):
    """Require unresolved target text and a useful gain from a native crop."""
    if not requested and not groups:
        return False, "no_target"
    text = "\n".join(str(r.get("text") or "") for r in lines)
    clear = bool(lines) and all(float(r.get("score", 1)) >= .8 for r in lines)
    for key in requested:
        label = LABELS.get(type_key, {}).get(key)
        if not label or not re.search(label, text, re.I):
            clear = False
        if key.endswith("tax_id") and not re.search(r"(?<!\d)\d{8}(?!\d)", text):
            clear = False
        if key in ("subtotal", "tax_amount", "total_amount") and not re.search(r"\d[\d,]*(?:\.\d+)?", text):
            clear = False
        if key == "voucher_id" and not re.search(r"[A-Z]{2}\s*\d{8}", text, re.I):
            clear = False
        if key == "date" and not re.search(r"\d{2,4}\s*[/年月.\-]\s*\d{1,2}", text):
            clear = False
    if groups and not re.search(r"\d", text):
        clear = False
    if clear:
        return False, "existing_ocr_readable"
    sw, sh = source_size
    crop_w = sw * (region["box"][2] - region["box"][0]) / 1000
    crop_h = sh * (region["box"][3] - region["box"][1]) / 1000
    full_scale = min(1, 2400 / max(sw, sh))
    crop_scale = min(1, 2400 / max(crop_w, crop_h))
    gain = crop_scale / full_scale
    if gain >= 1.25 or _area(region["box"]) <= .25:
        return True, "native_detail_or_isolation"
    return False, "no_detail_gain"
