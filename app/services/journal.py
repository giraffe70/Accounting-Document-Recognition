"""單月日記帳彙整：將「已核定」憑證的分錄，依期別彙整成當月日記帳。

每列 = 一筆分錄（借項或貸項），依日期、憑證號排序，並計算借貸合計與平衡。
"""
import json

from app.db.models import ReviewStatus
from app.services import review


def periods(company: str = "") -> list[str]:
    """有已核定憑證的期別清單（YYYY-MM），新到舊。"""
    recs = review.list_vouchers(status=ReviewStatus.accountant_approved, company=company)
    ps = sorted({r.period for r in recs if r.period}, reverse=True)
    return ps


def build_journal(period: str, company: str = "") -> dict:
    """彙整指定期別的日記帳。"""
    recs = review.list_vouchers(status=ReviewStatus.accountant_approved,
                                company=company, period=period)
    rows = []
    for rec in recs:
        cur = json.loads(rec.current_json)
        date = cur.get("date", "")
        vid = cur.get("voucher_id", "")
        for e in cur.get("entries", []):
            amt = e.get("amount") or 0
            has_debit = bool(e.get("debit_account") or e.get("debit_account_name"))
            has_credit = bool(e.get("credit_account") or e.get("credit_account_name"))
            rows.append({
                "date": date,
                "voucher_id": vid,
                "summary": e.get("description", ""),
                "debit_account": f"{e.get('debit_account','')} {e.get('debit_account_name','')}".strip(),
                "credit_account": f"{e.get('credit_account','')} {e.get('credit_account_name','')}".strip(),
                "debit_amount": amt if has_debit else None,
                "credit_amount": amt if has_credit else None,
            })
    rows.sort(key=lambda r: (r["date"], r["voucher_id"]))
    debit_total = sum(r["debit_amount"] or 0 for r in rows)
    credit_total = sum(r["credit_amount"] or 0 for r in rows)
    return {
        "period": period,
        "company": company,
        "rows": rows,
        "count": len(recs),
        "debit_total": debit_total,
        "credit_total": credit_total,
        "balance_check": abs(debit_total - credit_total) < 0.01,
    }
