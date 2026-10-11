"""Deterministic normalization; unreadable values never become guessed amounts."""
from __future__ import annotations
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

_DIGITS = {c: n for n, chars in enumerate(("零〇", "一壹", "二貳贰兩两", "三參叁", "四肆", "五伍", "六陸陆", "七柒", "八捌", "九玖")) for c in chars}
_SMALL = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}


def _integer_words(s: str) -> int:
    if s and all(c in _DIGITS for c in s):
        return int("".join(str(_DIGITS[c]) for c in s))
    number = section = total = 0
    for c in s:
        if c in _DIGITS:
            number = _DIGITS[c]
        elif c in _SMALL:
            section += (number or 1) * _SMALL[c]
            number = 0
        elif c in ("萬", "万"):
            total += (section + number) * 10000
            section = number = 0
        elif c in ("億", "亿"):
            total = (total + section + number) * 100000000
            section = number = 0
        else:
            raise ValueError("unrecognized money character")
    return total + section + number


def amount_annotation(value):
    """Return a candidate for a single amount with a known printed annotation."""
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    match = re.fullmatch(r"(?:(NT\$|TWD|新臺幣|新台幣)\s*)?(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:元\s*)?(\((?:現金|信用卡|刷卡|轉帳|匯款)\)|TX|TZ|TE)", text, re.I)
    if not match:
        return None
    number = Decimal(match[2].replace(",", ""))
    return {"raw": value, "value": int(number) if number == number.to_integral() else float(number),
            "annotation": match[3], "currency": "TWD" if match[1] or "元" in text else None}


def norm_amount(value):
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    original = str(value).strip()
    s = unicodedata.normalize("NFKC", original)
    s = re.sub(r"(?i)NT\$?|TWD|新[臺台]幣|人民幣|[,$\s]", "", s)
    numeric = s.rstrip("元圓圆")
    negative = numeric.startswith("(") and numeric.endswith(")")
    if negative:
        numeric = "-" + numeric[1:-1]
    try:
        if re.fullmatch(r"-?\d+(?:\.\d+)?", numeric):
            n = Decimal(numeric)
            return int(n) if n == n.to_integral() else float(n)
    except InvalidOperation:
        return value
    try:
        # A whole Chinese amount must consist only of numerals/units.
        s = s.rstrip("整正")
        parts = re.split("[元圓圆]", s)
        if len(parts) > 2:
            return value
        integer, fraction = (parts[0], parts[1]) if len(parts) == 2 else (s, "")
        if len(parts) == 1 and any(c in s for c in "角分"):
            integer, fraction = "", s
        result = Decimal(_integer_words(integer))
        if fraction:
            m = re.fullmatch(r"零?([一二三四五六七八九壹貳贰參叁肆伍陸陆柒捌玖])?角?零?([一二三四五六七八九壹貳贰參叁肆伍陸陆柒捌玖])?分?", fraction)
            # Require units; a bare digit is not a decimal fraction.
            if not m or not any(c in fraction for c in "角分"):
                return value
            if "角" in fraction:
                angle = re.search(r"(.)角", fraction)
                result += Decimal(_DIGITS[angle[1]]) / 10
            if "分" in fraction:
                cent = re.search(r"(.)分", fraction)
                result += Decimal(_DIGITS[cent[1]]) / 100
        if not integer and not fraction:
            return value
        return int(result) if result == result.to_integral() else float(result)
    except (ValueError, KeyError):
        return value


def norm_date(value: str) -> str:
    original = str(value or "").strip()
    s = unicodedata.normalize("NFKC", original)
    roc_explicit = "民國" in s
    s = s.replace("中華民國", "").replace("民國", "").strip()
    # A complete date may include a time. Keep the date and discard only a
    # clearly separated time suffix; date ranges and month-only values stay raw.
    s = re.sub(r"(?<=[\d日])[T\s]+(?:(?:上午|下午|早上|晚上|AM|PM)\s*)?\d{1,2}(?::\d{2}(?::\d{2}(?:\.\d+)?)?|時\d{1,2}分(?:\d{1,2}秒)?)(?:\s*(?:AM|PM)|Z|[+-]\d{2}:?\d{2})?$", "", s, flags=re.I)
    # Taiwanese forms commonly include spaces around the era/year or date parts.
    s = re.sub(r"\s+", "", s)
    compact = re.fullmatch(r"(\d{3,4})(\d{2})(\d{2})", s)
    match = compact or re.fullmatch(r"(\d{1,4})[/\.\-年](\d{1,2})[/\.\-月](\d{1,2})日?", s)
    if not match:
        return original
    year, month, day = map(int, match.groups())
    # Leading-zero ROC years and an explicit ROC era retain their calendar meaning.
    if roc_explicit or len(match.group(1)) <= 3 or year < 1000:
        year += 1911
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return original


def first_present(fields: dict, keys: list[str], default=None):
    return next((fields[k] for k in keys if fields.get(k) is not None and fields.get(k) != ""), default)


def normalize_payment(status=None, method=None) -> dict:
    """Keep the observed words; normalize only explicit settlement evidence."""
    def text(value):
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()
    state, way = text(status), text(method)
    unpaid = {"未付", "未付款", "尚未付款", "未繳", "未繳款", "未結清", "尚未結清", "待付款", "待繳款", "unpaid", "unsettled", "outstanding"}
    paid = {"已付", "已付款", "已繳", "已繳款", "已結清", "已繳清", "付款完成", "付款成功", "匯款成功", "扣款成功", "交易成功", "paid", "settled"}
    normalized = "unpaid" if state in unpaid else "paid" if state in paid else "unknown"
    bank = {"銀行", "銀行轉帳", "銀行扣款", "銀行匯款", "匯款", "轉帳", "自動扣繳", "電匯", "bank", "banktransfer", "wire"}
    normalized_method = "cash" if way in {"現金", "cash"} else "bank" if way in bank else "other" if way else "unknown"
    return {"raw_status": status, "raw_method": method, "status": normalized, "method": normalized_method}


def normalize_currency(value) -> str | None:
    s = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    aliases = {"新臺幣": "TWD", "新台幣": "TWD", "臺幣": "TWD", "台幣": "TWD", "NTD": "TWD", "NT$": "TWD",
               "美元": "USD", "US$": "USD", "日圓": "JPY", "日元": "JPY", "人民幣": "CNY", "RMB": "CNY", "歐元": "EUR"}
    return aliases.get(s, s if re.fullmatch(r"[A-Z]{3}", s) else None)
