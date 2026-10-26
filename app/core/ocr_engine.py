"""PaddleOCR 3.x 地端引擎封裝（延遲載入、失敗時給明確錯誤）。

回傳格式統一為 list[{"text": str, "box": [[x,y]*4], "score": float}]，
座標為原影像像素（未內部旋轉，方便與顯示影像對齊）。
"""
import os
import time
import threading
from functools import lru_cache
from importlib.metadata import version, PackageNotFoundError
from app.core import ocr_cache

# paddle 3.3 + paddleocr 3.7 在 Windows CPU 的 oneDNN 有相容性 bug，需停用
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

OPTIONS = {"enable_mkldnn": False, "use_doc_orientation_classify": False,
           "use_doc_unwarping": False, "use_textline_orientation": True}
INFERENCE_LOCK = threading.RLock()


def runtime_signature(lang="chinese_cht"):
    """Package defaults are versioned too; never mistake the UI label for a model ID."""
    dependencies = {}
    for package in ("paddleocr", "paddlex", "paddlepaddle", "paddlepaddle-gpu", "Pillow"):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = None
    return {"provider": "paddle", "implementation_version": 2, "lang": lang,
            "options": dict(OPTIONS), "dependencies": dependencies,
            "device": "package_default", "FLAGS_use_mkldnn": os.environ.get("FLAGS_use_mkldnn")}


class PaddleOCREngine:
    def __init__(self, lang: str = "chinese_cht"):
        self.lang = lang

    def recognize(self, image_path: str) -> list[dict]:
        metrics = {}
        return ocr_cache.recognize(image_path, runtime_signature(self.lang),
                                   lambda: self._recognize(image_path, metrics), metrics)

    def _recognize(self, image_path, metrics):
        started = time.perf_counter()
        try:
            ocr = _get_paddle(self.lang)
        finally:
            metrics["engine_init_seconds"] = time.perf_counter() - started
        started = time.perf_counter()
        try:
            with INFERENCE_LOCK:
                result = list(ocr.predict(image_path) or [])
        finally:
            metrics["predict_seconds"] = time.perf_counter() - started
        lines: list[dict] = []
        for r in result or []:
            texts = (r.get("rec_texts") if hasattr(r, "get") else None) or []
            polys = r.get("dt_polys") if hasattr(r, "get") else None
            scores = (r.get("rec_scores") if hasattr(r, "get") else None) or []
            for i, text in enumerate(texts):
                box = []
                if polys is not None and i < len(polys):
                    try:
                        box = polys[i].tolist()
                    except AttributeError:
                        box = list(polys[i])
                lines.append({
                    "text": text, "box": box,
                    "score": float(scores[i]) if i < len(scores) else 0.0,
                })
        return lines

    @staticmethod
    def plain_text(lines: list[dict]) -> str:
        return "\n".join(l["text"] for l in lines)


def _get_paddle(lang: str):
    with INFERENCE_LOCK:
        return _load_paddle(lang)


@lru_cache(maxsize=2)
def _load_paddle(lang: str):
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PaddleOCR 未安裝（需 Python ≤3.12 環境）；未裝時自動降級為 VLM 直讀。"
        ) from exc
    # 關閉文件方向自動校正（否則框會落在校正後座標系，與顯示影像不對齊）；
    # 方向由本專案 _auto_orient_ocr 以實體旋轉影像處理。保留單行方向(180)校正。
    return PaddleOCR(
        lang=lang,
        **OPTIONS,
    )
