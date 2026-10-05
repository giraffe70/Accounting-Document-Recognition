"""單據類型定義登錄表（Router 分流用）。

一份定義同時驅動：
  ① 辨識提示詞（依欄位自動生成）
  ② 專屬欄位契約（extraction 回傳的 fields）
  ③ 審核 UI 動態欄位（前端依此渲染）

新增類型只要在 DOC_TYPES 加一筆即可，三處自動同步。
"""


def _f(key, label, kind="text"):
    return {"key": key, "label": label, "kind": kind}


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
        "extra": ("本表單為台灣統一發票（三聯式／二聯式／電子發票證明聯／收銀機發票）。"
                  "統一編號為 8 位純數字；電話號碼（含 - 或括號、位數非 8）不是統編，切勿填入統編欄，寧可留空。"
                  "課稅別看勾選（✓）填『應稅』『零稅率』或『免稅』其一。"
                  "營業稅額通常＝未稅銷售額×5%，仍以圖為準。"
                  "買方多為仲彬股份有限公司(統編23051677)、中意企業股份有限公司(31182666)、天干；"
                  "賣方為開立發票之廠商。賣方統編常蓋在『統一發票專用章』內——印章顏色可能為"
                  "紅、藍、黑或紫，通常位於發票下方或右側，章內含賣方公司名、8 位統一編號與負責人；"
                  "請務必從印章內讀出賣方統編填入 seller_tax_id。"
                  "三聯式買受人註記欄若勾『固定資產』填『固定資產』，否則填『進貨及費用』。"
                  "**金額自我校驗（手寫三聯式尤其重要）**："
                  "① 手寫發票下方多有『總計新台幣（中文大寫）』，請讀入 amount_words；"
                  "大寫字（壹貳參肆伍陸柒捌玖拾佰仟萬）比手寫阿拉伯數字好辨認，"
                  "**大寫與阿拉伯數字不一致時，以大寫為準**回填 total_amount。"
                  "② 應稅發票應滿足『未稅銷售額 ＋ 營業稅額 ＝ 總計』；"
                  "若三個數字兜不攏，請回到圖上重新核對（別直接輸出矛盾的數字）。"
                  "③ 欄位在單據上空白就留空，**不要用其他欄位回推或編造**"
                  "（金額欄尤其嚴格：寧可留空給人工填，也不要給錯的數字）。"),
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
        "extra": ("本表單為支票／本票。金額以票面『國字大寫』為準轉為數字（如玖仟肆佰貳拾貳圓整＝9422）；"
                  "票號多在右上角（如 TD3642163），下方 MICR 磁碼含票號/帳號/行庫代號。"
                  "受款人（抬頭）為票據抬頭之公司；發票人為出票公司。日期為民國年，請轉西元。"
                  "注意：掃描頁若同時含信封、傳票或其他單據，只擷取『支票本身』的內容，不要混入其他單據。"),
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
        "extra": ("本表單為銀行匯款單／網路銀行轉帳水單。交易（匯款）金額為本金；"
                  "扣款金額＝交易金額＋手續費（手續費若『內扣』則兩者相同）。"
                  "付款人多為本公司（仲彬／中意／天干），收款人為受款廠商。"
                  "收款銀行常以代碼＋名稱呈現（如 007 第一銀行）。"),
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
        "extra": ("本表單為「海關進口（快遞）貨物稅費繳納證明」。一頁常有三聯（第一聯收據聯／第二聯扣抵聯／"
                  "第三聯副本），內容相同，取任一聯完整填入即可，不要跨聯混填。右側稅費項目表含進口稅、"
                  "推廣貿易服務費、營業稅、稅費合計、營業稅稅基，請逐項擷取為純數字。"
                  "報單號碼可與進口報單勾稽。日期為民國年（如 1150524＝民國115年5月24日）。"),
    },
    "export_declaration": {
        "label": "出口報單",
        "aliases": ["出口報單", "出口報關", "export declaration", "外貨復出口", "外貨出口"],
        "has_items": False,   # 48 格已含明細區(32-41)，主要明細以欄位呈現
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
        "extra": ("本表單為台灣海關「出口報單」，共 48 個欄位，標題旁均有括號編號，請「依編號」在對應格位定位擷取，避免張冠左右鄰格。"
                  "direction 固定為「出口」。declaration_no 請含開頭英文別碼與完整編號（如 CX/ /15/696/D6341）。"
                  "欄位 32~41 為貨物明細第一項的各欄；若有多項貨物，仍以第一項為主填入對應欄位。"),
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
        "extra": ("本表單為台灣海關「進口報單」，各欄位標題旁有括號編號，請依標題文字定位並讀出其編號（本欄位契約未逐一標號，"
                  "請以表單上實際印出的編號與標題為準）。direction 固定為「進口」。"
                  "納稅義務人為進口商、賣方為國外出口商；完稅價格以 CIF 為基礎；"
                  "稅費含進口稅、營業稅、推廣貿易服務費、貨物稅，請分別擷取並加總為稅費合計。"
                  "逐項貨物明細放入 items：description=貨物名稱及規格、hs_code=貨品分類號列（C.C.C.Code）、"
                  "quantity=數量、unit_price=單價、amount=完稅價格新臺幣、origin_country=生產國別。"),
    },
    "broker_note": {
        "label": "報關行收費通知單",
        "aliases": ["收費通知單", "報關行", "船務", "報關費用", "報關行對帳",
                    "強泰", "customs broker"],
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
        "extra": ("本表單為報關行開立的『收費通知單／費用明細』（進出口報關費用）。"
                  "左右兩欄列出各項費別與金額（關稅/滯報費、服務車資、驗對證費、理貨工資、"
                  "報關手續費、海關鍵輸費等），請把每一有金額的費別放入 items："
                  "description=費別名稱、amount=金額。關稅/滯報費(customs_duty)為代墊稅費、"
                  "報關手續費(broker_fee)為勞務費，請分別擷取為頂層欄位。"
                  "下方有費用合計、稅金、總計、預收費、應收（退）金額。金額純數字、"
                  "日期民國轉西元；報單號碼可與進口／出口報單勾稽；委託人多為本公司（仲彬／中意／天干）。"),
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
        "extra": "若能判斷是進口或出口報單，direction 請填「進口」或「出口」。",
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
        "extra": ("本表單為稅款繳款書（如營利事業所得稅結算自行繳納、營業稅等）。稅目代碼如 357（國稅）。"
                  "銷帳編號通常在底部條碼下方（如 A110016601654155011）。一頁常含三聯（收據／證明／收款機構留存），"
                  "內容相同取其一。金額純數字，日期民國年轉西元（如 1150531→2026-05-31）。"),
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
        "extra": ("本表單為水費／電費等公用事業帳單（台灣自來水公司、台電等）。"
                  "自 114 年 2 月份起，有統一編號之用戶水費項目為『含稅價』，另列示未稅小計與營業稅。"
                  "本期應繳合計＝水（電）費項目小計＋代徵／附加費用小計。"
                  "**total_amount 優先取帳單上獨立標示的「繳費總金額」「本期應繳總金額」"
                  "「應繳金額」欄位值**（通常在表頭的方框內，如中華電信繳費結果通知的"
                  "『繳費總金額(元) $3,532』）；不要拿下方費用明細裡的單項金額充當總額。"
                  "水號／電號為用戶識別碼；日期民國年轉西元。用戶多為本公司（天干／中意／仲彬）。"
                  "亦涵蓋電信帳單（如中華電信）與其繳費通知／繳費結果通知。"
                  "**識別碼絕對不是金額**：用戶號碼、電話號碼、水號電號、銷帳編號、"
                  "帳號、發票號碼都是識別碼——這些數字常有 8～12 位、沒有小數點也沒有千分位逗號，"
                  "**一律不得填入任何金額欄位**（basic_fee／usage_fee／subtotal／total_amount 等）。"
                  "金額欄只填帳單上標示為「元」或帶千分位／小數點的費用數字；"
                  "找不到對應金額就留空，寧可空著也不要拿識別碼充數。"),
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
            _f("tax_amount", "稅額", "number"), _f("total_amount", "金額", "number"),
        ],
        "extra": ("本表單為收據（可能為免用統一發票收據、一般收據、勞務報酬單、代收轉付收據等），"
                  "未必有統一編號或營業稅。統一編號為 8 位純數字，電話不是統編勿填入。"
                  "請擷取收款人／廠商、事由／品名、金額與日期；有扣繳稅額才填 tax_amount。"),
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
        "extra": ("本表單為對帳單／應收帳款（應付帳款）明細表，**不是統一發票**。"
                  "表格內的「發票號碼」欄是用來勾稽的既有發票號，請放入 ref_invoice_no，"
                  "**不要**把它當成本憑證的憑證號碼或視為發票。"
                  "抬頭（表頭最上方）的公司為開立廠商＝賣方／收款方；"
                  "「客戶名稱」為買方／付款方，多為本公司（仲彬／中意／天干）。"
                  "統一編號只在單據上明確印出時才填，**沒印出就留空，絕對不要推測或編造**；"
                  "單據上若只有一個統編（通常標示於客戶欄），那是客戶（買方）的，填入 customer_tax_id。"
                  "下方常有銷貨合計額、營業稅額、應收帳款總額三個數字，請分別擷取為純數字。"
                  "明細每一列放入 items：description=商品名稱及規格、quantity=數量、"
                  "unit_price=單價、amount=**金額欄（未稅）**（空白列略過）。"
                  "明細表常有數量／單價留白的列（如運費），該欄空白就留空，"
                  "**不可拿右邊的金額或合計去填補空欄位**；amount 取「金額」欄，不是「合計」欄。"
                  "日期為民國年，請轉西元。"),
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
        "extra": ("本表單為訂購單／採購單（Purchase Order）。採購方（buyer）為下單客戶、"
                  "供應商（supplier）為出貨方；請分別擷取雙方名稱與統一編號（8 位純數字，電話勿當統編）。"
                  "明細放入 items：description=品名／規格、quantity=數量、unit_price=單價、amount=金額。"
                  "付款條件如『月結75天／M75』；日期民國轉西元。"),
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
        "extra": ("本表單為衛生福利部食品藥物管理署『食品及相關產品輸入許可通知』。"
                  "報驗義務人（applicant）為進口商、製造廠（manufacturer）為國外生產廠。"
                  "報單號碼及項次可與進口報單勾稽；C.C.C.Code 為貨品分類號列。"
                  "統一編號為 8 位純數字；日期民國轉西元。"),
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
        "extra": ("本表單為勞保局／健保署的保險費繳款單或投保單位繳費證明書。"
                  "**保險費屬免稅，單據上沒有營業稅，絕對不要拆出進項稅額。**"
                  "表格常依『費用代號』分列（如勞保普通事故、職災保險、就業保險、"
                  "積欠工資墊償基金），請把每一列放入 items："
                  "description＝費用名稱或代號、amount＝該列金額；"
                  "total_amount 為各列合計或單據標示的應繳總額。"
                  "**單位負擔／個人負擔**：多數繳費證明並未區分，"
                  "**單據上沒有列出就留空，不要自行推算或分攤**。"
                  "日期為民國年（如 1121101＝民國112年11月1日），請轉西元。"),
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
        "extra": ("本表單是帳單的**費用明細附頁**或繳費結果通知（如中華電信「112年12月費用明細清單 第3頁」），"
                  "屬於佐證文件——真正的入帳依據是同一批文件中的統一發票或繳費憑證，"
                  "本頁**不單獨產生會計分錄**。"
                  "表格中的「用戶號碼」欄是電話號碼（如 27524123、0922000102），"
                  "「彙寄編號」「通知單流水號」「變動載具號碼」「金融機構代繳帳號」亦皆為識別碼，"
                  "**一律不得填入金額欄位**。"
                  "total_amount 只填本頁明確標示為「應繳金額」「合計」的費用數字；"
                  "若本頁只有明細沒有合計，請留空。"),
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
        "extra": ("本表單為**會計傳票（記帳憑證）**，是記帳人員依原始憑證編製的內部單據，"
                  "本身不是發票或收據——它的重點是**借貸分錄**。"
                  "傳票單別決定型態：**轉帳傳票**（如 A911）借貸兩面都列出、必然平衡；"
                  "**現支傳票**（如 A931）只列支出（借方），貸方為現金屬隱含不印在表上；"
                  "**收入傳票**反之只列收入面。請照表上實際印出的列擷取，"
                  "**不要為了讓借貸平衡而自行補上沒有印出的那一面**。"
                  "表格每一列的左欄是「會計科目／科目名稱」（上為代號、下為名稱，如「6205／運費」），"
                  "接著是摘要、部門別、立沖帳目（往來對象代號，如「047／聯邦快遞」），"
                  "最右兩欄是借方金額與貸方金額。"
                  "**每一列放入 entries**：account_code＝科目代號、account_name＝科目名稱、"
                  "description＝摘要、dept＝部門別、offset_code＝立沖帳目、"
                  "debit＝借方金額、credit＝貸方金額。"
                  "**一列只會有借方或貸方其中一邊有金額**，另一邊填 0，不要把同一個數字同時填兩邊。"
                  "**借方金額欄在貸方金額欄的左邊，兩欄相鄰**：請嚴格依數字實際落在哪一欄判定借貸，"
                  "**不要依科目性質推測**（例如不要因為是稅額就假設它在貸方）。"
                  "金額欄空白的列（如「以下空白」）請略過。"
                  "表格最底列的「合計」請填入 debit_total／credit_total，不要當成分錄。"
                  "**填完請自我檢查**：各列的 debit 加總必須等於 debit_total、credit 加總必須等於 "
                  "credit_total，且兩者相等（傳票一定借貸平衡）。若對不上，代表有某列的金額放錯邊，"
                  "請回到圖上重新確認該數字是印在借方欄還是貸方欄。"
                  "日期若為民國年請轉西元（西元＝民國＋1911）。"),
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
    """佐證附頁（帳單明細、繳費結果通知）不獨立產生會計分錄。

    一份帳單常有數頁：發票／繳費憑證是入帳依據，其餘明細頁只是佐證。
    若每頁都各自產生分錄再加總，金額會嚴重灌水（實測中華電信案：正解 3,532、
    加總後 646 萬）。
    """
    return bool(get(type_key).get("supporting"))


def get(type_key: str) -> dict:
    return DOC_TYPES.get(type_key, DOC_TYPES["other"])


def build_extract_prompt(type_key: str) -> str:
    """依類型欄位契約自動生成專屬辨識提示詞。
    密集表單（no_vlm_boxes）不要求 VLM 輸出 boxes——框改由 OCR 精修取得，
    避免 VLM 在密集版面把座標誤塞進欄位值。"""
    t = get(type_key)
    field_lines = "、".join(f'{f["key"]}（{f["label"]}）' for f in t["fields"])
    items_block = ""
    if t.get("has_entries"):
        # 傳票（記帳憑證）的明細本身就是借貸分錄，欄位與品名/數量/單價完全不同
        items_block = ('\n  "entries": [{"account_code": "科目代號", "account_name": "科目名稱", '
                       '"description": "摘要", "dept": "部門別", "offset_code": "立沖帳目", '
                       '"debit": 0, "credit": 0}],')
    elif t.get("has_items"):
        items_block = ('\n  "items": [{"description": "品名", "quantity": 0, '
                       '"unit_price": 0, "amount": 0}],')
    extra = ("\n- " + t["extra"]) if t.get("extra") else ""
    if type_key in NO_VLM_BOX_TYPES:
        boxes_line, boxes_rule = "", ""
    else:
        box_keys = ", ".join(f'"{f["key"]}": [x1,y1,x2,y2]' for f in t["fields"])
        boxes_line = f',\n  "boxes": {{ {box_keys} }}'
        boxes_rule = ("\n- boxes 為各欄位在影像上的邊界框，座標正規化 0~1000 整數"
                      "（左上為原點）；找不到的欄位省略該鍵。")
    return f"""你是專業會計憑證辨識助手。這是一張「{t['label']}」。以下提供原圖與 OCR 文字（供校對，數字以圖為準）。
請萃取此類單據的專屬欄位並僅以 JSON 回覆（不要多餘文字）：
{{
  "doc_type": "{t['label']}",
  "fields": {{ {field_lines} }},{items_block}
  "note": "摘要／備註",
  "confidence": "high|medium|low"{boxes_line}
}}
規則：
- 中文一律輸出**繁體字**（台灣單據），不得輸出簡體字；照單據原文抄錄，不要改寫用詞。
- 日期一律 YYYY-MM-DD。民國轉西元請用公式 **西元年 ＝ 民國年 ＋ 1911**
  （民國114年→2025、民國115年→2026；**不是加 1900**）。單據上多處日期時，
  以「中華民國○年○月○日」的填發日欄為主，表頭的「○年○、○月份」是發票期別不是日期。
- 金額純數字，不要 $、逗號或文字。**不可寫算式或計算過程**——
  錯誤範例：`"usage_fee": 2161 + 952 = 3113`、`"total": 1,234.00`；
  正確寫法：`"usage_fee": 3113`、`"total": 1234`。需要相加時請自行算完只寫結果。{boxes_rule}
- **單據上有多個合計時，total_amount 取涵蓋全部明細項目的那一個**。
  常見陷阱：另有「不含○○」「部分小計」之類的局部合計（如保費收據的
  「總保險費 NT$5,653（不含強制險）」與「總保費金額 NT$6,752」，後者才是總額）。
  請用各明細項目加總驗證：加總等於哪一個合計，就取哪一個。
- 無資料的欄位留空字串或 0。
- 欄位標題後括號內的數字是「表單欄位編號」，僅供你定位對應格位；**絕對不要把編號或座標當作欄位值輸出**。
- 每個 fields 的值必須是單一字串或數字，**不可輸出陣列或 [x,y,x,y] 之類座標**；金額為純數字。
- 請盡量擷取所有列出的欄位；即使版面密集，也逐格對照標題與編號填入。{extra}"""
