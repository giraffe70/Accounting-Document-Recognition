"""Evidence-based accounting proposals with company-scoped policy and examples."""
from __future__ import annotations
import json
import logging
import math
from decimal import Decimal
import requests
from pydantic import BaseModel, ConfigDict, Field
from app import config
from app.core import activity, doc_types, model_factory, prompts, settings_store, model_invocation
from app.core.normalization import norm_amount
from app.db.models import ReviewStatus
from app.models.schemas import JournalEntry, Voucher
from app.services import accounting_policy, accounts, review

logger = logging.getLogger("pdf_scan.classify")
EMBED_MODEL = "bge-m3"
MONETARY_FIELDS = {"subtotal", "tax_amount", "total_amount", "invoice_amount", "freight", "insurance", "add_fee", "deduct_fee",
 "total_fob_twd", "fob_value_twd", "total_cif_twd", "duty", "business_tax", "trade_fee", "commodity_tax", "total_tax", "customs_duty", "broker_fee", "edi_fee", "tax", "prepaid", "balance", "principal", "surcharge", "basic_fee", "usage_fee", "levy_fee", "debit_amount", "fee", "employer_share", "employee_share", "withholding_tax"}

class ProposedEntry(prompts.RawContract):
    description: str
    amount: str | int | float | None
    debit_account: str
    debit_account_name: str
    credit_account: str
    credit_account_name: str
    evidence_paths: list[str] = Field(default_factory=list)
    dept: str = ""
    offset_code: str = ""

class ProposedResult(prompts.RawContract):
    entries: list[ProposedEntry]
    warnings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


def _validate_output(obj):
    result = ProposedResult.model_validate(obj).model_dump()
    for entry in result["entries"]:
        if (entry["debit_account"] or entry["debit_account_name"]) and (entry["credit_account"] or entry["credit_account_name"]):
            raise ValueError("each entry must have at most one account side")
    return result


def _voucher_brief(v: Voucher) -> dict:
    # This compatibility name now returns the complete relevant document, not a lossy summary.
    result = v.model_dump(mode="json", include={"doc_type", "doc_type_key", "voucher_id", "date", "fields", "buyer", "seller", "items", "subtotal", "tax_amount", "total_amount", "currency", "note", "validation_warnings"})
    # Audit history stays stored; classification needs current evidence, not every past edit.
    result["field_evidence"] = {path: selected for path, records in v.field_evidence.items()
        if isinstance(records, list) and (selected := [r for r in records if isinstance(r, dict) and r.get("selected")])}
    return result


def _accounting_context(v: Voucher, policy: dict) -> dict:
    sources = {}
    def add(path, value):
        value = norm_amount(value)
        if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value):
            sources[path] = value
    for key, value in v.fields.items():
        if key in MONETARY_FIELDS:
            if key == "tax_amount" and doc_types.receipt_tax_ambiguous(v.doc_type_key, v.fields, v.field_evidence):
                continue  # Legacy receipt prompts used this key for withholding; require review.
            if policy["purpose"] == "import" and key not in {*policy["rules"]["debit_roles"], "total_tax", "total_amount"}:
                continue
            add("fields." + key, value)
    for key in ("subtotal", "tax_amount", "total_amount"):
        if policy["purpose"] == "import" and key != "tax_amount" and v.doc_type_key != "customs_tax":
            continue
        if v.fields.get(key) is None and getattr(v, key) is not None:
            add(key, getattr(v, key))
    for index, item in enumerate(v.items):
        if policy["purpose"] == "import" and v.doc_type_key == "import_declaration" and not policy["rules"].get("include_goods_values"):
            continue
        add(f"items[{index}].amount", item.amount)
    derived = {}
    if policy["purpose"] == "import":
        paths = ["fields." + key for key in policy["rules"]["payment_sum_fields"] if "fields." + key in sources]
        if paths:
            derived["derived.import_payment"] = {"amount": float(sum(Decimal(str(sources[p])) for p in paths)), "source_paths": paths, "operation": "sum"}
            printed = v.fields.get("total_tax") if v.doc_type_key == "import_declaration" else v.fields.get("total_amount")
            derived["derived.import_payment"]["verified_by_printed_total"] = isinstance(printed, (int, float)) and abs(printed-derived["derived.import_payment"]["amount"]) < .01
            sources["derived.import_payment"] = derived["derived.import_payment"]["amount"]
    return {"voucher": _voucher_brief(v), "amount_sources": sources, "derived_amounts": derived,
            "posting_currency": policy["rules"].get("posting_currency", v.currency)}


def _transaction_direction(v: Voucher, company: str) -> str | None:
    from app.services import taxid
    identities = [c for c in taxid.load_companies() if company and company in (c.get("name"), c.get("short"))]
    if len(identities) == 1:
        entity = identities[0]
        def matches(party):
            return bool(party.tax_id and entity.get("tax_id") and party.tax_id == entity["tax_id"]) or bool(party.name and party.name in (entity.get("name"), entity.get("short")))
        buyer, seller = matches(v.buyer), matches(v.seller)
        if buyer != seller:
            return "expense" if buyer else "income"
    if v.doc_type_key in ("customs_tax", "import_declaration", "broker_note", "tax_bill", "utility", "social_insurance", "purchase_order", "fda_import"):
        return "expense"
    return None


def _payment_assumptions(v: Voucher, policy: dict) -> list[str]:
    status = v.fields.get("payment_status")
    if status not in ("已付", "已付款", "已繳", "已繳款", "付款成功", "扣款成功", "交易成功", "未付", "未付款", "尚未付款", "未繳"):
        if policy.get("transaction_direction") != "expense":
            return ["付款／收款狀態未確認，請人工核對結算科目"]
        role = policy["rules"].get("unpaid_role", "payable_expense")
        return ["付款狀態未確認，暫以未付款候選處理：" + policy["account_roles"][role]["name"]]
    if status in ("已付", "已付款", "已繳", "已繳款", "付款成功", "扣款成功", "交易成功") and not v.fields.get("payment_method"):
        return ["付款方式未確認，現金或銀行待人工選擇"]
    if status in ("已付", "已付款", "已繳", "已繳款", "付款成功", "扣款成功", "交易成功") and v.fields.get("payment_method") not in ("現金", "cash", "銀行", "銀行轉帳", "銀行扣款", "匯款", "轉帳", "自動扣繳", "bank"):
        return ["付款方式缺少適用的科目角色，請人工核對"]
    return []


def _payment_role(v: Voucher, policy: dict) -> str | None:
    status, method = v.fields.get("payment_status"), v.fields.get("payment_method")
    if status in ("已付", "已付款", "已繳", "已繳款", "付款成功", "扣款成功", "交易成功"):
        if method in ("現金", "cash"):
            return policy["payment_roles"]["cash"]
        if method in ("銀行", "銀行轉帳", "銀行扣款", "匯款", "轉帳", "自動扣繳", "bank"):
            return policy["payment_roles"]["bank"]
        return None
    if policy["purpose"] == "import":
        return policy["rules"]["unpaid_role"]
    if policy.get("transaction_direction") != "expense":
        return None
    if v.doc_type_key == "purchase_order":
        return policy["payment_roles"]["unpaid_goods"]
    return policy["payment_roles"]["unpaid_expense"]


def _checked_entries(result: dict, context: dict, chart: list, policy: dict):
    warnings = list(result["warnings"])
    from collections import Counter
    counts = Counter(a.get("code") for a in chart if a.get("code"))
    by_code = {a["code"]: a for a in chart if a.get("code") and counts[a["code"]] == 1}
    entries = []
    for index, raw in enumerate(result["entries"]):
        row = dict(raw)
        amount = norm_amount(row["amount"])
        paths = row["evidence_paths"]
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount)):
            amount = None
        valid_paths = [p for p in paths if p in context["amount_sources"]]
        matched = amount is not None and any(abs(context["amount_sources"][p] - amount) < .01 for p in valid_paths)
        if not paths and amount is not None:
            matches = [p for p, value in context["amount_sources"].items() if abs(value - amount) < .01]
            if matches:
                valid_paths = matches
                matched = True
                warnings.append(f"分錄{index+1}未指定金額來源，依已知值匹配，需人工核對")
        if amount is None or not matched or len(valid_paths) != len(paths) and bool(paths):
            warnings.append(f"分錄{index+1}缺少有效金額證據，金額留空")
            amount = None
        row["amount"], row["evidence_paths"] = amount, valid_paths
        for side in ("debit", "credit"):
            code = row[side + "_account"]
            if code and code not in by_code:
                warnings.append(f"分錄{index+1}科目{code}未唯一對映當次科目表，代碼留空")
                row[side + "_account"] = ""
            elif code:
                row[side + "_account_name"] = by_code[code]["name"]
        tax_account = policy["account_roles"]["input_tax"]["code"]
        if tax_account and row["debit_account"] == tax_account and not any(p in ("tax_amount", "fields.tax_amount", "fields.business_tax", "fields.tax") for p in valid_paths):
            warnings.append(f"分錄{index+1}沒有明示營業稅來源，進項稅額科目留空")
            row["debit_account"] = ""
        if policy["purpose"] == "import" and not row["credit_account"]:
            roles = {policy["rules"]["debit_roles"][p.removeprefix("fields.")] for p in valid_paths if p.removeprefix("fields.") in policy["rules"]["debit_roles"]}
            if len(roles) == 1:
                expected = policy["account_roles"][roles.pop()]
                if row["debit_account"] != expected["code"]:
                    warnings.append(f"分錄{index+1}依進口政策對映科目，需人工核對")
                row["debit_account"], row["debit_account_name"] = expected["code"], expected["name"]
        if not row["debit_account"] and not row["credit_account"]:
            warnings.append(f"分錄{index+1}科目尚未對映")
        settlement = {r["code"] for key, r in policy["account_roles"].items() if key in ("bank", "cash", "payable_goods", "payable_expense") and r["code"]}
        is_settlement = row["credit_account"] in settlement or policy["purpose"] == "import" and bool(row["credit_account"] or row["credit_account_name"])
        if is_settlement and policy.get("transaction_direction") == "expense":
            role = policy.get("selected_payment_role")
            if role is None:
                warnings.append(f"分錄{index+1}付款科目缺少付款方式證據，需人工確認")
            else:
                expected = policy["account_roles"][role]
                if row["credit_account"] != expected["code"]:
                    warnings.append(f"分錄{index+1}依付款政策對映科目，需人工核對")
                row["credit_account"], row["credit_account_name"] = expected["code"], expected["name"]
                if not expected["code"]:
                    warnings.append(f"付款科目 {expected['name']} 未在當次科目表對映")
        entries.append(JournalEntry.model_validate(row))
    return entries, warnings


def classify(voucher: Voucher, company: str = "") -> Voucher:
    if doc_types.get(voucher.doc_type_key).get("has_entries"):
        original = voucher.classification_meta.get("original_checks")
        voucher.debit_total = sum(e.amount for e in voucher.entries if (e.debit_account or e.debit_account_name) and e.amount is not None)
        voucher.credit_total = sum(e.amount for e in voucher.entries if (e.credit_account or e.credit_account_name) and e.amount is not None)
        complete = bool(voucher.entries) and all(e.amount is not None and bool(e.debit_account or e.debit_account_name) != bool(e.credit_account or e.credit_account_name) for e in voucher.entries)
        voucher.balance_check = complete and abs(voucher.debit_total-voucher.credit_total) < .01
        voucher.classification_meta = {"status": "skipped" if voucher.entries else "candidate", "reason": "original_entries",
                                      "warnings": ["保留已擷取的原始傳票分錄，不另行產生或補平衡面"], "assumptions": []}
        if original:
            voucher.classification_meta["original_checks"] = original
        return voucher
    if doc_types.is_supporting(voucher.doc_type_key):
        voucher.entries = []
        voucher.debit_total = voucher.credit_total = None
        voucher.balance_check = False
        voucher.classification_meta = {"status": "skipped", "reason": "supporting", "warnings": ["佐證附頁不單獨產生分錄"], "assumptions": []}
        return voucher
    chart = accounts.load_accounts(company)
    policy = accounting_policy.load_policy(company, voucher.model_dump(), chart)
    policy["transaction_direction"] = _transaction_direction(voucher, company)
    if policy["transaction_direction"] is None:
        policy["warnings"].append("交易方向或公司身分未確認，請人工核對")
    context = _accounting_context(voucher, policy)
    if doc_types.receipt_tax_ambiguous(voucher.doc_type_key, voucher.fields, voucher.field_evidence):
        policy["warnings"].append("舊收據稅額語意未確認，請區分營業稅與扣繳稅額")
    if policy["purpose"] == "import" and not context["derived_amounts"].get("derived.import_payment", {}).get("verified_by_printed_total"):
        policy["warnings"].append("進口稅費分項合計尚未由印刷總額確認")
    assumptions = _payment_assumptions(voucher, policy)
    policy = {**policy, "payment_assumptions": assumptions, "selected_payment_role": _payment_role(voucher, policy)}
    examples = _retrieve_examples(voucher, company=company, chart=chart, policy=policy)
    examples += _seed_examples(company=company, voucher=voucher, chart=chart, policy=policy, k=3-len(examples))
    provider, model = settings_store.parse_setting(settings_store.get_settings()["classify"])
    chat = model_factory.get_chat_model(provider, model, temperature=0.0)
    with activity.stage("classify", "會計分類"):
        result = model_invocation.invoke_structured(chat, prompts.build_accounting_prompt(context, chart, policy, examples), _validate_output, task="accounting")
    entries, warnings = _checked_entries(result, context, chart, policy)
    warnings += policy["warnings"] + voucher.validation_warnings
    assumptions += result["assumptions"]
    if not chart:
        warnings.append("當次公司科目表為空，請人工設定")
    elif company and company not in accounts.companies_with_chart():
        warnings.append("公司未設定專屬科目表，使用通用表，請人工確認")
    voucher.entries = entries
    voucher.debit_total = sum(e.amount for e in entries if e.debit_account and e.amount is not None)
    voucher.credit_total = sum(e.amount for e in entries if e.credit_account and e.amount is not None)
    complete = bool(entries) and all(e.amount is not None and bool(e.debit_account) != bool(e.credit_account) for e in entries)
    voucher.balance_check = complete and abs(voucher.debit_total-voucher.credit_total) < .01
    if not voucher.balance_check:
        warnings.append("候選分錄未完整或借貸不平衡")
    voucher.classification_meta = {"status": "candidate" if warnings or assumptions else "proposed", "company": company,
        "policy_version": policy["policy_version"], "policy_sha256": policy["policy_sha256"], "purpose": policy["purpose"],
        "warnings": list(dict.fromkeys(warnings)), "assumptions": list(dict.fromkeys(assumptions)), "derived_amounts": context["derived_amounts"],
        "posting_currency": context["posting_currency"]}
    return voucher


def _example_valid(item: dict, voucher: Voucher, chart: list, policy: dict) -> bool:
    meta = item.get("classification_meta") or {}
    if item.get("doc_type_key") != voucher.doc_type_key or meta.get("policy_sha256") != policy["policy_sha256"] or meta.get("status") != "proposed" or item.get("validation_warnings"):
        return False
    entries = item.get("entries") or []
    codes = {a["code"] for a in chart if a.get("code")}
    names = {a["code"]: a["name"] for a in chart if a.get("code")}
    try:
        if not entries or any(e.get("amount") is None or bool(e.get("debit_account")) == bool(e.get("credit_account")) or any(e.get(side) and e[side] not in codes for side in ("debit_account", "credit_account")) for e in entries):
            return False
        if any(e.get(side + "_account") and e.get(side + "_account_name") != names[e[side + "_account"]] for e in entries for side in ("debit", "credit")):
            return False
        dr = sum(float(e["amount"]) for e in entries if e.get("debit_account"))
        cr = sum(float(e["amount"]) for e in entries if e.get("credit_account"))
        validated, errors = _checked_entries({"entries": entries, "warnings": []}, _accounting_context(Voucher.model_validate(item), policy), chart, policy)
        return abs(dr-cr) < .01 and not errors and all(e.amount is not None for e in validated) and not meta.get("warnings") and not meta.get("assumptions")
    except (ValueError, TypeError, KeyError):
        return False


def _seed_examples(company="", voucher=None, chart=None, policy=None, k=3) -> list:
    if not company or k <= 0 or voucher is None:
        return []
    path = config.DATA_DIR / "entry_examples.json"
    if not path.exists():
        return []
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            return []
        return [{"voucher": _voucher_brief(Voucher.model_validate(r["voucher"])), "entries": r["voucher"]["entries"]} for r in records
                if isinstance(r, dict) and r.get("company") == company and isinstance(r.get("voucher"), dict)
                and r.get("status") == ReviewStatus.accountant_approved.value and _example_valid(r["voucher"], voucher, chart, policy)][:k]
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning("種子案例略過：%s", exc)
        return []


def _retrieve_examples(voucher: Voucher, k=3, *, company="", chart=None, policy=None) -> list:
    if not company or chart is None or policy is None:
        return []
    try:
        approved = review.list_vouchers(status=ReviewStatus.accountant_approved, company=company)
        pool = []
        for record in approved:
            if record.company != company:
                continue
            item = json.loads(record.current_json)
            if _example_valid(item, voucher, chart, policy):
                pool.append(item)
        if not pool:
            return []
        query = _embed(json.dumps(_voucher_brief(voucher), ensure_ascii=False))
        scored = [(_cosine(query, _embed(json.dumps(_voucher_brief(Voucher.model_validate(item)), ensure_ascii=False))), item) for item in pool]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"voucher": _voucher_brief(Voucher.model_validate(item)), "entries": item["entries"]} for _, item in scored[:k]]
    except Exception as exc:
        logger.warning("RAG 檢索略過：%s", exc)
        return []


def _embed(text: str) -> list:
    r = requests.post(f"{config.OLLAMA_BASE_URL}/api/embed",
                      json={"model": EMBED_MODEL, "input": text}, timeout=10)
    r.raise_for_status()
    return r.json()["embeddings"][0]


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0



_parse_json = model_invocation._parse_json
