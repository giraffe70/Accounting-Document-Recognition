"""Router：先判斷單據類型與方向，再分配給專屬辨識提示詞。

輕量 VLM 呼叫，回傳 (類型代碼, 需順時針旋轉角度)；解析失敗時回 ('other', 0)。
"""
import json
import logging
import re

from app.core import doc_types, model_factory, settings_store

logger = logging.getLogger("pdf_scan.router")


def _menu() -> str:
    return "\n".join(f"- {k}：{v['label']}" for k, v in doc_types.DOC_TYPES.items())


ROUTER_PROMPT = (
    "你是單據分類器。判斷這張掃描件：\n"
    "1) 屬於下列哪一類（回代碼，如 cheque）\n"
    "2) 需『順時針』旋轉幾度才會正立（僅能是 0、90、180、270）\n"
    "只回 JSON：{\"type\":\"代碼\",\"rotate\":0}，不要其他文字。\n"
    "以**標題**為準，不要只看到「發票號碼」「營業稅額」就判成發票：\n"
    "- 標題寫「應收帳款明細表／應付帳款明細表／對帳單／月結單」→ statement"
    "（這類表格內的發票號碼只是勾稽用，本身不是發票）\n"
    "- 標題寫「轉帳傳票／收入傳票／支出傳票」，表格有「會計科目・借方金額・貸方金額」欄"
    "→ journal_voucher（這是記帳憑證，不是發票收據）\n"
    "- bill_detail **只用於帳單的費用明細附頁**：標題明確寫「費用明細清單／費用明細」，"
    "內容是逐項費用的清單。\n"
    "  以下一律**不是** bill_detail，它們本身就是入帳依據：印有「統一發票／電子發票證明聯」"
    "（→ invoice）、「繳費結果通知／繳費通知」且載明繳費總金額（→ utility）、"
    "勞健保台電的「繳費證明／繳款單」（→ tax_bill 或 utility）。\n"
    "- 勞保局／健保署／勞退的保險費繳款單、投保單位繳費證明書 → social_insurance\n"
    "- 標題寫「訂購單／採購單」→ purchase_order；報關行的「收費通知單」→ broker_note\n"
    "- 真正的發票（invoice）標題為「統一發票」「電子發票證明聯」或收銀機發票格式\n"
    "\n類別：\n" + _menu()
)


def classify(image_path: str, hint: str = "") -> tuple[str, int]:
    from app.core.pipeline import _data_uri  # 避免循環匯入

    settings = settings_store.get_settings()
    provider, model = settings_store.parse_setting(settings["structure"])
    chat = model_factory.get_chat_model(provider, model, temperature=0.0)
    prompt = ROUTER_PROMPT
    if hint:
        prompt += f"\n\n人工提示（請據此判斷類型）：{hint}"
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": _data_uri(image_path)}},
    ]
    try:
        from app.core import activity
        from app.core.pipeline import _invoke_text

        # 地端 Ollama 偶發失敗（CUDA error 500／空回應）時會靜默降級為 other，
        # 而類型判錯會讓後續整條鏈都套錯的欄位契約，故先重試再放棄。
        with activity.stage("structure", settings["structure"] + "（判類型）"):
            text = _invoke_text(chat, content).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Router 失敗，改用 other：%s", exc)
        return "other", 0

    type_key, rotate = "other", 0
    # 解析 rotate
    try:
        s, e = text.find("{"), text.rfind("}")
        obj = json.loads(text[s:e + 1]) if s >= 0 and e > s else {}
        r = int(obj.get("rotate", 0))
        rotate = r if r in (0, 90, 180, 270) else 0
        cand = str(obj.get("type", "")).lower()
    except (ValueError, TypeError, json.JSONDecodeError):
        cand = text.lower()
        m = re.search(r"(90|180|270)", text)
        rotate = int(m.group(1)) if m else 0

    low = (cand or text).lower()
    for key in doc_types.DOC_TYPES:
        if key in low:
            return key, rotate
    for key, t in doc_types.DOC_TYPES.items():
        if any(a.lower() in low for a in t.get("aliases", [])):
            return key, rotate
    logger.info("Router 未明確命中類型（回應：%s），改用 other", text[:40])
    return type_key, rotate
