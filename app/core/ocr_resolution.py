"""Conservative resolution selection; high-resolution evidence always wins."""
import time
from pathlib import Path
from PIL import Image
from app.core import image_evidence, invocation_trace, ocr_cache, ocr_policy

VERSION = 1


def choose_edge(path, policy=None):
    policy = policy or ocr_policy.get_policy()
    if isinstance(policy, str):
        policy = {"resolution": policy}
    mode = policy["resolution"]
    if mode != "adaptive":
        return int(mode.rsplit("_", 1)[1]), {"policy": mode, "reason": "fixed"}
    info = {"policy": mode, "reason": "uncertain", "blank": False}
    started = time.perf_counter()
    try:
        import cv2
        import numpy as np
        with Image.open(path) as image:
            image = image.convert("L")
            image.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
            gray = np.asarray(image)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        count, _, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
        h, w = gray.shape
        components = [s for s in stats[1:] if s[4] >= 8 and 3 <= s[3] <= h * .15 and 2 <= s[2] <= w * .15]
        height = float(np.percentile([s[3] for s in components], 10)) if components else 0
        table = any((s[2] >= w * .25 and s[3] <= 4) or (s[3] >= h * .25 and s[2] <= 4) for s in stats[1:])
        contrast = float(np.percentile(gray, 95) - np.percentile(gray, 5))
        # Sparse printed text may occupy less than 5% of the image.
        ink = gray[binary > 0]
        contrast = max(contrast, float(np.median(gray) - np.median(ink)) if ink.size else 0)
        blank = float((gray < 200).mean()) < .004
        safe = 20 <= len(components) <= 300 and height >= 16 and not table and contrast >= 45
        info.update(candidate_count=len(components), height_p10=height, table_lines=bool(table),
                    contrast=contrast, blank=blank, reason="large_sparse_text" if safe else "conservative_high_resolution")
        return (2000 if safe else 2400), info
    except Exception as exc:
        info["error"] = type(exc).__name__
        return 2400, info
    finally:
        invocation_trace.add({"kind": "resolution_assessment", **info, "seconds": time.perf_counter() - started})


def needs_upgrade(lines, size, assessment):
    if not lines:
        return not assessment.get("blank", False), "empty_nonblank"
    import numpy as np
    scores = [float(line.get("score", 0)) for line in lines]
    heights = [max(p[1] for p in line["box"]) - min(p[1] for p in line["box"]) for line in lines if line.get("box")]
    if sum(scores) / len(scores) < .95:
        return True, "low_confidence"
    if not heights or float(np.percentile(heights, 10)) < 16:
        return True, "small_text"
    return False, "readable"


def read(source, target, ocr, purpose="full"):
    from app.core.ocr_engine import runtime_signature
    with ocr_cache.scope():
        high = image_evidence.native_ocr(source, target, max_edge=2400)
        signature = runtime_signature(getattr(ocr, "lang", "chinese_cht"))
        if ocr_cache.contains(high, signature):
            with ocr_cache.purpose(purpose):
                return ocr.recognize(high), high
        policy = ocr_policy.get_policy()
        edge, assessment = choose_edge(source, policy)
        if edge < 2400:
            selected = str(Path(target).with_name(Path(target).stem + f"_{edge}.png"))
            image_evidence.native_ocr(source, selected, max_edge=edge)
        else:
            selected = high
        with ocr_cache.purpose(purpose):
            lines = ocr.recognize(selected)
        if edge == 2000 and policy["resolution"] == "adaptive":
            with Image.open(selected) as image:
                upgrade, reason = needs_upgrade(lines, image.size, assessment)
            if upgrade:
                invocation_trace.add({"kind": "resolution_upgrade", "from_edge": 2000, "to_edge": 2400, "reason": reason})
                with ocr_cache.purpose(purpose):
                    lines = ocr.recognize(high)
                selected = high
        invocation_trace.add({"kind": "resolution_selected", "edge": edge if selected != high else 2400, "assessment": assessment})
        return lines, selected
