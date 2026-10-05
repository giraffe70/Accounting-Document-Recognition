"""模型工廠：單一入口切換地端/雲端。

沿用 research_log_agent 的 LLMFactory 模式並擴充：
  - 文字/視覺 LLM：ollama / h200（自架 vLLM）/ google / anthropic / openai（皆走 LangChain 統一介面）
  - OCR：paddle（地端）/ docai・azure（雲端，預留）

所有 provider SDK 皆延遲匯入（lazy import），未安裝或未設金鑰時給明確錯誤，
不影響其他 provider 與伺服器啟動。
"""
from functools import lru_cache

import requests

from app import config
from app.services import sysconfig

# 固定隨機種子：同一張憑證重跑要得到同樣結果，人工修正才有意義
#（否則「改了規則有沒有效」與「這次剛好對」分不出來）。
RUN_SEED = 42

# ── 文字 / 視覺 LLM ─────────────────────────────────────────────


def get_chat_model(provider: str, model: str, temperature: float = 0.0):
    """憑證辨識與分錄分類都是「同一張單只有一個正確答案」的任務，不需要創造性。

    回傳 LangChain BaseChatModel。VLM 與 LLM 共用此入口（差別只在呼叫時是否附圖）。

    預設 temperature=0（貪婪解碼）以求可重現：實測每案跑 3 次時，16 案中有 6 案
    結果會變動（萃取、分類、連傳票辨識都會飄），使得任何改動的成效都分不出
    是真的還是抽樣運氣。Ollama 另固定 seed——即使貪婪解碼，隨機種子仍可能讓
    平手的 token 選擇不同。
    """
    llm = sysconfig.get_section("llm")
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=model, base_url=llm.get("ollama_base_url") or config.OLLAMA_BASE_URL,
            temperature=temperature, seed=RUN_SEED,
            num_ctx=config.OLLAMA_NUM_CTX,
        )
    if provider == "google":
        _require_key(llm.get("google_api_key"), "Google API Key")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model, google_api_key=llm["google_api_key"], temperature=temperature
        )
    if provider == "anthropic":
        _require_key(llm.get("anthropic_api_key"), "Anthropic API Key")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model, api_key=llm["anthropic_api_key"], temperature=temperature
        )
    if provider == "openai":
        _require_key(llm.get("openai_api_key"), "OpenAI API Key")
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model, api_key=llm["openai_api_key"], temperature=temperature
        )
    if provider == "h200":
        # 自架 H200 vLLM（OpenAI 相容），走內網 VPN；目前免金鑰但 SDK 需任意字串。
        # 文字（qwen3-32b）與視覺（qwen2.5-vl-7b）共用此端點，故不分 provider。
        # Qwen3 有思考模式：本專案任務皆為分類/萃取（簡單任務），依伺服器使用規範
        # 關閉思考以省時省 token；max_tokens 需 ≥2048，否則會被思考耗盡致 content 為 null。
        # enable_thinking 對 VL 模型是靜默忽略（伺服器端 2026-08-17 實測），故無條件帶著，
        # 不依模型名稱分支——少一個會隨模型換代而過期的判斷。
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            base_url=llm.get("h200_base_url") or config.H200_BASE_URL,
            api_key=config.H200_API_KEY,
            temperature=temperature,
            seed=RUN_SEED,
            max_tokens=4096,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    raise ValueError(f"不支援的 LLM provider：{provider}")


# ── OCR ─────────────────────────────────────────────────────────


def get_ocr(provider: str = "paddle"):
    """回傳 OCR 引擎物件（具 .recognize(image_path) -> list[dict]）。"""
    if provider == "paddle":
        from app.core.ocr_engine import PaddleOCREngine

        return PaddleOCREngine()
    if provider in ("disabled", "none"):
        raise RuntimeError("此辨識工作已停用 OCR")
    raise ValueError(f"不支援的 OCR provider：{provider}（雲端 OCR 尚未實作）")


# ── 可選模型清單（給前端下拉）───────────────────────────────────

_OLLAMA_CAPABILITIES: dict[str, set[str]] = {}


def _ollama_model_capabilities(model_id: str) -> set[str]:
    """Ask Ollama for declared capabilities, cached by exact model id.

    Model names are not a reliable capability signal: custom models such as
    gemma4:e4b support vision without containing "vl" or "vision" in the name.
    """
    model_id = str(model_id or "")
    if not model_id.startswith("ollama/"):
        return set()
    if model_id in _OLLAMA_CAPABILITIES:
        return _OLLAMA_CAPABILITIES[model_id]
    base = sysconfig.get("llm", "ollama_base_url") or config.OLLAMA_BASE_URL
    try:
        response = requests.post(
            f"{base}/api/show", json={"model": model_id.split("/", 1)[1]}, timeout=3)
        response.raise_for_status()
        capabilities = {str(x).lower() for x in response.json().get("capabilities", [])}
    except Exception:
        capabilities = set()
    _OLLAMA_CAPABILITIES[model_id] = capabilities
    return capabilities


def is_embedding_model(model_id: str) -> bool:
    """Known embedding families cannot generate classification/extraction text."""
    capabilities = _ollama_model_capabilities(model_id)
    if "embedding" in capabilities and "completion" not in capabilities:
        return True
    name = str(model_id or "").lower().rsplit("/", 1)[-1]
    return any(token in name for token in (
        "bge-", "bge_", "embed", "embedding", "all-minilm", "e5-", "e5_",
    ))


def is_vision_model(model_id: str) -> bool:
    """Return whether a selectable model accepts image input."""
    model_id = str(model_id or "").lower()
    configured_cloud_vlms = {
        item.lower()
        for provider in config.CLOUD_MODELS.values()
        for item in provider.get("vlm", [])
    }
    if model_id in configured_cloud_vlms:
        return True
    capabilities = _ollama_model_capabilities(model_id)
    if capabilities:
        return "vision" in capabilities
    name = model_id.rsplit("/", 1)[-1]
    return any(token in name for token in (
        "-vl", "vl:", "vl-", "vision", "llava", "bakllava", "moondream", "minicpm-v",
    ))


def list_models(kind: str = "llm") -> list[str]:
    """合併地端 Ollama 動態清單 + H200 vLLM 動態清單 + 已設金鑰的雲端模型。
    kind: 'vlm'、'llm' 或 'text'。vlm 僅回傳可看圖的模型；text 排除視覺與 embedding 模型。"""
    ollama = [m for m in _ollama_models() if not is_embedding_model(m)]
    if kind == "vlm":
        models = [m for m in ollama if is_vision_model(m)]
    elif kind == "text":
        models = [m for m in ollama if not is_vision_model(m)]
    else:
        models = ollama
    # H200 自 2026-08-17 起同時提供 qwen3-32b（純文字）與 qwen2.5-vl-7b（視覺）。
    # vlm 選單濾掉純文字模型——選錯了要等呼叫失敗才知道。以 id 含 "vl" 判定，
    # 是遷就 vLLM 那端的命名慣例（qwen2.5-vl-7b / 未來的 qwen3-vl-*）；
    # 日後若上了不照此命名的視覺模型，這裡要跟著補。
    h200 = [m for m in _h200_models() if not is_embedding_model(m)]
    if kind == "vlm":
        models.extend(m for m in h200 if is_vision_model(m))
    elif kind == "text":
        models.extend(m for m in h200 if not is_vision_model(m))
    else:
        models.extend(h200)
    llm = sysconfig.get_section("llm")
    keymap = {"google": "google_api_key", "anthropic": "anthropic_api_key", "openai": "openai_api_key"}
    for prov, info in config.CLOUD_MODELS.items():
        if llm.get(keymap.get(prov, "")):
            models.extend(info.get("vlm" if kind == "vlm" else "llm", info.get("llm", [])))
    # OCR 選項固定（目前僅地端 paddle）
    return sorted(set(models))


def list_ocr_providers() -> list[str]:
    return ["paddle/pp-ocrv5"]


def ocr_status() -> dict:
    """地端 OCR 是否真的可用（只查套件在不在，不載模型，故很快）。

    PaddlePaddle 無 Python 3.13 輪檔，換機時最容易漏建 3.12 環境；
    未裝時系統會靜默降級為 VLM 直讀（精準框消失、統編/金額略降），
    故把狀態掛到設定畫面讓人看得見。"""
    import importlib.util
    import sys

    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    if importlib.util.find_spec("paddleocr") is None:
        return {"available": False, "python": pyver,
                "detail": f"PaddleOCR 未安裝（目前 Python {pyver}；需 ≤3.12）"
                          "，辨識將降級為 VLM 直讀。"}
    return {"available": True, "python": pyver,
            "detail": f"地端 PaddleOCR 可用（Python {pyver}）"}


@lru_cache(maxsize=1)
def _ollama_models_cached():
    return _fetch_ollama_models()


def _ollama_models():
    try:
        return _fetch_ollama_models()
    except Exception:
        return ["ollama/qwen2.5:7b", "ollama/qwen2.5vl:7b"]


def _h200_models():
    """向 H200 vLLM 查詢已載入模型（GET /models）。

    連不上（未連 VPN、橋接沒跑）時仍列出預設模型讓人選得到——
    錯誤留到實際呼叫時再報，與 Ollama 清單的行為一致。"""
    base = sysconfig.get("llm", "h200_base_url") or config.H200_BASE_URL
    try:
        resp = requests.get(f"{base}/models", timeout=3)
        resp.raise_for_status()
        return [f"h200/{m['id']}" for m in resp.json().get("data", [])]
    except Exception:
        return ["h200/qwen3-32b", "h200/qwen2.5-vl-7b"]


def _fetch_ollama_models():
    base = sysconfig.get("llm", "ollama_base_url") or config.OLLAMA_BASE_URL
    resp = requests.get(f"{base}/api/tags", timeout=3)
    resp.raise_for_status()
    data = resp.json()
    return [f"ollama/{m['name']}" for m in data.get("models", [])]


def _require_key(key: str, name: str):
    if not key:
        raise RuntimeError(f"未設定 {name}，無法使用此雲端模型（請於 .env 填入或改用地端）。")
