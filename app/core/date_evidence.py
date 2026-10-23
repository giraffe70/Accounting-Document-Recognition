"""OCR 日期證據：只擷取印出的民國日期與發票期別，不改寫模型輸出。

模型日期與 OCR 的唯一候選衝突時，由 `taxid.check_date` 標記待覆核。
候選以未選取的 `field_evidence["fields.date"]` 紀錄保存，供覆核與重算使用。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

SOURCE = "ocr_candidate"
KIND_DATE, KIND_PERIOD = "roc_date", "invoice_period"
_MONTH = "[0-9一二三四五六七八九十]{1,3}"
_FULL_DATE = re.compile(r"(?<![0-9])(民國\s*)?(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|(?![0-9]))")
_PERIOD = re.compile(rf"(?<![0-9])(民國\s*)?(\d{{2,3}})\s*年\s*({_MONTH})\s*[、,，及/\-~－—至]\s*({_MONTH})\s*月(?:份)?")
_CN = {c: i for i, c in enumerate("零一二三四五六七八九")}


def _month(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十") and len(text) == 2 and text[1] in _CN:
        return 10 + _CN[text[1]]
    if len(text) == 1 and text in _CN:
        return _CN[text]
    return None


def _roc_year(prefix, digits: str) -> int | None:
    # A bare two-digit year is too ambiguous; require the explicit era marker.
    if not prefix and len(digits) < 3:
        return None
    year = int(digits)
    return year if 1 <= year <= 300 else None


def extract(text: str, *, include_period: bool = False) -> list[dict]:
    """Return unique printed ROC dates (and optionally the invoice period)."""
    text = unicodedata.normalize("NFKC", text or "")
    dates, periods = {}, {}
    for match in _FULL_DATE.finditer(text):
        year = _roc_year(match[1], match[2])
        if year is None:
            continue
        try:
            iso = date(year + 1911, int(match[3]), int(match[4])).isoformat()
        except ValueError:
            continue
        dates.setdefault(iso, {"source": SOURCE, "kind": KIND_DATE, "value": iso,
                               "raw": re.sub(r"\s+", "", match[0]), "roc_year": year, "selected": False})
    if include_period:
        for match in _PERIOD.finditer(text):
            year, first, second = _roc_year(match[1], match[2]), _month(match[3]), _month(match[4])
            if year is None or first is None or second is None or not (1 <= first < second <= 12) or second != first + 1:
                continue
            key = (year, first, second)
            periods.setdefault(key, {"source": SOURCE, "kind": KIND_PERIOD, "value": f"{year}年{first}、{second}月",
                                     "raw": re.sub(r"\s+", "", match[0]), "roc_year": year,
                                     "year": year + 1911, "months": [first, second], "selected": False})
    # Several different dates (print date, due date, shipment date) cannot identify the issue date.
    return [*(list(dates.values()) if len(dates) == 1 else []), *(list(periods.values()) if len(periods) == 1 else [])]


def record(data: dict, ocr_text: str, type_key: str) -> list[dict]:
    """Attach unique OCR candidates to an extraction without changing selected values."""
    if type_key not in ("invoice", "receipt") or "date" not in (data.get("fields") or {}):
        return []
    found = extract(ocr_text, include_period=type_key == "invoice")
    records = data.setdefault("field_evidence", {}).setdefault("fields.date", [])
    for item in found:
        if not any(r.get("source") == SOURCE and r.get("kind") == item["kind"] and r.get("value") == item["value"] for r in records):
            records.append(item)
    if not records:
        data["field_evidence"].pop("fields.date")
    return found
