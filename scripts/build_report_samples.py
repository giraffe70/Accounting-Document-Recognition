"""為測試報告產生「各類憑證辨識結果」：彩色欄位框標註圖 + 欄位值表格。

資料全部取自實際辨識結果（app/data/app.db），不重新呼叫模型，因此不佔 GPU、
可重複執行。每個單據類型挑一張「框最多、有值欄位最多」的代表樣本。

輸出：docs/report/samples/<type>.png（標註圖）與 samples.json（欄位表資料）
"""
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "app/data/app.db"
UP = ROOT / "app/data/uploads"
OUT = ROOT / "docs/report/samples"

# 與前端審核畫面同一組色票，讓報告的框色與客戶在系統上看到的一致
PALETTE = ["#0969da", "#1a7f37", "#cf222e", "#8250df", "#bc4c00",
           "#0891b2", "#be185d", "#4d7c0f", "#9a6700", "#6e40c9",
           "#1b7c83", "#a40e26"]


def content_box(img_path: pathlib.Path):
    """回傳影像實際有內容的區域（0~1 比例）。

    掃描件常有 8~9 成是空白邊界，原樣貼進報告會讓憑證只佔一小角、框也看不清。
    另可用來判斷欄位框是否落在空白區——落在空白區的框必定是座標錯誤。
    """
    from PIL import Image

    im = Image.open(img_path).convert("L")
    w, h = im.size
    small = im.copy()
    small.thumbnail((400, 400))
    sw, sh = small.size
    px = small.load()
    xs = [x for x in range(sw) for y in range(sh) if px[x, y] < 220]
    ys = [y for y in range(sh) for x in range(sw) if px[x, y] < 220]
    if not xs or not ys:
        return 0.0, 0.0, 1.0, 1.0
    pad = 0.02
    return (max(0.0, min(xs) / sw - pad), max(0.0, min(ys) / sh - pad),
            min(1.0, max(xs) / sw + pad), min(1.0, max(ys) / sh + pad))


def box_quality(boxes: dict, cb) -> float:
    """框落在內容區內的比例。座標錯的框多半跑到空白邊界，用這個挑代表樣本。"""
    if not boxes:
        return 0.0
    cx1, cy1, cx2, cy2 = cb
    good = 0
    for b in boxes.values():
        if not (isinstance(b, list) and len(b) >= 4):
            continue
        x1, y1, x2, y2 = [c / 1000 for c in b[:4]]
        if x1 >= cx1 - 0.02 and y1 >= cy1 - 0.02 and x2 <= cx2 + 0.02 and y2 <= cy2 + 0.02:
            good += 1
    return good / len(boxes)


def draw(img_path: pathlib.Path, boxes: dict, fields_order: list, out: pathlib.Path):
    """裁去空白邊後，在憑證上畫出彩色編號框（座標為 0~1000 正規化）。"""
    from PIL import Image, ImageDraw, ImageFont

    im = Image.open(img_path).convert("RGB")
    W, H = im.size
    cx1, cy1, cx2, cy2 = content_box(img_path)
    im = im.crop((int(cx1 * W), int(cy1 * H), int(cx2 * W), int(cy2 * H)))
    w, h = im.size
    scale = max(1.0, 1400 / max(w, h))       # 放大到長邊約 1400，框線與編號才清楚
    if scale > 1:
        im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        w, h = im.size
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msjh.ttc", max(15, int(h / 55)))
    except OSError:
        font = ImageFont.load_default()

    n = 0
    for i, key in enumerate(fields_order):
        b = boxes.get(key)
        if not (isinstance(b, list) and len(b) >= 4):
            continue
        n += 1
        col = PALETTE[i % len(PALETTE)]
        x1, y1, x2, y2 = [c / 1000 for c in b[:4]]
        # 座標是相對原圖的，裁切後要換算到新座標系
        rect = [(x1 - cx1) / (cx2 - cx1) * w, (y1 - cy1) / (cy2 - cy1) * h,
                (x2 - cx1) / (cx2 - cx1) * w, (y2 - cy1) / (cy2 - cy1) * h]
        d.rectangle(rect, outline=col, width=3)
        # 編號徽章（左上角）
        tag = str(n)
        tw = d.textlength(tag, font=font)
        bx, by = rect[0], max(0, rect[1] - font.size - 6)
        d.rectangle([bx, by, bx + tw + 12, by + font.size + 6], fill=col)
        d.text((bx + 6, by + 2), tag, fill="#fff", font=font)
    im.save(out)
    return n


def main():
    from app.core import doc_types

    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    best = {}
    for rid, cur, img, comp, src in con.execute(
            "select id, current_json, image_path, company, source_file from voucher_records"):
        if not img:
            continue
        d = json.loads(cur)
        k = d.get("doc_type_key") or "other"
        nb = len(d.get("boxes") or {})
        nf = sum(1 for v in (d.get("fields") or {}).values() if v not in ("", None))
        p_img = UP / img
        if not p_img.exists():
            continue
        q = box_quality(d.get("boxes") or {}, content_box(p_img))
        # 框品質優先：座標錯的框（落在空白邊界）不適合當代表樣本
        score = (round(q, 2), nb, nf)
        if k not in best or score > best[k][0]:
            best[k] = (score, rid, d, img, comp, src)

    samples = []
    for k, (score, rid, d, img, comp, src) in sorted(best.items(), key=lambda x: -x[1][0][0]):
        spec = doc_types.get(k)
        order = [f["key"] for f in spec["fields"]]
        p = UP / img
        if not p.exists():
            continue
        out = OUT / f"{k}.png"
        nbox = draw(p, d.get("boxes") or {}, order, out)
        rows = []
        for i, f in enumerate(spec["fields"]):
            v = (d.get("fields") or {}).get(f["key"])
            if v in ("", None):
                continue
            has_box = f["key"] in (d.get("boxes") or {})
            rows.append({"label": f["label"], "key": f["key"], "value": v, "boxed": has_box})
        samples.append({
            "type_key": k, "label": spec["label"], "rec_id": rid,
            "company": comp or "", "source": src or "",
            "image": out.name, "n_box": nbox, "fields": rows,
            "note": d.get("note", ""),
            "entries": d.get("entries") or [],
        })
        print(f"  {spec['label'][:16]:<18} #{rid:<4} 框 {nbox:>2} 欄位 {len(rows):>2}  → {out.name}")

    (OUT.parent / "samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n共 {len(samples)} 種類型 → docs/report/samples/")


if __name__ == "__main__":
    main()
