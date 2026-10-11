"""條碼／QR 解碼（零外部相依，使用 OpenCV）。

用途：
  ① 影像上若有可讀的 QR／條碼，解出其內容作為辨識 grounding（可信度高）。
  ② 台灣電子發票證明聯左側 QR 為明文前綴，可「直接」解出發票號碼、日期、
     未稅／含稅金額、買賣方統編等欄位。

限制：OpenCV 的 QRCodeDetector 對消費者電子發票證明聯 QR 效果佳；1D 條碼
（如海關報單的 Code 39）支援有限，且掃描解析度不足時常無法讀取——此時退回
由 VLM 依版面辨識。所有函式皆防呆，失敗不影響主流程。
"""
import logging

logger = logging.getLogger("pdf_scan.barcode")


def decode(image_path: str) -> list[str]:
    """Compatible code list; diagnostics distinguish detection and validation."""
    return decode_diagnostics(image_path)["codes"]


def summarize(codes, regions, size):
    candidates = parse_einvoice_qr_candidates(codes)
    status = ("conflict" if len(candidates) > 1 else "valid" if candidates else
              "invalid" if codes else "detected_undecoded" if regions else "not_detected")
    return {"status": status, "codes": codes, "candidates": candidates, "regions": regions,
            "source_size": size, "coordinate_unit": "pixels",
            "adoption_reason": "single valid candidate" if len(candidates) == 1 else "no unambiguous valid invoice"}


def decode_diagnostics(image_path: str) -> dict:
    """Detect native QR regions, decode each once; failure never blocks extraction."""
    found, regions, size, errors, detections = [], [], [], [], []
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("unreadable QR source")
        h, w = img.shape[:2]
        size = [w, h]
        qr = cv2.QRCodeDetector()
        ok, points = qr.detectMulti(img)
        if not ok:
            ok, points = qr.detect(img)
        for polygon in (np.asarray(points).reshape(-1,4,2) if ok and points is not None else []):
            x0,y0 = np.floor(polygon.min(axis=0)).astype(int)
            x1,y1 = np.ceil(polygon.max(axis=0)).astype(int)
            margin = max(4, round(max(x1-x0,y1-y0)*.1))
            box = [max(0,x0-margin), max(0,y0-margin), min(w,x1+margin), min(h,y1+margin)]
            regions.append([int(v) for v in box])
            raw, _, _ = qr.detectAndDecode(img[box[1]:box[3],box[0]:box[2]])
            detections.append({"box":regions[-1], "raw":raw, "candidates":parse_einvoice_qr_candidates([raw]) if raw else []})
            if raw and raw not in found:
                found.append(raw)
        try:
            result = cv2.barcode.BarcodeDetector().detectAndDecode(img)
            values = result[0] if isinstance(result[0], (list,tuple)) else [result[0]]
            found.extend(v for v in values if v and v not in found)
        except Exception as exc:
            errors.append("barcode: " + str(exc)[:120])
    except Exception as exc:
        errors.append(str(exc)[:160])
    result = summarize(found, regions, size)
    result.update(path=image_path, errors=errors, detections=detections)
    return result


def parse_einvoice_qr_candidates(codes: list[str]) -> list[dict]:
    """Decode valid left-QR prefixes; retain all conflicting candidates."""
    from datetime import date
    import re
    candidates = []
    for raw in codes or []:
        s = str(raw).strip()
        if len(s) < 53 or not re.fullmatch(r"[A-Za-z]{2}[0-9]{8}", s[:10]) or not s[10:17].isdigit():
            continue
        try:
            d = date(int(s[10:13]) + 1911, int(s[13:15]), int(s[15:17])).isoformat()
            subtotal, total = int(s[21:29], 16), int(s[29:37], 16)
        except (ValueError, TypeError):
            continue
        if not s[37:45].isascii() or not s[37:45].isdigit() or not s[45:53].isascii() or not s[45:53].isdigit():
            continue
        item = {"voucher_id": s[:10].upper(), "date": d, "subtotal": subtotal, "total_amount": total,
                "buyer_tax_id": s[37:45], "seller_tax_id": s[45:53]}
        if item not in candidates:
            candidates.append(item)
    return candidates


def parse_einvoice_qr(codes: list[str]) -> dict:
    """Compatibility: only a single unambiguous valid invoice may override fields."""
    candidates = parse_einvoice_qr_candidates(codes)
    return candidates[0] if len(candidates) == 1 else {}
