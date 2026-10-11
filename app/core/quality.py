"""Deterministic document quality signals for the review UI.

Quality states describe evidence completeness and review needs. They never
approve a voucher or change extracted values.
"""
from __future__ import annotations

from datetime import date, datetime
import math
import re
from typing import Any

from app.core import doc_types
from app.core.normalization import norm_amount, norm_date


_DERIVED_DEFAULTS = {"currency", "direction"}
_EMPTY_VALUES = (None, "")


def _has_content(value: Any, *, key: str = "") -> bool:
    """Return whether a source field contains meaningful extracted content."""
    if key in _DERIVED_DEFAULTS or value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_content(v, key=str(k)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_content(v) for v in value)
    # Numeric zero is observed data, not an empty/default value.
    return isinstance(value, (int, float)) and not isinstance(value, bool) or bool(value)


def _issue(code: str, category: str, severity: str, path: str, message: str) -> dict:
    return {"code": code, "category": category, "severity": severity,
            "path": path, "message": message}


def _warning_issue(warning: Any) -> dict | None:
    if isinstance(warning, dict):
        return {
            "code": str(warning.get("code") or "source_anomaly"),
            "category": str(warning.get("category") or "source"),
            "severity": str(warning.get("severity") or "advisory"),
            "path": str(warning.get("path") or ""),
            "message": str(warning.get("message") or warning.get("detail") or warning),
        }
    text = str(warning or "").strip()
    if not text:
        return None
    if "大寫" in text and ("衝突" in text or "不一致" in text):
        return _issue("amount_conflict", "amount", "review", "fields.amount_words", text)
    if "金額兜不攏" in text or "合計" in text and "不一致" in text:
        return _issue("amount_conflict", "amount", "review", "total_amount", text)
    if "金額無法解析" in text or "數字無法解析" in text:
        path = text.split(" ", 1)[0]
        return _issue("invalid_number", "number", "review", path, text)
    if "日期格式無法解析" in text or "日期無效" in text:
        return _issue("date_parse", "date", "review", "date", text)
    if "補讀候選缺乏獨立證據" in text or "多個有效 QR" in text:
        return _issue("evidence_conflict", "evidence", "review", "", text)
    if "欄位語意" in text or "OCR 原圖與增強" in text or "QR 原圖與增強" in text or "幣別證據衝突" in text:
        return _issue("evidence_conflict", "evidence", "review", "", text)
    if "文字區域可能模糊" in text:
        return _issue("image_blur", "image", "review", "", text)
    if "超過 5 年" in text or "原始傳票借貸不平衡" in text:
        return _issue("source_anomaly", "source", "advisory", "", text)
    if "舊收據稅額" in text:
        return _issue("source_anomaly", "source", "advisory", "fields.tax_amount", text)
    return _issue("source_anomaly", "source", "advisory", "", text)


def _valid_date(value: Any) -> bool:
    if not isinstance(value, (str, date, datetime)):
        return False
    if isinstance(value, datetime):
        return True
    if isinstance(value, date):
        return True
    raw = str(value).strip()
    try:
        date.fromisoformat(raw)
        return True
    except ValueError:
        pass
    normalized = norm_date(raw)
    if normalized == raw:
        return False
    try:
        date.fromisoformat(normalized)
        return True
    except ValueError:
        return False


def _has_value(fields: dict, aliases: list[str]) -> bool:
    return any(key in fields and fields[key] not in _EMPTY_VALUES for key in aliases)


def _matches_amount_conflict(fields: dict) -> bool:
    words = fields.get("amount_words")
    total = fields.get("total_amount")
    if words in _EMPTY_VALUES or total in _EMPTY_VALUES:
        return False
    parsed = norm_amount(words)
    if isinstance(parsed, bool) or not isinstance(parsed, (int, float)) or not math.isfinite(parsed):
        return False
    parsed_total = norm_amount(total)
    if isinstance(parsed_total, bool) or not isinstance(parsed_total, (int, float)) or not math.isfinite(parsed_total):
        return False
    return not math.isclose(float(parsed), float(parsed_total), abs_tol=0.01)


def _valid_entry_amount(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    for key in ("amount", "debit", "credit"):
        value = row.get(key)
        if value in _EMPTY_VALUES:
            continue
        parsed = norm_amount(value)
        if (not isinstance(parsed, bool) and isinstance(parsed, (int, float))
                and math.isfinite(parsed)):
            return True
    return False


def _confirmed_blank(voucher: dict) -> bool:
    origin = voucher.get("origin") if isinstance(voucher.get("origin"), dict) else {}
    layout = origin.get("layout") if isinstance(origin.get("layout"), dict) else {}
    if layout.get("content_kind") == "blank":
        return True
    quality = voucher.get("quality") if isinstance(voucher.get("quality"), dict) else {}
    content_kind = voucher.get("content_kind") or quality.get("content_kind")
    pixel_evidence = (voucher.get("pixel_evidence") or voucher.get("blank_evidence")
                      or origin.get("pixel_evidence") or origin.get("blank_evidence"))
    return content_kind == "blank" and bool(pixel_evidence)


def evaluate_quality(voucher: dict) -> dict:
    """Evaluate completeness and review signals without mutating ``voucher``."""
    voucher = voucher if isinstance(voucher, dict) else {}
    fields = voucher.get("fields") if isinstance(voucher.get("fields"), dict) else {}
    items = voucher.get("items") if isinstance(voucher.get("items"), list) else []
    entries = voucher.get("entries") if isinstance(voucher.get("entries"), list) else []
    type_key = voucher.get("doc_type_key") or voucher.get("type")
    known_type = type_key in doc_types.DOC_TYPES
    spec = doc_types.DOC_TYPES.get(type_key, {}) if known_type else {}
    is_supporting = bool(spec.get("supporting"))
    is_other = type_key == "other"

    has_content = (_has_content(fields) or any(_has_content(row) for row in items)
                   or any(_has_content(row) for row in entries))
    content_kind = "unknown" if not known_type or is_other else "supporting" if is_supporting else "document"
    issues: list[dict] = []
    if (voucher.get("origin") or {}).get("layout_fallback"):
        issues.append(_issue("layout_fallback", "layout", "review", "origin", "單張回退擷取，請人工核對完整單據範圍"))
    if not has_content:
        issues.append(_issue("empty_content", "content", "review", "fields", "尚無可評估的單據內容"))
    if not known_type or is_other:
        issues.append(_issue("unknown_type", "classification", "review", "doc_type_key", "單據類型未知，請人工確認"))

    # Box diagnostics are already normalized by their producer when possible.
    box_issues = voucher.get("box_issues") or []
    if isinstance(box_issues, (str, dict)):
        box_issues = [box_issues]
    if isinstance(box_issues, list):
        for warning in box_issues:
            parsed = _warning_issue(warning)
            if parsed:
                parsed["code"] = "location_issue"
                parsed["category"] = "location"
                parsed["severity"] = "review"
                parsed.setdefault("path", "boxes")
                if not parsed["path"]:
                    parsed["path"] = "boxes"
                issues.append(parsed)

    warnings = voucher.get("validation_warnings") or []
    if isinstance(warnings, (str, dict)):
        warnings = [warnings]
    if isinstance(warnings, list):
        issues.extend(issue for issue in (_warning_issue(w) for w in warnings) if issue)

    if has_content and _matches_amount_conflict(fields):
        issues.append(_issue("amount_conflict", "amount", "review", "fields.amount_words",
                             "中文大寫與數字金額不一致，請人工核對"))

    common = spec.get("common_fields") or {}
    if has_content and not is_supporting and not is_other:
        total_aliases = common.get("total_amount") or []
        if total_aliases and voucher.get("total_amount") is None:
            issues.append(_issue("missing_total", "completeness", "review", "total_amount", "缺少有效總金額"))
        if spec.get("has_entries") and not any(_valid_entry_amount(row) for row in entries):
            issues.append(_issue("missing_entries", "completeness", "review", "entries", "缺少有效分錄明細"))
        id_aliases = common.get("voucher_id") or []
        if id_aliases and not (_has_content(voucher.get("voucher_id")) or _has_value(fields, id_aliases)):
            issues.append(_issue("missing_identity", "completeness", "review", "voucher_id", "缺少單據識別資訊"))
        date_aliases = common.get("date") or []
        date_value = voucher.get("date") or next((fields.get(k) for k in date_aliases if fields.get(k) not in _EMPTY_VALUES), None)
        if date_aliases and not date_value:
            issues.append(_issue("missing_date", "completeness", "review", "date", "缺少單據日期"))
        elif date_value and not _valid_date(date_value):
            if not any(i["code"] == "date_parse" for i in issues):
                issues.append(_issue("date_parse", "date", "review", "date", "日期格式或日期值無法解析"))
        if date_value and _valid_date(date_value):
            try:
                parsed_date = date.fromisoformat(norm_date(date_value)) if not isinstance(date_value, date) else date_value
                if (date.today() - parsed_date).days > 5 * 366:
                    issues.append(_issue("historical_date", "source", "advisory", "date", "單據日期距今超過五年"))
            except (TypeError, ValueError):
                pass

    if _confirmed_blank(voucher):
        quality = voucher.get("quality") if isinstance(voucher.get("quality"), dict) else {}
        state = quality.get("state") or "usable"
        content_kind = "unknown"
        issues = []
    elif not has_content:
        quality = voucher.get("quality") if isinstance(voucher.get("quality"), dict) else {}
        prepared = quality.get("state") == "unassessed" and not fields and not items and not entries
        state = "unassessed" if prepared else "insufficient"
    elif any(i["code"] in {"missing_total", "missing_entries"} for i in issues):
        state = "insufficient"
    elif any(i["severity"] == "review" for i in issues):
        state = "needs_review"
    else:
        state = "usable"
    return {"version": 1, "state": state, "content_kind": content_kind, "issues": issues}
