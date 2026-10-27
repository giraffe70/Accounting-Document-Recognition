"""User-selected company defaults, kept separate from document evidence."""
import re


# Each pair belongs to the same party. For DEFAULT_ROLES the last pair is the
# company role only when that party has no printed name or tax ID.
PARTIES = {
    "invoice": (("seller_name", "seller_tax_id"), ("buyer_name", "buyer_tax_id")),
    "other": (("seller_name", "seller_tax_id"), ("buyer_name", "buyer_tax_id")),
    "statement": (("issuer", "issuer_tax_id"), ("customer", "customer_tax_id")),
    "purchase_order": (("supplier", "supplier_tax_id"), ("buyer", "buyer_tax_id")),
    "export_declaration": (("buyer", "buyer_tax_id"), ("exporter", "exporter_tax_id")),
    "import_declaration": (("taxpayer", "taxpayer_tax_id"),),
    "customs_tax": (("taxpayer", "tax_id"),),
    "declaration": (("trader", "tax_id"),),
    "tax_bill": (("taxpayer", "tax_id"),),
    "utility": (("customer_name", "customer_tax_id"),),
    "fda_import": (("applicant", "applicant_tax_id"),),
    "receipt": (("vendor", "tax_id"),),
}
DEFAULT_ROLES = frozenset(PARTIES) - {"receipt", "other"}
WARNING_PREFIX = "歸屬公司統編："


def _text(value):
    return re.sub(r"\s+", "", str(value or "")).replace("臺", "台")


def _matches(name, company):
    name = _text(name)
    full, short = _text(company.get("name")), _text(company.get("short"))
    if not name:
        return False
    if full and full in name:
        return True
    return bool(short and (name == short or any(name.startswith(short + suffix)
                for suffix in ("股份有限公司", "企業股份有限公司", "有限公司"))))


def apply(data, company, *, fill_fields=True):
    """Mutate a voucher dict; preserve printed/manual values, including blanks.

    ``company`` must come from the server registry, never from voucher JSON.
    Company-generated fields are reversible when the selection or type changes.
    """
    company = dict(company or {})
    data["company_info"] = {key: company.get(key) for key in ("id", "name", "short", "tax_id")} if company else {}
    fields = data["fields"] = data.get("fields") or {}
    evidence = data["field_evidence"] = data.get("field_evidence") or {}
    warnings = [w for w in data.get("validation_warnings", []) if not str(w).startswith(WARNING_PREFIX)]
    data["validation_warnings"] = warnings
    # Recompute defaults on every application. Human edits have already been
    # marked manual by the review service and therefore survive this cleanup.
    for path, records in evidence.items():
        selected = next((r for r in reversed(records) if r.get("selected")), None)
        if not selected or selected.get("source") != "company" or not path.startswith("fields."):
            continue
        key = path[7:]
        if fields.get(key) == selected.get("value"):
            fields[key] = None
        for record in records:
            if record.get("source") == "company":
                record["selected"] = False
    if not fill_fields or not company:
        return data
    from app.services.taxid import validate_tax_id
    tax_id = str(company.get("tax_id") or "").strip()
    if not validate_tax_id(tax_id):
        warnings.append(WARNING_PREFIX + "尚未設定有效統編，請檢查公司資料")
        return data
    pairs = PARTIES.get(data.get("doc_type_key"), ())
    matches = [pair for pair in pairs if _matches(fields.get(pair[0]), company) or str(fields.get(pair[1]) or "").strip() == tax_id]
    if len(matches) == 1:
        pair = matches[0]
    elif not matches and data.get("doc_type_key") in DEFAULT_ROLES and pairs and all(fields.get(key) in (None, "") for key in pairs[-1]):
        pair = pairs[-1]
    else:
        if pairs:
            warnings.append(WARNING_PREFIX + "無法確認所選公司對應的交易對象，請核對單據")
        return data
    _, tax_key = pair
    if fields.get(tax_key) not in (None, "", tax_id):
        warnings.append(WARNING_PREFIX + "單據統編與所選公司不同，保留單據值，請核對")
        return data
    path = "fields." + tax_key
    records = evidence.setdefault(path, [])
    if fields.get(tax_key) not in (None, "") or any(r.get("source") == "manual" and r.get("selected") for r in records):
        return data
    for record in records:
        record["selected"] = False
    # Reuse the last default record instead of growing history on each save.
    record = {"source": "company", "value": tax_id, "company_id": company.get("id"),
              "company_name": company.get("name"), "selected": True}
    if records and records[-1] == {**record, "selected": False}:
        records[-1]["selected"] = True
    else:
        records.append(record)
    fields[tax_key] = tax_id
    data.setdefault("boxes", {}).pop(tax_key, None)
    return data
