"""Three task templates. Contracts, evidence and policy are request data."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model

from app.core import doc_types

InputMode = Literal["image", "ocr_text"]
ROUTE_TEMPLATE = """任務：判斷文件類型及正立方向，只輸出指定 JSON。
以文件本身標題及版面判斷，不因引用發票號碼就分類為發票。未知類型填 other 並說明原因。
對帳單用 statement；會計傳票用 journal_voucher；純費用附頁用 bill_detail。
載明繳費總額的繳費通知用 utility；勞健保繳款用 social_insurance。
進口、出口報單分開；報關行收費通知用 broker_note。"""
EXTRACT_TEMPLATE = """任務：從文件證據擷取指定欄位，只依契約輸出 JSON。
僅填可確認的原文或數值；未知填 null，不補造明細，不以其他欄位推算缺值。
區分文件本身資料與引用資料；日期與金額可保留原文，格式及算術由程式處理。
人工提示只協助定位，不是已確認證據；不得為使合計相等而改寫數字。"""
ACCOUNTING_TEMPLATE = """任務：依已確認資料、公司科目表與適用政策提出分錄，只輸出指定 JSON。
不猜原始單據、不自創科目或金額；每列只填借或貸一側。
amount 取提供的金額來源，evidence_paths 列出來源路徑；無金額填 null。
缺科目代碼留空並列警告。資料不足仍可提出候選分錄，將假設列入 assumptions。
分項費用分列；不自行拆未印出的稅額或付款資料。平衡與科目驗證由程式執行。"""


class RawContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _mode(mode: InputMode) -> str:
    if mode not in ("image", "ocr_text"):
        raise ValueError("unsupported input mode")
    return "圖片模式：已提供圖片；OCR 僅供校對。" if mode == "image" else "純文字模式：沒有圖片，只能讀提供的 OCR；不得輸出圖片定位框。"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_route_prompt(mode: InputMode = "image", ocr_text: str = "", hint: str = "") -> str:
    menu = "；".join(f"{k}={v['label']}" for k, v in doc_types.DOC_TYPES.items())
    rotation = "rotate 為順時針 0、90、180、270。" if mode == "image" else "rotate 固定為 0。"
    return "\n".join([ROUTE_TEMPLATE, _mode(mode), "類型：" + menu, rotation,
                      '輸出：{"type":"other","rotate":0,"reason":"判斷依據或未知原因"}',
                      *(["定位提示：" + hint] if hint else []), *(["OCR：\n" + ocr_text] if ocr_text else [])])


def field_specs(type_key: str, fields: list[str] | None = None) -> list[dict]:
    specs = doc_types.get(type_key)["fields"]
    if fields is None:
        return specs
    if not fields or not set(fields) <= {s["key"] for s in specs}:
        raise ValueError("supplement fields must be a nonempty contract subset")
    return [s for s in specs if s["key"] in fields]


def _row_model(name: str, specs: list[dict]):
    return create_model(name, __base__=RawContract, **{
        s["key"]: (str | int | float | None if s["kind"] == "number" else str | None, ...)
        for s in specs})


def extraction_model(type_key: str, mode: InputMode = "image", fields: list[str] | None = None):
    _mode(mode)
    spec = doc_types.get(type_key)
    members = {"fields": (_row_model("Fields", field_specs(type_key, fields)), ...)}
    if fields is None:
        members.update({"doc_type": (str, ...), "note": (str | None, ...),
                        "confidence": (Literal["high", "medium", "low"] | None, ...)})
        if spec.get("has_items"):
            members["items"] = (list[_row_model("Item", spec["item_fields"])], ...)
        if spec.get("has_entries"):
            members["entries"] = (list[_row_model("Entry", spec["entry_fields"])], ...)
    if mode == "image" and type_key not in doc_types.NO_VLM_BOX_TYPES:
        members["boxes"] = (dict[str, list[int]], {})
    return create_model("Extraction", __base__=RawContract, **members)


def extraction_example(type_key: str, mode: InputMode = "image", fields: list[str] | None = None) -> dict:
    example = {"fields": {s["key"]: None for s in field_specs(type_key, fields)}}
    if fields is None:
        spec = doc_types.get(type_key)
        example.update(doc_type=spec["label"], note=None, confidence=None)
        for key, flag in (("items", "has_items"), ("entries", "has_entries")):
            if spec.get(flag):
                example[key] = []
    if mode == "image" and type_key not in doc_types.NO_VLM_BOX_TYPES:
        example["boxes"] = {}
    return example


def validate_extraction(data: dict, type_key: str, mode: InputMode = "image", fields: list[str] | None = None) -> dict:
    result = extraction_model(type_key, mode, fields).model_validate(data).model_dump()
    allowed = {s["key"] for s in field_specs(type_key, fields)}
    for key, box in result.get("boxes", {}).items():
        if key not in allowed or len(box) != 4 or not all(0 <= n <= 1000 for n in box) or box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("invalid box: " + key)
    return result


def build_extract_prompt(type_key: str, mode: InputMode = "image", ocr_text: str = "", hint: str = "",
                         fields: list[str] | None = None, evidence: dict | None = None) -> str:
    spec = doc_types.get(type_key)
    def describe(specs):
        return "；".join(f"{s['key']}={s['label']}({'數/原文' if s['kind']=='number' else '原文'}|null)" for s in specs)
    parts = [EXTRACT_TEMPLATE, _mode(mode), "文件類型：" + spec["label"],
             "欄位：" + describe(field_specs(type_key, fields)),
             "輸出範本（空明細不可補造）：" + _json(extraction_example(type_key, mode, fields))]
    if fields is None:
        for key, specs in (("items", spec["item_fields"]), ("entries", spec["entry_fields"])):
            if specs:
                parts.append(key + " 列契約：" + describe(specs))
    else:
        parts.append("補讀：僅輸出指定欄位；不修改其他資料。")
    if fields is None:
        parts += spec["type_hints"]
    if mode == "image" and type_key not in doc_types.NO_VLM_BOX_TYPES:
        parts.append("boxes 僅框可定位的指定欄位；[x1,y1,x2,y2] 為0至1000整數，未知省略。")
    if hint:
        parts.append("定位提示：" + hint)
    if evidence:
        parts.append("參考證據（公司資料不得用來補空值）：" + _json(evidence))
    if ocr_text:
        parts.append("OCR：\n" + ocr_text)
    return "\n".join(parts)


def build_accounting_prompt(context: dict, chart: list, policy: dict, examples: list | None = None) -> str:
    return "\n".join([ACCOUNTING_TEMPLATE,
        '輸出：{"entries":[{"description":"事由","amount":null,"debit_account":"","debit_account_name":"","credit_account":"","credit_account_name":"","evidence_paths":[]}],"warnings":[],"assumptions":[]}',
        "公司科目表：" + _json(chart), "適用政策：" + _json(policy), "已確認資料與可用金額來源：" + _json(context),
        *(["同公司已驗證案例：" + _json(examples[:3])] if examples else [])])
