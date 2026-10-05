"""客製會計科目表：載入／儲存（供分類參照與人工編輯）。

**分公司**：客戶各公司用的是不同的科目表——仲彬的現金是 1101、進項稅額 1147、
運費 6205；實業那套是 1105／1290／6050，代號完全對不起來。若共用一張表，
分類會給出另一家公司的科目代號，記帳士等於每張都要重改。故以公司別分開存放。

檔案格式（新）：{"_default": [...], "仲彬": [...], ...}
舊格式（單一 list）自動視為 _default，不需手動轉檔。
"""
import json

from app import config

_ACCOUNTS_FILE = config.DATA_DIR / "accounts.json"
_DEFAULT_KEY = "_default"


def _load_all() -> dict:
    if not _ACCOUNTS_FILE.exists():
        return {}
    try:
        data = json.loads(_ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list):          # 舊格式：單一全域科目表
        return {_DEFAULT_KEY: data}
    return data if isinstance(data, dict) else {}


def _clean(accounts: list) -> list:
    out = []
    for a in accounts or []:
        code = str(a.get("code", "")).strip()
        name = str(a.get("name", "")).strip()
        if code or name:
            out.append({"code": code, "name": name,
                        "category": str(a.get("category", "")).strip()})
    return out


def load_accounts(company: str = "") -> list:
    """取該公司的科目表；該公司沒有自己的表時回退到 _default。"""
    all_ = _load_all()
    if company and all_.get(company):
        return all_[company]
    return all_.get(_DEFAULT_KEY, [])


def save_accounts(accounts: list, company: str = "") -> list:
    """整表覆蓋儲存該公司的科目表（company 留空＝通用表）。"""
    all_ = _load_all()
    clean = _clean(accounts)
    all_[company or _DEFAULT_KEY] = clean
    _ACCOUNTS_FILE.write_text(json.dumps(all_, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def companies_with_chart() -> list:
    """有自訂科目表的公司別（供前端下拉標示）。"""
    return [k for k in _load_all() if k != _DEFAULT_KEY]


def as_prompt_list(company: str = "") -> str:
    """壓成一行供 LLM 提示（分類時優先使用該公司的客製科目）。"""
    return "、".join(f"{a['code']} {a['name']}"
                    for a in load_accounts(company) if a.get("code"))
