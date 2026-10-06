"""Build the manual's recognition templates and document contracts section."""
import html
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import doc_types as dt
from app.core.prompts import (
    ACCOUNTING_TEMPLATE,
    EXTRACT_TEMPLATE,
    ROUTE_TEMPLATE,
    build_extract_prompt,
    build_route_prompt,
)

HTML = ROOT / "docs/manual/index.html"
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
    "journal_voucher": "辨識傳票欄位與分錄列。",
    "bill_detail": "辨識帳單明細附頁欄位。",
    "social_insurance": "辨識社會保險繳費文件欄位。",
    "other": "辨識未歸類文件的通用欄位。",
}
ORDER = ["invoice", "receipt", "tax_bill", "export_declaration", "import_declaration",
         "customs_tax", "broker_note", "fda_import", "declaration",
         "cheque", "remittance", "utility", "purchase_order", "statement", "journal_voucher",
         "bill_detail", "social_insurance", "other"]


def field_table(type_key):
    fields = dt.DOC_TYPES[type_key]["fields"]
    rows = []
    for field in fields:
        rows.append(
            f'<tr><td><code>{esc(field["key"])}</code></td>'
            f'<td>{esc(field["label"])}</td><td>{KIND.get(field["kind"], esc(field["kind"]))}</td></tr>'
        )
    return (f'<details><summary>欄位契約（{len(fields)} 欄）</summary><table>'
            '<tr><th>key</th><th>欄位</th><th>型別</th></tr>' + "\n".join(rows) +
            '</table></details>')


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
    <p class="lead">辨識流程共用三個核心模板：路由分類、欄位萃取、會計分類。18 種文件各自提供欄位契約與 <code>type_hints</code>，不再維護每種文件的一整份獨立提示詞。</p>

    <h3>辨識流程</h3>
    <div class="diagram">
      <div class="mermaid">
flowchart LR
  IMG["頁面影像與 OCR"] --> ROUTE["路由模板"] --> TYPE["文件類型與契約"]
  TYPE --> EXTRACT["萃取模板"] --> READ["共用補讀與欄位檢查"] --> REVIEW["待審資料"]
  REVIEW --> ACCOUNT["會計模板"] --> CANDIDATE["候選分錄"]
      </div>
      <div class="diagram-cap">路由與萃取共用提示詞模板，再以文件類型契約和提示補充差異；會計模板產生供審核的候選分錄。</div>
    </div>
    <p>欄位契約失敗時，系統會在初次請求後再做 <strong>1 次契約重試</strong>；每一輪傳輸最多重試 <strong>3 次</strong>。影像補讀與輸出修正由共用流程處理。</p>
    <p>條碼或 QR 解碼值若與影像辨識欄位衝突，保留衝突警告供後續檢視，不靜默覆蓋其中一方。</p>

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

    <h3>三個共用核心模板</h3>
    <h4>① 路由模板（<code>ROUTE_TEMPLATE</code>）</h4>
    <p>由類型清單建立分類提示；以下展示建構器產生的預設版本。</p>
    <div class="codeblock"><span class="lang">prompt</span><pre>{esc(build_route_prompt())}</pre></div>
    <details><summary>ROUTE_TEMPLATE 原文</summary><div class="codeblock"><span class="lang">template</span><pre>{esc(ROUTE_TEMPLATE)}</pre></div></details>

    <h4>② 萃取模板（<code>EXTRACT_TEMPLATE</code>）</h4>
    <p>依所選類型契約產生欄位提示。以下展示預設影像模式下的發票範例。</p>
    <div class="codeblock"><span class="lang">prompt</span><pre>{esc(build_extract_prompt("invoice"))}</pre></div>
    <details><summary>EXTRACT_TEMPLATE 原文</summary><div class="codeblock"><span class="lang">template</span><pre>{esc(EXTRACT_TEMPLATE)}</pre></div></details>

    <h4>③ 會計模板（<code>ACCOUNTING_TEMPLATE</code>）</h4>
    <p>只對已確認為進口的文件套用進口規則；出口類型不套用。預設角色映射為：進口稅費→預付費用、海關營業稅→進項稅額、銀行手續費→其他費用。未付款進口／商品使用應付帳款，一般未付款費用使用應付費用；已付款須有付款方式，才能選銀行存款或現金。科目角色由當次公司的科目表解析，不固定寫入科目代碼。進口預設 <code>include_goods_values=false</code>、<code>posting_currency=TWD</code>；外幣發票與明細完整保留，但不將外幣貨款與臺幣稅費混合加總。</p>
    <p>付款方式或收支方向不明時，提出候選分錄並明示假設；不能據此強制貸記銀行。會計輸出標記為 <code>proposed</code>、<code>candidate</code> 或 <code>skipped</code>。缺金額或科目屬候選；原始單面傳票再次分類會以 <code>status=skipped</code>、<code>reason=original_entries</code> 保留既有分錄，不生成另一側。其 <code>original_checks</code> 記錄 <code>complete</code>、<code>single_sided</code>、<code>observed_totals</code> 與 <code>printed_totals_match</code>；<code>balance_check</code> 僅表示借貸實際相等，單面完整不代表平衡。候選結果供人員檢視確認，不代表已核定。</p>
    <p>辨識欄位可保留 <code>field_evidence</code> 與 <code>validation_warnings</code>；會計模型只接收已選取的現行證據，完整歷史仍保存。人工修改後會重算日期、金額、統編等目前警告，並保留原始來源與衝突診斷。分類結果記錄於 <code>classification_meta</code>，分錄證據路徑記錄於 <code>JournalEntry.evidence_paths</code>。日記帳列保留 null 金額與證據路徑，忽略舊 <code>bill_detail</code> 分錄列；前端與報表列印附上 escaped 來源、驗證及政策紀錄。舊瀏覽器重複儲存不覆蓋服務端 audit 與 stale 標記。</p>
    <div class="codeblock"><span class="lang">prompt</span><pre>{esc(ACCOUNTING_TEMPLATE)}</pre></div>
    <p class="muted">模板原文：<code>app/core/prompts.py</code>；類型契約：<code>app/core/doc_types.py</code>。</p>
  </section>
"""

source = HTML.read_text(encoding="utf-8")
anchor = "<!-- ░░░ THIRD-PARTY ░░░ -->"
assert source.count(anchor) == 1, "third-party anchor must occur exactly once"
pattern = re.compile(r'  <section class="doc" id="agents">.*?  </section>\s*', re.S)
if len(pattern.findall(source)) == 1:
    source = pattern.sub(SECTION.lstrip("\n") + "\n\n", source)
else:
    assert len(pattern.findall(source)) == 0, "expected at most one existing #agents section"
    source = source.replace(anchor, SECTION.lstrip("\n") + "\n" + anchor)

assert source.count('id="agents"') == 1
HTML.write_text(source, encoding="utf-8")
print("rebuilt #agents section from prompts.py and DOC_TYPES")
