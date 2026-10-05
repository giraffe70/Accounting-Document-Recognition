"""PaddleOCR 3.x 地端引擎封裝（延遲載入、失敗時給明確錯誤）。

回傳格式統一為 list[{"text": str, "box": [[x,y]*4], "score": float}]，
座標為原影像像素（未內部旋轉，方便與顯示影像對齊）。
"""
import os
import time
from functools import lru_cache

# paddle 3.3 + paddleocr 3.7 在 Windows CPU 的 oneDNN 有相容性 bug，需停用
os.environ.setdefault("FLAGS_use_mkldnn", "0")


class PaddleOCREngine:
    def __init__(self, lang: str = "chinese_cht"):
        self.lang = lang

    def recognize(self, image_path: str) -> list[dict]:
        started = time.perf_counter()
        ocr = _get_paddle(self.lang)
        result = ocr.predict(image_path)
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
        from app.core import invocation_trace
        invocation_trace.add({"kind": "ocr_result", "provider": "paddle",
                              "image": str(image_path), "seconds": round(time.perf_counter() - started, 6),
                              "lines": lines})
        return lines

    @staticmethod
    def plain_text(lines: list[dict]) -> str:
        return "\n".join(l["text"] for l in lines)


@lru_cache(maxsize=2)
def _get_paddle(lang: str):
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
        enable_mkldnn=False,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=True,
    )
