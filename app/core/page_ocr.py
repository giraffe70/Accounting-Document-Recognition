"""Immutable whole-page OCR cache, with explicit return to source coordinates."""
from __future__ import annotations
import json
import uuid
from pathlib import Path
from PIL import Image
from app.core import invocation_trace, ocr_cache

VERSION = 2


def identity(source, setting):
    from app.core.ocr_engine import runtime_signature
    return {"version": VERSION, **ocr_cache.pixels(source), "ocr": setting,
            "engine": runtime_signature(), "max_edge": 2400, "orientation_policy": 1}


def read(source, target_size, *, folder, setting="paddle/pp-ocrv5"):
    with ocr_cache.scope(Path(folder) / "ocr_cache"):
        return _read(source, target_size, folder=folder, setting=setting)


def _read(source, target_size, *, folder, setting):
    from app.core import model_factory, settings_store, pipeline, image_evidence
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    provider, _ = settings_store.parse_setting(setting)
    ocr = model_factory.get_ocr(provider)
    cache_identity = identity(source, setting)
    key = ocr_cache.digest(cache_identity)
    saved = folder / (key + ".json")
    record = None
    if saved.is_file():
        try:
            candidate = json.loads(saved.read_text(encoding="utf-8"))
            if ocr_cache.valid(candidate, cache_identity) and candidate.get("rotation") in (0, 90, 180, 270):
                record = candidate
        except (OSError, ValueError):
            pass
    # Unique immutable input copies also avoid concurrent orientation scratch collisions.
    copy = image_evidence.native_ocr(source, folder / (key + "_" + uuid.uuid4().hex + ".png"))
    if record is None:
        _, rotation = pipeline._auto_orient_ocr(copy, ocr)
        record = ocr_cache.seal({"identity": cache_identity, "rotation": rotation})
        try:
            invocation_trace.atomic_json(saved, record)
        except OSError as exc:
            invocation_trace.add({"kind": "ocr_cache_error", "operation": "orientation_write", "error": str(exc)})
    else:
        rotation = record["rotation"]
        if rotation:
            pipeline._apply_rotation(copy, rotation)
    # Direction records contain no OCR lines. Every full read uses the shared,
    # pixel-validated store, in the actual upright input frame.
    with ocr_cache.purpose("layout"):
        native_lines = ocr.recognize(copy)
    with Image.open(copy) as image:
        size = list(image.size)
    text = ocr.plain_text(native_lines)
    w, h = size
    tw,th=target_size
    matrix={0:[[tw/w,0,0],[0,th/h,0]],90:[[0,tw/h,0],[-th/w,0,th]],
            180:[[-tw/w,0,tw],[0,-th/h,th]],270:[[0,-tw/h,tw],[th/w,0,0]]}[rotation]
    def source_point(p):
        x, y = p[0]/w, p[1]/h
        x, y = {0:lambda:(x,y), 90:lambda:(y,1-x), 180:lambda:(1-x,1-y), 270:lambda:(1-y,x)}[rotation]()
        return [x*target_size[0], y*target_size[1]]
    lines = [{**line, "box": [source_point(p) for p in line["box"]]} for line in native_lines]
    invocation_trace.add({"kind":"page_ocr", "identity":cache_identity, "rotation":rotation, "source_size":size,
                          "target_size":list(target_size), "target_frame":"original_page", "lines":lines, "text":text})
    return lines, text, rotation, {"ocr_size":size, "target_size":list(target_size), "rotation":rotation, "coordinate_unit":"pixels", "target_frame":"original_page", "matrix":matrix,"cache_key":key}
