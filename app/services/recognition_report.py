"""純 HTML 憑證辨識報告渲染器。"""
from __future__ import annotations

import html
import json
from collections.abc import Mapping
from datetime import date, datetime
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from app.core import doc_types


_ITEM_COLUMNS = {
    "account_code", "account_name", "description", "quantity", "unit_price", "amount",
}


def _get(value, key, default=None):
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _text(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (dict, list, tuple, set)):
        return "—"
    if isinstance(value, (datetime, date)):
        value = value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return html.escape(str(value), quote=True)


def _present(value) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _record_attachments(rec) -> list:
    raw = _get(rec, "attachments", [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return raw if isinstance(raw, list) else []


def _image_url(rec, attachment_index: int | None = None) -> str:
    rec_id = _get(rec, "id", "")
    version = _get(rec, "updated_at")
    if isinstance(version, (datetime, date)):
        version = version.isoformat()
    params = {"expected_updated_at": str(version or "")}
    if attachment_index is not None:
        params["att"] = str(attachment_index)
    return f"/api/vouchers/{quote(str(rec_id), safe='')}/image?{urlencode(params)}"


def _field_rows(type_key, spec, fields, field_evidence=None) -> str:
    field_evidence = field_evidence if isinstance(field_evidence, Mapping) else {}
    rows = []
    for field in spec.get("fields", []):
        key = field["key"]
        value = fields.get(key)
        if _present(value):
            evidence = field_evidence.get(f"fields.{key}")
            company_filled = (isinstance(evidence, list)
                              and any(isinstance(record, Mapping)
                                      and record.get("source") == "company"
                                      and record.get("selected") is True
                                      for record in evidence))
            source_label = '<small class="company-filled-tag">公司帶入</small>' if company_filled else ""
            layout_class = " field-full" if type_key == "statement" and key in {"issuer", "customer"} else ""
            rows.append(
                f'<div class="field{layout_class}"><dt>{_text(field.get("label", key))}{source_label}</dt>'
                f'<dd>{_text(value)}</dd></div>'
            )
    return "".join(rows)


def _item_extra(item, special_fields) -> str:
    rows = []
    for field in special_fields:
        key = field.get("key")
        value = _get(item, key)
        if key not in _ITEM_COLUMNS and _present(value):
            rows.append(
                f'<div class="item-extra"><span>{_text(field.get("label", key))}：</span>'
                f'{_text(value)}</div>'
            )
    return "".join(rows)


def _items_table(items, special_fields) -> str:
    if not isinstance(items, list):
        items = []
    headers = ("科目代號", "科目名稱", "品名／摘要", "數量", "單價", "金額")
    head = "".join(f"<th>{label}</th>" for label in headers)
    rows = []
    for item in items:
        description = _text(_get(item, "description"))
        description += _item_extra(item, special_fields)
        cells = (
            _text(_get(item, "account_code")),
            _text(_get(item, "account_name")),
            description,
            _text(_get(item, "quantity")),
            _text(_get(item, "unit_price")),
            _text(_get(item, "amount")),
        )
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    if not rows:
        rows.append('<tr><td colspan="6" class="empty">無明細</td></tr>')
    return f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


def _image_pages(rec) -> str:
    pages = []
    if _get(rec, "image_path"):
        pages.append(("單據影像", _image_url(rec)))
    else:
        pages.append(("單據影像", None))
    pages.extend((f"附圖 {index + 1}", _image_url(rec, index))
                 for index, path in enumerate(_record_attachments(rec)) if path)
    rendered = []
    for index, (label, url) in enumerate(pages):
        content = (f'<img src="{html.escape(url, quote=True)}" alt="{_text(label)}">'
                   if url else '<p class="no-image">無單據影像</p>')
        rendered.append(
            f'<section class="image-page{" page-break" if index < len(pages) else ""}">'
            f'<div class="image-label">{_text(label)}</div>{content}</section>'
        )
    return "".join(rendered)


def render(rec, voucher, account_warnings=None) -> str:
    """Render a printable report using a record and a prepared voucher mapping."""
    voucher = voucher if isinstance(voucher, Mapping) else {}
    fields = voucher.get("fields") if isinstance(voucher.get("fields"), Mapping) else {}
    field_evidence = voucher.get("field_evidence") if isinstance(voucher.get("field_evidence"), Mapping) else {}
    company_info = voucher.get("company_info") if isinstance(voucher.get("company_info"), Mapping) else {}
    type_key = voucher.get("doc_type_key", "")
    spec = doc_types.get(type_key)
    doc_type_label = voucher.get("doc_type") or spec.get("label") or type_key
    items = voucher.get("items", [])
    company = _get(rec, "company") or _get(rec, "company_id")
    confirmed_at = _get(rec, "confirmed_at")
    if isinstance(confirmed_at, (datetime, date)):
        if isinstance(confirmed_at, datetime):
            if confirmed_at.utcoffset() is not None:
                confirmed_at = confirmed_at.astimezone(ZoneInfo("Asia/Taipei"))
            confirmed_at = confirmed_at.isoformat(sep=" ", timespec="seconds")
        else:
            confirmed_at = confirmed_at.isoformat()
    confirmed_by = _get(rec, "confirmed_by_name") or "未留存"

    if type_key == "journal_voucher":
        totals = (("原稿借方合計", fields.get("debit_total")),
                  ("原稿貸方合計", fields.get("credit_total")))
    else:
        totals = (("未稅小計", voucher.get("subtotal")),
                  ("稅額", voucher.get("tax_amount")),
                  ("總計", voucher.get("total_amount")))
    total_html = "".join(
        f'<div class="total"><dt>{label}</dt><dd>{_text(value)}</dd></div>'
        for label, value in totals
    )
    html_doc = f'''<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>會計憑證辨識報告</title><style>
@page {{ size: A4 portrait; margin: 14mm; }}
* {{ box-sizing: border-box; }} body {{ margin: 0; color: #17212b; font: 12pt/1.5 "Noto Sans TC", sans-serif; }}
.toolbar {{ position: sticky; top: 0; padding: 10px; background: #fff; border-bottom: 1px solid #ccd3da; }}
.toolbar button {{ padding: 8px 18px; }} #print-status {{ margin-left: 12px; color: #a12622; }}
.image-page {{ min-height: 265mm; display: flex; flex-direction: column; align-items: center; justify-content: center; page-break-after: always; break-after: page; }}
.image-label {{ width: 100%; margin-bottom: 4mm; font-size: 10pt; color: #56616d; }}
.image-page img {{ display: block; max-width: 100%; max-height: 245mm; object-fit: contain; }}
.no-image {{ color: #66717c; }}
h1 {{ text-align: center; font-size: 20pt; margin: 0 0 8mm; }} h2 {{ font-size: 14pt; margin: 7mm 0 3mm; }}
.fields {{ display: grid; grid-template-columns: 1fr 1fr; gap: 2mm 8mm; }}
.field-full {{ grid-column: 1 / -1; }}
.field, .total {{ display: flex; gap: 8px; min-width: 0; }} dt {{ color: #53606c; min-width: 7em; }} dd {{ margin: 0; overflow-wrap: anywhere; white-space: pre-wrap; }}
.company-filled-tag {{ margin-left: 5px; color: #53606c; font-size: 8pt; font-weight: 400; white-space: nowrap; }}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }} thead {{ display: table-header-group; }}
th, td {{ border: 1px solid #aab4be; padding: 2mm; text-align: left; vertical-align: top; overflow-wrap: anywhere; white-space: pre-wrap; }}
th {{ background: #eef2f5; }} th:nth-child(1) {{ width: 14%; }} th:nth-child(2) {{ width: 16%; }} th:nth-child(3) {{ width: 30%; }}
th:nth-child(4), th:nth-child(5) {{ width: 12%; }} th:nth-child(6) {{ width: 16%; }}
td:nth-child(4), td:nth-child(5), td:nth-child(6) {{ text-align: right; }}
tr {{ break-inside: avoid; page-break-inside: avoid; }}
.item-extra {{ font-size: 9pt; color: #53606c; }} .item-extra span {{ font-weight: 600; }} .empty {{ text-align: center; color: #66717c; }}
.totals {{ margin: 5mm 0 0 auto; width: 48%; }} .total {{ justify-content: space-between; border-bottom: 1px solid #d8dee4; padding: 1.5mm 0; }}
@media print {{ .toolbar {{ display: none !important; }} .image-page {{ min-height: 265mm; }} }}
</style></head><body>
<div class="toolbar"><button id="print-button" type="button" disabled>列印</button><span id="print-status" role="alert"></span></div>
<main>{_image_pages(rec)}
<article class="report"><h1>會計憑證辨識報告</h1>
<section class="fields">
<div class="field"><dt>公司</dt><dd>{_text(company)}</dd></div>
{f'<div class="field"><dt>統編</dt><dd>{_text(company_info.get("tax_id"))}</dd></div>' if _present(company_info.get("tax_id")) else ""}
<div class="field"><dt>單據類型</dt><dd>{_text(doc_type_label)}</dd></div>
<div class="field"><dt>號碼</dt><dd>{_text(voucher.get("voucher_id"))}</dd></div>
<div class="field"><dt>日期</dt><dd>{_text(voucher.get("date"))}</dd></div>
<div class="field"><dt>金額幣別</dt><dd>{_text(voucher.get("currency"))}</dd></div>
{_field_rows(type_key, spec, fields, field_evidence)}
<div class="field"><dt>確認人</dt><dd>{_text(confirmed_by)}</dd></div>
<div class="field"><dt>確認時間</dt><dd>{_text(confirmed_at)}</dd></div>
</section>
<h2>明細</h2>{_items_table(items, spec.get("item_fields", []))}
<dl class="totals">{total_html}</dl>
</article></main>
<script>
(function () {{
  const button = document.getElementById('print-button');
  const status = document.getElementById('print-status');
  let failed = false;
  let phase = 'awaiting';
  const images = Array.from(document.images);
  function fail() {{
    failed = true;
    phase = 'failed';
    button.disabled = true;
    status.textContent = '影像載入失敗，報告無法列印。請確認網路連線或重新載入頁面。';
  }}
  images.forEach(function (img) {{ img.addEventListener('error', fail); }});
  async function readyToPrint() {{
    if (phase !== 'awaiting') return;
    if (document.fonts && document.fonts.ready) await document.fonts.ready;
    await Promise.all(images.map(async function (img) {{
      if (!img.complete) await new Promise(function (resolve) {{
        img.addEventListener('load', resolve, {{ once: true }});
        img.addEventListener('error', resolve, {{ once: true }});
      }});
      if (!img.naturalWidth) fail();
      else if (img.decode) {{ try {{ await img.decode(); }} catch (_) {{ fail(); }} }}
    }}));
    if (!failed) {{
      phase = 'ready';
      button.disabled = false;
      window.print();
    }}
  }}
  button.addEventListener('click', function () {{ if (phase === 'ready') window.print(); }});
  window.addEventListener('load', readyToPrint, {{ once: true }});
}})();
</script></body></html>'''
    return html_doc
