"""統一編號核對 + 公司登錄（中意／仲彬／天干）。

statutory 統一編號檢查碼演算法（財政部現行版）：
權重 [1,2,1,2,1,2,4,1]，各位數乘權重後取「十位+個位」相加；
總和能被 5 整除即有效。特例：第 7 位為 7 時，總和 +1 亦可被 5 整除也算有效。
"""
import json

from app import config

_WEIGHTS = [1, 2, 1, 2, 1, 2, 4, 1]
_companies = None


def validate_tax_id(tax_id: str) -> bool:
    if not tax_id or not tax_id.isdigit() or len(tax_id) != 8:
        return False
    total = 0
    for i in range(8):
        prod = int(tax_id[i]) * _WEIGHTS[i]
        total += prod // 10 + prod % 10
    if total % 5 == 0:
        return True
    return tax_id[6] == "7" and (total + 1) % 5 == 0


def load_companies() -> list:
    global _companies
    if _companies is None:
        path = config.DATA_DIR / "companies.json"
        if path.exists():
            _companies = json.loads(path.read_text(encoding="utf-8"))
        else:
            _companies = []
    return _companies


def find_by_tax_id(tax_id: str):
    for c in load_companies():
        if c.get("tax_id") and c["tax_id"] == tax_id:
            return c
    return None


def check_party(name: str, tax_id: str) -> dict:
    """回傳單方（買/賣）的統編核對結果。"""
    valid = validate_tax_id(tax_id) if tax_id else None
    matched = find_by_tax_id(tax_id) if tax_id else None
    name_match = None
    if matched:
        # 已登錄公司：比對名稱是否相符（去空白包含即可）
        name_match = bool(name) and (matched["name"] in name or name in matched["name"]
                                     or matched["short"] in name)
    return {
        "tax_id": tax_id or "",
        "checksum_valid": valid,          # True/False/None（無統編）
        "known_company": matched["name"] if matched else "",
        "name_match": name_match,         # True/False/None
    }


def check_amounts(voucher: dict) -> dict:
    """金額算術一致性：未稅 ＋ 稅額 ＝ 總計；應稅另檢查稅額≈未稅×5%。

    手寫憑證的數字辨識不可能百分百（本系統實測手寫三聯式的「1575」會被讀成「1525」），
    與其寄望提示詞，不如把「兜不攏」這件事直接算給審核者看——算術是確定的，
    模型不是。回 {ok, warn}；缺值不算錯（很多單據本來就只有總計）。
    """
    f = (voucher or {}).get("fields") or {}

    def num(k):
        v = f.get(k)
        if v in (None, ""):
            return None
        try:
            return float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return None

    sub, tax, total = num("subtotal"), num("tax_amount"), num("total_amount")
    if sub is None or tax is None or total is None:
        return {"ok": True, "warn": ""}
    if abs(sub + tax - total) > 1:            # 容忍 1 元進位差
        return {"ok": False,
                "warn": f"金額兜不攏：未稅 {sub:,.0f} ＋ 稅額 {tax:,.0f} ＝ {sub + tax:,.0f}"
                        f"，但總計填 {total:,.0f}，請對照原圖核對"}
    if str(f.get("tax_type", "")).startswith("應稅") and sub > 0:
        expect = round(sub * 0.05)
        if abs(tax - expect) > max(1, expect * 0.02):
            return {"ok": False,
                    "warn": f"營業稅額 {tax:,.0f} 與未稅 {sub:,.0f} 的 5%（約 {expect:,.0f}）不符，請確認"}
    return {"ok": True, "warn": ""}


def check_date(date_str: str) -> dict:
    """憑證日期合理性檢查。

    民國轉西元算錯（如 1900+114=2014 而非 1911+114=2025）不會被任何既有檢查攔下，
    但 period 會跟著錯 → 該憑證從當期日記帳與報表中無聲消失。這是本系統最危險的
    錯誤類型，故單獨標示出來讓審核者當場看到。
    """
    from datetime import date as _date

    s = (date_str or "").strip()
    if not s:
        return {"ok": True, "warn": ""}
    try:
        y, m, d = (int(x) for x in s.split("-")[:3])
        v = _date(y, m, d)
    except (ValueError, TypeError):
        return {"ok": False, "warn": f"日期格式無法解析：{s}"}
    today = _date.today()
    days = (v - today).days
    if days > 90:
        return {"ok": False, "warn": f"日期在未來（{s}），請確認"}
    if days < -365 * 5:
        roc = y - 1911
        hint = f"；若單據上是民國 {y - 1900} 年，正確西元應為 {y - 1900 + 1911}" if 1990 <= y <= 2020 else ""
        return {"ok": False, "warn": f"日期距今超過 5 年（{s}，民國 {roc} 年），請確認{hint}"}
    return {"ok": True, "warn": ""}


def check_voucher(voucher: dict) -> dict:
    seller = voucher.get("seller", {}) or {}
    buyer = voucher.get("buyer", {}) or {}
    return {
        "seller": check_party(seller.get("name", ""), seller.get("tax_id", "")),
        "buyer": check_party(buyer.get("name", ""), buyer.get("tax_id", "")),
    }
