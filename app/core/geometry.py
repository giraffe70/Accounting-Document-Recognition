"""Explicit coordinate conversions. Model coordinates are never inferred or clipped."""
from __future__ import annotations
import math


def validate_box(box):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError("expected one flat [x1,y1,x2,y2] box")
    if not all(type(n) is int for n in box):
        raise ValueError("coordinates must be integers in 0–1000")
    if not all(0 <= n <= 1000 for n in box):
        raise ValueError("coordinate outside 0–1000")
    if box[0] >= box[2] or box[1] >= box[3]:
        raise ValueError("zero area or reversed coordinates")
    return list(box)


def validate_boxes(raw, allowed):
    valid, issues = {}, []
    if not isinstance(raw, dict):
        return {}, [{"field": "boxes", "raw": raw, "reason": "expected an object"}]
    for key, box in raw.items():
        try:
            if key not in allowed:
                raise ValueError("unknown field name")
            valid[key] = validate_box(box)
        except ValueError as exc:
            issues.append({"field": key, "raw": box, "reason": str(exc)})
    return valid, issues


def convert_box(box, *, unit, width, height):
    if unit == "normalized_1000":
        return validate_box(box)
    if unit not in ("pixels", "fraction") or width <= 0 or height <= 0:
        raise ValueError("explicit source unit and positive image size required")
    if len(box) != 4 or not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in box):
        raise ValueError("invalid source coordinates")
    scales = [1000 / width, 1000 / height] * 2 if unit == "pixels" else [1000] * 4
    return validate_box([round(n * scale) for n, scale in zip(box, scales)])


def iou(a, b):
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - overlap
    return overlap / union if union else 0.0


def rotate_box(box, degrees_cw):
    x1,y1,x2,y2 = validate_box(box)
    transform = {0: lambda x,y:(x,y), 90: lambda x,y:(1000-y,x),
                 180: lambda x,y:(1000-x,1000-y), 270: lambda x,y:(y,1000-x)}[degrees_cw]
    points = [transform(x,y) for x,y in ((x1,y1),(x2,y2),(x1,y2),(x2,y1))]
    return [min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]
