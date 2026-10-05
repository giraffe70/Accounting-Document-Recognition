"""Run the current recognition pipeline over every image in ref/test_img.

The runner keeps source images immutable, records the full structured response,
and creates the three evidence visuals required by the validation report:
annotated source, field-result panel, and item-detail panel.  It intentionally
does not score accuracy; human-reviewed labels are supplied separately.
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "ref" / "test_img"
OUT = ROOT / "artifacts" / "full_image_validation"
WORK = OUT / "working"
VISUALS = OUT / "visuals"

PALETTE = ["#0969da", "#1a7f37", "#cf222e", "#8250df", "#bc4c00", "#0891b2",
           "#be185d", "#4d7c0f", "#9a6700", "#6e40c9", "#1b7c83", "#a40e26"]


def font(size: int, bold: bool = False):
    names = (["C:/Windows/Fonts/msjhbd.ttc", "C:/Windows/Fonts/msjh.ttc"] if bold else
             ["C:/Windows/Fonts/msjh.ttc", "C:/Windows/Fonts/msjhbd.ttc"])
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def safe(value) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def prepare_image(source: Path, dest: Path, max_edge: int = 1000) -> None:
    """Copy and resize only the inference copy so raw data remain unchanged."""
    with Image.open(source) as img:
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        img.save(dest, format="JPEG", quality=94)


def add_annotated_image(image_path: Path, boxes: dict, field_keys: list[str], out: Path) -> int:
    with Image.open(image_path) as original:
        image = original.convert("RGB")
    w, h = image.size
    scale = max(1.0, 1600 / max(w, h))
    if scale > 1:
        image = image.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
        w, h = image.size
    draw = ImageDraw.Draw(image)
    tag_font = font(max(20, int(h / 45)), bold=True)
    number = 0
    for idx, key in enumerate(field_keys):
        raw = boxes.get(key)
        if not isinstance(raw, list) or len(raw) < 4:
            continue
        try:
            x1, y1, x2, y2 = [float(x) for x in raw[:4]]
        except (TypeError, ValueError):
            continue
        # Pipeline persists normalized 0~1000 bounding boxes.
        x1, x2 = x1 / 1000 * w, x2 / 1000 * w
        y1, y2 = y1 / 1000 * h, y2 / 1000 * h
        if x2 <= x1 or y2 <= y1:
            continue
        number += 1
        colour = PALETTE[idx % len(PALETTE)]
        draw.rectangle((x1, y1, x2, y2), outline=colour, width=max(3, int(h / 240)))
        badge = str(number)
        bx, by = x1, max(0, y1 - tag_font.size - 5)
        bbox = draw.textbbox((bx, by), badge, font=tag_font)
        draw.rounded_rectangle((bx, by, bbox[2] + 12, by + tag_font.size + 7), 4, fill=colour)
        draw.text((bx + 6, by + 1), badge, fill="white", font=tag_font)
    image.save(out, quality=94)
    return number


def _wrapped(draw: ImageDraw.ImageDraw, text: str, width: int, fnt):
    out = []
    current = ""
    for ch in str(text):
        candidate = current + ch
        if draw.textlength(candidate, font=fnt) > width and current:
            out.append(current)
            current = ch
        else:
            current = candidate
    if current:
        out.append(current)
    return out or ["—"]


def add_field_panel(record: dict, fields_spec: list[dict], out: Path) -> None:
    """Render a faithful right-side field-result panel from the saved system output."""
    fields = record.get("recognized", {}).get("fields") or {}
    voucher = record.get("recognized", {})
    rows = [(x["label"], safe(fields.get(x["key"]))) for x in fields_spec]
    if not rows:
        rows = [("憑證號碼", safe(voucher.get("voucher_id"))), ("日期", safe(voucher.get("date"))),
                ("總計", safe(voucher.get("total_amount")))]
    # Three columns deliberately mirror a compact review-panel screenshot and
    # keep the evidence readable beside the source image in a landscape report.
    cols, width = 3, 1500
    row_h, start_y = 78, 112
    height = max(320, start_y + row_h * ((len(rows) + cols - 1) // cols) + 58)
    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)
    title_font, label_font, value_font = font(27, True), font(16, True), font(20)
    draw.text((34, 22), "辨識欄位結果", font=title_font, fill="#172b4d")
    draw.text((34, 65), f"單據類型：{safe(voucher.get('doc_type'))}    信心度：{safe(voucher.get('confidence'))}",
              font=font(16), fill="#52606d")
    col_w = 448
    for i, (label, value) in enumerate(rows):
        col, row = i % cols, i // cols
        x, y = 34 + col * (col_w + 43), start_y + row * row_h
        colour = PALETTE[i % len(PALETTE)]
        draw.rounded_rectangle((x, y, x + col_w, y + 58), 7, outline="#c8d1dc", width=2, fill="#fbfcfe")
        draw.rounded_rectangle((x + col_w - 34, y - 11, x + col_w, y + 20), 16, fill=colour)
        draw.text((x + col_w - 25, y - 9), str(i + 1), font=font(14, True), fill="white")
        draw.text((x + 12, y + 7), label, font=label_font, fill="#425466")
        lines = _wrapped(draw, value, col_w - 24, value_font)
        draw.text((x + 12, y + 31), lines[0], font=value_font, fill="#101828")
    image.save(out, quality=95)


def add_detail_panel(record: dict, out: Path) -> None:
    items = record.get("recognized", {}).get("items") or []
    width = 1180
    row_h = 70
    height = max(260, 130 + row_h * max(1, len(items)) + 45)
    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)
    draw.text((44, 30), "明細辨識結果", font=font(31, True), fill="#172b4d")
    headers = [(44, 500, "品名"), (500, 665, "數量"), (665, 840, "單價"), (840, 1136, "合計")]
    for x1, x2, title in headers:
        draw.rectangle((x1, 84, x2, 130), fill="#eaf1f8", outline="#b9c7d6")
        draw.text((x1 + 12, 94), title, font=font(20, True), fill="#172b4d")
    if not items:
        items = [{"description": "未辨識到明細", "quantity": "—", "unit_price": "—", "amount": "—"}]
    for i, item in enumerate(items):
        y = 130 + i * row_h
        vals = [safe(item.get("description")), safe(item.get("quantity")), safe(item.get("unit_price")), safe(item.get("amount"))]
        for (x1, x2, _), val in zip(headers, vals):
            draw.rectangle((x1, y, x2, y + row_h), fill="#ffffff" if i % 2 == 0 else "#f8fafc", outline="#c8d1dc")
            line = _wrapped(draw, val, x2 - x1 - 24, font(21))[0]
            draw.text((x1 + 12, y + 20), line, font=font(21), fill="#101828")
    image.save(out, quality=95)


def run_one(source: Path, *, force: bool = False) -> dict:
    from app.core import doc_types, model_factory, pipeline

    stem = source.stem
    work = WORK / f"{stem}.jpg"
    if force or not work.exists():
        prepare_image(source, work)
    # The deployed Paddle OCR loader does not complete inference reliably.  Use the
    # same VLM-direct fallback documented in the initial report, without changing
    # the persisted system setting.
    model_factory.get_ocr = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("OCR disabled for validation fallback"))
    started = datetime.now(timezone.utc).isoformat()
    type_key = pipeline.prepare_image(str(work), auto_rotate=False)
    voucher = pipeline.extract_for_type(str(work), type_key)
    data = voucher.model_dump(mode="json")
    spec = doc_types.get(type_key)
    field_keys = [f["key"] for f in spec.get("fields", [])]
    image_out = VISUALS / f"{stem}_boxes.jpg"
    fields_out = VISUALS / f"{stem}_fields.jpg"
    details_out = VISUALS / f"{stem}_details.jpg"
    box_count = add_annotated_image(work, data.get("boxes") or {}, field_keys, image_out)
    record = {
        "source_image": str(source.relative_to(ROOT)).replace("\\", "/"),
        "working_image": str(work.relative_to(ROOT)).replace("\\", "/"),
        "started_at_utc": started,
        "doc_type_key": type_key,
        "recognized": data,
        "box_count": box_count,
        "visuals": {"annotated": str(image_out.relative_to(ROOT)).replace("\\", "/"),
                    "fields": str(fields_out.relative_to(ROOT)).replace("\\", "/"),
                    "details": str(details_out.relative_to(ROOT)).replace("\\", "/")},
    }
    add_field_panel(record, spec.get("fields", []), fields_out)
    add_detail_panel(record, details_out)
    return record


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", default="", help="comma-separated file names or stems")
    parser.add_argument("--render-only", action="store_true", help="rebuild evidence visuals from saved recognition JSON")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    VISUALS.mkdir(parents=True, exist_ok=True)
    result_path = OUT / "recognition_evidence.json"
    existing = {}
    if result_path.exists() and not args.force:
        existing = {Path(r["source_image"]).name: r for r in json.loads(result_path.read_text(encoding="utf-8")).get("results", [])}
    if args.render_only:
        from app.core import doc_types
        for name, record in sorted(existing.items()):
            if record.get("error") or not record.get("recognized"):
                continue
            spec = doc_types.get(record.get("doc_type_key", "other"))
            work = ROOT / record["working_image"]
            visuals = record.get("visuals") or {}
            if work.exists():
                add_annotated_image(work, record["recognized"].get("boxes") or {},
                                    [x["key"] for x in spec.get("fields", [])],
                                    ROOT / visuals["annotated"])
            add_field_panel(record, spec.get("fields", []), ROOT / visuals["fields"])
            add_detail_panel(record, ROOT / visuals["details"])
            print(f"rendered {name}")
        return
    wanted = {x.strip() for x in args.only.split(",") if x.strip()}
    files = sorted(SOURCE.glob("*.jpg"))
    if wanted:
        files = [x for x in files if x.name in wanted or x.stem in wanted]
    results = []
    for index, source in enumerate(files, 1):
        if source.name in existing and not args.force:
            result = existing[source.name]
            print(f"[{index}/{len(files)}] cached {source.name}")
        else:
            try:
                result = run_one(source, force=args.force)
                print(f"[{index}/{len(files)}] {source.name} {result['doc_type_key']} boxes={result['box_count']}")
            except Exception as exc:  # keep evidence for failed recognition as well
                result = {"source_image": str(source.relative_to(ROOT)).replace("\\", "/"),
                          "error": f"{type(exc).__name__}: {exc}",
                          "started_at_utc": datetime.now(timezone.utc).isoformat()}
                print(f"[{index}/{len(files)}] {source.name} ERROR {result['error']}")
        existing[source.name] = result
        results = [existing[k] for k in sorted(existing)]
        result_path.write_text(json.dumps({"scope": "all test_img live recognition", "ocr_runtime": "disabled VLM fallback", "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
