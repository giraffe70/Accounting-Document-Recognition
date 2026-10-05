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
    """回傳影像上偵測到的 QR／條碼內容字串（去重、非空）。失敗回空清單。"""
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return []
    # cv2.imread 在 Windows 無法讀取含中文／非 ASCII 的路徑（回 None 且不報錯），
    # 而本系統的頁圖檔名取自來源檔（多為中文），等於電子發票 QR 解碼會靜默失效。
    # 改用 numpy 讀 bytes 再 imdecode 繞過。
    try:
        import numpy as np

        buf = np.fromfile(image_path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
    except Exception:  # noqa: BLE001
        img = cv2.imread(image_path)
    if img is None:
        return []
    found: list[str] = []

    # QR（原圖 + 放大重試，提高低解析度命中率）
    try:
        qr = cv2.QRCodeDetector()
        for scale in (1, 2):
            src = img if scale == 1 else cv2.resize(img, None, fx=scale, fy=scale,
                                                    interpolation=cv2.INTER_CUBIC)
            ok, infos, _pts, _ = qr.detectAndDecodeMulti(src)
            if ok:
                for s in (infos or []):
                    if s and s not in found:
                        found.append(s)
            if found:
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("QR 解碼略過：%s", exc)

    # 1D 條碼（best-effort；多數掃描報單條碼讀不出屬正常）
    try:
        bd = cv2.barcode.BarcodeDetector()
        res = bd.detectAndDecode(img)
        for s in (res[0] or []) if isinstance(res[0], (list, tuple)) else ([res[0]] if res[0] else []):
            if s and s not in found:
                found.append(s)
    except Exception as exc:  # noqa: BLE001
        logger.debug("條碼解碼略過：%s", exc)

    return found


def parse_einvoice_qr(codes: list[str]) -> dict:
    """解析台灣電子發票證明聯「左側」QR 的明文前綴，回欄位 dict（無則回空）。

    版面（MIG）：發票號碼(10) 日期民國YYYMMDD(7) 隨機碼(4)
                 未稅金額(8 hex) 含稅金額(8 hex) 買方統編(8) 賣方統編(8) …後略。
    """
    for raw in codes or []:
        s = str(raw).strip()
        if len(s) < 53:
            continue
        inv = s[0:10]
        if not (inv[:2].isalpha() and inv[2:10].isdigit()):
            continue
        d = s[10:17]
        if not d.isdigit():
            continue
        try:
            yy, mm, dd = int(d[:3]) + 1911, d[3:5], d[5:7]
            date = f"{yy}-{mm}-{dd}"
            subtotal = int(s[21:29], 16)
            total = int(s[29:37], 16)
        except (ValueError, TypeError):
            continue
        buyer_id = s[37:45]
        seller_id = s[45:53]
        out = {
            "voucher_id": inv.upper(), "date": date,
            "subtotal": subtotal, "total_amount": total,
        }
        if buyer_id.isdigit():
            out["buyer_tax_id"] = buyer_id
        if seller_id.isdigit():
            out["seller_tax_id"] = seller_id
        return out
    return {}
