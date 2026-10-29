"""憑證明細彙總表：期別內憑證關鍵欄位與合計，可列印／另存 PDF。"""
import json
from html import escape

from app.services import review

_STATUS = {"prepared": "待外觀確認", "queued": "待提取",
           "pending": "辨識待確認", "confirmed": "辨識已確認"}

TEMPLATES = [
    {"id": "summary", "name": "憑證明細彙總表"},
]

_CSS = """
body{font-family:"Microsoft JhengHei",sans-serif;color:#1f2328;padding:28px;}
h1{font-size:20px;margin-bottom:2px;} .meta{color:#636c76;font-size:12px;margin-bottom:16px;}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin:8px 0;}
th,td{border:1px solid #999;padding:5px 8px;text-align:left;} th{background:#eee;}
.num{text-align:right;font-variant-numeric:tabular-nums;} tfoot td{font-weight:700;background:#f3f3f3;}
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
    return ("<section class='audit'><h4>來源與驗證紀錄</h4>"
            f"<pre>{rendered}</pre></section>")


def _page(title: str, body: str) -> str:
    return (f'<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><title>{_esc(title)}</title>'
            f'<style>{_CSS}</style></head><body>'
            f'<div class="toolbar no-print"><button onclick="window.print()">🖨 列印／另存 PDF</button></div>'
            f'{body}</body></html>')


def render(template: str, period: str, company: str = "") -> str:
    if template != "summary":
        raise ValueError("找不到報表模板")
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
