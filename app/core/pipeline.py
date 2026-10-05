"""辨識管線：PDF/圖片 → OCR → VLM/LLM 結構化 → （後續）科目分類。

骨架階段：串起流程並回傳 Voucher。VLM 結構化以「OCR 文字 + 原圖」為輸入。
科目分類、日記帳彙整、審核回寫於 Phase 1 後續補上。
"""
import base64
import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from app.core import activity, doc_router, doc_types, model_factory, settings_store
from app.models.schemas import Voucher

logger = logging.getLogger("pdf_scan.pipeline")

STRUCTURE_PROMPT = """你是專業會計憑證辨識助手。以下提供一張台灣會計憑證的原圖，以及 OCR 已抽取的文字（供校對，數字以圖為準）。
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
若無營業稅資訊，tax_amount 視為 0。"""


def render_to_images(file_path: str) -> list[str]:
    """PDF → 逐頁 PNG 路徑（輸出至暫存區，不污染來源目錄）；圖片 → 原路徑。"""
    p = Path(file_path)
    if p.suffix.lower() != ".pdf":
        return [str(p)]
    import pypdfium2 as pdfium

    from app import config

    render_dir = config.UPLOAD_DIR / "_pages"
    render_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(p))
    out = []
    for i in range(len(pdf)):
        img = pdf[i].render(scale=200 / 72).to_pil()  # ~200dpi
        img.thumbnail((1180, 1180))   # 控在 <1MP，避免 VLM 內部再縮放；且長邊>1000 便於判座標空間
        out_path = render_dir / f"{p.stem}_p{i}.png"
        img.save(out_path)
        out.append(str(out_path))
    return out


def analyze_pages(file_path: str) -> list[Voucher]:
    """PDF 逐頁辨識為多張憑證（掃描 PDF 常一頁一張）；圖片回單張。
    單頁失敗僅略過該頁，不影響其他頁。"""
    images = render_to_images(file_path)
    n = len(images)
    out = []
    for i, img in enumerate(images):
        try:
            out.append(_structure_image(img, file_path, i, n))
        except Exception as exc:  # noqa: BLE001
            logger.warning("第 %d/%d 頁辨識失敗，略過：%s", i + 1, n, exc)
    return out


def prepare_pages(file_path: str) -> list[Voucher]:
    """匯入用：逐頁只做 Router 判類型 + 自動轉正（不萃取欄位）。
    產出最小 Voucher（含 doc_type_key + 影像路徑），供「外觀確認」階段先確認
    類型／方向／裁切，之後再逐張做內容辨識。單頁失敗僅略過該頁。"""
    images = render_to_images(file_path)
    n = len(images)
    name = Path(file_path).name
    out = []
    for i, img in enumerate(images):
        try:
            if _is_blank_page(img):
                logger.info("第 %d/%d 頁近乎空白，略過匯入", i + 1, n)
                continue
            type_key = prepare_image(img)
            v = Voucher.model_validate({
                "doc_type_key": type_key,
                "doc_type": doc_types.get(type_key)["label"],
                "fields": {}, "boxes": {}, "items": [],
            })
            v.source_file = f"{name} p{i + 1}" if n > 1 else name
            v.source_image = img
            out.append(v)
        except Exception as exc:  # noqa: BLE001
            logger.warning("第 %d/%d 頁準備失敗，略過：%s", i + 1, n, exc)
    return out


def _is_blank_page(image_path: str, ink_thresh: float = 0.004) -> bool:
    """判斷是否近乎空白頁（如網頁列印多出的頁尾頁）：暗色像素比例極低即視為空白。"""
    from PIL import Image

    try:
        im = Image.open(image_path).convert("L")
    except OSError:
        return False
    im.thumbnail((400, 400))
    px = list(im.getdata())
    if not px:
        return False
    dark = sum(1 for p in px if p < 200)
    return (dark / len(px)) < ink_thresh


def prepare_image(image_path: str, hint: str = "", auto_rotate: bool = True) -> str:
    """外觀準備：Router 判類型 + 自動轉正（影像就地覆寫轉正）；回類型代碼。
    不做欄位萃取——留待使用者確認外觀後再 extract_for_type。"""
    settings = settings_store.get_settings()
    ocr_provider, _ = settings_store.parse_setting(settings["ocr"], ("paddle", "pp-ocrv5"))
    type_key, rotate_hint = doc_router.classify(image_path, hint=hint)
    if auto_rotate:
        ocr = None
        try:
            ocr = model_factory.get_ocr(ocr_provider)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 不可用，改用 VLM 方向提示：%s", exc)
        if ocr is not None:
            with activity.stage("ocr", settings["ocr"] + "（自動轉正）"):
                _, deg = _auto_orient_ocr(image_path, ocr)
            if deg:
                logger.info("OCR 自動轉正 %d 度", deg)
        elif rotate_hint:
            _apply_rotation(image_path, rotate_hint)
    return type_key


def extract_for_type(image_path: str, type_key: str, hint: str = "") -> Voucher:
    """依「已確認的類型」在「已準備好的影像」上萃取欄位＋精準框。
    不再判類型／不再自動轉正（外觀確認階段已完成）。"""
    settings = settings_store.get_settings()
    ocr_provider, _ = settings_store.parse_setting(settings["ocr"], ("paddle", "pp-ocrv5"))
    struct_provider, struct_model = settings_store.parse_setting(settings["structure"])

    ocr = None
    ocr_used = "（無，VLM 直讀）"
    try:
        ocr = model_factory.get_ocr(ocr_provider)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR 不可用，改用 VLM 直讀：%s", exc)

    ocr_lines, ocr_text = [], ""
    if ocr is not None:
        try:
            with activity.stage("ocr", settings["ocr"]):
                ocr_lines = ocr.recognize(image_path)
            ocr_text = ocr.plain_text(ocr_lines)
            ocr_used = settings["ocr"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 辨識失敗，改 VLM 直讀：%s", exc)
            ocr_lines = []

    # 條碼／QR grounding（若可讀，內容可信度高）
    from app.core import barcode
    codes = barcode.decode(image_path)
    code_block = ""
    einv = {}
    if codes:
        code_block = "\n\n影像偵測到的條碼／QR 內容（機器解碼，可信度高，可直接採用）：\n" + "\n".join(codes)
        if type_key == "invoice":
            einv = barcode.parse_einvoice_qr(codes)
            if einv:
                code_block += ("\n電子發票 QR 已解出下列欄位，請優先採用："
                               f"發票號碼={einv.get('voucher_id','')}、日期={einv.get('date','')}、"
                               f"未稅小計={einv.get('subtotal','')}、含稅總計={einv.get('total_amount','')}、"
                               f"買方統編={einv.get('buyer_tax_id','')}、賣方統編={einv.get('seller_tax_id','')}")

    chat = model_factory.get_chat_model(struct_provider, struct_model, temperature=0.0)
    grounding = f"\n\nOCR 文字（供校對，數字以圖為準）：\n{ocr_text}" if ocr_text else ""
    grounding += code_block
    hint_block = f"\n\n人工修正提示（審核人員指出的錯誤，請據此優先修正）：{hint}" if hint else ""
    content = [
        {"type": "text", "text": doc_types.build_extract_prompt(type_key) + grounding + hint_block},
        {"type": "image_url", "image_url": {"url": _data_uri(image_path)}},
    ]
    with activity.stage("structure", settings["structure"]):
        raw = _invoke_text(chat, content)
    try:
        data = _parse_json(raw)
    except json.JSONDecodeError as exc:
        # VLM 回傳非合法 JSON → 讓它自我修正一次（只回 JSON）
        logger.warning("首次 JSON 解析失敗（%s），要求 VLM 重新輸出合法 JSON", exc)
        with activity.stage("structure", settings["structure"] + "（JSON 修正）"):
            # 回應裡連一個 { 都沒有（空字串，或模型改用散文回答）→ 沒有 JSON 可修，
            # 要求它修正只會再拿到一段廢話，直接重問原題比較有機會成功。
            if "{" in str(raw):
                fix = _invoke_text(chat, [
                    {"type": "text", "text": "你剛才的輸出不是合法 JSON。請「只」輸出修正後的合法 JSON 物件"
                     "（不要任何說明文字、不要 markdown code fence、鍵與字串一律用雙引號、不要尾逗號）：\n\n"
                     + str(raw)[:4000]}])
            else:
                # 上一輪根本沒有內容可修，直接重問一次原題（多為 Ollama 偶發失敗）
                fix = _invoke_text(chat, content)
        try:
            data = _parse_json(fix)
        except json.JSONDecodeError:
            # 修正也失敗：多半是輸出被截斷或整段跑掉。重問原題一次——實測同一張圖
            # 有時回完整 JSON、有時不回，重新生成比繼續修同一段壞輸出有機會。
            logger.warning("JSON 修正仍失敗，重問原題最後一次")
            with activity.stage("structure", settings["structure"] + "（重問）"):
                data = _parse_json(_invoke_text(chat, content))
    # 印章聚焦補讀：發票/收據若賣方統編漏抓（常在印章內），才觸發第二次聚焦讀取
    if type_key in ("invoice", "receipt"):
        _f = data.get("fields") or {}
        if not _clean_taxid(_f.get("seller_tax_id", "")):
            seal = _read_seal_taxid(image_path, chat)
            if seal.get("seller_tax_id"):
                _f["seller_tax_id"] = seal["seller_tax_id"]
                if seal.get("seller_name") and not _f.get("seller_name"):
                    _f["seller_name"] = seal["seller_name"]
                data["fields"] = _f
                logger.info("印章聚焦補讀取得賣方統編：%s", seal["seller_tax_id"])

    # 電子發票 QR 為權威來源：覆蓋 VLM 對應欄位（QR 解碼優先）
    if einv:
        fields = data.get("fields") or {}
        fields.update({k: v for k, v in einv.items() if v not in ("", None)})
        data["fields"] = fields

    # 傳票：借貸方向改以 OCR 座標判定（VLM 判欄位歸屬不可靠）
    if doc_types.get(type_key).get("has_entries") and ocr_lines:
        n = _fix_entry_sides_with_ocr(data, ocr_lines)
        if n:
            logger.info("傳票借貸方向以 OCR 座標更正 %d 筆", n)

    vlm_boxes = _normalize_boxes(data.get("boxes"), image_path)
    # 密集表單：VLM 不出框（或框不準），框只保留 OCR 文字驗證過者，寧缺勿錯
    dense = type_key in doc_types.NO_VLM_BOX_TYPES
    data["boxes"] = _refine_boxes_with_ocr(data.get("fields") or {}, vlm_boxes, ocr_lines, image_path,
                                           keep_unmatched=not dense) \
        if ocr_lines else ({} if dense else vlm_boxes)

    voucher = _build_voucher(data, type_key)
    voucher.ocr_provider = ocr_used
    voucher.structure_model = settings["structure"]
    voucher.source_image = image_path
    return voucher


def analyze(file_path: str) -> Voucher:
    """單張（相容既有呼叫）：回第一頁。"""
    return analyze_pages(file_path)[0]


def recognize_image(image_path: str, hint: str = "") -> Voucher:
    """對既有單張影像重新辨識（供手動旋轉後重辨識；不再自動旋轉）。
    hint：人工修正提示（來自備註欄），會餵給 Router 與辨識提示詞作為修正指引。"""
    v = _structure_image(image_path, image_path, 0, 1, auto_rotate=False, hint=hint)
    v.source_image = ""   # 影像已在原位，不需再複製
    return v


def _structure_image(image_path: str, source_file: str, page_idx: int, page_count: int,
                     auto_rotate: bool = True, hint: str = "") -> Voucher:
    """單張全流程：準備（判類型+轉正）＋內容萃取。供批次與重新辨識沿用。"""
    type_key = prepare_image(image_path, hint=hint, auto_rotate=auto_rotate)
    voucher = extract_for_type(image_path, type_key, hint=hint)
    name = Path(source_file).name
    voucher.source_file = f"{name} p{page_idx + 1}" if page_count > 1 else name
    return voucher


def _norm_roc_date(s: str) -> str:
    """民國日期 → 西元 YYYY-MM-DD（如 115/06/04、114.07.23 → 2026-06-04、2025-07-23）。
    已是西元或無法解析則原樣回傳。"""
    import re

    s = str(s or "").strip().replace("民國", "").replace("中華民國", "").strip()
    m = re.match(r"^(\d{2,4})[/\.\-年](\d{1,2})[/\.\-月](\d{1,2})", s)
    if not m:
        return s
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 200:            # 兩三位數視為民國年
        y += 1911
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _read_seal_taxid(image_path: str, chat) -> dict:
    """聚焦補讀：只針對『統一發票專用章』讀賣方名稱與統編（印章顏色不定、位置多在下/右）。
    不裁切、不靠顏色——以單一目的提示讓模型專注於印章。回 {seller_name, seller_tax_id}。"""
    prompt = ("這是一張台灣統一發票。請只做一件事：找出『統一發票專用章』"
              "（印章顏色可能是紅、藍、黑或紫，通常蓋在發票下方或右側），"
              "讀出章內的賣方公司名稱與 8 位統一編號。"
              "只回 JSON：{\"seller_name\":\"\",\"seller_tax_id\":\"\"}；"
              "統一編號必須是 8 位純數字（電話不算），讀不到就留空字串。")
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": _data_uri(image_path)}},
    ]
    try:
        d = _parse_json(_invoke_text(chat, content))
        return {"seller_name": str(d.get("seller_name", "")).strip(),
                "seller_tax_id": _clean_taxid(d.get("seller_tax_id", ""))}
    except Exception as exc:  # noqa: BLE001
        logger.debug("印章聚焦補讀略過：%s", exc)
        return {}


def _clean_taxid(v):
    """統一編號防呆：8 位純數字才算統編；含電話符號或 9~10 位數（電話）則清空。
    非數字或其他長度（可能為國外稅籍）則原樣保留。"""
    import re

    s = str(v or "").strip()
    if not s:
        return s
    if any(c in s for c in "-()（）"):      # 電話樣式
        return ""
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        return digits
    if len(digits) in (9, 10):             # 電話號碼位數
        return ""
    return s


@lru_cache(maxsize=1)
def _s2t():
    """簡→繁轉換器（OpenCC s2t：純字元轉換，不做詞彙轉換）。

    刻意不用 s2twp／s2tw：那會把「智能」改成「智慧」、把大陸買方公司登記名稱一起改掉，
    等於竄改單據上的專有名詞。未安裝 opencc 時回 None，僅靠提示詞要求繁體。
    """
    try:
        import opencc

        return opencc.OpenCC("s2t")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenCC 未安裝，簡轉繁略過：%s", exc)
        return None


def to_traditional(s: str) -> str:
    """把 VLM 偶爾吐出的簡體字轉回繁體。

    台/臺 例外：OpenCC 視「台」為「臺」的簡化字，會把台灣公司登記名稱
    （如「台灣順豐速運股份有限公司」）改成「臺灣…」。原文寫「台」就保留「台」。
    """
    cc = _s2t()
    if not cc or not isinstance(s, str) or not s:
        return s
    out = cc.convert(s)
    if len(out) == len(s):          # 長度一致才能逐字還原例外字
        out = "".join(o if c != "台" else "台" for c, o in zip(s, out))
    return out


def _norm_amount(v):
    """金額字串轉數字：VLM 常回「9,900」「$1,050」「NT$10,395」，即使提示詞要求純數字。

    契約 kind=number 的欄位若留著千分位字串，前端 <input type="number"> 會判定為
    無效值而顯示**空白**，看起來像辨識失敗；使用者接著按「儲存修正」，空值又會把
    正確金額覆蓋成 null。解析失敗則原樣保留（寧可顯示原字串，也不要憑空丟值）。
    """
    import re

    if v is None or isinstance(v, (int, float, bool)):
        return v
    s = str(v).strip()
    if not s:
        return v
    t = s.translate(str.maketrans("０１２３４５６７８９．－，", "0123456789.-,"))
    t = re.sub(r"(?i)NT\$?|TWD|[,\s，、$＄元]", "", t)
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        n = float(t)
        return int(n) if n.is_integer() else n
    return v


def _reject_identifier_as_amount(v, key: str, fields: dict):
    """把誤填進金額欄的識別碼清掉。

    實測中華電信繳費通知的用戶號碼 216123417 被填進 usage_fee／subtotal，
    整案金額從 3,532 暴增到 65 萬。這類值有共同特徵：位數遠超常見金額、
    且與同張憑證的總計不成比例。提示詞擋不住（模型每次判斷都可能不同），
    故在此做確定性攔截：只清掉「明顯不可能是金額」的值，不做估算或推測。
    """
    if not isinstance(v, (int, float)) or v <= 0:
        return v
    # 1 億以上的「金額」在本系統的憑證情境幾乎不可能出現（最大宗的進口報單為百萬級）
    if v >= 100_000_000:
        logger.warning("欄位 %s 的值 %s 位數過大，判為識別碼誤填，已清空", key, v)
        return ""
    # 明細金額不該遠大於總計（差 100 倍以上）；總計本身另由 check_amounts 提示人工核對
    total = fields.get("total_amount")
    if key != "total_amount" and isinstance(total, (int, float)) and total > 0 and v > total * 100:
        logger.warning("欄位 %s 的值 %s 遠大於總計 %s，判為識別碼誤填，已清空", key, v, total)
        return ""
    return v


def _clean_fields(f: dict) -> dict:
    """清洗 fields 內層值：
    - VLM 偶把欄位值吐成框陣列（如 [501,268,743,291]）→ 疑似框直接捨棄；其餘陣列攤平。
    - 統編欄位（*tax_id）套統編防呆，避免把電話當統編。
    - 中文一律轉繁體（VLM 偶爾把報單印章、人名吐成簡體）。"""
    import re

    out = {}
    for k, v in (f or {}).items():
        if isinstance(v, (list, tuple)):
            nums = []
            for x in v:
                try:
                    nums.append(float(x))
                except (TypeError, ValueError):
                    nums = None
                    break
            if nums is not None and len(nums) == 4 and all(0 <= n <= 1000 for n in nums):
                out[k] = ""          # 疑似 bounding box，非欄位值
                continue
            scal = [str(x).strip() for x in v if x not in (None, "", [])]
            out[k] = scal[0] if len(scal) == 1 else " ".join(scal)
        else:
            out[k] = v
        # 字串型的框（VLM 把座標塞進欄位，如 "[28,46,63,61]"）→ 捨棄
        sv = out.get(k)
        if isinstance(sv, str) and re.match(r"^\s*[\[\(]?\s*\d{1,4}(\s*,\s*\d{1,4}){3}\s*[\]\)]?\s*$", sv):
            nums = [int(x) for x in re.findall(r"\d+", sv)]
            if all(0 <= n <= 1000 for n in nums):
                out[k] = ""
        if "tax_id" in k and isinstance(out.get(k), str):
            out[k] = _clean_taxid(out[k])
        elif isinstance(out.get(k), str):
            out[k] = to_traditional(out[k])
    return out


def _fix_entry_sides_with_ocr(data: dict, ocr_lines: list) -> int:
    """用 OCR 座標校正傳票分錄的借貸方向，回傳更正筆數。

    VLM 判斷「這個數字印在借方欄還是貸方欄」並不可靠（實測把借方的進項稅額
    120 判成貸方，導致借貸不平衡），而這正是 OCR 擅長的：傳票的「借方金額」與
    「貸方金額」是兩個固定欄位，用表頭的 x 座標就能決定分界線。

    只在 OCR 能**明確**定位該金額時才更正：同一個數字若在多處出現且分屬兩邊
    （如金額同時等於合計），視為無法判定，保留模型原判。
    """
    import re

    rows = [r for r in (data.get("entries") or []) if isinstance(r, dict)]
    if not rows or not ocr_lines:
        return 0

    def xmid(line):
        xs = [p[0] for p in line.get("box") or []]
        return sum(xs) / len(xs) if xs else None

    hd = hc = None
    for l in ocr_lines:
        t = str(l.get("text", "")).replace(" ", "")
        if hd is None and "借方" in t:
            hd = xmid(l)
        if hc is None and "貸方" in t:
            hc = xmid(l)
    if hd is None or hc is None or hd >= hc:
        return 0
    mid = (hd + hc) / 2

    def ymid(line):
        ys = [p[1] for p in line.get("box") or []]
        return sum(ys) / len(ys) if ys else None

    # OCR 行裡所有可解析為金額的數字 → {值: [(x 中心, y 中心), ...]}
    seen: dict[float, list] = {}
    for l in ocr_lines:
        s = str(l.get("text", "")).strip()
        if not re.fullmatch(r"[\d,]+(?:\.\d+)?", s):
            continue
        try:
            v = float(s.replace(",", ""))
        except ValueError:
            continue
        x, y = xmid(l), ymid(l)
        if x is not None:
            seen.setdefault(v, []).append((x, y))

    # 科目代號 → y 座標（供同列定位）。借貸金額相同時（如勞退兩邊都是 6,468），
    # 數字在兩側都出現，只看 x 無法判定；改以「與該列科目代號同一橫列」消歧義。
    code_y: dict[str, list] = {}
    for l in ocr_lines:
        s = str(l.get("text", "")).strip()
        if re.fullmatch(r"\d{4,5}", s):
            y = ymid(l)
            if y is not None:
                code_y.setdefault(s, []).append(y)

    fixed = 0
    for r in rows:
        d = _norm_amount(r.get("debit")) or 0
        c = _norm_amount(r.get("credit")) or 0
        amt = float(d or c or 0)
        if not amt or amt not in seen:
            continue
        cands = seen[amt]
        code = str(r.get("account_code", "")).strip()
        # 先試「與本列科目代號同一橫列」的金額
        rows_y = code_y.get(code) or []
        if rows_y:
            near = [(x, y) for (x, y) in cands
                    if y is not None and any(abs(y - cy) < 26 for cy in rows_y)]
            if len(near) == 1:
                cands = near
        sides = {"debit" if x < mid else "credit" for x, _ in cands}
        if len(sides) != 1:          # 仍無法判定（多半撞上合計列）→ 保留模型原判
            continue
        side = sides.pop()
        if side == "debit" and not d:
            r["debit"], r["credit"] = amt, 0
            fixed += 1
        elif side == "credit" and not c:
            r["debit"], r["credit"] = 0, amt
            fixed += 1
    return fixed


def _entries_from_voucher_form(data: dict, spec: dict) -> dict:
    """會計傳票（記帳憑證）：把辨識出的表格列轉成 JournalEntry。

    傳票的明細本身就是分錄，與系統自動分類產生的分錄是同一種東西，故填入
    voucher.entries 而非 items。每列只會有借方或貸方一邊有金額（另一邊 0）。
    合計以「實際各列加總」為準——傳票底部印的合計是給人看的，若模型讀錯反而誤導。
    """
    if not spec.get("has_entries"):
        return {}
    entries, dr, cr = [], 0.0, 0.0
    for row in data.get("entries") or []:
        if not isinstance(row, dict):
            continue
        d = _norm_amount(row.get("debit")) or 0
        c = _norm_amount(row.get("credit")) or 0
        d = float(d) if isinstance(d, (int, float)) else 0.0
        c = float(c) if isinstance(c, (int, float)) else 0.0
        if not d and not c:                     # 空白列／「以下空白」
            continue
        code = to_traditional(str(row.get("account_code", "")).strip())
        name = to_traditional(str(row.get("account_name", "")).strip())
        e = {"description": to_traditional(str(row.get("description", "")).strip()),
             "amount": d or c,
             "dept": to_traditional(str(row.get("dept", "")).strip()),
             "offset_code": to_traditional(str(row.get("offset_code", "")).strip()),
             "debit_account": code if d else "", "debit_account_name": name if d else "",
             "credit_account": code if c else "", "credit_account_name": name if c else ""}
        entries.append(e)
        dr += d
        cr += c
    # 現支／收入傳票只印一面（另一面為現金，隱含於傳票類型），單面即為完整，
    # 不能因借貸不等就判為辨識錯誤。
    kind = str((data.get("fields") or {}).get("voucher_kind", ""))
    one_sided_ok = any(k in kind for k in ("現支", "收入", "支出")) and bool(dr) != bool(cr)
    return {"entries": entries, "debit_total": dr, "credit_total": cr,
            "balance_check": bool(entries) and (abs(dr - cr) < 0.01 or one_sided_ok)}


def _build_voucher(data: dict, type_key: str) -> Voucher:
    """把專屬 fields 對映到共同會計欄位（供統編核對/分類/日記帳/報表沿用）。"""
    f = _clean_fields(data.get("fields") or {})
    t = doc_types.get(type_key)
    # 日期型欄位一律轉西元：原本只有頂層 voucher.date 有轉，fields 內仍是民國年，
    # 審核畫面因此看到「115.05.26」這種原始值。
    for spec in t["fields"]:
        k = spec["key"]
        if spec["kind"] == "date" and isinstance(f.get(k), str) and f[k]:
            f[k] = _norm_roc_date(f[k])
        elif spec["kind"] == "number" and f.get(k) not in (None, ""):
            f[k] = _norm_amount(f[k])
            f[k] = _reject_identifier_as_amount(f[k], k, f)
    # 進出口別：專屬類型直接依類型；泛用 declaration 才用關鍵字推導
    if type_key == "export_declaration":
        f["direction"] = "出口"
    elif type_key == "import_declaration":
        f["direction"] = "進口"
        # 進口報單：納稅義務人＝買方（本公司）、賣方＝國外出口商
        if f.get("taxpayer") and not f.get("buyer"):
            f["buyer"] = f.get("taxpayer")
        if f.get("taxpayer_tax_id") and not f.get("buyer_tax_id"):
            f["buyer_tax_id"] = f.get("taxpayer_tax_id")
    elif type_key == "declaration":
        cat = f"{f.get('doc_category', '')}{data.get('doc_type', '')}{data.get('note', '')}"
        if "出口" in cat:
            f["direction"] = "出口"
        elif "進口" in cat:
            f["direction"] = "進口"
    payload = {
        "doc_type_key": type_key,
        "doc_type": data.get("doc_type") or t["label"],
        "fields": f,
        "note": to_traditional(data.get("note", "")),
        "confidence": data.get("confidence"),
        "boxes": data.get("boxes") or {},
        "items": [{k: (to_traditional(v) if isinstance(v, str) else v) for k, v in (it or {}).items()}
                  for it in (data.get("items") or []) if isinstance(it, dict)],
        **_entries_from_voucher_form(data, t),
        "currency": f.get("currency") or "TWD",
        "voucher_id": (f.get("voucher_id") or f.get("doc_no") or f.get("tax_bill_no")
                       or f.get("payment_no") or f.get("po_no") or f.get("permit_no")
                       or f.get("cheque_no") or f.get("declaration_no")
                       or f.get("statement_no") or f.get("account") or ""),
        "date": _norm_roc_date(f.get("date") or f.get("issue_date") or f.get("payment_date")
                               or f.get("import_date") or f.get("pay_start") or f.get("due_date") or ""),
        "subtotal": f.get("subtotal"),
        "tax_amount": f.get("tax_amount") or f.get("business_tax") or f.get("tax"),
        "total_amount": (f.get("total_amount") or f.get("total_fob_twd")
                         or f.get("total_cif_twd") or f.get("total_tax") or f.get("principal")),
        # 出口報單：出口人＝賣方；進口報單：納稅義務人＝買方、賣方＝國外出口商
        "seller": {"name": (f.get("seller_name") or f.get("seller") or f.get("exporter")
                            or f.get("supplier") or f.get("manufacturer") or f.get("vendor")
                            or f.get("payee") or f.get("taxpayer") or f.get("trader")
                            or f.get("provider") or f.get("broker") or ""),
                   "tax_id": (f.get("seller_tax_id") or f.get("exporter_tax_id")
                              or f.get("supplier_tax_id") or f.get("tax_id") or "")},
        "buyer": {"name": (f.get("buyer_name") or f.get("buyer") or f.get("remitter")
                           or f.get("counterparty") or f.get("applicant") or ""),
                  "tax_id": (f.get("buyer_tax_id") or f.get("taxpayer_tax_id")
                             or f.get("applicant_tax_id") or "")},
    }
    return Voucher.model_validate(payload)


def _auto_orient_ocr(image_path: str, ocr) -> tuple[str, int]:
    """以 OCR 信心度在 0/90/180/270 四方向擇優（文字最多且最清晰＝正立），
    對原圖實體旋轉並覆寫，回 (image_path, 旋轉角度)。"""
    from PIL import Image

    try:
        base = Image.open(image_path)
    except OSError:
        return image_path, 0
    small = base.copy()
    small.thumbnail((720, 720))
    tmp = str(Path(image_path).with_name("_orient_tmp.png"))
    tp = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}

    def _score(img):
        try:
            img.save(tmp)
            lines = ocr.recognize(tmp)
            return sum(len(l["text"]) * max(l.get("score", 0), 0.1) for l in lines)
        except Exception:  # noqa: BLE001
            return 0.0

    s0 = _score(small)
    best_deg, best_score = 0, s0
    # 0° 已足夠清晰（文字多且信心高）→ 視為正立，省下其他三方向
    if s0 < 40:
        for deg in (90, 180, 270):
            sc = _score(small.transpose(tp[deg]))
            if sc > best_score:
                best_score, best_deg = sc, deg
    try:
        os.remove(tmp)
    except OSError:
        pass
    if best_deg:
        _apply_rotation(image_path, best_deg)
    return image_path, best_deg


def _match_ocr_line(s: str, ocr_lines: list):
    """把欄位值對上一條 OCR 行，回傳該行（找不到回 None）。

    只比對「整串值落在單一 OCR 行內」太嚴格：報單的 exporter 是中文名＋英文名＋
    地址三行、unit_price 的「USD 3,100」幣別與金額分行、date 已正規化為西元但
    單據印的是民國 115/06/04——這些全部配不上，欄位就沒有框可點。

    故加兩層退階，但維持密集表單「寧缺勿錯」：退階候選必須**唯一命中**才採用，
    命中多行代表不夠獨特（如 USD 出現十幾次），寧可不給框。
    """
    import re

    def norm(t):
        return str(t).replace(" ", "").replace(",", "")

    lines_n = [(l, norm(l.get("text", ""))) for l in ocr_lines]

    # ① 整串值（原樣或去空白逗號）
    sn = norm(s)
    for l, ln in lines_n:
        if s in str(l.get("text", "")) or (len(sn) >= 3 and sn in ln):
            return l

    # ② 民國日期：值已轉西元，單據上仍是民國
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    cands = []
    if m:
        y, mo, d = int(m.group(1)) - 1911, m.group(2), m.group(3)
        cands += [f"{y}/{mo}/{d}", f"{y}.{mo}.{d}", f"{y}{mo}{d}"]

    # ③ 多行值：改用值裡最長、最獨特的片段定位（長的先試，避免用 USD 這種常見詞）
    cands += sorted({t for t in re.split(r"[\s，、；;]+", s) if len(t) >= 4},
                    key=len, reverse=True)[:3]

    for cand in cands:
        cn = norm(cand)
        if len(cn) < 3:
            continue
        found = [l for l, ln in lines_n if cn in ln]
        if len(found) == 1:          # 唯一命中才算數
            return found[0]
    return None


def _refine_boxes_with_ocr(fields: dict, vlm_boxes: dict, ocr_lines: list, image_path: str,
                           keep_unmatched: bool = True) -> dict:
    """用 OCR 精確行框修正欄位框：欄位值文字對上某 OCR 行，就採用該行的精確框。
    keep_unmatched=False 時，對不上 OCR 的欄位不保留 VLM 框（密集表單寧缺勿錯）。"""
    from PIL import Image

    try:
        with Image.open(image_path) as source:
            w, h = source.size
    except OSError:
        return vlm_boxes
    if not w or not h:
        return vlm_boxes
    refined = dict(vlm_boxes or {}) if keep_unmatched else {}
    for key, val in (fields or {}).items():
        s = str(val).strip()
        if len(s) < 2:
            continue
        hit = _match_ocr_line(s, ocr_lines)
        box = hit.get("box") if hit else None
        if not box:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        refined[key] = [max(0, min(1000, round(min(xs) / w * 1000))),
                        max(0, min(1000, round(min(ys) / h * 1000))),
                        max(0, min(1000, round(max(xs) / w * 1000))),
                        max(0, min(1000, round(max(ys) / h * 1000)))]
    return refined


def _apply_rotation(image_path: str, degrees_cw: int) -> str:
    """順時針旋轉 90/180/270 度並覆寫存檔（無損 transpose）。"""
    from PIL import Image

    tp = {90: Image.Transpose.ROTATE_270,   # PIL transpose 為逆時針，90°CW = ROTATE_270
          180: Image.Transpose.ROTATE_180,
          270: Image.Transpose.ROTATE_90}.get(degrees_cw)
    if tp is None:
        return image_path
    try:
        im = Image.open(image_path).transpose(tp)
        im.save(image_path)
    except OSError:
        pass
    return image_path


def _normalize_boxes(raw, image_path: str) -> dict:
    """VLM 座標空間不一致（時而 0~1、時而 0~1000、時而絕對像素）。
    自動偵測後統一輸出正規化 0~1000（[x1,y1,x2,y2]）。"""
    if not isinstance(raw, dict):
        return {}
    from PIL import Image

    with Image.open(image_path) as source:
        w, h = source.size
    if not w or not h:
        return {}

    parsed = {}
    allvals = []
    for k, b in raw.items():
        if not isinstance(b, (list, tuple)) or len(b) < 4:
            continue
        try:
            vals = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
        except (TypeError, ValueError):
            continue
        parsed[k] = vals
        allvals.extend(vals)
    if not allvals:
        return {}

    mx = max(allvals)
    if mx <= 1.5:
        mode = "unit"        # 0~1
    elif mx <= 1000:
        mode = "norm"        # 已是 0~1000
    else:
        mode = "pixel"       # 絕對像素（依影像尺寸換算）

    def conv(v, axis):
        if mode == "unit":
            return v * 1000
        if mode == "norm":
            return v
        return v / (w if axis == "x" else h) * 1000

    clamp = lambda v: max(0, min(1000, round(v)))
    return {
        k: [clamp(conv(vs[0], "x")), clamp(conv(vs[1], "y")),
            clamp(conv(vs[2], "x")), clamp(conv(vs[3], "y"))]
        for k, vs in parsed.items()
    }


def _data_uri(image_path: str) -> str:
    raw = Path(image_path).read_bytes()
    b64 = base64.b64encode(raw).decode()
    suffix = Path(image_path).suffix.lower().lstrip(".") or "png"
    mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
    return f"data:image/{mime};base64,{b64}"


def _invoke_text(chat, content, tries: int = 3) -> str:
    """呼叫 VLM 並取回文字；空回應或例外時重試。

    地端 Ollama 偶發性失敗（實測見過 CUDA error 500、以及回傳空字串），
    單次失敗就讓整張憑證辨識中斷太脆弱——批次提取時等於整批少一張。
    """
    import time
    from app.core import invocation_trace

    last = None
    for i in range(tries):
        try:
            with invocation_trace.timed_call(content, i + 1) as traced:
                resp = chat.invoke([{"role": "user", "content": content}])
                text = resp.content if hasattr(resp, "content") else str(resp)
                traced["response"] = str(text)
            if str(text).strip():
                return text
            last = "空回應"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        if i < tries - 1:
            logger.warning("VLM 呼叫失敗（%s），第 %d 次重試", str(last)[:80], i + 1)
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"VLM 連續 {tries} 次無有效回應：{str(last)[:120]}")


def _parse_json(text: str) -> dict:
    """儘量把 VLM 輸出解析為 dict：處理 code fence、全形引號、註解、尾逗號、
    未加引號或單引號的鍵等常見瑕疵。逐步套用由輕到重的修復，最先成功者勝。"""
    import re

    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if "```" in t[3:] else t[3:]
        if t.startswith("json"):
            t = t[4:]
        t = t.strip().rstrip("`").strip()
    # 取第一個 { 到最後一個 }
    s, e = t.find("{"), t.rfind("}")
    frag = t[s : e + 1] if s >= 0 and e > s else t

    def _fancy(x):   # 全形／彎引號 → 直引號
        return (x.replace("“", '"').replace("”", '"')
                 .replace("‘", "'").replace("’", "'"))

    def _decomment(x):   # 去 // 與 /* */ 註解
        x = re.sub(r"//[^\n\"]*", "", x)
        return re.sub(r"/\*.*?\*/", "", x, flags=re.S)

    def _trailing(x):    # 去尾逗號
        return re.sub(r",(\s*[}\]])", r"\1", x)

    def _thousands(x):
        """數值欄位的千分位逗號 → 去掉（"debit": 33,390.00 是非法 JSON）。

        傳票的金額欄印成「2,406.00」，模型會原樣抄進 JSON 數值欄位，
        json.loads 在逗號處整個崩掉（回報 char 0，看起來像沒有 JSON）。
        一般發票印「2830」不帶逗號，所以只有傳票這類單據會踩到。
        只處理「冒號後、以 } ] , 結尾」的純數字，不動字串內容（摘要可能含逗號）。
        """
        return re.sub(
            r'(:\s*)(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?)(\s*[,}\]])',
            lambda m: m.group(1) + m.group(2).replace(",", "") + m.group(3), x)

    def _expr(x):
        """數值欄位寫成算式 → 取等號後的結果。

        實測電信帳單（多筆費用項目）會讓模型在 JSON 裡邊算邊寫：
        `"usage_fee": 2161 + 952 - (19 - 19) = 3124`。取 = 之後的數字即可，
        不自行計算（避免誤解模型的意圖）。
        """
        return re.sub(r"(:\s*)[-\d\s+*/().]*?=\s*(-?\d+(?:\.\d+)?)(\s*[,}\]])",
                      r"\1\2\3", x)

    def _close(x):
        """補上缺少的收尾括號（模型偶爾漏掉 } 或 ]）。字串內的括號不計。"""
        depth_c = depth_s = 0
        in_str = esc = False
        for ch in x:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth_c += 1
            elif ch == "}":
                depth_c -= 1
            elif ch == "[":
                depth_s += 1
            elif ch == "]":
                depth_s -= 1
        return x + ('"' if in_str else "") + "]" * max(0, depth_s) + "}" * max(0, depth_c)

    def _sq_key(x):      # 單引號 key → 雙引號
        return re.sub(r"([{,]\s*)'([^']*?)'(\s*:)", r'\1"\2"\3', x)

    def _sq_all(x):      # 其餘單引號字串（值／陣列元素）→ 雙引號
        return re.sub(r"'([^'\"]*)'", r'"\1"', x)

    def _bare_key(x):    # 未加引號 key → 雙引號（最後手段，較可能誤傷字串內容）
        return re.sub(r'([{,]\s*)([A-Za-z_][\w-]*)(\s*:)', r'\1"\2"\3', x)

    # 逐步累加修復，最先能解析成功的版本勝出
    candidates, cur = [frag], frag
    for step in (_fancy, _decomment, _thousands, _expr, _trailing, _sq_key, _sq_all, _bare_key, _close):
        cur = step(cur)
        candidates.append(cur)
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            pass
        # 後面還接了東西（qwen3 等會在 JSON 之後補說明或再吐一個物件，
        # 造成 "Extra data"）→ 只取第一個完整物件，忽略其餘。
        try:
            obj, _ = json.JSONDecoder().raw_decode(c.lstrip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    logger.warning("VLM JSON 解析失敗，原始輸出前 600 字：%s", frag[:600])
    raise json.JSONDecodeError("VLM 回傳非合法 JSON", frag, 0)
