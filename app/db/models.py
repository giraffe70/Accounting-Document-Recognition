"""資料庫模型（SQLModel）：憑證紀錄 + 審核狀態 + 對齊訊號。

設計重點：同時保留 AI 原始輸出（ai_json）與人工修正後（current_json），
兩者差異即為「模型對齊/微調」的訓練訊號。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel
from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Company(SQLModel, table=True):
    __tablename__ = "companies"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""
    short: str = ""
    tax_id: str = ""
    account_chart_key: str = Field(index=True, unique=True)


class ReviewStatus(str, Enum):
    prepared = "prepared"                  # 待外觀確認（已判類型/轉正，尚未萃取欄位）
    queued = "queued"                      # 掃描清單／待內容提取（外觀已確認）
    pending = "pending"                    # 待確認（內容辨識已完成）
    confirmed = "confirmed"                # 辨識內容人工確認完成


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
    company_id: Optional[int] = Field(default=None, foreign_key="companies.id", index=True)
    period: str = ""                       # 所屬期別 YYYY-MM（供日記帳彙整）

    status: ReviewStatus = Field(default=ReviewStatus.pending, index=True)
    confidence: str = ""

    ai_json: str = "{}"                    # AI 原始結構化輸出（凍結不改）
    current_json: str = "{}"               # 目前值（人工修正後）

    correction_note: str = ""
    confirmed_at: Optional[datetime] = None
    confirmed_by: Optional[int] = Field(default=None, foreign_key="users.id")
    confirmed_by_name: str = Field(default="", sa_column_kwargs={"server_default": ""})
    recognition_confirmed_version: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    entries_status: str = Field(default="none", index=True)
    entries_confirmed_at: Optional[datetime] = None
    entries_confirmed_by: Optional[int] = Field(default=None, foreign_key="users.id")
    entries_confirmed_by_name: str = ""
    entries_proposal_json: str = "{}"
    entries_history: str = "[]"

    # 手寫／字跡潦草等 AI 讀不可靠、欄位由人工鍵入者，由審核者勾記。
    # 用途：① 清單一眼看出哪些不是機器讀的 ② 重新辨識前擋下覆蓋 ③ 統計自動化涵蓋率
    manual_entry: bool = False

    ocr_provider: str = ""
    structure_model: str = ""
    image_path: str = ""              # 主頁掃描影像相對路徑（相對 uploads/）
    attachments: str = "[]"           # 附頁影像相對路徑清單（JSON）：多頁合併為一張時的其餘頁

    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ItemAccountMapping(SQLModel, table=True):
    __tablename__ = "item_account_mappings"
    __table_args__ = (UniqueConstraint("company_id", "item_name_key", name="uq_company_item"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id")
    doc_type_key: str = ""
    item_name: str = ""
    item_name_key: str
    account_code: str = ""
    account_name: str = ""
    source_voucher_id: Optional[int] = Field(default=None, sa_column=Column(
        Integer, ForeignKey("voucher_records.id", ondelete="SET NULL"), nullable=True))
    source_item_id: str = ""
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    created_by_name: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_by: Optional[int] = Field(default=None, foreign_key="users.id")
    updated_by_name: str = ""
    updated_at: datetime = Field(default_factory=utc_now)
    is_active: bool = True
    version: int = 1


class MappingHistory(SQLModel, table=True):
    __tablename__ = "mapping_history"
    id: Optional[int] = Field(default=None, primary_key=True)
    mapping_id: int = Field(foreign_key="item_account_mappings.id", index=True)
    version: int
    before_json: str = "null"
    after_json: str = "{}"
    source_voucher_id: Optional[int] = Field(default=None, sa_column=Column(
        Integer, ForeignKey("voucher_records.id", ondelete="SET NULL"), nullable=True))
    source_item_id: str = ""
    actor_id: Optional[int] = Field(default=None, foreign_key="users.id")
    actor_name: str = ""
    created_at: datetime = Field(default_factory=utc_now)
