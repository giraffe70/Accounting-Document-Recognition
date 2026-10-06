"""Import applicability; accounting instructions live in company policy data."""

def is_import(voucher: dict) -> bool:
    key = voucher.get("doc_type_key") or ""
    if key in ("import_declaration", "customs_tax"):
        return True
    if key in ("declaration", "broker_note"):
        return (voucher.get("fields") or {}).get("direction") == "進口"
    if key:
        return False
    # Only exact legacy labels; notes and the generic word 報單 are never routing evidence.
    return voucher.get("doc_type") in ("進口報單", "海關進口貨物稅費繳納證明", "進口關稅")
