"""憑證資料契約（Pydantic v2）。

相較封存 POC 的分錄導向模型，此處補齊報價單/合約要求的**完整憑證欄位**：
買受人/賣方名稱與統編、品名、數量、單價、合計、稅額、備註。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class _StrCoerceModel(BaseModel):
    """清洗 VLM/LLM 的常見髒輸出：
    - 字串欄位回傳 null → 空字串
    - 數字欄位回傳帶符號字串（如 "$575"、"23,611"、"NT$1,000"）→ 轉為數字
    - 欄位誤回傳陣列（如 ["USD","3,100"]、[26,1]）→ 數字取第一個可解析值、字串取合併
    """

    @staticmethod
    def _num(v):
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = (v.strip().replace("NT$", "").replace("$", "").replace(",", "")
                 .replace("，", "").replace(" ", "").replace("　", ""))
            if s in ("", "-", "—"):
                return None
            try:
                return float(s)
            except ValueError:
                return None
        return None

    @field_validator("*", mode="before")
    @classmethod
    def _coerce(cls, v, info):
        ann = cls.model_fields[info.field_name].annotation
        args = get_args(ann)
        is_str = ann is str
        is_num = ann in (float, int) or float in args or int in args
        if v is None:
            if is_str:
                return ""
            if ann is dict or ann in (list,):
                return {} if ann is dict else []
            return v
        # VLM 偶把欄位值輸出成陣列 → 依欄位型別攤平
        if isinstance(v, (list, tuple)) and (is_str or is_num):
            if is_num:
                for el in v:
                    r = cls._num(el)
                    if r is not None:
                        return r
                return None
            parts = [str(el).strip() for el in v if el not in (None, "", [])]
            return " ".join(parts)
        if is_num and isinstance(v, str):
            return cls._num(v)
        return v


class Party(_StrCoerceModel):
    """交易一方（買受人或賣方）。"""
    name: str = ""
    tax_id: str = ""          # 統一編號
    address: str = ""


class LineItem(_StrCoerceModel):
    """憑證明細一列。"""
    description: str = ""     # 品名
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None   # 合計（該列）
    hs_code: Optional[str] = None
    origin_country: Optional[str] = None
    fee_code: Optional[str] = None   # 勞健保費用代號
    # Application enrichment; deliberately absent from DOC_TYPES extraction fields.
    item_id: str = ""
    account_code: str = ""
    account_name: str = ""
    mapping_id: Optional[int] = None
    mapping_version: Optional[int] = None
    account_source: str = ""
    account_needs_review: bool = False
    remember_mapping: bool = True
    account_company_id: Optional[int] = None
    account_item_key: str = ""
    account_updated_by: Optional[int] = None
    account_updated_by_name: str = ""
    account_updated_at: str = ""
    model_config = ConfigDict(extra="allow")


class JournalEntry(_StrCoerceModel):
    """會計分錄（借貸）。

    系統自動分類產生的分錄，與從客戶傳票（記帳憑證）辨識回來的分錄共用此模型
    ——兩者是同一種東西，差別只在誰產生的。dept／offset_code 僅傳票有。
    """
    description: str = ""
    amount: Optional[float] = None
    debit_account: str = ""
    debit_account_name: str = ""
    credit_account: str = ""
    credit_account_name: str = ""
    dept: str = ""            # 部門別（傳票）
    offset_code: str = ""     # 立沖帳目：往來對象代號，如 047 聯邦快遞（傳票）
    evidence_paths: list[str] = Field(default_factory=list)
    amount_source: Optional[str] = None  # 主要金額來源
    entry_id: str = ""
    entry_origin: str = ""
    source_entry_index: Optional[int] = None
    model_config = ConfigDict(extra="allow")


class Voucher(_StrCoerceModel):
    """一張憑證的完整結構化結果。"""
    voucher_id: str = ""             # 憑證/發票號碼
    date: str = ""                   # YYYY-MM-DD
    doc_type: str = ""               # 憑證類型顯示名（發票/支票/匯款單…）
    doc_type_key: str = ""           # Router 判定的類型代碼（invoice/cheque/…）
    fields: dict = Field(default_factory=dict)  # 該類型專屬欄位值（key→value）
    buyer: Party = Field(default_factory=Party)
    seller: Party = Field(default_factory=Party)
    items: list[LineItem] = Field(default_factory=list)

    subtotal: Optional[float] = None  # 未稅小計
    tax_amount: Optional[float] = None  # 營業稅額
    total_amount: Optional[float] = None  # 總計（含稅）
    currency: str = "TWD"
    note: str = ""                   # 備註 → 摘要

    # 會計處理
    entries: list[JournalEntry] = Field(default_factory=list)
    debit_total: Optional[float] = None
    credit_total: Optional[float] = None
    balance_check: bool = False

    confidence: Optional[Confidence] = None
    field_evidence: dict = Field(default_factory=dict)
    validation_warnings: list[str] = Field(default_factory=list)
    origin: dict = Field(default_factory=dict)
    quality: dict = Field(default_factory=dict)
    recognition_diagnostics: dict = Field(default_factory=dict)
    box_issues: list[dict] = Field(default_factory=list)
    classification_meta: dict = Field(default_factory=dict)
    payment_state: dict = Field(default_factory=dict)  # 原始與正規化付款資訊
    model_config = ConfigDict(extra="allow")

    # 關鍵欄位在掃描影像上的邊界框（正規化 0–1000，[x1,y1,x2,y2]），供視覺對照
    boxes: dict = Field(default_factory=dict)

    # 稽核/來源
    source_file: str = ""
    ocr_provider: str = ""
    structure_model: str = ""
    # 該頁掃描影像的暫存路徑（不寫入 JSON 藍本，僅供落地時複製保存）
    source_image: str = Field(default="", exclude=True)


class AnalyzeResponse(BaseModel):
    ok: bool = True
    voucher: Optional[Voucher] = None
    detail: str = ""


class ConfigUpdate(BaseModel):
    """前端設定 modal 回傳：各階段的 provider/model 字串。"""
    ocr: Optional[str] = None
    structure: Optional[str] = None
    classify: Optional[str] = None
