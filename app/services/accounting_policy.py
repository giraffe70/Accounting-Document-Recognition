"""Company policy data. Defaults preserve the existing import posting convention."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json

from app import config
from app.services import importdecl

DEFAULT_POLICY = {
    "version": "default-v1",
    "roles": {
        "import_prepayment": {"name": "預付費用", "code": "1210"},
        "input_tax": {"name": "進項稅額", "code": "1290"},
        "fees": {"name": "其他費用", "code": "6230"},
        "bank": {"name": "銀行存款", "code": "1110"},
        "cash": {"name": "現金"}, "payable_goods": {"name": "應付帳款", "code": "2130"},
        "payable_expense": {"name": "應付費用"}, "insurance": {"name": "保險費"},
    },
    "general": {"select_specific_account": True, "split_fee_natures": True,
                "separate_observed_vat": True, "derive_unprinted_tax": False,
                "insurance_role": "insurance", "payroll_split_requires_evidence": True},
    "import": {"debit_roles": {"duty": "import_prepayment", "trade_fee": "import_prepayment",
               "commodity_tax": "import_prepayment", "business_tax": "input_tax", "fee": "fees"},
               "payment_sum_fields": ["duty", "trade_fee", "commodity_tax", "business_tax", "fee"],
               "unpaid_role": "payable_goods", "include_goods_values": False, "posting_currency": "TWD"},
    "payment_roles": {"bank": "bank", "cash": "cash", "unpaid_goods": "payable_goods", "unpaid_expense": "payable_expense"},
}


def _merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, value in override.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else deepcopy(value)
    return out


def _validate(policy: dict) -> None:
    if not isinstance(policy.get("version"), str) or not policy["version"]:
        raise ValueError("policy version must be a nonempty string")
    for key in ("roles", "import", "general", "payment_roles"):
        if not isinstance(policy.get(key), dict):
            raise ValueError(key + " must be an object")
    roles = policy["roles"]
    if any(not isinstance(v, dict) or not isinstance(v.get("name"), str) or not isinstance(v.get("code", ""), str) for v in roles.values()):
        raise ValueError("roles require name and optional string code")
    imported = policy["import"]
    if not isinstance(imported.get("debit_roles"), dict) or not isinstance(imported.get("payment_sum_fields"), list):
        raise ValueError("invalid import rules")
    if any(not isinstance(v, str) for v in imported["payment_sum_fields"]):
        raise ValueError("payment_sum_fields require field names")
    if not isinstance(imported.get("include_goods_values"), bool) or not isinstance(imported.get("posting_currency"), str) or not imported["posting_currency"]:
        raise ValueError("import requires boolean include_goods_values and nonempty posting_currency")
    refs = [*imported["debit_roles"].values(), imported.get("unpaid_role"), *policy["payment_roles"].values(), policy["general"].get("insurance_role")]
    if any(not isinstance(role, str) or role not in roles for role in refs):
        raise ValueError("policy references unknown account role")
    if any(role not in roles for role in ("payable_expense", "payable_goods")):
        raise ValueError("missing default payable roles")


def load_policy(company: str, voucher: dict, chart: list) -> dict:
    path = config.DATA_DIR / "accounting_policies.json"
    warnings = []
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("policy root must be an object")
        except (ValueError, OSError) as exc:
            warnings.append("公司政策無法讀取，使用預設：" + str(exc)[:80])
            data = {}
    shared = data.get("_default", {})
    local = data.get(company, {}) if company else {}
    if not isinstance(shared, dict):
        shared = {}
        warnings.append("共用政策格式錯誤，使用預設")
    if not isinstance(local, dict):
        local = {}
        warnings.append("公司政策格式錯誤，使用共用／預設政策")
    selected = _merge(shared, local)
    policy = _merge(DEFAULT_POLICY, selected)
    try:
        _validate(policy)
    except ValueError as exc:
        warnings.append("公司政策契約錯誤，使用預設：" + str(exc))
        policy, selected = deepcopy(DEFAULT_POLICY), {}
    purpose = "import" if importdecl.is_import(voucher) else "general"
    if voucher.get("doc_type_key") == "declaration" and (voucher.get("fields") or {}).get("direction") not in ("進口", "出口"):
        warnings.append("報單進出口用途未確認")
    resolved = {}
    for role, preference in policy["roles"].items():
        matches = [a for a in chart if a.get("code") == preference.get("code") and a.get("name") == preference.get("name")]
        if not matches:
            matches = [a for a in chart if a.get("name") == preference.get("name") and a.get("code")]
        # Explicit company mapping can refer to its own differently named account.
        if "code" in (selected.get("roles", {}).get(role) or {}):
            matches = [a for a in chart if a.get("code") == preference["code"]]
        resolved[role] = {"code": matches[0]["code"] if len(matches) == 1 else "", "name": matches[0]["name"] if len(matches) == 1 else preference.get("name", "")}
    material = json.dumps({"policy": policy, "company": company, "purpose": purpose, "account_roles": resolved}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {"policy_version": policy["version"], "policy_sha256": hashlib.sha256(material).hexdigest(),
            "company": company, "purpose": purpose, "rules": policy[purpose], "general": policy["general"],
            "account_roles": resolved, "payment_roles": policy["payment_roles"], "warnings": warnings}
