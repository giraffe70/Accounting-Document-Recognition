"""每辨識階段的模型選擇，持久化於 data/settings.json。

沿用 research_log_agent：預設值在程式碼，JSON 檔覆蓋其上；
"provider/model" 字串由 parse_setting 拆成 (provider, model)。
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar

from app import config

_settings = None
_override = ContextVar("recognition_settings", default=None)


@contextmanager
def isolated_settings(values: dict):
    """Override only this execution context; never persist experiment settings."""
    token = _override.set(dict(values))
    try:
        yield
    finally:
        _override.reset(token)


def _load():
    merged = dict(config.DEFAULT_SETTINGS)
    if config.SETTINGS_FILE.exists():
        try:
            merged.update(json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return merged


def get_settings() -> dict:
    global _settings
    if _override.get() is not None:
        return dict(_override.get())
    if _settings is None:
        _settings = _load()
    return dict(_settings)


def update_settings(new: dict) -> dict:
    global _settings
    cur = get_settings()
    # 只接受已知階段
    cur.update({k: v for k, v in new.items() if k in config.DEFAULT_SETTINGS})
    config.SETTINGS_FILE.write_text(
        json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _settings = cur
    return dict(cur)


def parse_setting(setting_str: str, fallback=("ollama", "qwen2.5:7b")):
    """'provider/model' → (provider, model)。"""
    if setting_str and "/" in setting_str:
        provider, model = setting_str.split("/", 1)
        return provider, model
    return fallback
