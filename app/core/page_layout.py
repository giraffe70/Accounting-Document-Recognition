"""Page routing, conservative document separation and immutable source crops."""
from __future__ import annotations
import hashlib
from pathlib import Path
from pydantic import Field
from PIL import Image
from app.core import prompts, doc_router, doc_types, invocation_trace, model_invocation, model_factory, settings_store
from app.core.geometry import validate_box, convert_box, iou

MAX_REGIONS = 16
LAYOUT_VERSION = 1


class Region(prompts.RouteOutput):
    confidence: str = "low"
    box: list[int] | None = None
    line_ids: list[int] = Field(default_factory=list)


class PageRoute(prompts.RouteOutput):
    content_kind: str = "document"
    regions: list[Region] = Field(default_factory=list)


def validate_layout(raw, mode="image", ocr_lines=None, size=None):
    # The legacy single-document router remains an explicit compatible contract.
    route = doc_router.validate_route({k: raw[k] for k in ("type", "rotate", "reason") if k in raw}, mode)
    extra = set(raw) - {"type", "rotate", "reason", "content_kind", "regions"}
    if extra:
        raise ValueError("unknown layout properties: " + str(sorted(extra)))
    kind = raw.get("content_kind", "document")
    if kind not in ("blank", "supporting", "document", "unknown"):
        raise ValueError("invalid content_kind")
    regions = raw.get("regions", [])
    issues, valid = [], []
    if not isinstance(regions, list) or len(regions) > MAX_REGIONS:
        return {**route, "content_kind": kind, "regions": [], "auto_split": False, "issues": ["region count must be 0–16"]}
    for index, raw_region in enumerate(regions):
        try:
            region = Region.model_validate(raw_region).model_dump()
            region.update(doc_router.validate_route({k: region[k] for k in ("type", "rotate", "reason")}, mode))
            if region["confidence"] not in ("high", "medium", "low"):
                raise ValueError("invalid confidence")
            if mode == "ocr_text":
                ids = region["line_ids"]
                if not ids or len(set(ids)) != len(ids) or any(type(i) is not int or i < 0 or i >= len(ocr_lines or []) for i in ids):
                    raise ValueError("invalid OCR line IDs")
                points = [p for i in ids for p in ocr_lines[i]["box"]]
                region["box"] = convert_box([min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)], unit="pixels", width=size[0], height=size[1])
            else:
                region["box"] = validate_box(region["box"])
            valid.append(region)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append({"index": index, "raw": raw_region, "reason": str(exc)})
    if not regions and kind != "blank":
        valid = [{**route, "box": [0, 0, 1000, 1000], "confidence": "high", "line_ids": list(range(len(ocr_lines or [])))}]
    auto = not issues and all(r["confidence"] == "high" for r in valid)
    if any(iou(a["box"], b["box"]) > .1 for i, a in enumerate(valid) for b in valid[i+1:]):
        auto = False
        issues.append("overlapping document regions")
    return {**route, "content_kind": kind, "regions": valid, "auto_split": auto, "issues": issues, "version": LAYOUT_VERSION}


def detect(image_path, *, mode="image", ocr_lines=None, ocr_text="", model_setting=None):
    from app.core import pipeline, activity
    setting = model_setting or settings_store.get_settings()["structure"]
    provider, model = settings_store.parse_setting(setting)
    chat = model_factory.get_chat_model(provider, model, temperature=0.0)
    instruction = ("先判斷每頁版面。只有各自具有獨立單號與金額的單據才建立不同 regions；多聯、明細列、續頁屬同一單據。"
        "最多16區域，box為原頁0–1000整數，rotate順時針；confidence為high/medium/low。"
        "content_kind為blank/supporting/document/unknown。單張可省略regions；空白regions=[]。"
        "每區域含type,rotate,reason,confidence,box,line_ids。文字模式box=null，只用提供的OCR行ID分組。")
    prompt = prompts.build_route_prompt(mode, ocr_text)
    instructions, data = prompt.split(prompts.DATA_SEPARATOR, 1)
    prompt = instructions + "\n" + instruction + prompts.DATA_SEPARATOR + prompts._json({"page_data": data, "ocr_lines": [{"id": i, "text": r.get("text", "")} for i,r in enumerate(ocr_lines or [])]})
    content = [{"type": "text", "text": prompt}]
    if mode == "image":
        content.append({"type": "image_url", "image_url": {"url": pipeline._data_uri(image_path)}})
    with Image.open(image_path) as image:
        size = image.size
    schema = PageRoute.model_json_schema()
    schema["required"] = ["type", "rotate", "reason", "content_kind", "regions"]
    schema["properties"]["content_kind"]["enum"] = ["blank", "supporting", "document", "unknown"]
    schema["properties"]["regions"]["maxItems"] = MAX_REGIONS
    region_schema = schema["$defs"]["Region"]
    region_schema["required"] = ["type", "rotate", "reason", "confidence", "box", "line_ids"]
    region_schema["properties"]["confidence"]["enum"] = ["high", "medium", "low"]
    region_schema["properties"]["box"] = {"type":"null"} if mode == "ocr_text" else {"type":"array","items":{"type":"integer","minimum":0,"maximum":1000},"minItems":4,"maxItems":4}
    region_schema["properties"]["rotate"]["enum"] = [0] if mode == "ocr_text" else [0,90,180,270]
    schema["properties"]["rotate"]["enum"] = region_schema["properties"]["rotate"]["enum"]
    with activity.stage("structure", setting + "（判類型／版面）"):
        result = model_invocation.invoke_structured(chat, content, lambda raw: validate_layout(raw, mode, ocr_lines, size), task="layout", schema=schema, invoke=pipeline._invoke_text)
    # A model blank judgment is only accepted when image and OCR independently agree.
    if result["content_kind"] == "blank" and (not pipeline._is_blank_page(image_path, ink_thresh=.0001) or ocr_text.strip()):
        result.update(auto_split=False, issues=[*result["issues"], "blank judgment conflicts with source evidence"])
    invocation_trace.add({"kind": "layout", "result": result})
    return result


def crop(source, region, target, *, max_edge=None):
    box = validate_box(region["box"])
    rotation = region.get("rotate", 0)
    if rotation not in (0, 90, 180, 270):
        raise ValueError("invalid rotation")
    with Image.open(source) as image:
        w, h = image.size
        pixels = [round(box[0]*w/1000), round(box[1]*h/1000), round(box[2]*w/1000), round(box[3]*h/1000)]
        if pixels[0] >= pixels[2] or pixels[1] >= pixels[3]:
            raise ValueError("region has no source pixels")
        result = image.convert("RGB").crop(pixels).rotate(-rotation, expand=True)
        if max_edge:
            result.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        result.save(target)
    return str(target)


def origin(source, region, page_no=1):
    return {"version": 1, "coordinate_unit": "normalized_1000", "region": region["box"],
            "rotation": region.get("rotate", 0), "page_no": page_no,
            "image_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest()}
