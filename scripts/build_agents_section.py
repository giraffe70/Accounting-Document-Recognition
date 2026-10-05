"""產生手冊〈辨識 Agent 一覽〉章節並插入 docs/manual/index.html。

表格列與提示詞全文一律由程式碼取得（DOC_TYPES / ROUTER_PROMPT / build_extract_prompt），
避免手抄 48 格欄位或提示詞走樣。
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.core import doc_router, doc_types as dt
from app.services.classify import CLASSIFY_PROMPT
from app.services.importdecl import IMPORT_CLASSIFY_PROMPT

HTML = pathlib.Path(pathlib.Path(__file__).resolve().parent.parent / "docs/manual/index.html")
KIND = {"text": "字串", "number": "數字", "date": "日期"}


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def field_rows(key, numbered=False):
    out = []
    for f in dt.DOC_TYPES[key]["fields"]:
        label, num = f["label"], ""
        if numbered:
            m = re.search(r"\((\d+)\)$", label)
            if m:
                num, label = m.group(1), label[:m.start()]
        cells = (f"<td>{num}</td>" if numbered else "") + \
                f"<td><code>{f['key']}</code></td><td>{esc(label)}</td><td>{KIND[f['kind']]}</td>"
        out.append("      <tr>" + cells + "</tr>")
    return "\n".join(out)


# ── agent 列表 ──
GROUP = {
    "invoice": "稅務核心", "receipt": "稅務核心", "tax_bill": "稅務核心",
    "export_declaration": "進出口", "import_declaration": "進出口", "declaration": "進出口",
    "customs_tax": "進出口", "broker_note": "進出口", "fda_import": "進出口",
    "cheque": "金流", "remittance": "金流",
    "utility": "其他", "purchase_order": "其他", "statement": "其他",
    "journal_voucher": "其他", "bill_detail": "其他", "social_insurance": "其他", "other": "其他",
}
TASK = {
    "invoice": "台灣統一發票（三聯式／二聯式／電子發票證明聯／收銀機）。課稅別看勾選；統編 8 位純數字、電話不得填入；賣方統編常在「統一發票專用章」內，漏抓才觸發印章聚焦補讀；電子發票 QR 解出的值為權威值，直接覆蓋。",
    "receipt": "免用統一發票收據、一般收據、勞務報酬單、代收轉付。未必有統編或營業稅；有扣繳才填 tax_amount。",
    "tax_bill": "營所稅結算自行繳納、營業稅等繳款書。稅目代碼（如 357）、銷帳編號在條碼下方；一頁三聯內容相同取其一。",
    "export_declaration": "海關出口報單，依官方 48 格編號逐格定位。direction 固定「出口」；32~41 為貨物明細第一項。",
    "import_declaration": "海關進口報單。納稅義務人＝進口商、賣方＝國外出口商；完稅價格以 CIF 為基礎；稅費分項擷取後加總。逐項貨物入 items。",
    "customs_tax": "海關進口（快遞）貨物稅費繳納證明。一頁三聯取其一不跨聯混填；報單號碼可與進口報單勾稽。",
    "broker_note": "報關行收費通知單／費用明細。代墊稅費（關稅/滯報費）與勞務費（報關手續費）分開擷取，逐項費別入 items。",
    "fda_import": "食藥署食品及相關產品輸入許可通知。報驗義務人＝進口商；報單號碼及項次可與進口報單勾稽。",
    "declaration": "無法判定進出口時的報單通用契約，由模型填 direction。",
    "cheque": "支票／本票。金額以票面國字大寫為準轉數字；同頁若含信封或其他單據只取支票本身。",
    "remittance": "銀行匯款單／網銀轉帳水單。扣款金額＝交易金額＋手續費（內扣時相同）。",
    "utility": "台水／台電等公用事業帳單。114/2 起有統編用戶水費為含稅價，另列未稅小計與營業稅。",
    "purchase_order": "訂購單／採購單。採購方與供應商雙方統編分別擷取；明細入 items。",
    "statement": "對帳單／應收（應付）帳款明細表。表內「發票號碼」只是勾稽用，放 ref_invoice_no 而非憑證號碼——這類表格常被誤判為發票。統編沒印出就留空不得推測；明細數量／單價留白不可拿金額填補。",
    "journal_voucher": "會計傳票（記帳憑證）：記帳人員依原始憑證編製的內部單據，明細本身就是借貸分錄，故填入 entries 而非 items。借貸方向以 OCR 座標判定（VLM 判欄位歸屬不可靠）；立沖帳目為往來對象代號。也是測試集標準答案的來源。",
    "social_insurance": "勞健保費繳款單／投保單位繳費證明。**保險費免稅，不得拆進項稅額**。表格依費用代號分列（勞保普通事故／職災／就業保險／積欠工資墊償）放入 items。單位負擔與員工自付的拆分需對照薪資資料，繳費證明上查不到就不拆，於摘要提示人工分攤。",
    "bill_detail": "帳單費用明細附頁（如中華電信費用明細清單第 3 頁）。**佐證文件，不獨立產生分錄**——入帳依據是同批的發票或繳費憑證，每頁各自產分錄會讓金額嚴重灌水（實測該案正解 3,532、逐頁加總後 646 萬）。刻意不含「繳費結果通知」：該文件印有繳費總金額與扣款帳戶，本身就是入帳依據。",
    "other": "未歸類憑證的通用契約：買賣方、未稅／稅額／總計。屬「雜項附件」。",
}
ORDER = ["invoice", "receipt", "tax_bill", "export_declaration", "import_declaration",
         "customs_tax", "broker_note", "fda_import", "declaration",
         "cheque", "remittance", "utility", "purchase_order", "statement", "journal_voucher", "bill_detail", "social_insurance", "other"]

rows, prev = [], None
for k in ORDER:
    v = dt.DOC_TYPES[k]
    g = GROUP[k]
    span = ""
    if g != prev:
        n = sum(1 for x in ORDER if GROUP[x] == g)
        span = f'<td rowspan="{n}">{g}</td>'
        prev = g
    box = "OCR 精修" if k in dt.NO_VLM_BOX_TYPES else "VLM+OCR"
    items = "✔" if v.get("has_items") else "—"
    rows.append(f'      <tr>{span}<td>{esc(v["label"])}<br><code>{k}</code></td>'
                f'<td>{esc(TASK[k])}</td><td>{len(v["fields"])}</td><td>{items}</td><td>{box}</td></tr>')
AGENT_ROWS = "\n".join(rows)

SECTION = f"""
  <!-- ░░░ AGENTS ░░░ -->
  <section class="doc" id="agents">
    <h2><span class="ic">🤖</span>辨識 Agent 一覽</h2>
    <p class="lead">一個 agent ＝ <code>doc_types.DOC_TYPES</code> 的一筆登錄。同一筆定義同時驅動三件事：
    <strong>①</strong> 專屬辨識提示詞（依欄位契約自動生成）<strong>②</strong> 萃取回傳的欄位契約 <code>fields</code>
    <strong>③</strong> 前端審核 UI 的動態欄位。新增類型只要加一筆，三處自動同步。</p>

    <h3>Agent Workflow</h3>
    <p>拆為兩張橫式圖，便於直接貼入簡報：<strong>圖 3a</strong> 是主幹（誰接手誰），
    <strong>圖 3b</strong> 是內容辨識與後處理的內部細節。</p>
    <div class="diagram">
      <div class="mermaid">
flowchart LR
  IMG["頁圖<br/>（已轉正）"] --> R["Router<br/>判類型"]
  R --> CONF{{"人工<br/>確認類型"}}
  CONF --> EX["專屬 agent<br/>萃取欄位"]
  EX --> POST["後處理鏈<br/>（圖 3b）"]
  POST --> DB[("待審<br/>pending")]
  DB --> C["分類 agent<br/>產生分錄"]
      </div>
      <div class="diagram-cap">圖 3a — 主流程：Router 先判類型，<strong>人工確認後</strong>才交給該類型的專屬 agent 萃取欄位</div>
    </div>
    <div class="diagram">
      <div class="mermaid">
flowchart TB
  subgraph EX["內容辨識 extract_for_type()"]
    direction LR
    O["OCR<br/>文字＋精準框"] --> Q["條碼／QR<br/>解碼"] --> V["VLM<br/>專屬提示詞＋OCR grounding"]
  end
  subgraph POST["後處理鏈：依序修補 VLM 的已知弱點"]
    direction LR
    F1["JSON<br/>修復"] --> F2["印章<br/>補讀統編"] --> F3["QR 值<br/>覆蓋"] --> F4["框精修<br/>只留 OCR<br/>驗證過"] --> F5["清洗<br/>統編／民國年<br/>／髒值"]
  end
  EX --> POST
      </div>
      <div class="diagram-cap">圖 3b — 內容辨識與後處理細節。後處理五步各對應一個實測過的 VLM 弱點：JSON 格式瑕疵、印章內統編漏抓、QR 值較可信、框座標不可靠、格式髒值</div>
    </div>

    <h3>Agent 列表</h3>
    <p>目前 <strong>17 種專屬 agent + 1 種通用</strong>（<code>other</code>）。「出框策略」欄見下方密集表單說明。</p>
    <table>
      <tr><th>分類</th><th>Agent（key）</th><th>任務與專屬規則</th><th>欄位數</th><th>明細列</th><th>出框策略</th></tr>
{AGENT_ROWS}
    </table>
    <div class="callout note">
      <div class="ct">💡 出框策略：為何密集表單不讓 VLM 出框</div>
      進出口報單、稅單、報關行收費單等多欄多聯表單（<code>NO_VLM_BOX_TYPES</code>）的提示詞<strong>不要求 VLM 輸出 boxes</strong>——
      密集版面下 VLM 常把 <code>[x,y,x,y]</code> 座標誤塞進欄位值。這些類型的框改由 OCR 行框精修取得，
      對不上 OCR 的欄位<strong>寧可不給框</strong>（<code>keep_unmatched=False</code>）。
    </div>

    <h3>提示詞</h3>
    <h4>① Router 分類提示詞（<code>doc_router.ROUTER_PROMPT</code>）</h4>
    <p>類別清單由 <code>DOC_TYPES</code> 自動生成，新增類型即自動出現在選單。</p>
    <div class="codeblock"><span class="lang">prompt</span><pre>{esc(doc_router.ROUTER_PROMPT)}</pre></div>

    <h4>② 專屬萃取提示詞（<code>doc_types.build_extract_prompt(type_key)</code>）</h4>
    <p>依該類型的欄位契約自動生成，非逐類手寫。以下為 <code>invoice</code> 的實際輸出：</p>
    <div class="codeblock"><span class="lang">prompt</span><pre>{esc(dt.build_extract_prompt("invoice"))}</pre></div>
    <p>密集表單（如 <code>export_declaration</code>）少掉 <code>boxes</code> 鍵與對應規則，其餘結構相同；
    每個類型的最後一段 <code>extra</code> 即該 agent 的專屬領域知識（三聯取一、民國年、印章位置、含稅價規則等）。</p>

    <h4>③ 印章聚焦補讀（<code>pipeline._read_seal_taxid</code>）</h4>
    <p>僅 <code>invoice</code>／<code>receipt</code> 且賣方統編漏抓時才觸發的第二次 VLM 呼叫——不裁切、不靠顏色，
    以單一目的提示讓模型專注於印章。</p>
    <div class="codeblock"><span class="lang">prompt</span><pre>這是一張台灣統一發票。請只做一件事：找出『統一發票專用章』（印章顏色可能是紅、藍、黑或紫，通常蓋在發票下方或右側），讀出章內的賣方公司名稱與 8 位統一編號。只回 JSON：{{"seller_name":"","seller_tax_id":""}}；統一編號必須是 8 位純數字（電話不算），讀不到就留空字串。</pre></div>

    <h4>④ 科目分類提示詞（<code>classify.CLASSIFY_PROMPT</code>）</h4>
    <div class="codeblock"><span class="lang">prompt</span><pre>{esc(CLASSIFY_PROMPT)}</pre></div>
    <p>進口報單走專用分支（<code>importdecl.IMPORT_CLASSIFY_PROMPT</code>），因其無一般發票的買賣方統編、且稅費結構不同：</p>
    <div class="codeblock"><span class="lang">prompt</span><pre>{esc(IMPORT_CLASSIFY_PROMPT)}</pre></div>

    <h3>Declaration 欄位契約</h3>
    <p>報單是本系統欄位最多、也最要求「依編號定位」的類型，故單獨列出完整契約。
    共三個 agent：<code>export_declaration</code>（出口）、<code>import_declaration</code>（進口）、
    <code>declaration</code>（無法判定進出口時的後備）。</p>

    <h4>出口報單 <code>export_declaration</code>（官方 48 格，依真實文件核對）</h4>
    <p>欄位標題旁的括號編號即報單上印出的格號；<code>direction</code> 固定「出口」，由類型直接判定不再問模型。
    欄位 32~41 為貨物明細第一項；多項貨物仍以第一項填入。</p>
    <table>
      <tr><th>格號</th><th>key</th><th>欄位</th><th>型別</th></tr>
{field_rows("export_declaration", numbered=True)}
    </table>

    <h4>進口報單 <code>import_declaration</code>（33 欄 + items）</h4>
    <p>本契約未逐一標號（真本編號待客戶提供正式文件核對），提示詞要求模型「以表單上實際印出的編號與標題為準」定位。
    逐項貨物明細放入 <code>items</code>。</p>
    <table>
      <tr><th>key</th><th>欄位</th><th>型別</th></tr>
{field_rows("import_declaration")}
    </table>
    <div class="codeblock"><span class="lang">json</span><pre>"items": [{{"description": "貨物名稱及規格", "hs_code": "貨品分類號列 C.C.C.Code",
            "quantity": 0, "unit_price": 0, "amount": 0, "origin_country": "生產國別"}}]</pre></div>

    <h4>後備 <code>declaration</code>（未分進出口）</h4>
    <table>
      <tr><th>key</th><th>欄位</th><th>型別</th></tr>
{field_rows("declaration")}
    </table>

    <div class="callout warn">
      <div class="ct">⚠️ 報單辨識的三個坑</div>
      <ul>
        <li><strong>編號不是值</strong>：欄位標題後括號內的數字是格號，提示詞明文禁止把編號或座標當欄位值輸出；
        另有 <code>_clean_fields</code> 清洗陣列型髒值。</li>
        <li><strong>條碼靠不住</strong>：海關報單條碼為 Code39，掃描解析度不足多半讀不出，報單主要靠專屬提示詞而非條碼。</li>
        <li><strong>進口報單會被鎖定</strong>：<code>importdecl.is_import()</code> 判定後套用專用分錄規則，
        且不做一般統編核對（賣方為國外供應商）。</li>
      </ul>
    </div>
  </section>
"""

s = HTML.read_text(encoding="utf-8")
anchor = "  <!-- ░░░ THIRD-PARTY ░░░ -->"
assert s.count(anchor) == 1
assert 'id="agents"' not in s, "章節已存在，請先移除舊版再重建"
HTML.write_text(s.replace(anchor, SECTION.lstrip("\n") + "\n" + anchor), encoding="utf-8")
print("inserted #agents section")
