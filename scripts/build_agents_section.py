"""Build the standalone developer appendix for OCR/structure contracts."""
import html
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import doc_types as dt
from app.core.prompts import EXTRACT_TEMPLATE, ROUTE_TEMPLATE, build_extract_prompt, build_route_prompt

OUTPUT = ROOT / "artifacts/recognition-contracts.html"
KIND = {"text": "字串", "number": "數字", "date": "日期"}


def esc(value):
    return html.escape(str(value), quote=False)


GROUP = {
    "invoice": "稅務核心", "receipt": "稅務核心", "tax_bill": "稅務核心",
    "export_declaration": "進出口", "import_declaration": "進出口", "declaration": "進出口",
    "customs_tax": "進出口", "broker_note": "進出口", "fda_import": "進出口",
    "cheque": "金流", "remittance": "金流",
    "utility": "其他", "purchase_order": "其他", "statement": "其他",
    "journal_voucher": "其他", "bill_detail": "其他", "social_insurance": "其他", "other": "其他",
}
TASK = {
    "invoice": "辨識發票基本資料與金額欄位。",
    "receipt": "辨識收據資料，並依契約填寫扣繳相關欄位。",
    "tax_bill": "辨識稅款繳款文件欄位。",
    "export_declaration": "辨識出口報單欄位。",
    "import_declaration": "辨識進口報單欄位與貨物明細。",
    "customs_tax": "辨識進口貨物稅費繳納文件。",
    "broker_note": "辨識報關服務費用與明細。",
    "fda_import": "辨識食品及相關產品輸入文件。",
    "declaration": "辨識未區分進出口的報單欄位。",
    "cheque": "辨識支票或本票欄位。",
    "remittance": "辨識匯款或銀行轉帳欄位。",
    "utility": "辨識公用事業帳單欄位。",
    "purchase_order": "辨識訂購或採購文件欄位。",
    "statement": "辨識對帳單與帳款明細欄位。",
    "journal_voucher": "辨識紙面傳票欄位、借貸明細、合計與來源證據。",
    "bill_detail": "辨識帳單明細附頁欄位。",
    "social_insurance": "辨識社會保險繳費文件欄位。",
    "other": "辨識未歸類文件的通用欄位。",
}
ORDER = ["invoice", "receipt", "tax_bill", "export_declaration", "import_declaration",
         "customs_tax", "broker_note", "fda_import", "declaration",
         "cheque", "remittance", "utility", "purchase_order", "statement", "journal_voucher",
         "bill_detail", "social_insurance", "other"]


def field_table(type_key):
    spec = dt.DOC_TYPES[type_key]

    def table(fields):
        rows = []
        for field in fields:
            money = field.get("money") or {}
            metadata = []
            if money:
                currency = money.get("currency", "")
                if currency == "$document":
                    currency = "文件明示幣別；未知保留未知"
                metadata.append("purpose=" + str(money.get("purpose", "")))
                metadata.append("currency=" + str(currency))
                if money.get("policy_key"):
                    metadata.append("policy_key=" + str(money["policy_key"]))
            rows.append(
                f'<tr><td><code>{esc(field["key"])}</code></td>'
                f'<td>{esc(field["label"])}</td>'
                f'<td>{KIND.get(field["kind"], esc(field["kind"]))}</td>'
                f'<td>{esc("；".join(metadata) if metadata else "—")}</td></tr>'
            )
        return ('<table><tr><th>key</th><th>欄位</th><th>型別</th>'
                '<th>money metadata（purpose／currency／policy_key）</th></tr>' + "\n".join(rows) + '</table>')

    fields = spec["fields"]
    default_currency = spec.get("default_currency") or "依文件明示／未知時保留未知"
    result = (f'<details><summary>欄位契約（{len(fields)} 欄）</summary>'
              f'<p>預設幣別：{esc(default_currency)}。金額來源與用途由契約明列。</p>'
              + table(fields))
    item_fields = spec.get("item_fields") or []
    if item_fields:
        result += (f'<details><summary>明細列契約（{len(item_fields)} 欄）</summary>'
                   + table(item_fields) + '</details>')
    return result + '</details>'


assert len(ORDER) == len(GROUP) == len(TASK) == 18
assert set(ORDER) == set(GROUP) == set(TASK) == set(dt.DOC_TYPES)

rows = []
prev = None
for key in ORDER:
    spec = dt.DOC_TYPES[key]
    group = GROUP[key]
    span = ""
    if group != prev:
        count = sum(1 for item in ORDER if GROUP[item] == group)
        span = f'<td rowspan="{count}">{group}</td>'
        prev = group
    hints = spec.get("type_hints", "")
    if isinstance(hints, (list, tuple)):
        hints = "；".join(str(item) for item in hints)
    if not hints:
        hints = "—"
    rows.append(
        f'<tr>{span}<td>{esc(spec["label"])}<br><code>{key}</code></td>'
        f'<td>{TASK[key]}<br><small>{esc(hints)}</small></td>'
        f'<td>{field_table(key)}</td></tr>'
    )
AGENT_ROWS = "\n".join(rows)

SECTION = f"""
  <!-- ░░░ AGENTS ░░░ -->
  <section class="doc" id="agents">
    <h2><span class="ic">🤖</span>辨識模板與資料契約</h2>
    <p class="lead">辨識流程使用路由（單據分類）與欄位萃取兩個模板。主設定只包含 OCR 與 structure；AI 會計分錄建議、草稿、確認、日記帳與會計傳票格式已退役。紙面傳票的借貸明細、合計及來源證據仍會辨識並保留。</p>

    <h3>辨識流程</h3>
    <p>頁面影像與 OCR → 路由模板判定單據類型 → 欄位萃取及檢查 → 人工覆核與辨識確認。</p>
    <p>欄位契約失敗時，系統會保留原始訊息，在初次請求後再做 <strong>1 次契約重試</strong>；重試將靜態 system 指示併入第一個 system 訊息（保留原任務前綴），錯誤另作 user JSON，原 user 證據不變；每一輪傳輸最多重試 <strong>3 次</strong>。影像補讀與輸出修正由共用流程處理。</p>
    <p>條碼或 QR 解碼值若與影像辨識欄位衝突，保留衝突警告供後續檢視，不靜默覆蓋其中一方。</p>
    <p>固定辨識任務規則由 system 訊息承載；OCR 與其他來源資料作為辨識輸入，不因此取得指令權威。選取證據只表示提供來源資訊，不等於人工確認。</p>

    <h3>18 種文件資料契約</h3>
    <p>欄位名稱、標籤與型別直接取自 <code>DOC_TYPES</code>；類型提示取自 <code>type_hints</code>。展開列可檢視各類型完整欄位。</p>
    <table class="contracts">
      <tr><th>分類</th><th>文件類型</th><th>辨識任務與類型提示</th><th>資料契約</th></tr>
{AGENT_ROWS}
    </table>
    <div class="callout note">
      <div class="ct">欄位補充</div>
      收據契約包含 <code>withholding_tax</code>；貨物明細支援 <code>hs_code</code> 與 <code>origin_country</code>。細節以契約列出的欄位和萃取模板為準。
    </div>
    <p>舊收據稅額尚未確認語意時，來源記錄為 <code>semantic_role=unconfirmed_tax</code>；canonical 再次儲存不會把它當作共同營業稅。人工確認並移入 <code>withholding_tax</code> 後，未確認狀態解除。</p>
    <p>金額欄位契約會標示用途、幣別與來源語意；通用報單估價欄位仍按原文件資料辨識，出口報單保留多商品 items 及既有首項欄位，勞健保明細列包含 <code>fee_code</code>。</p>

    <p>單據類型分類仍由路由模板執行；獨立驗證工具的 <code>classify_model</code> 也保留作單據分類。它不是會計科目分類。</p>

    <h3>兩個辨識模板</h3>
    <h4>① 路由模板（<code>ROUTE_TEMPLATE</code>）</h4>
    <p>由類型清單建立分類提示。Router 只將水電及電信等公用事業文件歸入 <code>utility</code>；<code>rotate</code> 只接受 0、90、180、270，<code>other</code> 必須提供原因。以下展示建構器產生的預設版本。</p>
    <div class="codeblock"><span class="lang">prompt</span><pre>{esc(build_route_prompt())}</pre></div>
    <details><summary>ROUTE_TEMPLATE 原文</summary><div class="codeblock"><span class="lang">template</span><pre>{esc(ROUTE_TEMPLATE)}</pre></div></details>

    <h4>② 萃取模板（<code>EXTRACT_TEMPLATE</code>）</h4>
    <p>依所選類型契約產生欄位提示。<code>confidence</code> 列舉由契約生成，重試錯誤會列出可用值。付款狀態保留原文及正規化值：狀態為 <code>paid</code>、<code>unpaid</code>、<code>unknown</code>；付款方式為 <code>bank</code>、<code>cash</code>、<code>other</code>、<code>unknown</code>，並保留「已結清」等原始表述。以下展示預設影像模式下的發票範例。</p>
    <div class="codeblock"><span class="lang">prompt</span><pre>{esc(build_extract_prompt("invoice"))}</pre></div>
    <details><summary>EXTRACT_TEMPLATE 原文</summary><div class="codeblock"><span class="lang">template</span><pre>{esc(EXTRACT_TEMPLATE)}</pre></div></details>

    <p>辨識報告列印保留。原始 <code>journal_voucher</code> 紙面借貸 <code>entries</code>、合計及 evidence 仍是辨識資料；明細科目與品名科目記憶規則仍可使用。摘要彙總表 API 保留，其他日記帳／會計傳票格式端點已退役並回傳 404。</p>
    <p class="muted">模板原文：<code>app/core/prompts.py</code>；類型契約：<code>app/core/doc_types.py</code>。</p>
  </section>
"""

DOCUMENT = f'''<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>開發用辨識契約附錄</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ max-width: 1240px; margin: 0 auto; padding: 28px; color: #202124; font: 16px/1.7 "Microsoft JhengHei", sans-serif; }}
    h1, h2, h3, h4 {{ line-height: 1.4; color: #111; }}
    h1 {{ font-size: 30px; }}
    h2 {{ font-size: 24px; }}
    h3 {{ margin-top: 28px; font-size: 20px; }}
    h4 {{ margin-top: 22px; font-size: 17px; }}
    p {{ margin: 0 0 14px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 18px 0 26px; font-size: 14px; }}
    th, td {{ border: 1px solid #d9d9d9; padding: 10px 12px; text-align: left; vertical-align: top; }}
    th {{ background: #e6edf2; color: #111; }}
    code {{ overflow-wrap: anywhere; }}
    details {{ margin: 6px 0; }}
    summary {{ cursor: pointer; font-weight: 600; }}
    .codeblock {{ overflow-x: auto; margin: 12px 0 18px; padding: 14px; background: #f5f6f7; }}
    pre {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: 13px/1.55 Consolas, monospace; }}
    .callout {{ margin: 18px 0; }}
    .muted {{ color: #555; }}
    @media (max-width: 760px) {{ body {{ padding: 16px; }} table {{ font-size: 12px; }} th, td {{ padding: 7px; }} }}
    @media print {{ @page {{ margin: 15mm; }} body {{ max-width: none; padding: 0; }} table {{ font-size: 9pt; }} tr {{ break-inside: avoid; }} }}
  </style>
</head>
<body>
  <h1>開發用辨識契約附錄</h1>
{SECTION.strip()}
</body>
</html>
'''
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(DOCUMENT, encoding="utf-8")
print(f"wrote standalone recognition contracts appendix: {OUTPUT.relative_to(ROOT)}")
