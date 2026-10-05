"""會計科目自動分類：由 classify LLM 依憑證欄位產生借貸分錄。

RAG（選用）：以 bge-m3 對「已核定」歷史紀錄做相似檢索，取 top-k 作為 few-shot；
無歷史資料時自動降級為 zero-shot。台灣標準會計科目為基礎，客製科目表為 Phase 2。
"""
import json
import logging
from typing import Optional

import requests

from app import config
from app.core import model_factory, settings_store
from app.db.models import ReviewStatus
from app.models.schemas import JournalEntry, Voucher
from app.services import accounts, importdecl, review

logger = logging.getLogger("pdf_scan.classify")

EMBED_MODEL = "bge-m3"

CLASSIFY_PROMPT = """你是台灣會計師。依據下列憑證資訊，產生借貸分錄（雙式簿記）。

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

僅回 JSON，不要多餘文字。"""


def classify(voucher: Voucher, company: str = "") -> Voucher:
    """company：憑證的歸屬公司，用來取該公司的科目表（各公司代號不同，
    給錯表等於每張分錄都要人工重改）。"""
    from app.core import doc_types

    # 佐證附頁（帳單明細、繳費結果通知）不產生分錄：入帳依據是同批的發票／繳費憑證，
    # 每頁都各自產分錄會讓金額嚴重灌水。
    if doc_types.is_supporting(voucher.doc_type_key):
        voucher.entries = []
        voucher.debit_total = voucher.credit_total = None
        voucher.balance_check = False
        voucher.note = (voucher.note or "") + "（佐證附頁，不單獨產生分錄）"
        return voucher

    settings = settings_store.get_settings()
    provider, model = settings_store.parse_setting(settings["classify"])

    examples = _retrieve_examples(voucher)
    few_shot = ""
    if examples:
        few_shot = "\n\n參考過往已核定分錄（相似憑證）：\n" + json.dumps(examples, ensure_ascii=False)
    seeds = _seed_examples()
    if seeds:
        few_shot += ("\n\n**本客戶既有記帳慣例（實際傳票，請比照科目選法）**："
                     "\n" + json.dumps(seeds, ensure_ascii=False)
                     + "\n（上列只給科目名稱，代號請對照下方科目表取用。）")

    chart = accounts.as_prompt_list(company)
    chart_hint = (f"\n\n請**只**使用下列 {company or '通用'}科目表中的科目（代號 名稱），"
                  f"不要自創代號：\n{chart}") if chart else ""

    # 進口報單走專用處理邏輯
    base_prompt = importdecl.IMPORT_CLASSIFY_PROMPT if importdecl.is_import(voucher.model_dump()) else CLASSIFY_PROMPT

    chat = model_factory.get_chat_model(provider, model, temperature=0.0)
    prompt = (base_prompt + chart_hint + few_shot
              + "\n\n憑證資訊：\n" + json.dumps(_voucher_brief(voucher), ensure_ascii=False))
    from app.core import activity

    with activity.stage("classify", settings["classify"]):
        resp = chat.invoke([{"role": "user", "content": prompt}])
    text = resp.content if hasattr(resp, "content") else str(resp)
    data = _parse_json(text)

    entries = [JournalEntry.model_validate(e) for e in data.get("entries", [])]
    voucher.entries = entries
    voucher.debit_total = sum((e.amount or 0) for e in entries if e.debit_account)
    voucher.credit_total = sum((e.amount or 0) for e in entries if e.credit_account)
    voucher.balance_check = abs((voucher.debit_total or 0) - (voucher.credit_total or 0)) < 0.01
    return voucher


def _voucher_brief(v: Voucher) -> dict:
    return {
        "doc_type": v.doc_type, "date": v.date,
        "seller": v.seller.name, "buyer": v.buyer.name,
        "items": [{"description": i.description, "amount": i.amount} for i in v.items],
        "subtotal": v.subtotal, "tax_amount": v.tax_amount,
        "total_amount": v.total_amount, "note": v.note,
    }


# ── bge-m3 RAG（歷史核定紀錄）──────────────────────────────────
def _seed_examples() -> list:
    """客戶既有記帳慣例的 few-shot 種子（由 scripts/seed_examples.py 從客戶傳票產生）。

    系統剛上線時資料庫沒有已核定紀錄，RAG 檢索是空的、分類等於 zero-shot，
    實測會挑籠統科目（有「運費」卻用「其他費用」）。種子只含科目名稱不含代號，
    代號由模型對照該公司科目表取得。
    """
    f = config.DATA_DIR / "entry_examples.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _retrieve_examples(voucher: Voucher, k: int = 3) -> list:
    """回傳最相似的已核定分錄範例；無歷史或失敗時回 []（zero-shot）。"""
    try:
        approved = review.list_vouchers(status=ReviewStatus.accountant_approved)
        pool = [json.loads(r.current_json) for r in approved if json.loads(r.current_json).get("entries")]
        if not pool:
            return []
        query_vec = _embed(json.dumps(_voucher_brief(voucher), ensure_ascii=False))
        scored = []
        for item in pool:
            vec = _embed(json.dumps({"doc_type": item.get("doc_type"), "seller": item.get("seller", {}).get("name"),
                                     "note": item.get("note")}, ensure_ascii=False))
            scored.append((_cosine(query_vec, vec), item.get("entries")))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:k]]
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG 檢索略過（zero-shot）：%s", exc)
        return []


def _embed(text: str) -> list:
    r = requests.post(f"{config.OLLAMA_BASE_URL}/api/embed",
                      json={"model": EMBED_MODEL, "input": text}, timeout=10)
    r.raise_for_status()
    return r.json()["embeddings"][0]


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _parse_json(text: str) -> dict:
    """沿用 pipeline 的解析器（處理 code fence、全形引號、尾逗號、JSON 後接說明等）。

    原本這裡有一份只處理 code fence 的簡化版，導致 qwen3 這類會在 JSON 之後
    補說明的模型 100% 解析失敗——同一件事有兩套實作，修了一邊另一邊還是壞的。
    """
    from app.core.pipeline import _parse_json as _shared

    return _shared(text)
