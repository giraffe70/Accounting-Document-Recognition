"""產製報表：客製 Web 模板兩套（可列印／另存 PDF 的獨立 HTML 頁）。

模板 A：憑證明細彙總表（期別內所有憑證的關鍵欄位 + 合計）
模板 B：會計傳票格式（每張已核定憑證的借貸分錄，傳統傳票版面）
"""
import json
from html import escape

from app.db.models import ReviewStatus
from app.services import review

_STATUS = {"pending": "待審", "clerk_reviewed": "記帳士已確認",
           "accountant_approved": "會計師已核定", "rejected": "退回"}

TEMPLATES = [
    {"id": "summary", "name": "憑證明細彙總表"},
    {"id": "journal_voucher", "name": "會計傳票格式"},
]

_CSS = """
body{font-family:"Microsoft JhengHei",sans-serif;color:#1f2328;padding:28px;}
h1{font-size:20px;margin-bottom:2px;} .meta{color:#636c76;font-size:12px;margin-bottom:16px;}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin:8px 0;}
th,td{border:1px solid #999;padding:5px 8px;text-align:left;} th{background:#eee;}
.num{text-align:right;font-variant-numeric:tabular-nums;} tfoot td{font-weight:700;background:#f3f3f3;}
.voucher{border:1px solid #999;border-radius:6px;padding:12px;margin:12px 0;page-break-inside:avoid;}
.voucher h3{font-size:14px;margin-bottom:6px;}
.audit{border-top:1px solid #d0d7de;margin-top:10px;padding-top:4px;font-size:11px;}
.audit pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8fa;padding:8px;}
.toolbar{margin-bottom:16px;} .toolbar button{padding:8px 16px;font-size:14px;cursor:pointer;}
@media print{.no-print{display:none;}}
"""


def _num(v):
    return f"{v:,.0f}" if isinstance(v, (int, float)) else ""


def _esc(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def _audit(c: dict) -> str:
    """Render optional provenance and validation data without dropping new fields."""
    audit = {}
    for key in ("field_evidence", "validation_warnings", "classification_meta", "payment_state"):
        value = c.get(key)
        if value not in (None, {}, [], ""):
            audit[key] = value

    standard_item_keys = {"description", "quantity", "unit_price", "amount"}
    extra_items = []
    for index, item in enumerate(c.get("items") or [], 1):
        extra = {key: value for key, value in item.items() if key not in standard_item_keys}
        if any(value not in (None, "", [], {}) for value in extra.values()):
            extra_items.append({"item": index, "fields": extra})
    if extra_items:
        audit["item_extra_fields"] = extra_items

    entry_evidence = [
        {"entry": index, "amount_source": entry.get("amount_source"),
         "evidence_paths": entry.get("evidence_paths") or []}
        for index, entry in enumerate(c.get("entries") or [], 1)
        if entry.get("amount_source") or entry.get("evidence_paths")
    ]
    if entry_evidence:
        audit["entry_evidence_paths"] = entry_evidence

    if not audit:
        return ""
    rendered = _esc(json.dumps(audit, ensure_ascii=False, sort_keys=True))
    return ("<section class='audit'><h4>來源、驗證與政策紀錄</h4>"
            f"<pre>{rendered}</pre></section>")


def _page(title: str, body: str) -> str:
    return (f'<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><title>{_esc(title)}</title>'
            f'<style>{_CSS}</style></head><body>'
            f'<div class="toolbar no-print"><button onclick="window.print()">🖨 列印／另存 PDF</button></div>'
            f'{body}</body></html>')


def render(template: str, period: str, company: str = "") -> str:
    if template == "journal_voucher":
        return _journal_voucher(period, company)
    return _summary(period, company)


def _summary(period: str, company: str) -> str:
    recs = review.list_vouchers(company=company, period=period)
    rows, st, tx, tot = [], 0, 0, 0
    for r in recs:
        c = json.loads(r.current_json)
        audit = _audit(c)
        st += c.get("subtotal") or 0
        tx += c.get("tax_amount") or 0
        tot += c.get("total_amount") or 0
        rows.append(
            f"<tr><td>{_esc(c.get('date',''))}</td><td>{_esc(c.get('voucher_id',''))}</td>"
            f"<td>{_esc(c.get('doc_type',''))}</td><td>{_esc((c.get('seller') or {}).get('name',''))}</td>"
            f"<td>{_esc((c.get('buyer') or {}).get('name',''))}</td>"
            f"<td class='num'>{_num(c.get('subtotal'))}</td><td class='num'>{_num(c.get('tax_amount'))}</td>"
            f"<td class='num'>{_num(c.get('total_amount'))}</td><td>{_esc(_STATUS.get(r.status, r.status))}</td></tr>"
            + (f"<tr class='audit-row'><td colspan='9'>{audit}</td></tr>" if audit else ""))
    body = (f"<h1>憑證明細彙總表</h1><div class='meta'>期別 {_esc(period)}"
            f"{'　公司 ' + _esc(company) if company else ''}　共 {len(recs)} 張</div>"
            "<table><thead><tr><th>日期</th><th>憑證號</th><th>類型</th><th>賣方</th><th>買方</th>"
            "<th>未稅</th><th>稅額</th><th>總計</th><th>狀態</th></tr></thead><tbody>"
            + "".join(rows) +
            f"</tbody><tfoot><tr><td colspan='5'>合計</td><td class='num'>{_num(st)}</td>"
            f"<td class='num'>{_num(tx)}</td><td class='num'>{_num(tot)}</td><td></td></tr></tfoot></table>")
    return _page(f"憑證明細彙總表 {period}", body)


def _journal_voucher(period: str, company: str) -> str:
    recs = review.list_vouchers(status=ReviewStatus.accountant_approved,
                                company=company, period=period)
    blocks = []
    for r in recs:
        c = json.loads(r.current_json)
        erows = []
        for e in c.get("entries", []):
            amt = e.get("amount")
            has_d = bool(e.get("debit_account") or e.get("debit_account_name"))
            has_c = bool(e.get("credit_account") or e.get("credit_account_name"))
            erows.append(
                f"<tr><td>{_esc(e.get('debit_account',''))} {_esc(e.get('debit_account_name',''))}</td>"
                f"<td class='num'>{_num(amt) if has_d else ''}</td>"
                f"<td>{_esc(e.get('credit_account',''))} {_esc(e.get('credit_account_name',''))}</td>"
                f"<td class='num'>{_num(amt) if has_c else ''}</td></tr>")
        blocks.append(
            f"<div class='voucher'><h3>{_esc(c.get('date',''))}　傳票 {_esc(c.get('voucher_id',''))}　{_esc(c.get('note',''))}</h3>"
            "<table><thead><tr><th>借方科目</th><th>借方金額</th><th>貸方科目</th><th>貸方金額</th></tr></thead>"
            f"<tbody>{''.join(erows) or '<tr><td colspan=4>（無分錄）</td></tr>'}</tbody></table>"
            f"{_audit(c)}</div>")
    body = (f"<h1>會計傳票</h1><div class='meta'>期別 {_esc(period)}"
            f"{'　公司 ' + _esc(company) if company else ''}　共 {len(recs)} 張已核定</div>"
            + "".join(blocks or ["<p>此期別無已核定憑證。</p>"]))
    return _page(f"會計傳票 {period}", body)
