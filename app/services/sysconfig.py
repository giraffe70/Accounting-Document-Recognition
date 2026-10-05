"""系統組態（可由管理員於 WebUI 編輯，持久化於 data/system_config.json）。

三大區塊：
  llm          — LLM 連線方式（Ollama / H200 vLLM 端點）與雲端 API Key
  scan_source  — 掃描單據的遠端存取點（本機/SMB/NAS/雲端硬碟/SFTP）
  cloud_deploy — 雲端部署空間與權限設定

密鑰欄位在讀取時遮罩；儲存時若送出空值則保留原值（避免被遮罩佔位覆蓋）。
預設值取自 .env，之後以本檔為準。
"""
import json

from app import config

_FILE = config.DATA_DIR / "system_config.json"

_SECRET_KEYS = {"google_api_key", "openai_api_key", "anthropic_api_key",
                "password", "secret_key", "access_key"}

_DEFAULTS = {
    "llm": {
        "ollama_base_url": config.OLLAMA_BASE_URL,
        "h200_base_url": config.H200_BASE_URL,
        "google_api_key": config.GOOGLE_API_KEY,
        "openai_api_key": config.OPENAI_API_KEY,
        "anthropic_api_key": config.ANTHROPIC_API_KEY,
    },
    "scan_source": {
        "type": "local",      # local / smb / nas / gdrive / onedrive / sftp
        "host": "", "path": "", "username": "", "password": "", "note": "",
    },
    "cloud_deploy": {
        "provider": "none",   # none / azure / gcp / aws
        "region": "", "bucket": "", "access_key": "", "secret_key": "",
        "permission": "private",  # private / team / public
        "note": "",
    },
}


def _load_raw() -> dict:
    data = {k: dict(v) for k, v in _DEFAULTS.items()}
    if _FILE.exists():
        try:
            saved = json.loads(_FILE.read_text(encoding="utf-8"))
            for sec, vals in saved.items():
                if sec in data and isinstance(vals, dict):
                    data[sec].update(vals)
        except (json.JSONDecodeError, OSError):
            pass
    return data


def get(section: str, key: str, default: str = ""):
    return _load_raw().get(section, {}).get(key, default)


def get_section(section: str) -> dict:
    return _load_raw().get(section, {})


def view() -> dict:
    """給前端：密鑰不回傳實值，改以 secrets_set 標記是否已設定。"""
    data = _load_raw()
    out, sset = {}, {}
    for sec, vals in data.items():
        out[sec], sset[sec] = {}, {}
        for k, v in vals.items():
            if k in _SECRET_KEYS:
                out[sec][k] = ""
                sset[sec][k] = bool(v)
            else:
                out[sec][k] = v
    return {"config": out, "secrets_set": sset}


def save_section(section: str, values: dict) -> dict:
    data = _load_raw()
    if section not in data:
        return None
    for k, v in values.items():
        if k not in data[section]:
            continue
        # 密鑰欄位：空值不覆蓋（保留原值）
        if k in _SECRET_KEYS and (v is None or v == ""):
            continue
        data[section][k] = v
    _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return view()
