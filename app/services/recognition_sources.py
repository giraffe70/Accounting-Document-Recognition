"""Original voucher row identity, observed totals and recognition comparisons."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
import re
from uuid import UUID, uuid4

from app.core import doc_types
from app.core.normalization import norm_amount
from app.services.item_account_mapping import strip_accounting

ENTRY_META = {"entry_id", "entry_origin", "source_entry_index"}


class ReviewConflict(ValueError):
    def __init__(self, detail, latest=None):
        super().__init__(detail)
        self.latest = latest


def normalize_entries(rows, previous=None, origin="manual", *, split_legacy=False):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("分錄格式不合法")
    old_rows = {row.get("entry_id"): row for row in previous or [] if row.get("entry_id")}
    result, used = [], set()
    for index, raw in enumerate(rows):
        row = deepcopy(raw)
        identity = row.get("entry_id") or str(uuid4())
        try:
            identity = str(UUID(identity))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("分錄 ID 格式不合法") from exc
        if identity in used:
            raise ValueError("分錄 ID 重複")
        used.add(identity)
        old = old_rows.get(identity)
        row["entry_id"] = identity
        row["entry_origin"] = (old.get("entry_origin") or "manual") if old else origin
        row["source_entry_index"] = old.get("source_entry_index") if old else index if origin == "observed" else None
        if not old and origin == "manual":
            row["evidence_paths"], row["amount_source"] = [], None
        elif old:
            row["evidence_paths"] = deepcopy(old.get("evidence_paths") or [])
            row["amount_source"] = old.get("amount_source")
        for key in ("description", "dept", "offset_code", "debit_account", "debit_account_name", "credit_account", "credit_account_name"):
            row[key] = str(row.get(key) or "").strip()
        # Incomplete observed rows still expose their original account to the editor.
        if not any(row[key] for key in ("debit_account", "credit_account", "debit_account_name", "credit_account_name")):
            row["raw_account_code"] = str(row.get("raw_account_code") or "")
            row["raw_account_name"] = str(row.get("raw_account_name") or "")
        amount = row.get("amount")
        parsed = norm_amount(amount)
        if isinstance(parsed, (float, int)) and not isinstance(parsed, bool) and math.isfinite(parsed):
            row["amount"] = parsed
            row.pop("raw_amount", None)
        else:
            row["amount"] = None
            if amount not in (None, ""):
                row["raw_amount"] = str(amount)
        has_debit = bool(row["debit_account"] or row["debit_account_name"])
        has_credit = bool(row["credit_account"] or row["credit_account_name"])
        if split_legacy and has_debit and has_credit:
            debit, credit = deepcopy(row), deepcopy(row)
            debit["credit_account"] = debit["credit_account_name"] = ""
            credit["debit_account"] = credit["debit_account_name"] = ""
            credit["entry_id"] = str(uuid4())
            result.extend((debit, credit))
        else:
            result.append(row)
    return result


def checks(data):
    debit, credit, issues = Decimal(0), Decimal(0), []
    rows = data.get("entries") or []
    for index, row in enumerate(rows):
        prefix = f"第 {index + 1} 列"
        d = bool(row.get("debit_account") or row.get("debit_account_name"))
        c = bool(row.get("credit_account") or row.get("credit_account_name"))
        if d == c:
            issues.append(f"{prefix}：請指定單一借方或貸方")
        elif not row.get(("debit" if d else "credit") + "_account") or not row.get(("debit" if d else "credit") + "_account_name"):
            issues.append(f"{prefix}：科目代號與名稱未完整")
        marker = re.sub(r"[\s/／\\。．.！!－—_-]+", "", str(row.get("description") or ""))
        if marker in ("以下空白", "本頁以下空白", "以下無資料", "以下無明細"):
            issues.append(f"{prefix}：版面空白標記，請刪除錯誤列")
        try:
            if isinstance(row.get("amount"), bool):
                raise InvalidOperation
            amount = Decimal(str(row.get("amount")))
            if not amount.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError):
            issues.append(f"{prefix}：金額未填或格式不合法")
            continue
        if d and not c:
            debit += amount
        elif c and not d:
            credit += amount
    difference = debit - credit
    complete = bool(rows) and not issues
    balanced = complete and abs(difference) < Decimal("0.01")
    if rows and abs(difference) >= Decimal("0.01"):
        issues.append("借貸未平衡，請核對來源並補齊分錄")
    return {"debit_total": float(debit), "credit_total": float(credit), "difference": float(difference),
            "complete": complete, "balance_check": balanced, "issues": issues}


def recompute(data):
    summary = checks(data)
    for key in ("debit_total", "credit_total", "balance_check"):
        data[key] = summary[key]
    return summary


def source_payload(data):
    result = strip_accounting(data)
    for key in ("entries", "debit_total", "credit_total", "balance_check", "classification_meta",
                "field_evidence", "validation_warnings", "quality", "recognition_diagnostics", "boxes", "box_issues"):
        result.pop(key, None)
    return result


def alignment_payload(data):
    result = strip_accounting(data)
    spec = doc_types.get(result.get("doc_type_key", ""))
    result.pop("debit_total", None)
    result.pop("credit_total", None)
    result.pop("balance_check", None)
    result.pop("classification_meta", None)
    observed = [row for row in result.get("entries", []) if row.get("entry_origin") == "observed" or
                (not row.get("entry_origin") and spec.get("has_entries"))]
    result["entries"] = [{k: v for k, v in row.items() if k not in ENTRY_META} for row in observed]
    result["field_evidence"] = {path: records for path, records in result.get("field_evidence", {}).items()
                                if path != "entries" and not path.startswith("entries[")}
    return result
