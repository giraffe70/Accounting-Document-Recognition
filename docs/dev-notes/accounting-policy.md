# 公司會計政策與候選分錄

> 目的：記錄會計分類使用的公司政策預設、角色解析與候選分錄條件。這些政策只供分類，不取代人工核定或公司的正式會計判斷。

## 政策選擇

正式設定檔為執行期 `app/data/accounting_policies.json`。`load_policy(company, voucher, chart)` 先深度合併 `_default` 與公司名稱區塊，再合併程式內 `DEFAULT_POLICY`；公司區塊未指定的巢狀鍵沿用預設值。若沒有公司專屬區塊，使用 `_default`。`policy_sha256` 涵蓋有效合併政策、公司、用途與已解析科目角色；科目角色映射改變時，既有範例 hash 不再相符。

`default-v1` 的角色預設如下：

| 角色 | 預設科目名稱 | 適用說明 |
|---|---|---|
| `import_prepayment` | 預付費用 | 已確認進口類型的進口稅費 |
| `input_tax` | 進項稅額 | 已確認進口類型的海關營業稅 |
| `fees` | 其他費用 | 銀行手續費 |
| `bank` | 銀行存款 | 明示已付款且付款方式為銀行 |
| `cash` | 現金 | 明示已付款且付款方式為現金 |
| `payable_goods` | 應付帳款 | 未付款的進口貨物或商品 |
| `payable_expense` | 應付費用 | 未付款的一般費用 |
| `insurance` | 保險費 | 保險費用途 |

每個角色設定值必須是物件 `{ "name": "科目名稱", "code": "可選科目代碼" }`。有明示 `code` 時以當次科目表解析該代碼；沒有明示代碼時以名稱匹配。設定中的代碼若不在當次科目表，解析結果留空並在分類結果保留 warning，不以相似科目代換。進口政策只在 `importdecl.is_import()` 判定為進口用途時生效；出口類型不套用進口規則。進口預設 `include_goods_values=false`、`posting_currency=TWD`：完整保留外幣發票與明細，但不將外幣貨款與臺幣稅費混合加總。

## 設定範例

`accounting_policies.json` 使用公司名稱作為可選覆寫鍵。以下範例採用正式契約的角色物件與巢狀鍵；`示範公司` 只覆寫手續費角色和進口費用對應，其餘值由深度合併沿用 `_default`。

```json
{
  "_default": {
    "version": "default-v1",
    "roles": {
      "import_prepayment": { "name": "預付費用", "code": "1210" },
      "input_tax": { "name": "進項稅額", "code": "1290" },
      "fees": { "name": "其他費用", "code": "6230" },
      "bank": { "name": "銀行存款", "code": "1110" },
      "cash": { "name": "現金" },
      "payable_goods": { "name": "應付帳款", "code": "2130" },
      "payable_expense": { "name": "應付費用" },
      "insurance": { "name": "保險費" }
    },
    "general": {
      "select_specific_account": true,
      "split_fee_natures": true,
      "separate_observed_vat": true,
      "derive_unprinted_tax": false,
      "insurance_role": "insurance",
      "payroll_split_requires_evidence": true
    },
    "import": {
      "debit_roles": {
        "duty": "import_prepayment",
        "trade_fee": "import_prepayment",
        "commodity_tax": "import_prepayment",
        "business_tax": "input_tax",
        "fee": "fees"
      },
      "payment_sum_fields": ["duty", "trade_fee", "commodity_tax", "business_tax", "fee"],
      "unpaid_role": "payable_goods",
      "include_goods_values": false,
      "posting_currency": "TWD"
    },
    "payment_roles": {
      "bank": "bank",
      "cash": "cash",
      "unpaid_goods": "payable_goods",
      "unpaid_expense": "payable_expense"
    }
  },
  "示範公司": {
    "version": "demo-company-v1",
    "roles": {
      "fees": { "name": "手續費科目", "code": "6299" }
    },
    "import": {
      "debit_roles": { "fee": "fees" }
    }
  }
}
```

修改預設或公司覆寫時，應提高 `version`。`roles` 內每個值必須是包含 `name` 及可選字串 `code` 的物件。顯式代碼若不存在於當次科目表，該角色的 code 留空並由分類結果留下 warning；手續費缺科目時不得併入其他金額。

## 金額來源與欄位用途

每個金額欄位的契約 metadata 描述 `purpose`、`currency`、`policy_key`；它決定金額用途和適用政策角色，不單獨證明讀值正確。每列分錄的 `amount_source` 是金額主來源；舊資料只有一條 `evidence_path` 時可相容地沿用為主來源，兩條以上必須明示主要來源，不依金額相等猜測來源。VAT 借方或貸方金額都必須引用用途為 VAT 且 Decimal 數值相等的來源（例如 5.0 與 5 相等，5.001 與 5 不相等）；多項相加的總額僅可使用程式產生的 `derived_amounts`，不可由模型把若干 OCR 數字自行合併。選取證據只代表送交本次模型的資料，不代表人工已確認。

`broker_note.customs_duty`、`broker_fee`、`edi_fee`、`tax` 各有對應來源用途及政策鍵。通用 `declaration` 的估價金額預設不納入進口分錄；僅由已確認的文件用途及當次政策決定入帳範圍。幣別沿用文件可確認的幣別；進口稅費預設台幣，不與外幣貨款混加。

## 付款狀態

`payment_state` 同時保存文件上的付款原文與正規化結果。狀態限定為 `paid`、`unpaid`、`unknown`；付款方式限定為 `bank`、`cash`、`other`、`unknown`。像「已結清」的原始字樣保留，並正規化為已付款；付款方式未知時不可假設銀行或現金。分類 API 回應附上 `payment_state` 供前端收取，日記帳列提供主金額來源與付款狀態。舊保存資料沒有此欄位時維持可讀，新增輸出可包含其 JSON 值。

## 候選分錄與付款證據

會計模板由 `_accounting_context()` 傳入完整憑證、可用金額來源、程式推導值、科目表及政策。明示已付款時還需付款方式才能選擇 `bank` 或 `cash`；若付款方式不明，列 warning，不強制貸記銀行。未付款進口使用 `payable_goods`；未付款採購／商品使用 `payable_goods`，其他已判為費用的文件使用 `payable_expense`。若收支方向不明，保留候選與警告，不強制貸記銀行或應付款。

缺少金額或科目屬 `candidate`，保留缺漏 warning；不要標成 `skipped`。`skipped` 僅用於佐證附頁，或原始會計傳票保留紙面既有分錄、不再生成新分錄（`reason=original_entries`）。付款狀態、付款方式、金額來源或科目解析有疑義時，將前提記在 `assumptions`／`warnings`。候選分錄不是核定結果。

只有同一公司、政策 `policy_sha256` 與當次一致、狀態為 `proposed`、已核定且金額有效平衡的既有分錄，才可作為會計分類範例。舊版未帶公司或政策範圍的 `entry_examples.json` 不納入提示上下文；應先以有來源、有公司、有政策版本的資料重新建立。

分類輸出由 `classification_meta` 記錄 `status`、`policy_version`、`policy_sha256`、`purpose`、`warnings`、`assumptions` 與 `derived_amounts`。缺金額或科目、有 warning 或 assumption 時為 `candidate`。`skipped` 僅適用佐證附頁，或含原始分錄的 `journal_voucher`：保留原列並記錄 `reason=original_entries`，不另產生或補平衡另一側。會計模型只使用目前已選取的證據；完整證據歷史仍保存。人工修改後會重算日期、金額、統編等目前警告，並保留原始來源與衝突診斷。欄位來源及疑義分別保存在 `field_evidence`、`validation_warnings`；每筆分錄的依據保存在 `JournalEntry.evidence_paths`。金額原文、正規化值和程式推導值須分開保存，不能把推導結果偽裝成單據原文。原始單面傳票再次分類時保留 `original_checks` 並以 `status=skipped`、`reason=original_entries` 結束，不造另一側。檢查欄位含 `complete`、`single_sided`、`observed_totals` 及 `printed_totals_match`；`balance_check` 只代表借貸金額實際相等，不因單面完整就宣稱平衡。日記帳列保留 null 金額與 evidence，忽略舊 `bill_detail` 分錄列；列印附錄含 escaped 來源、驗證與政策紀錄。舊收據稅額語意不明時，`field_evidence.semantic_role=unconfirmed_tax` 保留疑義，canonical 再存不映射為共同營業稅；人工移至 `withholding_tax` 後解除。舊瀏覽器重複儲存不覆蓋服務端 audit/stale 標記。

## 保存、備份與批次版本

現有保存層須繼續讀取沒有新增 metadata 的舊憑證；新版 API 可追加 metadata 欄位，不要求舊記錄補造證據。`accounting_policies.json` 是影響分類結果的設定，已納入系統備份與還原範圍。

批次分類使用 v2 metadata：完整來源清單及 runtime digest 都納入版本摘要；前端、CSV 及 hash v2 已整合。舊批次保留唯讀；不得在舊批次上接續寫入新版本結果，也不得讓 v1 與 v2 設定／來源混用於同一批次。本輪全套離線驗收 107 項通過；前輪 74 項為歷史基準。真實模型比較仍另行待辦。

相關說明：[提示詞改版盤點](prompt-review.md)、[系統架構](architecture.md)。程式介面：[政策載入與角色解析](../../app/services/accounting_policy.py)、[會計分類](../../app/services/classify.py)、[提示模板](../../app/core/prompts.py)、[憑證與分錄契約](../../app/models/schemas.py)。設定檔位置為執行期 `app/data/accounting_policies.json`，不屬於版控文件。
