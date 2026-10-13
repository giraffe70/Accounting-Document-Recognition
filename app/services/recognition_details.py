"""Reviewed recognition rows, independent of posting drafts and model accounting."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from uuid import NAMESPACE_URL, uuid5

from app.core import doc_types

VERSION = 1
SOURCE_FIELDS = frozenset({"source_entry_id", "source_direction", "original_account_code", "original_account_name"})


def ensure(data, record_id=None):
    """Initialize once; an intentionally empty reviewed table stays empty."""
    if data.get("details_version") == VERSION:
        return data
    if doc_types.get(data.get("doc_type_key", "")).get("has_entries") and not data.get("items"):
        rows = []
        for index, entry in enumerate(data.get("entries") or []):
            if entry.get("entry_origin") == "generated":
                continue
            direction = entry.get("source_direction") or ""
            if not direction:
                debit = bool(entry.get("debit_account") or entry.get("debit_account_name"))
                credit = bool(entry.get("credit_account") or entry.get("credit_account_name"))
                if debit != credit:
                    direction = "debit" if debit else "credit"
                else:
                    paths = entry.get("evidence_paths") or []
                    directions = {side for side in ("debit", "credit") if any(p.endswith("." + side) for p in paths)}
                    if len(directions) == 1:
                        direction = directions.pop()
            code = entry.get(direction + "_account") or entry.get("raw_account_code") or ""
            name = entry.get(direction + "_account_name") or entry.get("raw_account_name") or ""
            entry_id = entry.get("entry_id") or f"{record_id}:{index}"
            rows.append({"item_id": str(uuid5(NAMESPACE_URL, "pdfscan-detail:" + entry_id)),
                         "description": entry.get("description") or "", "quantity": None, "unit_price": None,
                         "amount": entry.get("amount"), "account_code": code, "account_name": name,
                         "account_source": "observed" if code or name else "", "remember_mapping": False,
                         "source_entry_id": entry_id, "source_direction": direction,
                         "original_account_code": code, "original_account_name": name})
        data["items"] = rows
    data["details_version"] = VERSION
    return data


def protect_sources(data, previous):
    old = {row.get("item_id"): row for row in previous.get("items") or []}
    for row in data.get("items") or []:
        original = dict(old.get(row.get("item_id"), {}))
        for key in SOURCE_FIELDS:
            row.pop(key, None)
            if key in original:
                row[key] = original[key]
    data["details_version"] = VERSION


def observed_accounts(data):
    return {row["item_id"]: (row.get("original_account_code", ""), row.get("original_account_name", ""))
            for row in data.get("items") or [] if row.get("source_entry_id")}


def content(data):
    """Only values visible on the report participate in recognition approval."""
    result = {key: deepcopy(data.get(key)) for key in ("doc_type_key", "voucher_id", "date", "fields", "buyer", "seller",
              "subtotal", "tax_amount", "total_amount", "currency", "note")}
    spec = doc_types.get(data.get("doc_type_key", ""))
    keys = {"account_code", "account_name", "description", "quantity", "unit_price", "amount"}
    keys.update(field["key"] for field in spec.get("item_fields") or [])
    result["items"] = [{key: row.get(key) if row.get(key) != "" else None for key in sorted(keys)}
                       for row in data.get("items") or []]
    return result


def account_warnings(data, chart):
    names = {str(row["code"]): row["name"] for row in chart}
    warnings = []
    for index, row in enumerate(data.get("items") or [], 1):
        code, name = str(row.get("account_code") or "").strip(), str(row.get("account_name") or "").strip()
        if not code or not name:
            warnings.append(f"第 {index} 列：科目待指定" if not code and not name else f"第 {index} 列：科目代號或名稱未完整")
        elif code not in names:
            warnings.append(f"第 {index} 列：科目 {code} 未列於公司科目表")
        elif name != names[code]:
            warnings.append(f"第 {index} 列：科目代號與名稱不符")
    return warnings


def report_warnings(data, chart):
    warnings = account_warnings(data, chart)
    if doc_types.get(data.get("doc_type_key", "")).get("has_entries"):
        for side, label in (("debit", "借方"), ("credit", "貸方")):
            rows = [row for row in data.get("items") or [] if row.get("source_direction") == side]
            printed = (data.get("fields") or {}).get(side + "_total")
            if not rows or printed in (None, ""):
                continue
            try:
                amounts = [Decimal(str(row.get("amount"))) for row in rows]
                total = Decimal(str(printed))
                if not total.is_finite() or any(not value.is_finite() for value in amounts):
                    continue
            except (InvalidOperation, TypeError, ValueError):
                continue
            if abs(sum(amounts) - total) >= Decimal("0.01"):
                warnings.append(f"明細{label}金額與原稿{label}合計不符，請對照原圖核對")
    return warnings
