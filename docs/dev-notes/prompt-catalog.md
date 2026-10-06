# 提示詞完整清單與原文快照

> **目前架構索引（2026-10-06 改版）**：正式流程使用 `ROUTE_TEMPLATE`、`EXTRACT_TEMPLATE`、`ACCOUNTING_TEMPLATE`，定義於 [prompts.py](../../app/core/prompts.py)；路由及萃取分別由 `build_route_prompt(mode='image', ocr_text='', hint='')`、`build_extract_prompt(type_key, mode='image', ocr_text='', hint='', fields=None)` 組裝。[18 類 DOC_TYPES 契約](../../app/core/doc_types.py) 以 `type_hints` 提供類型差異，包含 `receipt.withholding_tax` 及貨物明細 `hs_code`／`origin_country`。欄位補讀共用兩語意輪、每輪最多 3 次傳輸嘗試；QR 多來源衝突保留警告。會計政策、範例篩選及設定格式見 [會計政策](accounting-policy.md)。
>
> **改版前基準（保留原始內容）**：以下第 1 節起的 43 份提示快照與第 5 節 10 個動態片段，均為本輪重構前的 2026-10-06 程式基準。保留其完整文字、舊來源雜湊與盤點結果，供變更對照；不得當作現行提示詞、契約或執行行為。舊 43 份展開快照不等於現行有 43 套模板。
>
> 原快照使用的動態值以 `{{...}}` 表示，不含私有掃描、歷史分錄、種子或人工備註實際內容。變更前的分析見 [提示詞盤點與架構分析](prompt-review.md)；現行實作說明見 [目前系統架構](architecture.md)。

## 1. 改版前基準：範圍與來源

正式流程 23 份、OCR 純文字驗證變體 19 份、未使用歷史模板 1 份，共 43 份展開後快照。共用模板不算獨立模型工作；動態組合片段另列第 5 節。生成僅執行純類型登錄與模板生成器，其餘以 AST 擷取字串，不呼叫模型或讀取執行期資料。

| 原始檔案 | SHA-256 |
|---|---|
| [app/core/doc_router.py](../../app/core/doc_router.py) | `d5ffcc36f0f9acffc9c182212b7c36bc10596ff20a299c42d8b601324befb569` |
| [app/core/doc_types.py](../../app/core/doc_types.py) | `bc6145355372e29f7e2241863171faeb78077bec7fe05ef46740803fda5bef85` |
| [app/core/pipeline.py](../../app/core/pipeline.py) | `7886b5535fe716fcc4bbc74e258371846af53c5f788a57b19e1949611f5a426f` |
| [app/services/classify.py](../../app/services/classify.py) | `68971b7930533797b9af105462180dc42bd9272fe73d53699129b9770019ce56` |
| [app/services/importdecl.py](../../app/services/importdecl.py) | `6c0bb0306fca5c66ae826550d8e9b112add258a90a3d4fa8ec46459190e19ca8` |
| [app/validation.py](../../app/validation.py) | `3002c642662be4511707ace8c40ddb792dc891b6acfd539f1878827a668e327d` |
| [app/models/schemas.py](../../app/models/schemas.py) | `29f31de5f6897a3f8ecab60ba69d7b09b758dd1ad416be4d476ae3910dadb255` |
| [app/services/accounts.py](../../app/services/accounts.py) | `38f76028a4904c7c642348147cac7047cd943b92d876f5a37e92246b15e6fac8` |

## 2. 正式流程的固定指令

### 2.1 Router（含當前 18 類選單）

~~~~text
你是單據分類器。判斷這張掃描件：
1) 屬於下列哪一類（回代碼，如 cheque）
2) 需『順時針』旋轉幾度才會正立（僅能是 0、90、180、270）
只回 JSON：{"type":"代碼","rotate":0}，不要其他文字。
以**標題**為準，不要只看到「發票號碼」「營業稅額」就判成發票：
- 標題寫「應收帳款明細表／應付帳款明細表／對帳單／月結單」→ statement（這類表格內的發票號碼只是勾稽用，本身不是發票）
- 標題寫「轉帳傳票／收入傳票／支出傳票」，表格有「會計科目・借方金額・貸方金額」欄→ journal_voucher（這是記帳憑證，不是發票收據）
- bill_detail **只用於帳單的費用明細附頁**：標題明確寫「費用明細清單／費用明細」，內容是逐項費用的清單。
  以下一律**不是** bill_detail，它們本身就是入帳依據：印有「統一發票／電子發票證明聯」（→ invoice）、「繳費結果通知／繳費通知」且載明繳費總金額（→ utility）、勞健保台電的「繳費證明／繳款單」（→ tax_bill 或 utility）。
- 勞保局／健保署／勞退的保險費繳款單、投保單位繳費證明書 → social_insurance
- 標題寫「訂購單／採購單」→ purchase_order；報關行的「收費通知單」→ broker_note
- 真正的發票（invoice）標題為「統一發票」「電子發票證明聯」或收銀機發票格式

類別：
- invoice：統一發票／電子發票
- cheque：支票
- remittance：匯款單／銀行轉帳水單
- customs_tax：海關進口貨物稅費繳納證明
- export_declaration：出口報單
- import_declaration：進口報單
- broker_note：報關行收費通知單
- declaration：進出口報單（未分進出口）
- tax_bill：稅款繳款書
- utility：水電費／公用事業帳單
- receipt：收據／雜項開銷
- statement：對帳單／應收帳款明細表
- purchase_order：訂購單／採購單
- fda_import：食品輸入許可通知
- social_insurance：勞健保費繳款／繳費證明
- bill_detail：帳單費用明細（附頁佐證）
- journal_voucher：會計傳票（記帳憑證）
- other：其他憑證
~~~~

### 2.2 印章補讀

~~~~text
這是一張台灣統一發票。請只做一件事：找出『統一發票專用章』（印章顏色可能是紅、藍、黑或紫，通常蓋在發票下方或右側），讀出章內的賣方公司名稱與 8 位統一編號。只回 JSON：{"seller_name":"","seller_tax_id":""}；統一編號必須是 8 位純數字（電話不算），讀不到就留空字串。
~~~~

### 2.3 JSON 修正

~~~~text
你剛才的輸出不是合法 JSON。請「只」輸出修正後的合法 JSON 物件（不要任何說明文字、不要 markdown code fence、鍵與字串一律用雙引號、不要尾逗號）：

{{RAW_RESPONSE_FIRST_4000_CHARACTERS}}
~~~~

### 2.4 一般會計科目分類

~~~~text
你是台灣會計師。依據下列憑證資訊，產生借貸分錄（雙式簿記）。

規則：
- **每一筆的 amount 必須填入實際金額**（取自憑證的未稅／稅額／總計），不得留 0。
- 一筆分錄只填借方或只填貸方，另一側留空字串。
- **借方費用科目要選最貼切的那一個**：科目表裡有「運費」就不要用「其他費用」、
  有「保險費」就不要用「稅捐」。只有在科目表中確實找不到對應科目時，
  才使用「其他費用」「雜項購置」這類概括科目。
- **貸方看是否已付款**：憑證顯示已由銀行扣繳／已付款 → 用「銀行存款」；
  以現金支付 → 用「現金」；尚未付款的**費用**（水電、運費、勞務等）→ 用「應付費用」；
  尚未付款的**進貨／商品**才用「應付帳款」。兩者不同，勿一律填應付帳款。
- 一張憑證含多種性質的費用時（如影印機月租＋碳粉耗材），**分別列為不同科目**，
  不要合併成一筆。
- 含營業稅時，進項／銷項稅額**獨立列一筆**（金額＝稅額），費用或收入用未稅金額。
- **憑證上沒有稅額就不要生出進項稅額**。保險費（商業保險、勞保、健保、勞退）、
  規費、稅捐、薪資、國外勞務等屬免稅或不得扣抵，這類憑證只有一個總額，
  借方直接用該總額，不可自行拆成「未稅＋稅額」。
- 勞健保費繳款單：借方用「保險費」全額。單位負擔與員工自付的拆分需對照薪資資料，
  **繳費證明上查不到就不要拆**，於摘要註明「單位／個人負擔待人工分攤」。
- 借方金額合計必須等於貸方金額合計。
- 摘要寫憑證上的品名／事由，不要寫「摘要」這類佔位字。

範例（進貨含稅 2,830：未稅 2,695、稅額 135）：
{"entries":[
 {"description":"倉租及理貨費","amount":2695,"debit_account":"6020","debit_account_name":"租金支出","credit_account":"","credit_account_name":""},
 {"description":"進項稅額","amount":135,"debit_account":"1290","debit_account_name":"進項稅額","credit_account":"","credit_account_name":""},
 {"description":"應付詮瑞福物流","amount":2830,"debit_account":"","debit_account_name":"","credit_account":"2130","credit_account_name":"應付帳款"}
]}

僅回 JSON，不要多餘文字。
~~~~

### 2.5 進口會計科目分類

~~~~text
你是台灣會計師，處理「進口報單／進口關稅」之會計分錄（雙式簿記）。
請依本客戶的既有記帳慣例，**不要**改用其他教科書作法：

- **進口稅費一律借記「1210 預付費用」（資產科目），不是進貨成本、不是營業費用**。
  包含：進口稅（關稅）、推廣貿易服務費、船公司費／吊櫃費、FDA 或商檢等規費。
  每一項費用各開一筆分錄（不要合併成一筆），摘要註明是哪一項費用。
  （客戶 ERP 原以「1254 預付進口款項」記帳，事務所科目表無此科目，故對映為 1210。）
- **海關代徵之營業稅**單獨借記「1290 進項稅額」。
- **銀行匯費／手續費**借記「6230 其他費用」（事務所科目表無「手續費」科目）。
- **最後必須有一筆貸方分錄**：借方各筆的合計，貸記實際付款的「1110 銀行存款」
  （尚未付款則為「2130 應付帳款」）。缺了這筆借貸就不平衡，等於分錄不可用。
- 進口報單無一般統一發票之買賣方統編，賣方視為國外供應商，不做統編核對。
- **金額一律直接取自憑證欄位，不得自行拆分、加總或估算**（例：進口稅 200,028
  就是一筆 200,028，不可拆成 150,000 + 50,428）。
- 摘要請寫上報單號碼／貨名等可追溯資訊；憑證上沒有的資訊就省略，不要填
  「[請填寫]」這類佔位字串。

僅回 JSON（不要多餘文字）。每筆只填借方或只填貸方，另一側留空字串：
{"entries":[
  {"description":"進口稅 報單CUI...","amount":200028,"debit_account":"1210","debit_account_name":"預付費用","credit_account":"","credit_account_name":""},
  {"description":"海關代徵營業稅","amount":60008,"debit_account":"1290","debit_account_name":"進項稅額","credit_account":"","credit_account_name":""},
  {"description":"支付進口稅費","amount":260436,"debit_account":"","debit_account_name":"","credit_account":"1110","credit_account_name":"銀行存款"}
]}
借方合計必須等於貸方合計。
~~~~

## 3. 正式流程的十八種類型擷取提示詞

### 3.1 `invoice`：統一發票／電子發票

欄位數：13；明細：items；VLM 框：要求。

| key | 標籤 | kind |
|---|---|---|
| `voucher_id` | 發票號碼 | `text` |
| `date` | 發票日期 | `date` |
| `invoice_type` | 發票格式（三聯式／二聯式／電子發票／收銀機） | `text` |
| `tax_type` | 課稅別（應稅／零稅率／免稅） | `text` |
| `seller_name` | 賣方名稱 | `text` |
| `seller_tax_id` | 賣方統編 | `text` |
| `buyer_name` | 買方名稱 | `text` |
| `buyer_tax_id` | 買方統編 | `text` |
| `subtotal` | 未稅銷售額 | `number` |
| `tax_amount` | 營業稅額 | `number` |
| `total_amount` | 總計（含稅） | `number` |
| `amount_words` | 總計金額中文大寫（如有） | `text` |
| `usage_note` | 買受人註記（進貨及費用／固定資產） | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「統一發票／電子發票」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "統一發票／電子發票",
  "fields": { voucher_id（發票號碼）、date（發票日期）、invoice_type（發票格式（三聯式／二聯式／電子發票／收銀機））、tax_type（課稅別（應稅／零稅率／免稅））、seller_name（賣方名稱）、seller_tax_id（賣方統編）、buyer_name（買方名稱）、buyer_tax_id（買方統編）、subtotal（未稅銷售額）、tax_amount（營業稅額）、total_amount（總計（含稅））、amount_words（總計金額中文大寫（如有））、usage_note（買受人註記（進貨及費用／固定資產）） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low",
  "boxes": { "voucher_id": [x1,y1,x2,y2], "date": [x1,y1,x2,y2], "invoice_type": [x1,y1,x2,y2], "tax_type": [x1,y1,x2,y2], "seller_name": [x1,y1,x2,y2], "seller_tax_id": [x1,y1,x2,y2], "buyer_name": [x1,y1,x2,y2], "buyer_tax_id": [x1,y1,x2,y2], "subtotal": [x1,y1,x2,y2], "tax_amount": [x1,y1,x2,y2], "total_amount": [x1,y1,x2,y2], "amount_words": [x1,y1,x2,y2], "usage_note": [x1,y1,x2,y2] }
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- boxes 為各欄位在影像上的邊界框，座標正規化 0~1000 整數（左上為原點）；找不到的欄位省略該鍵。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為台灣統一發票（三聯式／二聯式／電子發票證明聯／收銀機發票）。統一編號為 8 位純數字；電話號碼（含 - 或括號、位數非 8）不是統編，切勿填入統編欄，寧可留空。課稅別看勾選（✓）填『應稅』『零稅率』或『免稅』其一。營業稅額通常＝未稅銷售額×5%，仍以圖為準。買方多為仲彬股份有限公司(統編23051677)、中意企業股份有限公司(31182666)、天干；賣方為開立發票之廠商。賣方統編常蓋在『統一發票專用章』內——印章顏色可能為紅、藍、黑或紫，通常位於發票下方或右側，章內含賣方公司名、8 位統一編號與負責人；請務必從印章內讀出賣方統編填入 seller_tax_id。三聯式買受人註記欄若勾『固定資產』填『固定資產』，否則填『進貨及費用』。**金額自我校驗（手寫三聯式尤其重要）**：① 手寫發票下方多有『總計新台幣（中文大寫）』，請讀入 amount_words；大寫字（壹貳參肆伍陸柒捌玖拾佰仟萬）比手寫阿拉伯數字好辨認，**大寫與阿拉伯數字不一致時，以大寫為準**回填 total_amount。② 應稅發票應滿足『未稅銷售額 ＋ 營業稅額 ＝ 總計』；若三個數字兜不攏，請回到圖上重新核對（別直接輸出矛盾的數字）。③ 欄位在單據上空白就留空，**不要用其他欄位回推或編造**（金額欄尤其嚴格：寧可留空給人工填，也不要給錯的數字）。
~~~~

### 3.2 `cheque`：支票

欄位數：9；明細：無；VLM 框：要求。

| key | 標籤 | kind |
|---|---|---|
| `cheque_no` | 票號 | `text` |
| `issue_date` | 發票日 | `date` |
| `due_date` | 到期（票載）日 | `date` |
| `drawer` | 發票人／出票人 | `text` |
| `payer_bank` | 付款銀行 | `text` |
| `payee` | 受款人／抬頭 | `text` |
| `account_no` | 帳號 | `text` |
| `total_amount` | 金額 | `number` |
| `amount_words` | 金額大寫 | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「支票」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "支票",
  "fields": { cheque_no（票號）、issue_date（發票日）、due_date（到期（票載）日）、drawer（發票人／出票人）、payer_bank（付款銀行）、payee（受款人／抬頭）、account_no（帳號）、total_amount（金額）、amount_words（金額大寫） },
  "note": "摘要／備註",
  "confidence": "high|medium|low",
  "boxes": { "cheque_no": [x1,y1,x2,y2], "issue_date": [x1,y1,x2,y2], "due_date": [x1,y1,x2,y2], "drawer": [x1,y1,x2,y2], "payer_bank": [x1,y1,x2,y2], "payee": [x1,y1,x2,y2], "account_no": [x1,y1,x2,y2], "total_amount": [x1,y1,x2,y2], "amount_words": [x1,y1,x2,y2] }
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- boxes 為各欄位在影像上的邊界框，座標正規化 0~1000 整數（左上為原點）；找不到的欄位省略該鍵。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為支票／本票。金額以票面『國字大寫』為準轉為數字（如玖仟肆佰貳拾貳圓整＝9422）；票號多在右上角（如 TD3642163），下方 MICR 磁碼含票號/帳號/行庫代號。受款人（抬頭）為票據抬頭之公司；發票人為出票公司。日期為民國年，請轉西元。注意：掃描頁若同時含信封、傳票或其他單據，只擷取『支票本身』的內容，不要混入其他單據。
~~~~

### 3.3 `remittance`：匯款單／銀行轉帳水單

欄位數：14；明細：無；VLM 框：要求。

| key | 標籤 | kind |
|---|---|---|
| `date` | 交易日期 | `date` |
| `ref_no` | 交易序號／建檔批號 | `text` |
| `remitter` | 付款人戶名 | `text` |
| `payer_account` | 付款帳號 | `text` |
| `remitter_bank` | 付款／匯出銀行 | `text` |
| `payment_method` | 付款方式 | `text` |
| `total_amount` | 交易（匯款）金額 | `number` |
| `debit_amount` | 扣款金額 | `number` |
| `fee` | 手續費 | `number` |
| `currency` | 幣別 | `text` |
| `payee` | 收款人戶名 | `text` |
| `payee_bank` | 收款銀行 | `text` |
| `account_no` | 收款帳號 | `text` |
| `payee_code` | 收款人代號 | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「匯款單／銀行轉帳水單」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "匯款單／銀行轉帳水單",
  "fields": { date（交易日期）、ref_no（交易序號／建檔批號）、remitter（付款人戶名）、payer_account（付款帳號）、remitter_bank（付款／匯出銀行）、payment_method（付款方式）、total_amount（交易（匯款）金額）、debit_amount（扣款金額）、fee（手續費）、currency（幣別）、payee（收款人戶名）、payee_bank（收款銀行）、account_no（收款帳號）、payee_code（收款人代號） },
  "note": "摘要／備註",
  "confidence": "high|medium|low",
  "boxes": { "date": [x1,y1,x2,y2], "ref_no": [x1,y1,x2,y2], "remitter": [x1,y1,x2,y2], "payer_account": [x1,y1,x2,y2], "remitter_bank": [x1,y1,x2,y2], "payment_method": [x1,y1,x2,y2], "total_amount": [x1,y1,x2,y2], "debit_amount": [x1,y1,x2,y2], "fee": [x1,y1,x2,y2], "currency": [x1,y1,x2,y2], "payee": [x1,y1,x2,y2], "payee_bank": [x1,y1,x2,y2], "account_no": [x1,y1,x2,y2], "payee_code": [x1,y1,x2,y2] }
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- boxes 為各欄位在影像上的邊界框，座標正規化 0~1000 整數（左上為原點）；找不到的欄位省略該鍵。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為銀行匯款單／網路銀行轉帳水單。交易（匯款）金額為本金；扣款金額＝交易金額＋手續費（手續費若『內扣』則兩者相同）。付款人多為本公司（仲彬／中意／天干），收款人為受款廠商。收款銀行常以代碼＋名稱呈現（如 007 第一銀行）。
~~~~

### 3.4 `customs_tax`：海關進口貨物稅費繳納證明

欄位數：18；明細：無；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `tax_bill_no` | 稅單號碼 | `text` |
| `taxpayer` | 納稅義務人 | `text` |
| `tax_id` | 統一編號 | `text` |
| `import_date` | 貨物進口日期 | `date` |
| `ship_name` | 船（機）名及呼號（班次） | `text` |
| `broker_box` | 報關行箱號 | `text` |
| `bl_no` | 提裝貨單號 | `text` |
| `declaration_no` | 報單號碼 | `text` |
| `goods_no` | 貨名／稅則號列 | `text` |
| `qty` | 件數 | `text` |
| `courier` | 快遞業者／列印者代號 | `text` |
| `payment_date` | 填發（繳款）日期 | `date` |
| `duty` | 進口稅 | `number` |
| `trade_fee` | 推廣貿易服務費 | `number` |
| `business_tax` | 營業稅 | `number` |
| `total_amount` | 稅費合計 | `number` |
| `business_tax_base` | 營業稅稅基 | `number` |
| `issuer` | 核發機關 | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「海關進口貨物稅費繳納證明」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "海關進口貨物稅費繳納證明",
  "fields": { tax_bill_no（稅單號碼）、taxpayer（納稅義務人）、tax_id（統一編號）、import_date（貨物進口日期）、ship_name（船（機）名及呼號（班次））、broker_box（報關行箱號）、bl_no（提裝貨單號）、declaration_no（報單號碼）、goods_no（貨名／稅則號列）、qty（件數）、courier（快遞業者／列印者代號）、payment_date（填發（繳款）日期）、duty（進口稅）、trade_fee（推廣貿易服務費）、business_tax（營業稅）、total_amount（稅費合計）、business_tax_base（營業稅稅基）、issuer（核發機關） },
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為「海關進口（快遞）貨物稅費繳納證明」。一頁常有三聯（第一聯收據聯／第二聯扣抵聯／第三聯副本），內容相同，取任一聯完整填入即可，不要跨聯混填。右側稅費項目表含進口稅、推廣貿易服務費、營業稅、稅費合計、營業稅稅基，請逐項擷取為純數字。報單號碼可與進口報單勾稽。日期為民國年（如 1150524＝民國115年5月24日）。
~~~~

### 3.5 `export_declaration`：出口報單

欄位數：48；明細：無；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `transport_mode` | 海空運別(1) | `text` |
| `doc_category` | 報單類別(2) | `text` |
| `declaration_no` | 報單號碼(3) | `text` |
| `customs_clear_no` | 海關通關號碼(4) | `text` |
| `ship_code` | 出口船機代碼(5) | `text` |
| `voyage` | 船舶航次／船機班次(6) | `text` |
| `date` | 報關日期(7) | `date` |
| `loading_port` | 裝貨港名稱及代碼(8) | `text` |
| `dest_country` | 目的地國家及代碼(9) | `text` |
| `storage_code` | 卸存地點代碼(10) | `text` |
| `master_awb` | 託運單主號(11) | `text` |
| `house_awb` | 託運單分號(12) | `text` |
| `refund_material_tax` | 申請沖退原料稅(13) | `text` |
| `transport_method` | 運輸方式(14) | `text` |
| `ship_name` | 出口船舶名稱(15) | `text` |
| `invoice_amount` | 發票總金額(16) | `number` |
| `freight` | 運費(17) | `number` |
| `insurance` | 保險費(18) | `number` |
| `add_fee` | 應加費用(19) | `number` |
| `deduct_fee` | 應減費用(20) | `number` |
| `total_fob_twd` | 總離岸價格新臺幣(21) | `number` |
| `exporter` | 出口人名稱地址(22) | `text` |
| `exporter_tax_id` | 出口人統一編號(23) | `text` |
| `exporter_customs_no` | 出口人海關監管編號(24) | `text` |
| `payment_method` | 繳納方式代碼(25) | `text` |
| `buyer` | 買方名稱地址(26) | `text` |
| `buyer_country` | 買方國家代碼(27) | `text` |
| `buyer_tax_id` | 買方統一編號(28) | `text` |
| `buyer_customs_no` | 買方海關監管編號(29) | `text` |
| `price_term` | 單價條件代碼(30) | `text` |
| `exchange_rate` | 匯率(31) | `number` |
| `item_no` | 項次(32) | `text` |
| `goods_desc` | 貨物名稱商標(牌名)及規格(33) | `text` |
| `permit_no` | 輸出入許可文件號碼-項次(34) | `text` |
| `hs_code` | 輸出入貨品分類號列／統計號別／檢查號碼(35) | `text` |
| `unit_price` | 單價／幣別／金額(36) | `text` |
| `net_weight` | 淨重公斤(37) | `number` |
| `quantity` | 數量(單位)(38) | `text` |
| `stat_use` | 統計用(39) | `text` |
| `fob_value_twd` | 離岸價格FOB Value新臺幣(40) | `number` |
| `stat_method` | 統計方式(41) | `text` |
| `total_packages` | 總件數(單位)(42) | `text` |
| `packing_desc` | 包裝說明(43) | `text` |
| `gross_weight` | 總毛重公斤(44) | `number` |
| `marks` | 標記及貨櫃號碼(45) | `text` |
| `other_declared` | 其它申報事項(46) | `text` |
| `broker` | 報關人／AEO編號(47) | `text` |
| `specialist` | 專責人員(48) | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「出口報單」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "出口報單",
  "fields": { transport_mode（海空運別(1)）、doc_category（報單類別(2)）、declaration_no（報單號碼(3)）、customs_clear_no（海關通關號碼(4)）、ship_code（出口船機代碼(5)）、voyage（船舶航次／船機班次(6)）、date（報關日期(7)）、loading_port（裝貨港名稱及代碼(8)）、dest_country（目的地國家及代碼(9)）、storage_code（卸存地點代碼(10)）、master_awb（託運單主號(11)）、house_awb（託運單分號(12)）、refund_material_tax（申請沖退原料稅(13)）、transport_method（運輸方式(14)）、ship_name（出口船舶名稱(15)）、invoice_amount（發票總金額(16)）、freight（運費(17)）、insurance（保險費(18)）、add_fee（應加費用(19)）、deduct_fee（應減費用(20)）、total_fob_twd（總離岸價格新臺幣(21)）、exporter（出口人名稱地址(22)）、exporter_tax_id（出口人統一編號(23)）、exporter_customs_no（出口人海關監管編號(24)）、payment_method（繳納方式代碼(25)）、buyer（買方名稱地址(26)）、buyer_country（買方國家代碼(27)）、buyer_tax_id（買方統一編號(28)）、buyer_customs_no（買方海關監管編號(29)）、price_term（單價條件代碼(30)）、exchange_rate（匯率(31)）、item_no（項次(32)）、goods_desc（貨物名稱商標(牌名)及規格(33)）、permit_no（輸出入許可文件號碼-項次(34)）、hs_code（輸出入貨品分類號列／統計號別／檢查號碼(35)）、unit_price（單價／幣別／金額(36)）、net_weight（淨重公斤(37)）、quantity（數量(單位)(38)）、stat_use（統計用(39)）、fob_value_twd（離岸價格FOB Value新臺幣(40)）、stat_method（統計方式(41)）、total_packages（總件數(單位)(42)）、packing_desc（包裝說明(43)）、gross_weight（總毛重公斤(44)）、marks（標記及貨櫃號碼(45)）、other_declared（其它申報事項(46)）、broker（報關人／AEO編號(47)）、specialist（專責人員(48)） },
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為台灣海關「出口報單」，共 48 個欄位，標題旁均有括號編號，請「依編號」在對應格位定位擷取，避免張冠左右鄰格。direction 固定為「出口」。declaration_no 請含開頭英文別碼與完整編號（如 CX/ /15/696/D6341）。欄位 32~41 為貨物明細第一項的各欄；若有多項貨物，仍以第一項為主填入對應欄位。
~~~~

### 3.6 `import_declaration`：進口報單

欄位數：33；明細：items；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `doc_category` | 報單類別 | `text` |
| `declaration_no` | 報單號碼 | `text` |
| `customs_clear_no` | 海關通關號碼 | `text` |
| `date` | 報關日期 | `date` |
| `import_date` | 進口日期 | `date` |
| `ship_name` | 進口船（機）名及呼號（班次） | `text` |
| `bl_no` | 提單號碼 | `text` |
| `loading_port` | 裝貨港（代碼及名稱） | `text` |
| `origin_port` | 起運口岸（代碼及名稱） | `text` |
| `transport_method` | 運輸方式 | `text` |
| `taxpayer` | 納稅義務人名稱地址 | `text` |
| `taxpayer_tax_id` | 納稅義務人統一編號 | `text` |
| `customs_supervision` | 海關監管編號 | `text` |
| `seller` | 賣方名稱地址 | `text` |
| `seller_country` | 賣方國家代碼 | `text` |
| `tax_method` | 納稅辦法 | `text` |
| `permit_no` | 輸入許可證號碼 | `text` |
| `price_term` | 條件（起岸價格條件） | `text` |
| `currency` | 幣別 | `text` |
| `exchange_rate` | 匯率 | `number` |
| `invoice_amount` | 發票金額 | `number` |
| `freight` | 運費 | `number` |
| `insurance` | 保險費 | `number` |
| `total_cif_twd` | 完稅價格總計新臺幣 | `number` |
| `duty` | 進口稅 | `number` |
| `business_tax` | 營業稅 | `number` |
| `trade_fee` | 推廣貿易服務費 | `number` |
| `commodity_tax` | 貨物稅 | `number` |
| `total_tax` | 稅費合計 | `number` |
| `gross_weight` | 總毛重公斤 | `number` |
| `total_packages` | 總件數及單位 | `text` |
| `broker` | 報關人／AEO編號 | `text` |
| `specialist` | 專責人員 | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「進口報單」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "進口報單",
  "fields": { doc_category（報單類別）、declaration_no（報單號碼）、customs_clear_no（海關通關號碼）、date（報關日期）、import_date（進口日期）、ship_name（進口船（機）名及呼號（班次））、bl_no（提單號碼）、loading_port（裝貨港（代碼及名稱））、origin_port（起運口岸（代碼及名稱））、transport_method（運輸方式）、taxpayer（納稅義務人名稱地址）、taxpayer_tax_id（納稅義務人統一編號）、customs_supervision（海關監管編號）、seller（賣方名稱地址）、seller_country（賣方國家代碼）、tax_method（納稅辦法）、permit_no（輸入許可證號碼）、price_term（條件（起岸價格條件））、currency（幣別）、exchange_rate（匯率）、invoice_amount（發票金額）、freight（運費）、insurance（保險費）、total_cif_twd（完稅價格總計新臺幣）、duty（進口稅）、business_tax（營業稅）、trade_fee（推廣貿易服務費）、commodity_tax（貨物稅）、total_tax（稅費合計）、gross_weight（總毛重公斤）、total_packages（總件數及單位）、broker（報關人／AEO編號）、specialist（專責人員） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為台灣海關「進口報單」，各欄位標題旁有括號編號，請依標題文字定位並讀出其編號（本欄位契約未逐一標號，請以表單上實際印出的編號與標題為準）。direction 固定為「進口」。納稅義務人為進口商、賣方為國外出口商；完稅價格以 CIF 為基礎；稅費含進口稅、營業稅、推廣貿易服務費、貨物稅，請分別擷取並加總為稅費合計。逐項貨物明細放入 items：description=貨物名稱及規格、hs_code=貨品分類號列（C.C.C.Code）、quantity=數量、unit_price=單價、amount=完稅價格新臺幣、origin_country=生產國別。
~~~~

### 3.7 `broker_note`：報關行收費通知單

欄位數：19；明細：items；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `doc_no` | 單號 | `text` |
| `date` | 日期 | `date` |
| `broker` | 報關行名稱 | `text` |
| `client` | 委託人／客戶 | `text` |
| `declaration_no` | 報單號碼 | `text` |
| `bl_no` | B/L S/O NO | `text` |
| `customs_date` | 報關日期 | `date` |
| `vessel` | 船名 | `text` |
| `port` | 進出口岸 | `text` |
| `goods_name` | 貨名 | `text` |
| `qty` | 件數 | `text` |
| `customs_duty` | 關稅／滯報費 | `number` |
| `broker_fee` | 報關手續費 | `number` |
| `edi_fee` | 海關鍵輸費 | `number` |
| `subtotal` | 費用合計 | `number` |
| `tax` | 稅金 | `number` |
| `total_amount` | 總計 | `number` |
| `prepaid` | 預收費 | `number` |
| `balance` | 應收（退）金額 | `number` |

~~~~text
你是專業會計憑證辨識助手。這是一張「報關行收費通知單」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "報關行收費通知單",
  "fields": { doc_no（單號）、date（日期）、broker（報關行名稱）、client（委託人／客戶）、declaration_no（報單號碼）、bl_no（B/L S/O NO）、customs_date（報關日期）、vessel（船名）、port（進出口岸）、goods_name（貨名）、qty（件數）、customs_duty（關稅／滯報費）、broker_fee（報關手續費）、edi_fee（海關鍵輸費）、subtotal（費用合計）、tax（稅金）、total_amount（總計）、prepaid（預收費）、balance（應收（退）金額） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為報關行開立的『收費通知單／費用明細』（進出口報關費用）。左右兩欄列出各項費別與金額（關稅/滯報費、服務車資、驗對證費、理貨工資、報關手續費、海關鍵輸費等），請把每一有金額的費別放入 items：description=費別名稱、amount=金額。關稅/滯報費(customs_duty)為代墊稅費、報關手續費(broker_fee)為勞務費，請分別擷取為頂層欄位。下方有費用合計、稅金、總計、預收費、應收（退）金額。金額純數字、日期民國轉西元；報單號碼可與進口／出口報單勾稽；委託人多為本公司（仲彬／中意／天干）。
~~~~

### 3.8 `declaration`：進出口報單（未分進出口）

欄位數：9；明細：items；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `declaration_no` | 報單號碼 | `text` |
| `doc_category` | 報單類別 | `text` |
| `date` | 報關日期 | `date` |
| `direction` | 進出口別 | `text` |
| `trader` | 出口人／納稅義務人 | `text` |
| `tax_id` | 統一編號 | `text` |
| `counterparty` | 買方／賣方 | `text` |
| `total_amount` | 總離岸／完稅價格新臺幣 | `number` |
| `currency` | 幣別 | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「進出口報單（未分進出口）」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "進出口報單（未分進出口）",
  "fields": { declaration_no（報單號碼）、doc_category（報單類別）、date（報關日期）、direction（進出口別）、trader（出口人／納稅義務人）、tax_id（統一編號）、counterparty（買方／賣方）、total_amount（總離岸／完稅價格新臺幣）、currency（幣別） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 若能判斷是進口或出口報單，direction 請填「進口」或「出口」。
~~~~

### 3.9 `tax_bill`：稅款繳款書

欄位數：14；明細：無；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `tax_agency` | 徵收機關 | `text` |
| `tax_code` | 稅目代碼 | `text` |
| `tax_type` | 稅目名稱 | `text` |
| `period` | 所屬年度／期別 | `text` |
| `payment_no` | 銷帳編號 | `text` |
| `taxpayer` | 納稅義務人／營利事業名稱 | `text` |
| `tax_id` | 統一編號 | `text` |
| `responsible_person` | 負責人 | `text` |
| `principal` | 本稅 | `number` |
| `surcharge` | 滯納金／加計利息 | `number` |
| `total_amount` | 應繳金額合計 | `number` |
| `pay_start` | 繳納期間起 | `date` |
| `due_date` | 繳納期限止 | `date` |
| `phone` | 納稅義務人電話 | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「稅款繳款書」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "稅款繳款書",
  "fields": { tax_agency（徵收機關）、tax_code（稅目代碼）、tax_type（稅目名稱）、period（所屬年度／期別）、payment_no（銷帳編號）、taxpayer（納稅義務人／營利事業名稱）、tax_id（統一編號）、responsible_person（負責人）、principal（本稅）、surcharge（滯納金／加計利息）、total_amount（應繳金額合計）、pay_start（繳納期間起）、due_date（繳納期限止）、phone（納稅義務人電話） },
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為稅款繳款書（如營利事業所得稅結算自行繳納、營業稅等）。稅目代碼如 357（國稅）。銷帳編號通常在底部條碼下方（如 A110016601654155011）。一頁常含三聯（收據／證明／收款機構留存），內容相同取其一。金額純數字，日期民國年轉西元（如 1150531→2026-05-31）。
~~~~

### 3.10 `utility`：水電費／公用事業帳單

欄位數：17；明細：無；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `provider` | 事業機構 | `text` |
| `period` | 計費（用水／用電）期間 | `text` |
| `customer_name` | 用戶名稱 | `text` |
| `customer_tax_id` | 用戶統一編號 | `text` |
| `account` | 水號／電號／用戶號 | `text` |
| `meter_no` | 水／電表表號 | `text` |
| `usage` | 使用度數 | `text` |
| `basic_fee` | 基本費 | `number` |
| `usage_fee` | 用水／用電費 | `number` |
| `subtotal` | 未稅小計 | `number` |
| `business_tax` | 營業稅 | `number` |
| `levy_fee` | 代徵／附加費用 | `number` |
| `total_amount` | 本期應繳合計 | `number` |
| `pay_start` | 繳費起始日 | `date` |
| `due_date` | 繳費期限 | `date` |
| `debit_date` | 預定扣繳日 | `date` |
| `pay_account` | 委託代繳帳號 | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「水電費／公用事業帳單」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "水電費／公用事業帳單",
  "fields": { provider（事業機構）、period（計費（用水／用電）期間）、customer_name（用戶名稱）、customer_tax_id（用戶統一編號）、account（水號／電號／用戶號）、meter_no（水／電表表號）、usage（使用度數）、basic_fee（基本費）、usage_fee（用水／用電費）、subtotal（未稅小計）、business_tax（營業稅）、levy_fee（代徵／附加費用）、total_amount（本期應繳合計）、pay_start（繳費起始日）、due_date（繳費期限）、debit_date（預定扣繳日）、pay_account（委託代繳帳號） },
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為水費／電費等公用事業帳單（台灣自來水公司、台電等）。自 114 年 2 月份起，有統一編號之用戶水費項目為『含稅價』，另列示未稅小計與營業稅。本期應繳合計＝水（電）費項目小計＋代徵／附加費用小計。**total_amount 優先取帳單上獨立標示的「繳費總金額」「本期應繳總金額」「應繳金額」欄位值**（通常在表頭的方框內，如中華電信繳費結果通知的『繳費總金額(元) $3,532』）；不要拿下方費用明細裡的單項金額充當總額。水號／電號為用戶識別碼；日期民國年轉西元。用戶多為本公司（天干／中意／仲彬）。亦涵蓋電信帳單（如中華電信）與其繳費通知／繳費結果通知。**識別碼絕對不是金額**：用戶號碼、電話號碼、水號電號、銷帳編號、帳號、發票號碼都是識別碼——這些數字常有 8～12 位、沒有小數點也沒有千分位逗號，**一律不得填入任何金額欄位**（basic_fee／usage_fee／subtotal／total_amount 等）。金額欄只填帳單上標示為「元」或帶千分位／小數點的費用數字；找不到對應金額就留空，寧可空著也不要拿識別碼充數。
~~~~

### 3.11 `receipt`：收據／雜項開銷

欄位數：9；明細：items；VLM 框：要求。

| key | 標籤 | kind |
|---|---|---|
| `voucher_id` | 單號／收據號 | `text` |
| `date` | 日期 | `date` |
| `receipt_type` | 收據類型（免用統一發票／一般收據／勞務報酬） | `text` |
| `vendor` | 廠商／收款人 | `text` |
| `tax_id` | 統一編號 | `text` |
| `item_desc` | 品名／事由 | `text` |
| `subtotal` | 未稅 | `number` |
| `tax_amount` | 稅額 | `number` |
| `total_amount` | 金額 | `number` |

~~~~text
你是專業會計憑證辨識助手。這是一張「收據／雜項開銷」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "收據／雜項開銷",
  "fields": { voucher_id（單號／收據號）、date（日期）、receipt_type（收據類型（免用統一發票／一般收據／勞務報酬））、vendor（廠商／收款人）、tax_id（統一編號）、item_desc（品名／事由）、subtotal（未稅）、tax_amount（稅額）、total_amount（金額） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low",
  "boxes": { "voucher_id": [x1,y1,x2,y2], "date": [x1,y1,x2,y2], "receipt_type": [x1,y1,x2,y2], "vendor": [x1,y1,x2,y2], "tax_id": [x1,y1,x2,y2], "item_desc": [x1,y1,x2,y2], "subtotal": [x1,y1,x2,y2], "tax_amount": [x1,y1,x2,y2], "total_amount": [x1,y1,x2,y2] }
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- boxes 為各欄位在影像上的邊界框，座標正規化 0~1000 整數（左上為原點）；找不到的欄位省略該鍵。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為收據（可能為免用統一發票收據、一般收據、勞務報酬單、代收轉付收據等），未必有統一編號或營業稅。統一編號為 8 位純數字，電話不是統編勿填入。請擷取收款人／廠商、事由／品名、金額與日期；有扣繳稅額才填 tax_amount。
~~~~

### 3.12 `statement`：對帳單／應收帳款明細表

欄位數：14；明細：items；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `statement_no` | 對帳單號／單號 | `text` |
| `date` | 日期 | `date` |
| `period` | 交易日區間／結帳期間 | `text` |
| `issuer` | 開立廠商（賣方／收款方） | `text` |
| `issuer_tax_id` | 開立廠商統編 | `text` |
| `customer` | 客戶名稱（買方／付款方） | `text` |
| `customer_tax_id` | 客戶統一編號 | `text` |
| `payment_term` | 付款方式／條件 | `text` |
| `ref_invoice_no` | 表列發票號碼（勾稽用，非本憑證號碼） | `text` |
| `subtotal` | 銷貨合計額（未稅） | `number` |
| `tax_amount` | 營業稅額 | `number` |
| `total_amount` | 應收（應付）帳款總額 | `number` |
| `bank_account` | 銀行資料（戶名／銀行／帳號） | `text` |
| `remark` | 備註 | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「對帳單／應收帳款明細表」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "對帳單／應收帳款明細表",
  "fields": { statement_no（對帳單號／單號）、date（日期）、period（交易日區間／結帳期間）、issuer（開立廠商（賣方／收款方））、issuer_tax_id（開立廠商統編）、customer（客戶名稱（買方／付款方））、customer_tax_id（客戶統一編號）、payment_term（付款方式／條件）、ref_invoice_no（表列發票號碼（勾稽用，非本憑證號碼））、subtotal（銷貨合計額（未稅））、tax_amount（營業稅額）、total_amount（應收（應付）帳款總額）、bank_account（銀行資料（戶名／銀行／帳號））、remark（備註） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為對帳單／應收帳款（應付帳款）明細表，**不是統一發票**。表格內的「發票號碼」欄是用來勾稽的既有發票號，請放入 ref_invoice_no，**不要**把它當成本憑證的憑證號碼或視為發票。抬頭（表頭最上方）的公司為開立廠商＝賣方／收款方；「客戶名稱」為買方／付款方，多為本公司（仲彬／中意／天干）。統一編號只在單據上明確印出時才填，**沒印出就留空，絕對不要推測或編造**；單據上若只有一個統編（通常標示於客戶欄），那是客戶（買方）的，填入 customer_tax_id。下方常有銷貨合計額、營業稅額、應收帳款總額三個數字，請分別擷取為純數字。明細每一列放入 items：description=商品名稱及規格、quantity=數量、unit_price=單價、amount=**金額欄（未稅）**（空白列略過）。明細表常有數量／單價留白的列（如運費），該欄空白就留空，**不可拿右邊的金額或合計去填補空欄位**；amount 取「金額」欄，不是「合計」欄。日期為民國年，請轉西元。
~~~~

### 3.13 `purchase_order`：訂購單／採購單

欄位數：12；明細：items；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `po_no` | 採購單號 | `text` |
| `date` | 訂購日期 | `date` |
| `buyer` | 採購方（買方） | `text` |
| `buyer_tax_id` | 買方統編 | `text` |
| `supplier` | 供應商（賣方） | `text` |
| `supplier_code` | 供應商代碼 | `text` |
| `supplier_tax_id` | 供應商統編 | `text` |
| `contact` | 聯絡人 | `text` |
| `currency` | 幣別 | `text` |
| `payment_term` | 付款條件 | `text` |
| `delivery_date` | 交貨／需求日期 | `date` |
| `total_amount` | 總金額 | `number` |

~~~~text
你是專業會計憑證辨識助手。這是一張「訂購單／採購單」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "訂購單／採購單",
  "fields": { po_no（採購單號）、date（訂購日期）、buyer（採購方（買方））、buyer_tax_id（買方統編）、supplier（供應商（賣方））、supplier_code（供應商代碼）、supplier_tax_id（供應商統編）、contact（聯絡人）、currency（幣別）、payment_term（付款條件）、delivery_date（交貨／需求日期）、total_amount（總金額） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為訂購單／採購單（Purchase Order）。採購方（buyer）為下單客戶、供應商（supplier）為出貨方；請分別擷取雙方名稱與統一編號（8 位純數字，電話勿當統編）。明細放入 items：description=品名／規格、quantity=數量、unit_price=單價、amount=金額。付款條件如『月結75天／M75』；日期民國轉西元。
~~~~

### 3.14 `fda_import`：食品輸入許可通知

欄位數：18；明細：無；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `permit_no` | 申請書號碼 | `text` |
| `fda_no` | FDA中字號 | `text` |
| `import_date` | 進口日期 | `date` |
| `applicant` | 報驗義務人 | `text` |
| `applicant_tax_id` | 報驗義務人統編 | `text` |
| `goods_name` | 貨物名稱（品名） | `text` |
| `ccc_code` | 貨品分類號列（C.C.C.Code） | `text` |
| `declaration_no` | 報單號碼及項次 | `text` |
| `manufactured_date` | 製造日期 | `date` |
| `expiration_date` | 有效日期 | `date` |
| `trademark` | 商標（牌名） | `text` |
| `manufacturer` | 製造廠名稱 | `text` |
| `spec` | 規格 | `text` |
| `lot_no` | 製造批號 | `text` |
| `origin_country` | 生產國別 | `text` |
| `quantity` | 數量 | `text` |
| `net_weight` | 淨重 | `text` |
| `remark` | 備註 | `text` |

~~~~text
你是專業會計憑證辨識助手。這是一張「食品輸入許可通知」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "食品輸入許可通知",
  "fields": { permit_no（申請書號碼）、fda_no（FDA中字號）、import_date（進口日期）、applicant（報驗義務人）、applicant_tax_id（報驗義務人統編）、goods_name（貨物名稱（品名））、ccc_code（貨品分類號列（C.C.C.Code））、declaration_no（報單號碼及項次）、manufactured_date（製造日期）、expiration_date（有效日期）、trademark（商標（牌名））、manufacturer（製造廠名稱）、spec（規格）、lot_no（製造批號）、origin_country（生產國別）、quantity（數量）、net_weight（淨重）、remark（備註） },
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為衛生福利部食品藥物管理署『食品及相關產品輸入許可通知』。報驗義務人（applicant）為進口商、製造廠（manufacturer）為國外生產廠。報單號碼及項次可與進口報單勾稽；C.C.C.Code 為貨品分類號列。統一編號為 8 位純數字；日期民國轉西元。
~~~~

### 3.15 `social_insurance`：勞健保費繳款／繳費證明

欄位數：12；明細：items；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `agency` | 主辦機關（勞保局／健保署） | `text` |
| `insurance_type` | 保險別 | `text` |
| `unit_name` | 投保單位名稱 | `text` |
| `unit_no` | 投保單位代號 | `text` |
| `period` | 計費年月 | `text` |
| `payment_no` | 銷帳編號 | `text` |
| `payment_date` | 繳納日期 | `date` |
| `due_date` | 繳款期限 | `date` |
| `employer_share` | 單位負擔金額（如單據有列） | `number` |
| `employee_share` | 個人負擔金額（如單據有列） | `number` |
| `surcharge` | 滯納金 | `number` |
| `total_amount` | 應繳／已繳總金額 | `number` |

~~~~text
你是專業會計憑證辨識助手。這是一張「勞健保費繳款／繳費證明」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "勞健保費繳款／繳費證明",
  "fields": { agency（主辦機關（勞保局／健保署））、insurance_type（保險別）、unit_name（投保單位名稱）、unit_no（投保單位代號）、period（計費年月）、payment_no（銷帳編號）、payment_date（繳納日期）、due_date（繳款期限）、employer_share（單位負擔金額（如單據有列））、employee_share（個人負擔金額（如單據有列））、surcharge（滯納金）、total_amount（應繳／已繳總金額） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為勞保局／健保署的保險費繳款單或投保單位繳費證明書。**保險費屬免稅，單據上沒有營業稅，絕對不要拆出進項稅額。**表格常依『費用代號』分列（如勞保普通事故、職災保險、就業保險、積欠工資墊償基金），請把每一列放入 items：description＝費用名稱或代號、amount＝該列金額；total_amount 為各列合計或單據標示的應繳總額。**單位負擔／個人負擔**：多數繳費證明並未區分，**單據上沒有列出就留空，不要自行推算或分攤**。日期為民國年（如 1121101＝民國112年11月1日），請轉西元。
~~~~

### 3.16 `bill_detail`：帳單費用明細（附頁佐證）

欄位數：6；明細：items；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `provider` | 事業機構 | `text` |
| `customer_name` | 用戶名稱 | `text` |
| `account` | 用戶帳號／號碼 | `text` |
| `period` | 計費期間 | `text` |
| `page_no` | 頁次 | `text` |
| `total_amount` | 本頁應繳金額合計 | `number` |

~~~~text
你是專業會計憑證辨識助手。這是一張「帳單費用明細（附頁佐證）」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "帳單費用明細（附頁佐證）",
  "fields": { provider（事業機構）、customer_name（用戶名稱）、account（用戶帳號／號碼）、period（計費期間）、page_no（頁次）、total_amount（本頁應繳金額合計） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單是帳單的**費用明細附頁**或繳費結果通知（如中華電信「112年12月費用明細清單 第3頁」），屬於佐證文件——真正的入帳依據是同一批文件中的統一發票或繳費憑證，本頁**不單獨產生會計分錄**。表格中的「用戶號碼」欄是電話號碼（如 27524123、0922000102），「彙寄編號」「通知單流水號」「變動載具號碼」「金融機構代繳帳號」亦皆為識別碼，**一律不得填入金額欄位**。total_amount 只填本頁明確標示為「應繳金額」「合計」的費用數字；若本頁只有明細沒有合計，請留空。
~~~~

### 3.17 `journal_voucher`：會計傳票（記帳憑證）

欄位數：9；明細：entries；VLM 框：不要求。

| key | 標籤 | kind |
|---|---|---|
| `voucher_kind` | 傳票單別（如 A911 轉帳傳票） | `text` |
| `voucher_id` | 傳票單號 | `text` |
| `date` | 傳票日期 | `date` |
| `prepared_date` | 製表日期 | `date` |
| `company_name` | 公司名稱（表頭） | `text` |
| `page_no` | 頁次（如 1/2） | `text` |
| `remark` | 備註 | `text` |
| `debit_total` | 借方合計 | `number` |
| `credit_total` | 貸方合計 | `number` |

~~~~text
你是專業會計憑證辨識助手。這是一張「會計傳票（記帳憑證）」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "會計傳票（記帳憑證）",
  "fields": { voucher_kind（傳票單別（如 A911 轉帳傳票））、voucher_id（傳票單號）、date（傳票日期）、prepared_date（製表日期）、company_name（公司名稱（表頭））、page_no（頁次（如 1/2））、remark（備註）、debit_total（借方合計）、credit_total（貸方合計） },
  "entries": [{"account_code": "科目代號", "account_name": "科目名稱", "description": "摘要", "dept": "部門別", "offset_code": "立沖帳目", "debit": 0, "credit": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為**會計傳票（記帳憑證）**，是記帳人員依原始憑證編製的內部單據，本身不是發票或收據——它的重點是**借貸分錄**。傳票單別決定型態：**轉帳傳票**（如 A911）借貸兩面都列出、必然平衡；**現支傳票**（如 A931）只列支出（借方），貸方為現金屬隱含不印在表上；**收入傳票**反之只列收入面。請照表上實際印出的列擷取，**不要為了讓借貸平衡而自行補上沒有印出的那一面**。表格每一列的左欄是「會計科目／科目名稱」（上為代號、下為名稱，如「6205／運費」），接著是摘要、部門別、立沖帳目（往來對象代號，如「047／聯邦快遞」），最右兩欄是借方金額與貸方金額。**每一列放入 entries**：account_code＝科目代號、account_name＝科目名稱、description＝摘要、dept＝部門別、offset_code＝立沖帳目、debit＝借方金額、credit＝貸方金額。**一列只會有借方或貸方其中一邊有金額**，另一邊填 0，不要把同一個數字同時填兩邊。**借方金額欄在貸方金額欄的左邊，兩欄相鄰**：請嚴格依數字實際落在哪一欄判定借貸，**不要依科目性質推測**（例如不要因為是稅額就假設它在貸方）。金額欄空白的列（如「以下空白」）請略過。表格最底列的「合計」請填入 debit_total／credit_total，不要當成分錄。**填完請自我檢查**：各列的 debit 加總必須等於 debit_total、credit 加總必須等於 credit_total，且兩者相等（傳票一定借貸平衡）。若對不上，代表有某列的金額放錯邊，請回到圖上重新確認該數字是印在借方欄還是貸方欄。日期若為民國年請轉西元（西元＝民國＋1911）。
~~~~

### 3.18 `other`：其他憑證

欄位數：9；明細：items；VLM 框：要求。

| key | 標籤 | kind |
|---|---|---|
| `voucher_id` | 單號 | `text` |
| `date` | 日期 | `date` |
| `seller_name` | 賣方／來源 | `text` |
| `seller_tax_id` | 賣方統編 | `text` |
| `buyer_name` | 買方／對象 | `text` |
| `buyer_tax_id` | 買方統編 | `text` |
| `subtotal` | 未稅 | `number` |
| `tax_amount` | 稅額 | `number` |
| `total_amount` | 總計 | `number` |

~~~~text
你是專業會計憑證辨識助手。這是一張「其他憑證」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "其他憑證",
  "fields": { voucher_id（單號）、date（日期）、seller_name（賣方／來源）、seller_tax_id（賣方統編）、buyer_name（買方／對象）、buyer_tax_id（買方統編）、subtotal（未稅）、tax_amount（稅額）、total_amount（總計） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low",
  "boxes": { "voucher_id": [x1,y1,x2,y2], "date": [x1,y1,x2,y2], "seller_name": [x1,y1,x2,y2], "seller_tax_id": [x1,y1,x2,y2], "buyer_name": [x1,y1,x2,y2], "buyer_tax_id": [x1,y1,x2,y2], "subtotal": [x1,y1,x2,y2], "tax_amount": [x1,y1,x2,y2], "total_amount": [x1,y1,x2,y2] }
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- boxes 為各欄位在影像上的邊界框，座標正規化 0~1000 整數（左上為原點）；找不到的欄位省略該鍵。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
~~~~

## 4. OCR 純文字驗證提示詞

以下依 `validation._text_classify()`／`_text_extract()` 目前的替換及刪行規則生成；保留其原有矛盾、視覺指令與尾逗號，不在盤點時偷偷修正。`{{OCR_TEXT}}` 是當次 OCR 字串的位置。

### 4.1 純文字 Router

~~~~text
你是單據分類器。判斷以下 OCR 文字所屬的掃描件：
1) 屬於下列哪一類（回代碼，如 cheque）
2) 需『順時針』旋轉幾度才會正立（僅能是 0、90、180、270）
只回 JSON：{"type":"代碼","rotate":0}，不要其他文字。
以**標題**為準，不要只看到「發票號碼」「營業稅額」就判成發票：
- 標題寫「應收帳款明細表／應付帳款明細表／對帳單／月結單」→ statement（這類表格內的發票號碼只是勾稽用，本身不是發票）
- 標題寫「轉帳傳票／收入傳票／支出傳票」，表格有「會計科目・借方金額・貸方金額」欄→ journal_voucher（這是記帳憑證，不是發票收據）
- bill_detail **只用於帳單的費用明細附頁**：標題明確寫「費用明細清單／費用明細」，內容是逐項費用的清單。
  以下一律**不是** bill_detail，它們本身就是入帳依據：印有「統一發票／電子發票證明聯」（→ invoice）、「繳費結果通知／繳費通知」且載明繳費總金額（→ utility）、勞健保台電的「繳費證明／繳款單」（→ tax_bill 或 utility）。
- 勞保局／健保署／勞退的保險費繳款單、投保單位繳費證明書 → social_insurance
- 標題寫「訂購單／採購單」→ purchase_order；報關行的「收費通知單」→ broker_note
- 真正的發票（invoice）標題為「統一發票」「電子發票證明聯」或收銀機發票格式

類別：
- invoice：統一發票／電子發票
- cheque：支票
- remittance：匯款單／銀行轉帳水單
- customs_tax：海關進口貨物稅費繳納證明
- export_declaration：出口報單
- import_declaration：進口報單
- broker_note：報關行收費通知單
- declaration：進出口報單（未分進出口）
- tax_bill：稅款繳款書
- utility：水電費／公用事業帳單
- receipt：收據／雜項開銷
- statement：對帳單／應收帳款明細表
- purchase_order：訂購單／採購單
- fda_import：食品輸入許可通知
- social_insurance：勞健保費繳款／繳費證明
- bill_detail：帳單費用明細（附頁佐證）
- journal_voucher：會計傳票（記帳憑證）
- other：其他憑證
純文字模式沒有圖片，rotate 固定回 0。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.2 `invoice`：統一發票／電子發票

~~~~text
你是專業會計憑證辨識助手。這是一張「統一發票／電子發票」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "統一發票／電子發票",
  "fields": { voucher_id（發票號碼）、date（發票日期）、invoice_type（發票格式（三聯式／二聯式／電子發票／收銀機））、tax_type（課稅別（應稅／零稅率／免稅））、seller_name（賣方名稱）、seller_tax_id（賣方統編）、buyer_name（買方名稱）、buyer_tax_id（買方統編）、subtotal（未稅銷售額）、tax_amount（營業稅額）、total_amount（總計（含稅））、amount_words（總計金額中文大寫（如有））、usage_note（買受人註記（進貨及費用／固定資產）） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low",
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為台灣統一發票（三聯式／二聯式／電子發票證明聯／收銀機發票）。統一編號為 8 位純數字；電話號碼（含 - 或括號、位數非 8）不是統編，切勿填入統編欄，寧可留空。課稅別看勾選（✓）填『應稅』『零稅率』或『免稅』其一。營業稅額通常＝未稅銷售額×5%，仍以圖為準。買方多為仲彬股份有限公司(統編23051677)、中意企業股份有限公司(31182666)、天干；賣方為開立發票之廠商。賣方統編常蓋在『統一發票專用章』內——印章顏色可能為紅、藍、黑或紫，通常位於發票下方或右側，章內含賣方公司名、8 位統一編號與負責人；請務必從印章內讀出賣方統編填入 seller_tax_id。三聯式買受人註記欄若勾『固定資產』填『固定資產』，否則填『進貨及費用』。**金額自我校驗（手寫三聯式尤其重要）**：① 手寫發票下方多有『總計新台幣（中文大寫）』，請讀入 amount_words；大寫字（壹貳參肆伍陸柒捌玖拾佰仟萬）比手寫阿拉伯數字好辨認，**大寫與阿拉伯數字不一致時，以大寫為準**回填 total_amount。② 應稅發票應滿足『未稅銷售額 ＋ 營業稅額 ＝ 總計』；若三個數字兜不攏，請回到圖上重新核對（別直接輸出矛盾的數字）。③ 欄位在單據上空白就留空，**不要用其他欄位回推或編造**（金額欄尤其嚴格：寧可留空給人工填，也不要給錯的數字）。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.3 `cheque`：支票

~~~~text
你是專業會計憑證辨識助手。這是一張「支票」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "支票",
  "fields": { cheque_no（票號）、issue_date（發票日）、due_date（到期（票載）日）、drawer（發票人／出票人）、payer_bank（付款銀行）、payee（受款人／抬頭）、account_no（帳號）、total_amount（金額）、amount_words（金額大寫） },
  "note": "摘要／備註",
  "confidence": "high|medium|low",
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為支票／本票。金額以票面『國字大寫』為準轉為數字（如玖仟肆佰貳拾貳圓整＝9422）；票號多在右上角（如 TD3642163），下方 MICR 磁碼含票號/帳號/行庫代號。受款人（抬頭）為票據抬頭之公司；發票人為出票公司。日期為民國年，請轉西元。注意：掃描頁若同時含信封、傳票或其他單據，只擷取『支票本身』的內容，不要混入其他單據。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.4 `remittance`：匯款單／銀行轉帳水單

~~~~text
你是專業會計憑證辨識助手。這是一張「匯款單／銀行轉帳水單」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "匯款單／銀行轉帳水單",
  "fields": { date（交易日期）、ref_no（交易序號／建檔批號）、remitter（付款人戶名）、payer_account（付款帳號）、remitter_bank（付款／匯出銀行）、payment_method（付款方式）、total_amount（交易（匯款）金額）、debit_amount（扣款金額）、fee（手續費）、currency（幣別）、payee（收款人戶名）、payee_bank（收款銀行）、account_no（收款帳號）、payee_code（收款人代號） },
  "note": "摘要／備註",
  "confidence": "high|medium|low",
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為銀行匯款單／網路銀行轉帳水單。交易（匯款）金額為本金；扣款金額＝交易金額＋手續費（手續費若『內扣』則兩者相同）。付款人多為本公司（仲彬／中意／天干），收款人為受款廠商。收款銀行常以代碼＋名稱呈現（如 007 第一銀行）。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.5 `customs_tax`：海關進口貨物稅費繳納證明

~~~~text
你是專業會計憑證辨識助手。這是一張「海關進口貨物稅費繳納證明」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "海關進口貨物稅費繳納證明",
  "fields": { tax_bill_no（稅單號碼）、taxpayer（納稅義務人）、tax_id（統一編號）、import_date（貨物進口日期）、ship_name（船（機）名及呼號（班次））、broker_box（報關行箱號）、bl_no（提裝貨單號）、declaration_no（報單號碼）、goods_no（貨名／稅則號列）、qty（件數）、courier（快遞業者／列印者代號）、payment_date（填發（繳款）日期）、duty（進口稅）、trade_fee（推廣貿易服務費）、business_tax（營業稅）、total_amount（稅費合計）、business_tax_base（營業稅稅基）、issuer（核發機關） },
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為「海關進口（快遞）貨物稅費繳納證明」。一頁常有三聯（第一聯收據聯／第二聯扣抵聯／第三聯副本），內容相同，取任一聯完整填入即可，不要跨聯混填。右側稅費項目表含進口稅、推廣貿易服務費、營業稅、稅費合計、營業稅稅基，請逐項擷取為純數字。報單號碼可與進口報單勾稽。日期為民國年（如 1150524＝民國115年5月24日）。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.6 `export_declaration`：出口報單

~~~~text
你是專業會計憑證辨識助手。這是一張「出口報單」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "出口報單",
  "fields": { transport_mode（海空運別(1)）、doc_category（報單類別(2)）、declaration_no（報單號碼(3)）、customs_clear_no（海關通關號碼(4)）、ship_code（出口船機代碼(5)）、voyage（船舶航次／船機班次(6)）、date（報關日期(7)）、loading_port（裝貨港名稱及代碼(8)）、dest_country（目的地國家及代碼(9)）、storage_code（卸存地點代碼(10)）、master_awb（託運單主號(11)）、house_awb（託運單分號(12)）、refund_material_tax（申請沖退原料稅(13)）、transport_method（運輸方式(14)）、ship_name（出口船舶名稱(15)）、invoice_amount（發票總金額(16)）、freight（運費(17)）、insurance（保險費(18)）、add_fee（應加費用(19)）、deduct_fee（應減費用(20)）、total_fob_twd（總離岸價格新臺幣(21)）、exporter（出口人名稱地址(22)）、exporter_tax_id（出口人統一編號(23)）、exporter_customs_no（出口人海關監管編號(24)）、payment_method（繳納方式代碼(25)）、buyer（買方名稱地址(26)）、buyer_country（買方國家代碼(27)）、buyer_tax_id（買方統一編號(28)）、buyer_customs_no（買方海關監管編號(29)）、price_term（單價條件代碼(30)）、exchange_rate（匯率(31)）、item_no（項次(32)）、goods_desc（貨物名稱商標(牌名)及規格(33)）、permit_no（輸出入許可文件號碼-項次(34)）、hs_code（輸出入貨品分類號列／統計號別／檢查號碼(35)）、unit_price（單價／幣別／金額(36)）、net_weight（淨重公斤(37)）、quantity（數量(單位)(38)）、stat_use（統計用(39)）、fob_value_twd（離岸價格FOB Value新臺幣(40)）、stat_method（統計方式(41)）、total_packages（總件數(單位)(42)）、packing_desc（包裝說明(43)）、gross_weight（總毛重公斤(44)）、marks（標記及貨櫃號碼(45)）、other_declared（其它申報事項(46)）、broker（報關人／AEO編號(47)）、specialist（專責人員(48)） },
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為台灣海關「出口報單」，共 48 個欄位，標題旁均有括號編號，請「依編號」在對應格位定位擷取，避免張冠左右鄰格。direction 固定為「出口」。declaration_no 請含開頭英文別碼與完整編號（如 CX/ /15/696/D6341）。欄位 32~41 為貨物明細第一項的各欄；若有多項貨物，仍以第一項為主填入對應欄位。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.7 `import_declaration`：進口報單

~~~~text
你是專業會計憑證辨識助手。這是一張「進口報單」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "進口報單",
  "fields": { doc_category（報單類別）、declaration_no（報單號碼）、customs_clear_no（海關通關號碼）、date（報關日期）、import_date（進口日期）、ship_name（進口船（機）名及呼號（班次））、bl_no（提單號碼）、loading_port（裝貨港（代碼及名稱））、origin_port（起運口岸（代碼及名稱））、transport_method（運輸方式）、taxpayer（納稅義務人名稱地址）、taxpayer_tax_id（納稅義務人統一編號）、customs_supervision（海關監管編號）、seller（賣方名稱地址）、seller_country（賣方國家代碼）、tax_method（納稅辦法）、permit_no（輸入許可證號碼）、price_term（條件（起岸價格條件））、currency（幣別）、exchange_rate（匯率）、invoice_amount（發票金額）、freight（運費）、insurance（保險費）、total_cif_twd（完稅價格總計新臺幣）、duty（進口稅）、business_tax（營業稅）、trade_fee（推廣貿易服務費）、commodity_tax（貨物稅）、total_tax（稅費合計）、gross_weight（總毛重公斤）、total_packages（總件數及單位）、broker（報關人／AEO編號）、specialist（專責人員） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為台灣海關「進口報單」，各欄位標題旁有括號編號，請依標題文字定位並讀出其編號（本欄位契約未逐一標號，請以表單上實際印出的編號與標題為準）。direction 固定為「進口」。納稅義務人為進口商、賣方為國外出口商；完稅價格以 CIF 為基礎；稅費含進口稅、營業稅、推廣貿易服務費、貨物稅，請分別擷取並加總為稅費合計。逐項貨物明細放入 items：description=貨物名稱及規格、hs_code=貨品分類號列（C.C.C.Code）、quantity=數量、unit_price=單價、amount=完稅價格新臺幣、origin_country=生產國別。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.8 `broker_note`：報關行收費通知單

~~~~text
你是專業會計憑證辨識助手。這是一張「報關行收費通知單」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "報關行收費通知單",
  "fields": { doc_no（單號）、date（日期）、broker（報關行名稱）、client（委託人／客戶）、declaration_no（報單號碼）、bl_no（B/L S/O NO）、customs_date（報關日期）、vessel（船名）、port（進出口岸）、goods_name（貨名）、qty（件數）、customs_duty（關稅／滯報費）、broker_fee（報關手續費）、edi_fee（海關鍵輸費）、subtotal（費用合計）、tax（稅金）、total_amount（總計）、prepaid（預收費）、balance（應收（退）金額） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為報關行開立的『收費通知單／費用明細』（進出口報關費用）。左右兩欄列出各項費別與金額（關稅/滯報費、服務車資、驗對證費、理貨工資、報關手續費、海關鍵輸費等），請把每一有金額的費別放入 items：description=費別名稱、amount=金額。關稅/滯報費(customs_duty)為代墊稅費、報關手續費(broker_fee)為勞務費，請分別擷取為頂層欄位。下方有費用合計、稅金、總計、預收費、應收（退）金額。金額純數字、日期民國轉西元；報單號碼可與進口／出口報單勾稽；委託人多為本公司（仲彬／中意／天干）。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.9 `declaration`：進出口報單（未分進出口）

~~~~text
你是專業會計憑證辨識助手。這是一張「進出口報單（未分進出口）」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "進出口報單（未分進出口）",
  "fields": { declaration_no（報單號碼）、doc_category（報單類別）、date（報關日期）、direction（進出口別）、trader（出口人／納稅義務人）、tax_id（統一編號）、counterparty（買方／賣方）、total_amount（總離岸／完稅價格新臺幣）、currency（幣別） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 若能判斷是進口或出口報單，direction 請填「進口」或「出口」。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.10 `tax_bill`：稅款繳款書

~~~~text
你是專業會計憑證辨識助手。這是一張「稅款繳款書」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "稅款繳款書",
  "fields": { tax_agency（徵收機關）、tax_code（稅目代碼）、tax_type（稅目名稱）、period（所屬年度／期別）、payment_no（銷帳編號）、taxpayer（納稅義務人／營利事業名稱）、tax_id（統一編號）、responsible_person（負責人）、principal（本稅）、surcharge（滯納金／加計利息）、total_amount（應繳金額合計）、pay_start（繳納期間起）、due_date（繳納期限止）、phone（納稅義務人電話） },
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為稅款繳款書（如營利事業所得稅結算自行繳納、營業稅等）。稅目代碼如 357（國稅）。銷帳編號通常在底部條碼下方（如 A110016601654155011）。一頁常含三聯（收據／證明／收款機構留存），內容相同取其一。金額純數字，日期民國年轉西元（如 1150531→2026-05-31）。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.11 `utility`：水電費／公用事業帳單

~~~~text
你是專業會計憑證辨識助手。這是一張「水電費／公用事業帳單」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "水電費／公用事業帳單",
  "fields": { provider（事業機構）、period（計費（用水／用電）期間）、customer_name（用戶名稱）、customer_tax_id（用戶統一編號）、account（水號／電號／用戶號）、meter_no（水／電表表號）、usage（使用度數）、basic_fee（基本費）、usage_fee（用水／用電費）、subtotal（未稅小計）、business_tax（營業稅）、levy_fee（代徵／附加費用）、total_amount（本期應繳合計）、pay_start（繳費起始日）、due_date（繳費期限）、debit_date（預定扣繳日）、pay_account（委託代繳帳號） },
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為水費／電費等公用事業帳單（台灣自來水公司、台電等）。自 114 年 2 月份起，有統一編號之用戶水費項目為『含稅價』，另列示未稅小計與營業稅。本期應繳合計＝水（電）費項目小計＋代徵／附加費用小計。**total_amount 優先取帳單上獨立標示的「繳費總金額」「本期應繳總金額」「應繳金額」欄位值**（通常在表頭的方框內，如中華電信繳費結果通知的『繳費總金額(元) $3,532』）；不要拿下方費用明細裡的單項金額充當總額。水號／電號為用戶識別碼；日期民國年轉西元。用戶多為本公司（天干／中意／仲彬）。亦涵蓋電信帳單（如中華電信）與其繳費通知／繳費結果通知。**識別碼絕對不是金額**：用戶號碼、電話號碼、水號電號、銷帳編號、帳號、發票號碼都是識別碼——這些數字常有 8～12 位、沒有小數點也沒有千分位逗號，**一律不得填入任何金額欄位**（basic_fee／usage_fee／subtotal／total_amount 等）。金額欄只填帳單上標示為「元」或帶千分位／小數點的費用數字；找不到對應金額就留空，寧可空著也不要拿識別碼充數。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.12 `receipt`：收據／雜項開銷

~~~~text
你是專業會計憑證辨識助手。這是一張「收據／雜項開銷」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "收據／雜項開銷",
  "fields": { voucher_id（單號／收據號）、date（日期）、receipt_type（收據類型（免用統一發票／一般收據／勞務報酬））、vendor（廠商／收款人）、tax_id（統一編號）、item_desc（品名／事由）、subtotal（未稅）、tax_amount（稅額）、total_amount（金額） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low",
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為收據（可能為免用統一發票收據、一般收據、勞務報酬單、代收轉付收據等），未必有統一編號或營業稅。統一編號為 8 位純數字，電話不是統編勿填入。請擷取收款人／廠商、事由／品名、金額與日期；有扣繳稅額才填 tax_amount。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.13 `statement`：對帳單／應收帳款明細表

~~~~text
你是專業會計憑證辨識助手。這是一張「對帳單／應收帳款明細表」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "對帳單／應收帳款明細表",
  "fields": { statement_no（對帳單號／單號）、date（日期）、period（交易日區間／結帳期間）、issuer（開立廠商（賣方／收款方））、issuer_tax_id（開立廠商統編）、customer（客戶名稱（買方／付款方））、customer_tax_id（客戶統一編號）、payment_term（付款方式／條件）、ref_invoice_no（表列發票號碼（勾稽用，非本憑證號碼））、subtotal（銷貨合計額（未稅））、tax_amount（營業稅額）、total_amount（應收（應付）帳款總額）、bank_account（銀行資料（戶名／銀行／帳號））、remark（備註） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為對帳單／應收帳款（應付帳款）明細表，**不是統一發票**。表格內的「發票號碼」欄是用來勾稽的既有發票號，請放入 ref_invoice_no，**不要**把它當成本憑證的憑證號碼或視為發票。抬頭（表頭最上方）的公司為開立廠商＝賣方／收款方；「客戶名稱」為買方／付款方，多為本公司（仲彬／中意／天干）。統一編號只在單據上明確印出時才填，**沒印出就留空，絕對不要推測或編造**；單據上若只有一個統編（通常標示於客戶欄），那是客戶（買方）的，填入 customer_tax_id。下方常有銷貨合計額、營業稅額、應收帳款總額三個數字，請分別擷取為純數字。明細每一列放入 items：description=商品名稱及規格、quantity=數量、unit_price=單價、amount=**金額欄（未稅）**（空白列略過）。明細表常有數量／單價留白的列（如運費），該欄空白就留空，**不可拿右邊的金額或合計去填補空欄位**；amount 取「金額」欄，不是「合計」欄。日期為民國年，請轉西元。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.14 `purchase_order`：訂購單／採購單

~~~~text
你是專業會計憑證辨識助手。這是一張「訂購單／採購單」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "訂購單／採購單",
  "fields": { po_no（採購單號）、date（訂購日期）、buyer（採購方（買方））、buyer_tax_id（買方統編）、supplier（供應商（賣方））、supplier_code（供應商代碼）、supplier_tax_id（供應商統編）、contact（聯絡人）、currency（幣別）、payment_term（付款條件）、delivery_date（交貨／需求日期）、total_amount（總金額） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為訂購單／採購單（Purchase Order）。採購方（buyer）為下單客戶、供應商（supplier）為出貨方；請分別擷取雙方名稱與統一編號（8 位純數字，電話勿當統編）。明細放入 items：description=品名／規格、quantity=數量、unit_price=單價、amount=金額。付款條件如『月結75天／M75』；日期民國轉西元。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.15 `fda_import`：食品輸入許可通知

~~~~text
你是專業會計憑證辨識助手。這是一張「食品輸入許可通知」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "食品輸入許可通知",
  "fields": { permit_no（申請書號碼）、fda_no（FDA中字號）、import_date（進口日期）、applicant（報驗義務人）、applicant_tax_id（報驗義務人統編）、goods_name（貨物名稱（品名））、ccc_code（貨品分類號列（C.C.C.Code））、declaration_no（報單號碼及項次）、manufactured_date（製造日期）、expiration_date（有效日期）、trademark（商標（牌名））、manufacturer（製造廠名稱）、spec（規格）、lot_no（製造批號）、origin_country（生產國別）、quantity（數量）、net_weight（淨重）、remark（備註） },
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為衛生福利部食品藥物管理署『食品及相關產品輸入許可通知』。報驗義務人（applicant）為進口商、製造廠（manufacturer）為國外生產廠。報單號碼及項次可與進口報單勾稽；C.C.C.Code 為貨品分類號列。統一編號為 8 位純數字；日期民國轉西元。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.16 `social_insurance`：勞健保費繳款／繳費證明

~~~~text
你是專業會計憑證辨識助手。這是一張「勞健保費繳款／繳費證明」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "勞健保費繳款／繳費證明",
  "fields": { agency（主辦機關（勞保局／健保署））、insurance_type（保險別）、unit_name（投保單位名稱）、unit_no（投保單位代號）、period（計費年月）、payment_no（銷帳編號）、payment_date（繳納日期）、due_date（繳款期限）、employer_share（單位負擔金額（如單據有列））、employee_share（個人負擔金額（如單據有列））、surcharge（滯納金）、total_amount（應繳／已繳總金額） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為勞保局／健保署的保險費繳款單或投保單位繳費證明書。**保險費屬免稅，單據上沒有營業稅，絕對不要拆出進項稅額。**表格常依『費用代號』分列（如勞保普通事故、職災保險、就業保險、積欠工資墊償基金），請把每一列放入 items：description＝費用名稱或代號、amount＝該列金額；total_amount 為各列合計或單據標示的應繳總額。**單位負擔／個人負擔**：多數繳費證明並未區分，**單據上沒有列出就留空，不要自行推算或分攤**。日期為民國年（如 1121101＝民國112年11月1日），請轉西元。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.17 `bill_detail`：帳單費用明細（附頁佐證）

~~~~text
你是專業會計憑證辨識助手。這是一張「帳單費用明細（附頁佐證）」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "帳單費用明細（附頁佐證）",
  "fields": { provider（事業機構）、customer_name（用戶名稱）、account（用戶帳號／號碼）、period（計費期間）、page_no（頁次）、total_amount（本頁應繳金額合計） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單是帳單的**費用明細附頁**或繳費結果通知（如中華電信「112年12月費用明細清單 第3頁」），屬於佐證文件——真正的入帳依據是同一批文件中的統一發票或繳費憑證，本頁**不單獨產生會計分錄**。表格中的「用戶號碼」欄是電話號碼（如 27524123、0922000102），「彙寄編號」「通知單流水號」「變動載具號碼」「金融機構代繳帳號」亦皆為識別碼，**一律不得填入金額欄位**。total_amount 只填本頁明確標示為「應繳金額」「合計」的費用數字；若本頁只有明細沒有合計，請留空。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.18 `journal_voucher`：會計傳票（記帳憑證）

~~~~text
你是專業會計憑證辨識助手。這是一張「會計傳票（記帳憑證）」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "會計傳票（記帳憑證）",
  "fields": { voucher_kind（傳票單別（如 A911 轉帳傳票））、voucher_id（傳票單號）、date（傳票日期）、prepared_date（製表日期）、company_name（公司名稱（表頭））、page_no（頁次（如 1/2））、remark（備註）、debit_total（借方合計）、credit_total（貸方合計） },
  "entries": [{"account_code": "科目代號", "account_name": "科目名稱", "description": "摘要", "dept": "部門別", "offset_code": "立沖帳目", "debit": 0, "credit": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low"
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
- 本表單為**會計傳票（記帳憑證）**，是記帳人員依原始憑證編製的內部單據，本身不是發票或收據——它的重點是**借貸分錄**。傳票單別決定型態：**轉帳傳票**（如 A911）借貸兩面都列出、必然平衡；**現支傳票**（如 A931）只列支出（借方），貸方為現金屬隱含不印在表上；**收入傳票**反之只列收入面。請照表上實際印出的列擷取，**不要為了讓借貸平衡而自行補上沒有印出的那一面**。表格每一列的左欄是「會計科目／科目名稱」（上為代號、下為名稱，如「6205／運費」），接著是摘要、部門別、立沖帳目（往來對象代號，如「047／聯邦快遞」），最右兩欄是借方金額與貸方金額。**每一列放入 entries**：account_code＝科目代號、account_name＝科目名稱、description＝摘要、dept＝部門別、offset_code＝立沖帳目、debit＝借方金額、credit＝貸方金額。**一列只會有借方或貸方其中一邊有金額**，另一邊填 0，不要把同一個數字同時填兩邊。**借方金額欄在貸方金額欄的左邊，兩欄相鄰**：請嚴格依數字實際落在哪一欄判定借貸，**不要依科目性質推測**（例如不要因為是稅額就假設它在貸方）。金額欄空白的列（如「以下空白」）請略過。表格最底列的「合計」請填入 debit_total／credit_total，不要當成分錄。**填完請自我檢查**：各列的 debit 加總必須等於 debit_total、credit 加總必須等於 credit_total，且兩者相等（傳票一定借貸平衡）。若對不上，代表有某列的金額放錯邊，請回到圖上重新確認該數字是印在借方欄還是貸方欄。日期若為民國年請轉西元（西元＝民國＋1911）。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

### 4.19 `other`：其他憑證

~~~~text
你是專業會計憑證辨識助手。這是一張「其他憑證」。以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{
  "doc_type": "其他憑證",
  "fields": { voucher_id（單號）、date（日期）、seller_name（賣方／來源）、seller_tax_id（賣方統編）、buyer_name（買方／對象）、buyer_tax_id（買方統編）、subtotal（未稅）、tax_amount（稅額）、total_amount（總計） },
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "note": "摘要／備註",
  "confidence": "high|medium|low",
}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。
純文字模式不可輸出 boxes。

OCR 文字：
{{OCR_TEXT}}
~~~~

## 5. 動態附加片段與組裝順序

### 5.1 Router 的人工提示

~~~~text


人工提示（請據此判斷類型）：{{HINT}}
~~~~

### 5.2 OCR 文字

~~~~text


OCR 文字（供校對，數字以圖為準）：
{{OCR_TEXT}}
~~~~

### 5.3 原始條碼／QR

~~~~text


影像偵測到的條碼／QR 內容（機器解碼，可信度高，可直接採用）：
{{DECODED_CODES_JOINED_BY_NEWLINE}}
~~~~

### 5.4 電子發票已解析欄位

~~~~text

電子發票 QR 已解出下列欄位，請優先採用：發票號碼={{voucher_id}}、日期={{date}}、未稅小計={{subtotal}}、含稅總計={{total_amount}}、買方統編={{buyer_tax_id}}、賣方統編={{seller_tax_id}}
~~~~

### 5.5 擷取的人工修正提示

~~~~text


人工修正提示（審核人員指出的錯誤，請據此優先修正）：{{HINT}}
~~~~

### 5.6 公司科目表

~~~~text


請**只**使用下列 {{COMPANY_OR_通用}}科目表中的科目（代號 名稱），不要自創代號：
{{CHART}}
~~~~

### 5.7 過往已核定分錄

~~~~text


參考過往已核定分錄（相似憑證）：
{{JSON_EXAMPLES}}
~~~~

### 5.8 客戶記帳種子

~~~~text


**本客戶既有記帳慣例（實際傳票，請比照科目選法）**：
{{JSON_SEEDS}}
（上列只給科目名稱，代號請對照下方科目表取用。）
~~~~

### 5.9 本次憑證摘要

~~~~text


憑證資訊：
{{JSON_VOUCHER_BRIEF}}
~~~~

### 5.10 憑證摘要實際欄位生成函式

~~~~python
def _voucher_brief(v: Voucher) -> dict:
    return {
        "doc_type": v.doc_type, "date": v.date,
        "seller": v.seller.name, "buyer": v.buyer.name,
        "items": [{"description": i.description, "amount": i.amount} for i in v.items],
        "subtotal": v.subtotal, "tax_amount": v.tax_amount,
        "total_amount": v.total_amount, "note": v.note,
    }
~~~~

擷取文字順序為類型模板 → OCR → 原始 QR → 已解析電子發票欄位 → 人工提示，另附完整 image_url；會計分類文字順序為一般／進口模板 → 公司科目表 → 歷史示例 → 種子 → 憑證摘要，沒有附圖。未提供資料的可選片段不加入。

## 6. 未使用的歷史提示詞

### 6.1 `pipeline.STRUCTURE_PROMPT`

~~~~text
你是專業會計憑證辨識助手。以下提供一張台灣會計憑證的原圖，以及 OCR 已抽取的文字（供校對，數字以圖為準）。
請萃取完整欄位並以 JSON 回覆（僅回 JSON，不要多餘文字）：
{
  "voucher_id": "發票/憑證號碼",
  "date": "YYYY-MM-DD（請將民國年轉為西元，如民國115年→2026）",
  "doc_type": "發票/收據/進口報單…",
  "buyer": {"name": "", "tax_id": "統編", "address": ""},
  "seller": {"name": "", "tax_id": "統編", "address": ""},
  "items": [{"description": "品名", "quantity": 0, "unit_price": 0, "amount": 0}],
  "subtotal": 0, "tax_amount": 0, "total_amount": 0, "currency": "TWD",
  "note": "備註/摘要",
  "confidence": "high|medium|low",
  "boxes": {
    "voucher_id": [x1,y1,x2,y2], "date": [x1,y1,x2,y2],
    "seller_name": [x1,y1,x2,y2], "seller_tax_id": [x1,y1,x2,y2],
    "buyer_name": [x1,y1,x2,y2], "buyer_tax_id": [x1,y1,x2,y2],
    "subtotal": [x1,y1,x2,y2], "tax_amount": [x1,y1,x2,y2], "total_amount": [x1,y1,x2,y2]
  }
}
boxes 為各欄位在影像上的邊界框，座標一律正規化為 0~1000 整數（左上角為原點，x 向右、y 向下）；
只需框有把握的欄位，找不到的欄位請省略該鍵。金額數字請純數字不要加 $ 或逗號。
若無營業稅資訊，tax_amount 視為 0。
~~~~

此常數目前只有定義，沒有呼叫；不得將它當作目前動態擷取的模板。

## 7. 維護方式

修改提示詞或類型後須重新核對本清單的來源雜湊與全部模式，避免只更新文字版或只更新圖片版。生成器由目前 DOC_TYPES／build_extract_prompt 展開，固定指令從 AST 讀取；需要復查的執行時資料應在各次 trace 中保留來源，不直接抄入本文件。
