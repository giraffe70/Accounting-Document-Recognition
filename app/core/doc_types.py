"""單據類型定義登錄表（Router 分流用）。

一份定義同時驅動：
  ① 辨識提示詞（依欄位自動生成）
  ② 專屬欄位契約（extraction 回傳的 fields）
  ③ 審核 UI 動態欄位（前端依此渲染）

新增類型只要在 DOC_TYPES 加一筆即可，三處自動同步。
"""
from copy import deepcopy


def _f(key, label, kind="text"):
    return {"key": key, "label": label, "kind": kind, "nullable": True}


# 各類型：label（顯示名）、fields（欄位契約）、has_items（是否有明細列）
DOC_TYPES = {
    "invoice": {
        "label": "統一發票／電子發票",
        "aliases": ["發票", "電子發票", "統一發票", "invoice", "三聯式", "二聯式",
                    "電子發票證明聯", "收銀機發票"],
        "has_items": True,
        "fields": [
            _f("voucher_id", "發票號碼"), _f("date", "發票日期", "date"),
            _f("invoice_type", "發票格式（三聯式／二聯式／電子發票／收銀機）"),
            _f("tax_type", "課稅別（應稅／零稅率／免稅）"),
            _f("seller_name", "賣方名稱"), _f("seller_tax_id", "賣方統編"),
            _f("buyer_name", "買方名稱"), _f("buyer_tax_id", "買方統編"),
            _f("subtotal", "未稅銷售額", "number"), _f("tax_amount", "營業稅額", "number"),
            _f("total_amount", "總計（含稅）", "number"),
            _f("amount_words", "總計金額中文大寫（如有）"),
            _f("usage_note", "買受人註記（進貨及費用／固定資產）"),
        ],
    },
    "cheque": {
        "label": "支票",
        "aliases": ["支票", "cheque", "check", "本票", "即期支票"],
        "has_items": False,
        "fields": [
            _f("cheque_no", "票號"), _f("issue_date", "發票日", "date"),
            _f("due_date", "到期（票載）日", "date"), _f("drawer", "發票人／出票人"),
            _f("payer_bank", "付款銀行"), _f("payee", "受款人／抬頭"),
            _f("account_no", "帳號"), _f("total_amount", "金額", "number"),
            _f("amount_words", "金額大寫"),
        ],
    },
    "remittance": {
        "label": "匯款單／銀行轉帳水單",
        "aliases": ["匯款", "水單", "匯出", "匯入", "電匯", "remittance", "wire",
                    "轉帳", "網路銀行", "ATM轉帳"],
        "has_items": False,
        "fields": [
            _f("date", "交易日期", "date"), _f("ref_no", "交易序號／建檔批號"),
            _f("remitter", "付款人戶名"), _f("payer_account", "付款帳號"),
            _f("remitter_bank", "付款／匯出銀行"), _f("payment_method", "付款方式"),
            _f("total_amount", "交易（匯款）金額", "number"),
            _f("debit_amount", "扣款金額", "number"), _f("fee", "手續費", "number"),
            _f("currency", "幣別"), _f("payee", "收款人戶名"),
            _f("payee_bank", "收款銀行"), _f("account_no", "收款帳號"),
            _f("payee_code", "收款人代號"),
        ],
    },
    "customs_tax": {
        "label": "海關進口貨物稅費繳納證明",
        "aliases": ["海關", "繳稅證明", "稅費繳納", "進口稅", "快遞貨物稅費", "UPS繳稅",
                    "稅費繳納證明", "稅金繳納證明"],
        "has_items": False,
        "fields": [
            _f("tax_bill_no", "稅單號碼"), _f("taxpayer", "納稅義務人"),
            _f("tax_id", "統一編號"), _f("import_date", "貨物進口日期", "date"),
            _f("ship_name", "船（機）名及呼號（班次）"), _f("broker_box", "報關行箱號"),
            _f("bl_no", "提裝貨單號"), _f("declaration_no", "報單號碼"),
            _f("goods_no", "貨名／稅則號列"), _f("qty", "件數"),
            _f("courier", "快遞業者／列印者代號"), _f("payment_date", "填發（繳款）日期", "date"),
            _f("duty", "進口稅", "number"), _f("trade_fee", "推廣貿易服務費", "number"),
            _f("business_tax", "營業稅", "number"), _f("total_amount", "稅費合計", "number"),
            _f("business_tax_base", "營業稅稅基", "number"), _f("issuer", "核發機關"),
        ],
    },
    "export_declaration": {
        "label": "出口報單",
        "aliases": ["出口報單", "出口報關", "export declaration", "外貨復出口", "外貨出口"],
        "has_items": True,    # 保留首項欄位相容舊紀錄，多項貨物以 items 保存
        "fields": [
            _f("transport_mode", "海空運別(1)"), _f("doc_category", "報單類別(2)"),
            _f("declaration_no", "報單號碼(3)"), _f("customs_clear_no", "海關通關號碼(4)"),
            _f("ship_code", "出口船機代碼(5)"), _f("voyage", "船舶航次／船機班次(6)"),
            _f("date", "報關日期(7)", "date"), _f("loading_port", "裝貨港名稱及代碼(8)"),
            _f("dest_country", "目的地國家及代碼(9)"), _f("storage_code", "卸存地點代碼(10)"),
            _f("master_awb", "託運單主號(11)"), _f("house_awb", "託運單分號(12)"),
            _f("refund_material_tax", "申請沖退原料稅(13)"), _f("transport_method", "運輸方式(14)"),
            _f("ship_name", "出口船舶名稱(15)"), _f("invoice_amount", "發票總金額(16)", "number"),
            _f("freight", "運費(17)", "number"), _f("insurance", "保險費(18)", "number"),
            _f("add_fee", "應加費用(19)", "number"), _f("deduct_fee", "應減費用(20)", "number"),
            _f("total_fob_twd", "總離岸價格新臺幣(21)", "number"),
            _f("exporter", "出口人名稱地址(22)"), _f("exporter_tax_id", "出口人統一編號(23)"),
            _f("exporter_customs_no", "出口人海關監管編號(24)"), _f("payment_method", "繳納方式代碼(25)"),
            _f("buyer", "買方名稱地址(26)"), _f("buyer_country", "買方國家代碼(27)"),
            _f("buyer_tax_id", "買方統一編號(28)"), _f("buyer_customs_no", "買方海關監管編號(29)"),
            _f("price_term", "單價條件代碼(30)"), _f("exchange_rate", "匯率(31)", "number"),
            _f("item_no", "項次(32)"), _f("goods_desc", "貨物名稱商標(牌名)及規格(33)"),
            _f("permit_no", "輸出入許可文件號碼-項次(34)"),
            _f("hs_code", "輸出入貨品分類號列／統計號別／檢查號碼(35)"),
            _f("unit_price", "單價／幣別／金額(36)"), _f("net_weight", "淨重公斤(37)", "number"),
            _f("quantity", "數量(單位)(38)"), _f("stat_use", "統計用(39)"),
            _f("fob_value_twd", "離岸價格FOB Value新臺幣(40)", "number"),
            _f("stat_method", "統計方式(41)"), _f("total_packages", "總件數(單位)(42)"),
            _f("packing_desc", "包裝說明(43)"), _f("gross_weight", "總毛重公斤(44)", "number"),
            _f("marks", "標記及貨櫃號碼(45)"), _f("other_declared", "其它申報事項(46)"),
            _f("broker", "報關人／AEO編號(47)"), _f("specialist", "專責人員(48)"),
        ],
    },
    "import_declaration": {
        "label": "進口報單",
        "aliases": ["進口報單", "進口報關", "import declaration", "外貨進口"],
        "has_items": True,
        "fields": [
            _f("doc_category", "報單類別"), _f("declaration_no", "報單號碼"),
            _f("customs_clear_no", "海關通關號碼"), _f("date", "報關日期", "date"),
            _f("import_date", "進口日期", "date"), _f("ship_name", "進口船（機）名及呼號（班次）"),
            _f("bl_no", "提單號碼"), _f("loading_port", "裝貨港（代碼及名稱）"),
            _f("origin_port", "起運口岸（代碼及名稱）"), _f("transport_method", "運輸方式"),
            _f("taxpayer", "納稅義務人名稱地址"), _f("taxpayer_tax_id", "納稅義務人統一編號"),
            _f("customs_supervision", "海關監管編號"), _f("seller", "賣方名稱地址"),
            _f("seller_country", "賣方國家代碼"), _f("tax_method", "納稅辦法"),
            _f("permit_no", "輸入許可證號碼"), _f("price_term", "條件（起岸價格條件）"),
            _f("currency", "幣別"), _f("exchange_rate", "匯率", "number"),
            _f("invoice_amount", "發票金額", "number"), _f("freight", "運費", "number"),
            _f("insurance", "保險費", "number"), _f("total_cif_twd", "完稅價格總計新臺幣", "number"),
            _f("duty", "進口稅", "number"), _f("business_tax", "營業稅", "number"),
            _f("trade_fee", "推廣貿易服務費", "number"), _f("commodity_tax", "貨物稅", "number"),
            _f("total_tax", "稅費合計", "number"), _f("gross_weight", "總毛重公斤", "number"),
            _f("total_packages", "總件數及單位"), _f("broker", "報關人／AEO編號"),
            _f("specialist", "專責人員"),
        ],
    },
    "broker_note": {
        "label": "報關行收費通知單",
        "aliases": ["收費通知單", "報關行", "船務", "報關費用", "報關行對帳",
                    "customs broker"],
        "has_items": True,
        "fields": [
            _f("doc_no", "單號"), _f("date", "日期", "date"),
            _f("broker", "報關行名稱"), _f("client", "委託人／客戶"),
            _f("declaration_no", "報單號碼"), _f("bl_no", "B/L S/O NO"),
            _f("customs_date", "報關日期", "date"), _f("vessel", "船名"),
            _f("port", "進出口岸"), _f("goods_name", "貨名"), _f("qty", "件數"),
            _f("customs_duty", "關稅／滯報費", "number"),
            _f("broker_fee", "報關手續費", "number"),
            _f("edi_fee", "海關鍵輸費", "number"),
            _f("subtotal", "費用合計", "number"), _f("tax", "稅金", "number"),
            _f("total_amount", "總計", "number"), _f("prepaid", "預收費", "number"),
            _f("balance", "應收（退）金額", "number"),
        ],
    },
    "declaration": {
        "label": "進出口報單（未分進出口）",
        "aliases": ["報單", "報關", "declaration"],
        "has_items": True,
        "fields": [
            _f("declaration_no", "報單號碼"), _f("doc_category", "報單類別"),
            _f("date", "報關日期", "date"), _f("direction", "進出口別"),
            _f("trader", "出口人／納稅義務人"), _f("tax_id", "統一編號"),
            _f("counterparty", "買方／賣方"), _f("total_amount", "總離岸／完稅價格新臺幣", "number"),
            _f("currency", "幣別"),
        ],
    },
    "tax_bill": {
        "label": "稅款繳款書",
        "aliases": ["繳款書", "稅款", "自行繳納", "所得稅", "營業稅繳款", "國稅", "營所稅"],
        "has_items": False,
        "fields": [
            _f("tax_agency", "徵收機關"), _f("tax_code", "稅目代碼"),
            _f("tax_type", "稅目名稱"), _f("period", "所屬年度／期別"),
            _f("payment_no", "銷帳編號"), _f("taxpayer", "納稅義務人／營利事業名稱"),
            _f("tax_id", "統一編號"), _f("responsible_person", "負責人"),
            _f("principal", "本稅", "number"), _f("surcharge", "滯納金／加計利息", "number"),
            _f("total_amount", "應繳金額合計", "number"),
            _f("pay_start", "繳納期間起", "date"), _f("due_date", "繳納期限止", "date"),
            _f("phone", "納稅義務人電話"),
        ],
    },
    "utility": {
        "label": "水電費／公用事業帳單",
        "aliases": ["水費", "電費", "瓦斯", "台電", "自來水", "公用事業", "帳單", "自來水公司",
                    "電信", "電話費", "中華電信", "繳費通知", "繳費結果"],
        "has_items": False,
        "fields": [
            _f("provider", "事業機構"), _f("period", "計費（用水／用電）期間"),
            _f("customer_name", "用戶名稱"), _f("customer_tax_id", "用戶統一編號"),
            _f("account", "水號／電號／用戶號"), _f("meter_no", "水／電表表號"),
            _f("usage", "使用度數"), _f("basic_fee", "基本費", "number"),
            _f("usage_fee", "用水／用電費", "number"), _f("subtotal", "未稅小計", "number"),
            _f("business_tax", "營業稅", "number"), _f("levy_fee", "代徵／附加費用", "number"),
            _f("total_amount", "本期應繳合計", "number"), _f("pay_start", "繳費起始日", "date"),
            _f("due_date", "繳費期限", "date"), _f("debit_date", "預定扣繳日", "date"),
            _f("pay_account", "委託代繳帳號"),
        ],
    },
    "receipt": {
        "label": "收據／雜項開銷",
        "aliases": ["收據", "運費", "開銷", "雜支", "receipt", "免用統一發票", "勞務報酬"],
        "has_items": True,
        "fields": [
            _f("voucher_id", "單號／收據號"), _f("date", "日期", "date"),
            _f("receipt_type", "收據類型（免用統一發票／一般收據／勞務報酬）"),
            _f("vendor", "廠商／收款人"), _f("tax_id", "統一編號"),
            _f("item_desc", "品名／事由"), _f("subtotal", "未稅", "number"),
            _f("tax_amount", "營業稅額", "number"), _f("withholding_tax", "扣繳稅額", "number"),
            _f("total_amount", "金額", "number"),
        ],
    },
    "statement": {
        "label": "對帳單／應收帳款明細表",
        "aliases": ["對帳單", "statement", "月結", "月結單", "應收帳款明細表",
                    "應付帳款明細表", "帳款明細", "應收帳款", "應付帳款", "明細表",
                    "對帳", "請款單"],
        "has_items": True,
        "fields": [
            _f("statement_no", "對帳單號／單號"), _f("date", "日期", "date"),
            _f("period", "交易日區間／結帳期間"),
            _f("issuer", "開立廠商（賣方／收款方）"), _f("issuer_tax_id", "開立廠商統編"),
            _f("customer", "客戶名稱（買方／付款方）"), _f("customer_tax_id", "客戶統一編號"),
            _f("payment_term", "付款方式／條件"),
            _f("ref_invoice_no", "表列發票號碼（勾稽用，非本憑證號碼）"),
            _f("subtotal", "銷貨合計額（未稅）", "number"),
            _f("tax_amount", "營業稅額", "number"),
            _f("total_amount", "應收（應付）帳款總額", "number"),
            _f("bank_account", "銀行資料（戶名／銀行／帳號）"),
            _f("remark", "備註"),
        ],
    },
    "purchase_order": {
        "label": "訂購單／採購單",
        "aliases": ["訂購單", "採購單", "purchase order", "訂單", "po"],
        "has_items": True,
        "fields": [
            _f("po_no", "採購單號"), _f("date", "訂購日期", "date"),
            _f("buyer", "採購方（買方）"), _f("buyer_tax_id", "買方統編"),
            _f("supplier", "供應商（賣方）"), _f("supplier_code", "供應商代碼"),
            _f("supplier_tax_id", "供應商統編"), _f("contact", "聯絡人"),
            _f("currency", "幣別"), _f("payment_term", "付款條件"),
            _f("delivery_date", "交貨／需求日期", "date"),
            _f("total_amount", "總金額", "number"),
        ],
    },
    "fda_import": {
        "label": "食品輸入許可通知",
        "aliases": ["輸入許可", "食品藥物管理署", "食藥署", "食品及相關產品輸入",
                    "輸入許可通知", "food and drug"],
        "has_items": False,
        "fields": [
            _f("permit_no", "申請書號碼"), _f("fda_no", "FDA中字號"),
            _f("import_date", "進口日期", "date"), _f("applicant", "報驗義務人"),
            _f("applicant_tax_id", "報驗義務人統編"), _f("goods_name", "貨物名稱（品名）"),
            _f("ccc_code", "貨品分類號列（C.C.C.Code）"),
            _f("declaration_no", "報單號碼及項次"),
            _f("manufactured_date", "製造日期", "date"), _f("expiration_date", "有效日期", "date"),
            _f("trademark", "商標（牌名）"), _f("manufacturer", "製造廠名稱"),
            _f("spec", "規格"), _f("lot_no", "製造批號"),
            _f("origin_country", "生產國別"), _f("quantity", "數量"),
            _f("net_weight", "淨重"), _f("remark", "備註"),
        ],
    },
    "social_insurance": {
        "label": "勞健保費繳款／繳費證明",
        "aliases": ["勞工保險局", "投保單位繳費證明", "勞保費", "健保費", "勞退",
                    "勞工退休金", "健康保險", "衛生福利部中央健康保險署", "計費保險費",
                    "投保單位", "繳費證明書"],
        "has_items": True,
        "fields": [
            _f("agency", "主辦機關（勞保局／健保署）"), _f("insurance_type", "保險別"),
            _f("unit_name", "投保單位名稱"), _f("unit_no", "投保單位代號"),
            _f("period", "計費年月"), _f("payment_no", "銷帳編號"),
            _f("payment_date", "繳納日期", "date"), _f("due_date", "繳款期限", "date"),
            _f("employer_share", "單位負擔金額（如單據有列）", "number"),
            _f("employee_share", "個人負擔金額（如單據有列）", "number"),
            _f("surcharge", "滯納金", "number"),
            _f("total_amount", "應繳／已繳總金額", "number"),
        ],
    },
    "bill_detail": {
        "label": "帳單費用明細（附頁佐證）",
        # 刻意不含「繳費結果通知」：實測中華電信案的繳費結果通知印有繳費總金額與
        # 扣款帳戶，本身就是入帳依據（該批文件內沒有發票頁），誤列為佐證會讓整案零分錄。
        "aliases": ["費用明細清單", "費用明細", "通信費明細", "帳單明細", "話費明細"],
        "has_items": True,
        "supporting": True,     # 佐證附頁：不獨立產生分錄（入帳依據是同批的發票／繳費憑證）
        "fields": [
            _f("provider", "事業機構"), _f("customer_name", "用戶名稱"),
            _f("account", "用戶帳號／號碼"), _f("period", "計費期間"),
            _f("page_no", "頁次"), _f("total_amount", "本頁應繳金額合計", "number"),
        ],
    },
    "journal_voucher": {
        "label": "會計傳票（記帳憑證）",
        "aliases": ["傳票", "轉帳傳票", "收入傳票", "支出傳票", "記帳憑證",
                    "journal voucher", "分錄傳票"],
        "has_items": False,
        "has_entries": True,      # 明細是借貸分錄，不是品名/數量/單價
        "fields": [
            _f("voucher_kind", "傳票單別（如 A911 轉帳傳票）"),
            _f("voucher_id", "傳票單號"),
            _f("date", "傳票日期", "date"),
            _f("prepared_date", "製表日期", "date"),
            _f("company_name", "公司名稱（表頭）"),
            _f("page_no", "頁次（如 1/2）"),
            _f("remark", "備註"),
            _f("debit_total", "借方合計", "number"),
            _f("credit_total", "貸方合計", "number"),
        ],
    },
    "other": {
        "label": "其他憑證",
        "aliases": ["其他", "other"],
        "has_items": True,
        "fields": [
            _f("voucher_id", "單號"), _f("date", "日期", "date"),
            _f("seller_name", "賣方／來源"), _f("seller_tax_id", "賣方統編"),
            _f("buyer_name", "買方／對象"), _f("buyer_tax_id", "買方統編"),
            _f("subtotal", "未稅", "number"), _f("tax_amount", "稅額", "number"),
            _f("total_amount", "總計", "number"),
        ],
    },
}

ROUTER_LABELS = {k: v["label"] for k, v in DOC_TYPES.items()}

# 密集／多欄／多聯表單：不要求 VLM 輸出 boxes（框改由 OCR 精修取得），
# 避免 VLM 在密集版面把 [x,y,x,y] 座標誤塞進欄位值。
NO_VLM_BOX_TYPES = {"export_declaration", "import_declaration", "declaration",
                    "customs_tax", "tax_bill", "utility", "broker_note",
                    "purchase_order", "fda_import", "statement", "journal_voucher",
                    "bill_detail", "social_insurance"}


def is_supporting(type_key: str) -> bool:
    """佐證附頁（純帳單費用明細）不獨立產生會計分錄。

    一份帳單常有數頁：發票／繳費憑證是入帳依據，其餘明細頁只是佐證。
    若每頁都各自產生分錄再加總，金額會嚴重灌水（實測中華電信案：正解 3,532、
    加總後 646 萬）。
    """
    return bool(get(type_key).get("supporting"))


def get(type_key: str) -> dict:
    return DOC_TYPES.get(type_key, DOC_TYPES["other"])


def receipt_tax_ambiguous(type_key: str, fields: dict, evidence: dict | None = None) -> bool:
    """Legacy receipt tax had no VAT/withholding distinction; require confirmation."""
    if type_key != "receipt" or fields.get("tax_amount") is None:
        return False
    current = [r for r in (evidence or {}).get("fields.tax_amount", []) if r.get("selected")]
    if any(r.get("source") == "manual" for r in current):
        return False
    return "withholding_tax" not in fields or any(r.get("semantic_role") == "unconfirmed_tax" for r in current)


# 只保存定位與語意；格式、算術與公司政策由程式負責。
TYPE_HINTS = {
    "invoice": ["日期取填發日，不是發票期別。賣方資料可能在專用章；amount_words 抄錄大寫原文。"],
    "cheque": ["只讀支票區域；出票人與受款人不同。amount_words 抄錄票面大寫原文。"],
    "remittance": ["交易金額是本金，扣款金額及手續費分別讀取；付款人與收款人分開。"],
    "customs_tax": ["多聯內容相同時讀完整一聯，不跨聯混填；營業稅稅基不是營業稅。"],
    "export_declaration": ["按商品序號逐列讀取全部可見貨物及續頁；32至41格首項仍填原 fields，items 不重複底列合計。"],
    "import_declaration": ["納稅義務人是進口商，賣方是國外出口商；items.amount 讀完稅價格新臺幣。"],
    "broker_note": ["代墊稅費與報關勞務費分開；預收費、應收退金額不是總計。"],
    "declaration": ["direction 只依文件明示的進口或出口填入。"],
    "tax_bill": ["多聯讀完整一聯；銷帳編號是識別碼，繳納期間不是填發日。"],
    "utility": ["總額讀獨立標示的繳費總金額；用戶號碼不是金額，預定扣繳不代表已付款。"],
    "receipt": ["廠商資料可能在印章。扣繳稅額填 withholding_tax；tax_amount 僅讀明示營業稅。"],
    "statement": ["表列發票號碼是參照號，填 ref_invoice_no。issuer 是開立方，customer 是客戶。"],
    "purchase_order": ["buyer 是採購方，supplier 是供應商；採購訂單不代表已付款。"],
    "fda_import": ["applicant 是報驗進口商，manufacturer 是生產廠；許可通知不是稅費繳款證明。"],
    "social_insurance": ["單位與個人負擔只有文件分列才讀取；每項費用代號填 items.fee_code，不猜代號。"],
    "bill_detail": ["只讀純佐證費用明細；含繳費總額的公用事業或電信通知用 utility，其他付款文件依用途分類。"],
    "journal_voucher": ["entries 照印出各列讀取；借貸依所在欄，單面傳票不補另一面；底列合計不是分錄。"],
    "other": [],
}
ITEM_FIELDS = [_f("description", "品名／事由"), _f("quantity", "數量", "number"),
               _f("unit_price", "單價", "number"), _f("amount", "列金額", "number")]
ENTRY_FIELDS = [_f("account_code", "科目代號"), _f("account_name", "科目名稱"),
                _f("description", "摘要"), _f("dept", "部門"), _f("offset_code", "立沖帳目"),
                _f("debit", "借方金額", "number"), _f("credit", "貸方金額", "number")]
COMMON_FIELDS = {
    "voucher_id": ["voucher_id", "doc_no", "tax_bill_no", "payment_no", "po_no", "permit_no",
                   "cheque_no", "declaration_no", "statement_no", "account"],
    "date": ["date", "issue_date", "payment_date", "import_date", "pay_start", "due_date"],
    "subtotal": ["subtotal"], "tax_amount": ["tax_amount", "business_tax", "tax"],
    "total_amount": ["total_amount", "total_fob_twd", "total_cif_twd", "total_tax", "principal"],
    "seller_name": ["seller_name", "seller", "exporter", "supplier", "manufacturer", "vendor",
                    "payee", "taxpayer", "trader", "provider", "broker", "issuer", "agency"],
    "seller_tax_id": ["seller_tax_id", "exporter_tax_id", "supplier_tax_id", "tax_id", "issuer_tax_id"],
    "buyer_name": ["buyer_name", "buyer", "remitter", "counterparty", "applicant", "customer", "customer_name", "unit_name"],
    "buyer_tax_id": ["buyer_tax_id", "taxpayer_tax_id", "applicant_tax_id", "customer_tax_id"],
}
for _key, _spec in DOC_TYPES.items():
    _spec["type_hints"] = TYPE_HINTS[_key]
    _spec["item_fields"] = deepcopy(ITEM_FIELDS) if _spec.get("has_items") else []
    _spec["entry_fields"] = deepcopy(ENTRY_FIELDS) if _spec.get("has_entries") else []
    _allowed_fields = {f["key"] for f in _spec["fields"]}
    if _key == "import_declaration":
        _allowed_fields.update(("buyer", "buyer_tax_id"))
    _spec["common_fields"] = {name: [key for key in keys if key in _allowed_fields]
                              for name, keys in COMMON_FIELDS.items()}
    if not _spec.get("supporting") and not _spec.get("has_entries"):
        _existing = {f["key"] for f in _spec["fields"]}
        for _field in [_f("payment_status", "明示付款狀態"), _f("payment_method", "明示付款方式")]:
            if _field["key"] not in _existing:
                _spec["fields"].append(_field)
DOC_TYPES["import_declaration"]["item_fields"] += [_f("hs_code", "貨品分類號列"), _f("origin_country", "生產國別")]
DOC_TYPES["broker_note"]["fields"].append(_f("direction", "明示進出口別"))
DOC_TYPES["social_insurance"]["item_fields"].append(_f("fee_code", "費用代號"))
DOC_TYPES["export_declaration"]["fields"].append(_f("currency", "外幣發票幣別"))
DOC_TYPES["statement"]["fields"].append(_f("currency", "帳款幣別"))
DOC_TYPES["export_declaration"]["item_fields"] += [
    _f("item_no", "商品序號"), _f("hs_code", "貨品分類號列"), _f("origin_country", "生產國別"),
    _f("brand", "廠牌"), _f("unit", "計價單位"), _f("stat_qty", "統計數量", "number"),
    _f("stat_unit", "統計單位"), _f("net_weight", "淨重公斤", "number")]
for _fob_field in DOC_TYPES["export_declaration"]["item_fields"]:
    if _fob_field["key"] == "amount":
        _fob_field["label"] = "商品離岸價格新臺幣"

# 金額用途、幣別與政策鍵由契約登錄；數量/重量不會成為可記帳金額。
# $document 表示文件明示幣別，無法確認時保留 unknown。
def _money(purpose, currency="TWD", policy_key=None):
    return {"purpose": purpose, "currency": currency, "policy_key": policy_key}


MONEY_CONTRACTS = {
    "invoice": {"subtotal": _money("subtotal"), "tax_amount": _money("vat", policy_key="business_tax"), "total_amount": _money("document_total")},
    "cheque": {"total_amount": _money("document_total")},
    "remittance": {"total_amount": _money("document_total", "$document"), "debit_amount": _money("payment_total", "$document"), "fee": _money("fee", "$document", "fee")},
    "customs_tax": {"duty": _money("duty", policy_key="duty"), "trade_fee": _money("tax_fee", policy_key="trade_fee"), "business_tax": _money("vat", policy_key="business_tax"), "total_amount": _money("document_total")},
    "import_declaration": {"invoice_amount": _money("goods_value", "$document"), "freight": _money("valuation_adjustment", "$document"), "insurance": _money("valuation_adjustment", "$document"), "total_cif_twd": _money("goods_value"), "duty": _money("duty", policy_key="duty"), "business_tax": _money("vat", policy_key="business_tax"), "trade_fee": _money("tax_fee", policy_key="trade_fee"), "commodity_tax": _money("tax_fee", policy_key="commodity_tax"), "total_tax": _money("document_total")},
    "export_declaration": {"invoice_amount": _money("goods_value", "$document"), "freight": _money("valuation_adjustment", "$document"), "insurance": _money("valuation_adjustment", "$document"), "add_fee": _money("valuation_adjustment", "$document"), "deduct_fee": _money("valuation_adjustment", "$document"), "total_fob_twd": _money("goods_value"), "fob_value_twd": _money("goods_value")},
    "broker_note": {"customs_duty": _money("duty", policy_key="duty"), "broker_fee": _money("fee", policy_key="fee"), "edi_fee": _money("fee", policy_key="fee"), "subtotal": _money("subtotal"), "tax": _money("vat", policy_key="business_tax"), "total_amount": _money("document_total"), "prepaid": _money("prepayment"), "balance": _money("outstanding")},
    "declaration": {"total_amount": _money("goods_value")},
    "tax_bill": {"principal": _money("tax_fee"), "surcharge": _money("fee"), "total_amount": _money("document_total")},
    "utility": {"basic_fee": _money("fee"), "usage_fee": _money("fee"), "subtotal": _money("subtotal"), "business_tax": _money("vat", policy_key="business_tax"), "levy_fee": _money("fee"), "total_amount": _money("document_total")},
    "receipt": {"subtotal": _money("subtotal"), "tax_amount": _money("vat", policy_key="business_tax"), "withholding_tax": _money("withholding"), "total_amount": _money("document_total")},
    "statement": {"subtotal": _money("subtotal", "$document"), "tax_amount": _money("vat", "$document", "business_tax"), "total_amount": _money("document_total", "$document")},
    "purchase_order": {"total_amount": _money("document_total", "$document")},
    "fda_import": {},
    "social_insurance": {"employer_share": _money("fee"), "employee_share": _money("fee"), "surcharge": _money("fee"), "total_amount": _money("document_total")},
    "bill_detail": {"total_amount": _money("document_total")},
    "journal_voucher": {"debit_total": _money("printed_total"), "credit_total": _money("printed_total")},
    "other": {"subtotal": _money("subtotal"), "tax_amount": _money("vat", policy_key="business_tax"), "total_amount": _money("document_total")},
}
for _key, _spec in DOC_TYPES.items():
    _spec["default_currency"] = None if any(f["key"] == "currency" for f in _spec["fields"]) else "TWD"
    for _field in _spec["fields"]:
        if _field["key"] in MONEY_CONTRACTS[_key]:
            _field["money"] = MONEY_CONTRACTS[_key][_field["key"]]
    for _field in _spec["item_fields"]:
        if _field["key"] == "amount":
            _field["money"] = _money("goods_value" if _key in ("import_declaration", "export_declaration", "declaration") else "line_amount",
                "TWD" if _key in ("export_declaration", "declaration") else "$document")
    _spec["common_money"] = {name: next((MONEY_CONTRACTS[_key][alias] for alias in _spec["common_fields"].get(name, []) if alias in MONEY_CONTRACTS[_key]), None)
                            for name in ("subtotal", "tax_amount", "total_amount")}


def build_extract_prompt(type_key: str, **kwargs) -> str:
    """相容入口；圖片及純文字均使用同一生成器。"""
    from app.core.prompts import build_extract_prompt as build
    return build(type_key, **kwargs)
