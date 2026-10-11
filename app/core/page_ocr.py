"""Immutable whole-page OCR cache, with explicit return to source coordinates."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from PIL import Image
from app.core import invocation_trace

VERSION = 1


def read(source, target_size, *, folder, setting="paddle/pp-ocrv5"):
    from app.core import model_factory, settings_store, pipeline, image_evidence
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    identity = {"version": VERSION, "source_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(), "ocr": setting, "max_edge": 2400}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    saved = folder / (key + ".json")
    record = None
    if saved.is_file():
        try:
            candidate = json.loads(saved.read_text(encoding="utf-8"))
            if candidate.get("identity") == identity:
                record = candidate
        except (OSError, ValueError):
            pass
    if record is None:
        provider, _ = settings_store.parse_setting(setting)
        ocr = model_factory.get_ocr(provider)
        copy = image_evidence.native_ocr(source, folder / (key + ".png"))
        _, rotation = pipeline._auto_orient_ocr(copy, ocr)
        lines = ocr.recognize(copy)
        with Image.open(copy) as image:
            size = image.size
        record = {"identity": identity, "rotation": rotation, "size": list(size), "lines": lines,
                  "text": ocr.plain_text(lines), "path": copy}
        invocation_trace.atomic_json(saved, record)
    w, h = record["size"]
    rotation = record["rotation"]
    tw,th=target_size
    matrix={0:[[tw/w,0,0],[0,th/h,0]],90:[[0,tw/h,0],[-th/w,0,th]],
            180:[[-tw/w,0,tw],[0,-th/h,th]],270:[[0,-tw/h,tw],[th/w,0,0]]}[rotation]
    def source_point(p):
        x, y = p[0]/w, p[1]/h
        x, y = {0:lambda:(x,y), 90:lambda:(y,1-x), 180:lambda:(1-x,1-y), 270:lambda:(1-y,x)}[rotation]()
        return [x*target_size[0], y*target_size[1]]
    lines = [{**line, "box": [source_point(p) for p in line["box"]]} for line in record["lines"]]
    invocation_trace.add({"kind":"page_ocr", "identity":identity, "rotation":rotation, "source_size":record["size"],
                          "target_size":list(target_size), "target_frame":"original_page", "lines":lines, "text":record["text"]})
    return lines, record["text"], rotation, {"ocr_size":record["size"], "target_size":list(target_size), "rotation":rotation, "coordinate_unit":"pixels", "target_frame":"original_page", "matrix":matrix,"cache_key":key}
