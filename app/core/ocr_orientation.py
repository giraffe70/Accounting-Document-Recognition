"""Local-only document orientation classification, with an observable fallback."""
import hashlib
import math
import time
from functools import lru_cache
from pathlib import Path
from app.core import invocation_trace, ocr_policy

MODEL = "PP-LCNet_x1_0_doc_ori"
VERSION = 1


@lru_cache(maxsize=8)
def _weights(path, stamps):
    return {name: hashlib.sha256((Path(path) / name).read_bytes()).hexdigest()
            for name, _, _ in stamps}


def signature():
    policy = ocr_policy.get_policy()
    folder = Path(policy["orientation_model_dir"])
    files = sorted(p for p in folder.glob("*") if p.is_file() and p.suffix in (".json", ".pdiparams", ".yml", ".yaml")) if folder.is_dir() else []
    stamps = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files)
    return {"version": VERSION, "model": MODEL, "mode": policy["orientation_mode"],
            "threshold": policy["orientation_threshold"], "weights": _weights(str(folder), stamps)}


@lru_cache(maxsize=2)
def _load_classifier(model_dir):
    from paddleocr import DocImgOrientationClassification
    return DocImgOrientationClassification(model_name=MODEL, model_dir=model_dir,
                                            device="cpu", enable_mkldnn=False)


def classify(path):
    policy = ocr_policy.get_policy()
    result = {"angle": None, "score": None, "accepted": False, "reason": "disabled"}
    if policy["orientation_mode"] != "auto":
        return result
    started = time.perf_counter()
    init = 0
    invocation_trace.add({"kind": "orientation_classifier_start", "model": MODEL})
    try:
        folder = Path(policy["orientation_model_dir"])
        if not folder.is_dir() or not list(folder.glob("*.pdiparams")):
            result["reason"] = "model_missing"
            return result
        from app.core.ocr_engine import INFERENCE_LOCK
        with INFERENCE_LOCK:
            init_start = time.perf_counter()
            model = _load_classifier(str(folder))
            init = time.perf_counter() - init_start
            predictions = list(model.predict(str(path)))
        if len(predictions) != 1:
            raise ValueError("orientation classifier must return one result")
        value = predictions[0]
        predicted_angle = int(value["label_names"][0])
        score = float(value["scores"][0])
        if predicted_angle not in (0, 90, 180, 270) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("invalid orientation prediction")
        # Paddle's label is the counterclockwise correction; our geometry uses CW.
        angle = (-predicted_angle) % 360
        accepted = score >= float(policy["orientation_threshold"])
        result.update(angle=angle, predicted_angle=predicted_angle, score=score, accepted=accepted,
                      reason="high_confidence" if accepted else "low_confidence")
    except Exception as exc:
        result.update(reason="classifier_error", error=f"{type(exc).__name__}: {exc}")
    finally:
        invocation_trace.add({"kind": "orientation_classifier", **result,
                              "seconds": time.perf_counter() - started, "engine_init_seconds": init})
    return result
