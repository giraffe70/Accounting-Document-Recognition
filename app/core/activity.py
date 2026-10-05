"""三個辨識階段（OCR／VLM 結構化／LLM 分類）的即時使用狀態。

地端模型單張要跑數十秒，畫面上若只有一句「辨識中…」，使用者無從得知
卡在哪一段、還要多久。各階段進出時登記於此，供 /api/engine-status 輪詢，
前端據以顯示狀態列與進度條。純記憶體、行程重啟即清空。
"""
import threading
import time

STAGES = ("ocr", "structure", "classify")
STAGE_LABEL = {"ocr": "OCR 文字", "structure": "VLM 結構化", "classify": "LLM 科目分類"}

_lock = threading.Lock()
_running: dict[str, dict] = {}   # stage -> {"note": str, "started": float}
_last: dict[str, dict] = {}      # stage -> {"note": str, "secs": float, "at": float}


class stage:
    """with activity.stage("ocr", "PP-OCRv6"): ...

    可重入巢狀（批次多頁時同階段會重複進出），以最後一次 enter 的時間為準。
    """

    def __init__(self, name: str, note: str = ""):
        self.name = name
        self.note = note

    def __enter__(self):
        self._perf_started = time.perf_counter()
        with _lock:
            _running[self.name] = {"note": self.note, "started": time.time()}
        return self

    def __exit__(self, exc_type, exc, tb):
        from app.core import invocation_trace

        invocation_trace.record_stage(
            self.name, self.note, time.perf_counter() - self._perf_started)
        with _lock:
            info = _running.pop(self.name, None)
            if info:
                _last[self.name] = {"note": self.note,
                                    "secs": round(time.time() - info["started"], 1),
                                    "at": time.time()}
        return False


def snapshot() -> dict:
    """各階段目前是否執行中、已跑幾秒、上次耗時。"""
    now = time.time()
    with _lock:
        return {
            s: {
                "running": s in _running,
                "note": (_running.get(s) or _last.get(s) or {}).get("note", ""),
                "elapsed": round(now - _running[s]["started"], 1) if s in _running else None,
                "last_secs": (_last.get(s) or {}).get("secs"),
            }
            for s in STAGES
        }


def any_running() -> bool:
    with _lock:
        return bool(_running)
