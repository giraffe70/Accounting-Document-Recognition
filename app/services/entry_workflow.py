"""Editable accounting drafts, source identity, and independent posting approval."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from uuid import UUID, uuid4

from sqlmodel import select
from app.db.models import ReviewStatus, UserAccount, VoucherRecord, utc_now
from app.core import doc_types
from app import config
from app.core.normalization import norm_amount
from app.services.item_account_mapping import ACCOUNT_FIELDS, strip_accounting

ENTRY_META = {"entry_id", "entry_origin", "source_entry_index"}


class EntryConflict(ValueError):
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
        row["entry_origin"] = old.get("entry_origin", "manual") if old else origin
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
        marker = re.sub(r"[\s/／\\。．.！!－—_-]+", "", str(row.get("description") or ""))
        if marker in ("以下空白", "本頁以下空白", "以下無資料", "以下無明細"):
            issues.append(f"{prefix}：版面空白標記，請刪除錯誤列")
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


def source_payload(data, *, accounting=False):
    result = deepcopy(data if accounting else strip_accounting(data))
    for key in ("entries", "debit_total", "credit_total", "balance_check", "classification_meta",
                "field_evidence", "validation_warnings", "quality", "recognition_diagnostics", "boxes", "box_issues"):
        result.pop(key, None)
    if accounting:
        result["items"] = [{k: v for k, v in row.items() if k not in ACCOUNT_FIELDS or k in ("account_code", "account_name", "account_needs_review")}
                           for row in result.get("items", [])]
    return result


def source_fingerprint(rec, data=None):
    data = data if data is not None else json.loads(rec.current_json)
    source = {"company": getattr(rec, "company_id", None) or getattr(rec, "company", ""),
              "document": source_payload(data, accounting=True), "image_path": getattr(rec, "image_path", "")}
    if source["image_path"]:
        path = (config.UPLOAD_DIR / source["image_path"]).resolve()
        if path.is_relative_to(config.UPLOAD_DIR.resolve()) and path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as image_file:
                for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            source["image_sha256"] = digest.hexdigest()
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def user_identity(session, actor):
    if not actor:
        return None, "系統"
    user = session.exec(select(UserAccount).where(UserAccount.username == actor.get("username", ""))).first()
    if not user:
        raise ValueError("無法識別登入人員")
    return user.id, user.name or user.username


def event(rec, action, before, after, actor=None, reason=""):
    history = json.loads(getattr(rec, "entries_history", "[]") or "[]")
    history.append({"action": action, "at": utc_now().isoformat() + "Z", "actor_id": actor[0] if actor else None,
                    "actor_name": actor[1] if actor else "系統", "reason": reason,
                    "before": deepcopy(before), "after": deepcopy(after)})
    rec.entries_history = json.dumps(history, ensure_ascii=False)


def invalidate(rec, reason, actor=None, before=None, *, stale_proposal=True):
    rows = json.loads(rec.current_json).get("entries") or []
    old_status = getattr(rec, "entries_status", "none")
    rec.entries_status = "draft" if rows else "none"
    rec.entries_confirmed_at = rec.entries_confirmed_by = None
    rec.entries_confirmed_by_name = ""
    proposal = json.loads(getattr(rec, "entries_proposal_json", "{}") or "{}")
    if stale_proposal and proposal and proposal.get("status") == "pending":
        previous = deepcopy(proposal)
        proposal.update(status="stale", reason=reason)
        rec.entries_proposal_json = json.dumps(proposal, ensure_ascii=False)
        event(rec, "proposal_invalidated", previous, proposal, actor, reason)
    if old_status == "ready":
        event(rec, "confirmation_revoked", before if before is not None else rows, rows, actor, reason)


def is_ready(rec):
    data = json.loads(rec.current_json)
    return (rec.status == ReviewStatus.confirmed and getattr(rec, "entries_status", "none") == "ready"
            and bool(getattr(rec, "company_id", None)) and not doc_types.is_supporting(data.get("doc_type_key", ""))
            and checks(data)["balance_check"])


def ensure_ownership(session, rec_id, rows, previous):
    old_ids = {r.get("entry_id") for r in previous}
    for row in rows:
        if row["entry_id"] in old_ids:
            continue
        for raw in session.exec(select(VoucherRecord.current_json).where(
                VoucherRecord.id != rec_id, VoucherRecord.current_json.contains(row["entry_id"]))).all():
            try:
                foreign = json.loads(raw).get("entries") or []
            except (ValueError, AttributeError):
                continue
            if any(isinstance(r, dict) and r.get("entry_id") == row["entry_id"] for r in foreign):
                raise ValueError("分錄 ID 屬於其他單據")


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
