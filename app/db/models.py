"""資料庫模型（SQLModel）：憑證紀錄 + 審核狀態 + 對齊訊號。

設計重點：同時保留 AI 原始輸出（ai_json）與人工修正後（current_json），
兩者差異即為「模型對齊/微調」的訓練訊號。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class ReviewStatus(str, Enum):
    prepared = "prepared"                  # 待外觀確認（已判類型/轉正，尚未萃取欄位）
    queued = "queued"                      # 掃描清單／待內容提取（外觀已確認）
    pending = "pending"                    # 待確認（內容辨識已完成）
    confirmed = "confirmed"                # 人工確認完成，可納入日記帳


class UserAccount(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    password_hash: str = ""
    salt: str = ""
    role: str = "clerk"          # clerk（記帳士）/ accountant（會計師）/ admin（管理員）
    name: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class VoucherRecord(SQLModel, table=True):
    __tablename__ = "voucher_records"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_file: str = ""
    record_role: str = "voucher"
    source_parent_id: Optional[int] = Field(default=None, foreign_key="voucher_records.id")
    recognition_history: str = "[]"
    company: str = ""                      # 中意/仲彬/天干（多公司支援）
    period: str = ""                       # 所屬期別 YYYY-MM（供日記帳彙整）

    status: ReviewStatus = Field(default=ReviewStatus.pending, index=True)
    confidence: str = ""

    ai_json: str = "{}"                    # AI 原始結構化輸出（凍結不改）
    current_json: str = "{}"               # 目前值（人工修正後）

    correction_note: str = ""
    confirmed_at: Optional[datetime] = None
    # 舊流程欄位只保留歷史資料，正式流程不再寫入。
    clerk_note: str = ""
    accountant_note: str = ""
    dev_note: str = ""               # 開發者備註（管理員專屬）：紀錄辨識錯誤供修正回饋
    clerk_reviewed_at: Optional[datetime] = None
    accountant_approved_at: Optional[datetime] = None

    # 手寫／字跡潦草等 AI 讀不可靠、欄位由人工鍵入者，由審核者勾記。
    # 用途：① 清單一眼看出哪些不是機器讀的 ② 重新辨識前擋下覆蓋 ③ 統計自動化涵蓋率
    manual_entry: bool = False

    ocr_provider: str = ""
    structure_model: str = ""
    image_path: str = ""              # 主頁掃描影像相對路徑（相對 uploads/）
    attachments: str = "[]"           # 附頁影像相對路徑清單（JSON）：多頁合併為一張時的其餘頁

    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
