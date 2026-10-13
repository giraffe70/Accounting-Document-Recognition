"""Company-scoped item suggestions and explicit, transactional rule publication."""
from copy import deepcopy
import json
import re
from uuid import UUID, uuid4

from sqlmodel import select
from app.db.models import ItemAccountMapping, MappingHistory, UserAccount, VoucherRecord, utc_now

ACCOUNT_FIELDS = frozenset({"item_id", "account_code", "account_name", "mapping_id", "mapping_version",
    "account_source", "account_needs_review", "remember_mapping", "account_company_id", "account_item_key",
    "account_updated_by", "account_updated_by_name", "account_updated_at"})
from app.services.recognition_details import SOURCE_FIELDS
ACCOUNT_FIELDS = ACCOUNT_FIELDS | SOURCE_FIELDS


class MappingConflict(ValueError):
    def __init__(self, detail, conflicts=None, *, latest=None):
        super().__init__(detail)
        self.conflicts = conflicts or []
        self.latest = latest


def item_key(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def defaults(row):
    row = dict(row)
    if not row.get("item_id"):
        row["item_id"] = str(uuid4())
    row.setdefault("account_code", "")
    row.setdefault("account_name", "")
    row.setdefault("mapping_id", None)
    row.setdefault("mapping_version", None)
    row.setdefault("account_source", "manual" if row["account_code"] or row["account_name"] else "")
    row.setdefault("account_needs_review", False)
    row.setdefault("remember_mapping", False)
    return row


def rules_for(session, company_id, names):
    keys = {item_key(name) for name in names} - {""}
    if not company_id or not keys:
        return {}
    # One indexed lookup per batch, never a lookup against another company.
    return {rule.item_name_key: rule for rule in session.exec(select(ItemAccountMapping).where(
        ItemAccountMapping.company_id == company_id, ItemAccountMapping.item_name_key.in_(keys))).all()}


def suggestion(rule):
    if not rule or not rule.is_active or not rule.account_code or not rule.account_name:
        return None
    return {"mapping_id": rule.id, "mapping_version": rule.version,
            "account_code": rule.account_code, "account_name": rule.account_name}


def lookup(session, company_id, items):
    rules = rules_for(session, company_id, [row.get("description") for row in items])
    return [{"item_id": row.get("item_id"), "item_name_key": item_key(row.get("description")),
             "match": suggestion(rules.get(item_key(row.get("description")))),
             "expected_version": rules[item_key(row.get("description"))].version
                if item_key(row.get("description")) in rules else 0} for row in items]


def strip_accounting(data):
    """Keep application-added fields out of extraction alignment and classification."""
    result = deepcopy(data)
    result.pop("details_version", None)
    if "items" in result:
        result["items"] = [{k: v for k, v in row.items() if k not in ACCOUNT_FIELDS}
                           for row in result.get("items", [])]
    if "field_evidence" in result:
        result["field_evidence"] = {path: records for path, records in result.get("field_evidence", {}).items()
            if not (path.startswith("items[") and path.rsplit(".", 1)[-1] in ACCOUNT_FIELDS)}
    return result


def remap_evidence(previous, rows):
    old = {row.get("item_id"): index for index, row in enumerate(previous.get("items") or [])}
    new = {index: old.get(row.get("item_id")) for index, row in enumerate(rows)}
    evidence = {path: deepcopy(records) for path, records in (previous.get("field_evidence") or {}).items()
                if not path.startswith("items[")}
    for index, old_index in new.items():
        if old_index is not None:
            prefix = f"items[{old_index}]."
            for path, records in (previous.get("field_evidence") or {}).items():
                if path.startswith(prefix):
                    evidence[f"items[{index}]." + path[len(prefix):]] = deepcopy(records)
    return evidence


def prepare_items(session, data, company_id, *, previous=None, previous_company_id=None,
                  model_result=False, reviewed=None, rec_id=None, observed_accounts=None):
    """Derive provenance on the server. The browser may only acknowledge a new context."""
    previous = previous or {}
    old_rows = {row.get("item_id"): row for row in previous.get("items", []) if row.get("item_id")}
    rows = []
    reserved = {row.get("item_id") for row in data.get("items") or [] if row.get("item_id")}
    for raw in data.get("items") or []:
        raw = dict(raw)
        if not raw.get("item_id") and not model_result:
            candidates = [old for old in old_rows.values() if old["item_id"] not in reserved and
                          item_key(old.get("description")) == item_key(raw.get("description"))]
            if len(candidates) == 1:
                # Compatibility for an old client: match only an unambiguous item, never its array position.
                raw = {**{k: v for k, v in candidates[0].items() if k in ACCOUNT_FIELDS}, **raw,
                       "item_id": candidates[0]["item_id"]}
        row = defaults(raw)
        reserved.add(row["item_id"])
        rows.append(row)
    rules = rules_for(session, company_id, [row.get("description") for row in rows])
    seen = set()
    reviewed = reviewed or {}
    observed_accounts = observed_accounts or {}
    for row in rows:
        try:
            row["item_id"] = str(UUID(row["item_id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("明細 ID 格式不合法") from exc
        identity = row["item_id"]
        if identity in seen:
            raise ValueError("明細 ID 重複")
        seen.add(identity)
        old = old_rows.get(identity)
        if rec_id and not old:
            candidates = session.exec(select(VoucherRecord.current_json).where(
                VoucherRecord.id != rec_id, VoucherRecord.current_json.contains(identity))).all()
            for candidate in candidates:
                try:
                    foreign_items = json.loads(candidate).get("items") or []
                except (ValueError, TypeError, AttributeError):
                    continue
                if any(isinstance(item, dict) and item.get("item_id") == identity for item in foreign_items):
                    raise ValueError("明細 ID 屬於其他單據")
        key = item_key(row.get("description"))
        match = suggestion(rules.get(key))
        pair = (str(row.get("account_code") or "").strip(), str(row.get("account_name") or "").strip())
        old_pair = (old.get("account_code", ""), old.get("account_name", "")) if old else ("", "")
        changed_context = bool(old and (previous_company_id != company_id or
                                       item_key(old.get("description")) != key))
        # Preserve protected model rows, but never accept model-generated accounting attributes.
        if model_result and not old:
            pair = observed_accounts.get(identity, ("", ""))
        if old and pair == old_pair:
            source = old.get("account_source", "")
            needs_review = bool(old.get("account_needs_review"))
            mapping_id, mapping_version = old.get("mapping_id"), old.get("mapping_version")
        else:
            source, needs_review, mapping_id, mapping_version = ("manual" if any(pair) else ""), False, None, None
        if not old and any(pair) and pair == observed_accounts.get(identity):
            source = "observed"
        if changed_context:
            if old.get("account_source") == "mapping" and pair == old_pair:
                pair = ("", "")
                source, mapping_id, mapping_version = "", None, None
            elif any(pair):
                needs_review = True
            mapping_id, mapping_version = None, None
        if not old and source != "observed" and match and pair == (match["account_code"], match["account_name"]):
            source = "mapping"
            mapping_id, mapping_version = match["mapping_id"], match["mapping_version"]
        # An unchanged saved snapshot is never refreshed when a rule has changed.
        fill = not old or changed_context or model_result
        if fill and not any(pair) and match:
            pair = (match["account_code"], match["account_name"])
            source, needs_review = "mapping", False
            mapping_id, mapping_version = match["mapping_id"], match["mapping_version"]
        elif changed_context and match and pair == (match["account_code"], match["account_name"]):
            source, needs_review = "mapping", False
            mapping_id, mapping_version = match["mapping_id"], match["mapping_version"]
        acknowledgement = reviewed.get(identity)
        if acknowledgement == {"company_id": company_id, "item_name_key": key,
                                "account_code": pair[0], "account_name": pair[1]}:
            needs_review = False
        row.update(account_code=pair[0], account_name=pair[1], account_source=source,
                   account_needs_review=needs_review, mapping_id=mapping_id, mapping_version=mapping_version,
                   remember_mapping=row.get("remember_mapping") is not False,
                   account_company_id=company_id, account_item_key=key)
        for field in ("account_updated_by", "account_updated_by_name", "account_updated_at"):
            if old and field in old:
                row[field] = old[field]
            else:
                row.pop(field, None)
    data["items"] = rows
    return rows


def audit_items(session, data, previous, actor):
    old_rows = {r.get("item_id"): r for r in previous.get("items", [])}
    changed = [r for r in data.get("items", []) if any(r.get(key) != old_rows.get(r["item_id"], {}).get(key)
        for key in ("account_code", "account_name", "account_company_id", "account_needs_review"))]
    if not changed:
        return
    user = session.exec(select(UserAccount).where(UserAccount.username == actor.get("username", ""))).first()
    if not user:
        raise ValueError("無法識別登入人員")
    for row in changed:
        if row.get("account_code") or row.get("account_name"):
            row.update(account_updated_by=user.id, account_updated_by_name=user.name or user.username,
                       account_updated_at=utc_now().isoformat() + "Z")


def publish(session, rec, data, actor, choices=None, *, nonblocking=False):
    """Validate every choice first, then publish all rule changes in the caller's transaction."""
    choices = choices or {}
    groups, skipped = {}, []
    for row in data.get("items", []):
        if not row.get("remember_mapping", False):
            continue
        key = item_key(row.get("description"))
        reason = ("未指定公司" if not rec.company_id else "品名空白" if not key else
                  "科目代號與名稱未完整" if not row.get("account_code") or not row.get("account_name") else
                  "科目待核對" if row.get("account_needs_review") else None)
        if reason:
            skipped.append({"item_id": row["item_id"], "reason": reason})
        else:
            groups.setdefault(key, []).append(row)
    rules = rules_for(session, rec.company_id, list(groups))
    conflicts, pending = [], []
    for key, rows in groups.items():
        rule = rules.get(key)
        choice = choices.get(key) or {}
        pairs = {(row["account_code"], row["account_name"]) for row in rows}
        row = next((r for r in rows if r["item_id"] == choice.get("item_id")), rows[0])
        old = {"account_code": rule.account_code, "account_name": rule.account_name,
               "is_active": rule.is_active} if rule else None
        current_version = rule.version if rule else 0
        unchanged = rule and rule.is_active and pairs == {(rule.account_code, rule.account_name)}
        if unchanged:
            for r in rows:
                r.update(mapping_id=rule.id, mapping_version=rule.version)
            continue
        if choice.get("action") == "local":
            for r in rows:
                r["remember_mapping"] = False
            continue
        valid = (choice.get("action") == "update" and choice.get("expected_version") == current_version
                 and (len(pairs) == 1 or choice.get("item_id") in {r["item_id"] for r in rows}))
        if rule or len(pairs) > 1:
            if not valid:
                conflicts.append({"item_name_key": key, "item_name": row.get("description", ""),
                    "expected_version": current_version, "old": old,
                    "rows": [{"item_id": r["item_id"], "account_code": r["account_code"],
                              "account_name": r["account_name"]} for r in rows]})
                continue
        elif choice and not valid:
            conflicts.append({"item_name_key": key, "item_name": row.get("description", ""),
                              "expected_version": 0, "old": None, "rows": rows})
            continue
        pending.append((key, row, rows, rule))
    if conflicts:
        if nonblocking:
            local_choices = {**choices, **{row["item_name_key"]: {"action": "local"} for row in conflicts}}
            notices = [{"item_id": row["item_id"], "reason": "共用科目規則衝突，僅保存本張，未更新規則"}
                       for conflict in conflicts for row in conflict["rows"]]
            return notices + publish(session, rec, data, actor, local_choices)
        raise MappingConflict("請選擇只修改本張或更新共用規則；規則有變更時請重新選擇", conflicts)
    user = session.exec(select(UserAccount).where(UserAccount.username == actor.get("username", ""))).first()
    if pending and not user:
        raise ValueError("無法識別登入人員")
    for key, row, rows, rule in pending:
        before = rule.model_dump(mode="json") if rule else None
        now = utc_now()
        if not rule:
            rule = ItemAccountMapping(company_id=rec.company_id, item_name_key=key,
                created_by=user.id, created_by_name=user.name or user.username, created_at=now)
        else:
            rule.version += 1
        rule.account_code, rule.account_name = row["account_code"], row["account_name"]
        rule.item_name, rule.doc_type_key = row.get("description", ""), data.get("doc_type_key", "")
        rule.source_voucher_id, rule.source_item_id = rec.id, row["item_id"]
        rule.updated_by, rule.updated_by_name, rule.updated_at = user.id, user.name or user.username, now
        rule.is_active = True
        session.add(rule)
        session.flush()
        after = rule.model_dump(mode="json")
        session.add(MappingHistory(mapping_id=rule.id, version=rule.version,
            before_json=json.dumps(before, ensure_ascii=False), after_json=json.dumps(after, ensure_ascii=False),
            source_voucher_id=rec.id, source_item_id=row["item_id"], actor_id=user.id,
            actor_name=user.name or user.username, created_at=now))
        for r in rows:
            if (r["account_code"], r["account_name"]) == (rule.account_code, rule.account_name):
                r.update(mapping_id=rule.id, mapping_version=rule.version)
            else:
                r["remember_mapping"] = False
    return skipped
