"""全域設定：路徑、端點、金鑰、各辨識階段預設模型。

沿用 research_log_agent 的模式：以 dotenv 載入端點/金鑰，
每個階段的模型選擇以 "provider/model" 字串存於 data/settings.json。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# PaddleOCR(paddle 3.x) 在 Windows CPU 的 oneDNN 有相容性 bug，全域停用
os.environ.setdefault("FLAGS_use_mkldnn", "0")

load_dotenv()

# ── 路徑 ──
APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
DATA_DIR = APP_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
STATIC_DIR = APP_DIR / "static"
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "app.db"
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"

for _d in (DATA_DIR, UPLOAD_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── 伺服器 ──
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8090"))

# ── 端點 / 金鑰 ──
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
# 圖片、完整 OCR 與欄位契約共用 context；所有階段使用同一設定。
SUPPORTED_OLLAMA_CONTEXTS = (8192, 12288, 16384)


def validate_ollama_context(value):
    if type(value) is not int or value not in SUPPORTED_OLLAMA_CONTEXTS:
        raise ValueError("OLLAMA_NUM_CTX 必須為 8192、12288 或 16384")
    return value


try:
    OLLAMA_NUM_CTX = validate_ollama_context(int(os.getenv("OLLAMA_NUM_CTX", "16384")))
except ValueError as exc:
    raise ValueError("OLLAMA_NUM_CTX 必須為 8192、12288 或 16384") from exc


def get_ollama_context():
    return validate_ollama_context(OLLAMA_NUM_CTX)


# 自架 H200 vLLM 推論伺服器（OpenAI 相容 API；規格見 C:\Dev\H200\docs\api-usage-prompt.md）
# 過渡期經 SSH 橋接走 127.0.0.1:18000，防火牆開通後改 192.168.10.105:22003（皆需 VPN）
# 同一端點同時提供 qwen3-32b（分類）與 qwen2.5-vl-7b（結構化），不需拆雙端點
H200_BASE_URL = os.getenv("H200_BASE_URL", "http://127.0.0.1:18000/v1")
H200_API_KEY = os.getenv("H200_API_KEY", "EMPTY")  # 目前免金鑰，SDK 需要任意字串
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── 上傳限制 ──
MAX_FILE_SIZE = 5 * 1024 * 1024  # 合約規定 5MB
ALLOWED_SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}

# ── 各階段預設模型（地端優先）──
# 階段：ocr（辨識文字）/ structure（VLM 結構化）/ classify（文字 LLM 科目分類）
DEFAULT_SETTINGS = {
    "ocr": "paddle/pp-ocrv5",
    "structure": "ollama/qwen2.5vl:7b",
    "classify": "ollama/qwen2.5:7b",
}

# ── 雲端模型清單（有設對應金鑰才會出現在選單）──
CLOUD_MODELS = {
    "google": {
        "key": GOOGLE_API_KEY,
        "vlm": ["google/gemini-2.5-flash", "google/gemini-2.5-pro"],
        "llm": ["google/gemini-2.5-flash", "google/gemini-2.5-pro"],
    },
    "anthropic": {
        "key": ANTHROPIC_API_KEY,
        "vlm": ["anthropic/claude-sonnet-4", "anthropic/claude-opus-4"],
        "llm": ["anthropic/claude-sonnet-4", "anthropic/claude-opus-4"],
    },
    "openai": {
        "key": OPENAI_API_KEY,
        "vlm": ["openai/gpt-4o"],
        "llm": ["openai/gpt-4o", "openai/gpt-4o-mini"],
    },
}
