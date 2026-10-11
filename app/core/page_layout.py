"""Page routing, conservative document separation and immutable source crops."""
from __future__ import annotations
import hashlib
from pathlib import Path
from pydantic import Field
from PIL import Image
from app.core import prompts, doc_router, doc_types, invocation_trace, model_invocation, model_factory, settings_store
from app.core.geometry import validate_box, convert_box, iou

MAX_REGIONS = 16
LAYOUT_VERSION = 2


class Region(prompts.RouteOutput):
    confidence: str = "low"
    box: list[int] | None = None
    line_ids: list[int] = Field(default_factory=list)


class PageRoute(prompts.RouteOutput):
    content_kind: str = "document"
    layout_kind: str = "uncertain"
    regions: list[Region] = Field(default_factory=list)


def validate_layout(raw, mode="image", ocr_lines=None, size=None, evidence=None):
    # The legacy single-document router remains an explicit compatible contract.
    route = doc_router.validate_route({k: raw[k] for k in ("type", "rotate", "reason") if k in raw}, mode)
    extra = set(raw) - {"type", "rotate", "reason", "content_kind", "layout_kind", "regions"}
    if extra:
        raise ValueError("unknown layout properties: " + str(sorted(extra)))
    kind = raw.get("content_kind", "document")
    if kind not in ("blank", "supporting", "non_voucher", "document", "unknown"):
        raise ValueError("invalid content_kind")
    if raw.get("type") not in doc_types.DOC_TYPES or len(str(raw.get("reason", ""))) > 120:
        raise ValueError("invalid page type or reason longer than 120 characters")
    layout_kind = raw.get("layout_kind", "multiple" if len(raw.get("regions", [])) > 1 else "single")
    if layout_kind not in ("single", "multiple", "uncertain"):
        raise ValueError("invalid layout_kind")
    regions = raw.get("regions", [])
    issues, valid = [], []
    if not isinstance(regions, list) or len(regions) > MAX_REGIONS:
        return {**route, "content_kind": kind, "regions": [], "auto_split": False, "issues": ["region count must be 0–16"]}
    for index, raw_region in enumerate(regions):
        try:
            if not isinstance(raw_region, dict):
                raise ValueError("region must be an object")
            if raw_region.get("type") not in doc_types.DOC_TYPES:
                raise ValueError("region type is not a document type")
            if len(str(raw_region.get("reason", ""))) > 120:
                raise ValueError("reason longer than 120 characters")
            if mode != "ocr_text" and "line_ids" in raw_region:
                raise ValueError("image regions must not contain line_ids")
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
                # Text bounds exclude the paper's margins. Expand only to a unique
                # independently observed complete border, never to another group.
                box = region["box"]
                enclosing = [frame for frame in (evidence or {}).get("frames", []) if
                    frame[0] <= box[0] and frame[1] <= box[1] and frame[2] >= box[2] and frame[3] >= box[3]]
                if len(enclosing) == 1:
                    region["ocr_region_box"] = box
                    region["box"] = enclosing[0]
            else:
                region["box"] = validate_box(region["box"])
            valid.append(region)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append({"index": index, "raw": raw_region, "reason": str(exc)})
    if not regions and kind == "document" and layout_kind == "single":
        valid = [{**route, "box": [0, 0, 1000, 1000], "confidence": "high", "line_ids": list(range(len(ocr_lines or [])))}]
    auto = not issues and bool(valid) and layout_kind != "uncertain" and all(r["confidence"] == "high" for r in valid)
    if valid and (layout_kind == "single" and len(valid) > 1 or layout_kind == "multiple" and len(valid) == 1):
        issues.append("layout kind disagrees with region count")
        auto = False
    if any(iou(a["box"], b["box"]) > .1 for i, a in enumerate(valid) for b in valid[i+1:]):
        auto = False
        issues.append("overlapping document regions")
    from app.core import image_evidence
    evidence = evidence or image_evidence.layout_evidence(lines=ocr_lines, size=size)
    independent_issues = image_evidence.verify_regions(valid, evidence)
    issues.extend(independent_issues)
    auto = auto and not independent_issues and kind == "document"
    fallback = None
    if kind == "document" and not auto:
        single = image_evidence.single_document(evidence)
        if single is not None:
            if any(a.get("type") == "invoice" for a in evidence["anchors"]):
                route["type"] = "invoice"
            valid = [{**route, "box": single, "confidence": "high", "line_ids": list(range(len(ocr_lines or [])))}]
            auto = True
            fallback = "independent source evidence supports one complete document"
    return {**route, "content_kind": kind, "layout_kind": layout_kind, "regions": valid,
            "auto_split": auto, "issues": issues, "fallback_reason": fallback, "source_evidence": evidence, "version": LAYOUT_VERSION}


def detect(image_path, *, mode="image", ocr_lines=None, ocr_text="", model_setting=None, native_path=None):
    from app.core import pipeline, activity
    setting = model_setting or settings_store.get_settings()["structure"]
    provider, model = settings_store.parse_setting(setting)
    chat = model_factory.get_chat_model(provider, model, temperature=0.0)
    instruction = ("先判斷每頁版面。只有各自具有獨立單號與金額的單據才建立不同 regions；多聯、明細列、續頁屬同一單據。"
        "最多16區域，box為原頁0–1000整數，rotate順時針；confidence為high/medium/low。"
        "頁面角色content_kind為blank/supporting/non_voucher/document/unknown；要保書保障額度不是付款金額。"
        "layout_kind為single/multiple/uncertain。reason最多120字，單張使用完整單據區域；不可將文字行、表格欄位、合計區當單據。"
        "每區域含type,rotate,reason,confidence,box。圖片模式禁止line_ids；文字模式box=null並加line_ids，只用已提供的唯一OCR行ID。")
    prompt = prompts.build_route_prompt(mode, ocr_text)
    instructions, data = prompt.split(prompts.DATA_SEPARATOR, 1)
    prompt = instructions + "\n" + instruction + prompts.DATA_SEPARATOR + prompts._json({"page_data": data, "ocr_lines": [{"id": i, "text": r.get("text", "")} for i,r in enumerate(ocr_lines or [])]})
    content = [{"type": "text", "text": prompt}]
    if mode == "image":
        content.append({"type": "image_url", "image_url": {"url": pipeline._data_uri(image_path)}})
    with Image.open(image_path) as image:
        size = image.size
    from app.core import image_evidence, barcode
    qr = barcode.decode_diagnostics(native_path or image_path)
    invocation_trace.add({"kind":"layout_qr", **qr})
    evidence = image_evidence.layout_evidence(native_path or image_path, ocr_lines, size, qr)
    schema = PageRoute.model_json_schema()
    schema["required"] = ["type", "rotate", "reason", "content_kind", "layout_kind", "regions"]
    schema["properties"]["content_kind"]["enum"] = ["blank", "supporting", "non_voucher", "document", "unknown"]
    schema["properties"]["layout_kind"]["enum"] = ["single", "multiple", "uncertain"]
    schema["properties"]["regions"]["maxItems"] = MAX_REGIONS
    region_schema = schema["$defs"]["Region"]
    region_schema["required"] = ["type", "rotate", "reason", "confidence", "box"]
    for definition in (schema, region_schema):
        definition["properties"]["type"]["enum"] = list(doc_types.DOC_TYPES)
        definition["properties"]["reason"]["maxLength"] = 120
    if mode == "ocr_text":
        region_schema["required"].append("line_ids")
        region_schema["properties"]["line_ids"].update(maxItems=len(ocr_lines or []), uniqueItems=True,
            items={"type":"integer", "minimum":0, "maximum":max(0,len(ocr_lines or [])-1)})
    else:
        region_schema["properties"].pop("line_ids")
    region_schema["properties"]["confidence"]["enum"] = ["high", "medium", "low"]
    region_schema["properties"]["box"] = {"type":"null"} if mode == "ocr_text" else {"type":"array","items":{"type":"integer","minimum":0,"maximum":1000},"minItems":4,"maxItems":4}
    region_schema["properties"]["rotate"]["enum"] = [0] if mode == "ocr_text" else [0,90,180,270]
    schema["properties"]["rotate"]["enum"] = region_schema["properties"]["rotate"]["enum"]
    def validate_output(raw):
        missing = set(schema["required"]) - set(raw)
        if missing:
            raise ValueError("missing page layout fields: " + str(sorted(missing)))
        for region in raw.get("regions", []):
            if not isinstance(region, dict) or set(region_schema["required"]) - set(region):
                raise ValueError("missing required document region fields")
        return validate_layout(raw, mode, ocr_lines, size, evidence)
    with activity.stage("structure", setting + "（判類型／版面）"):
        result = model_invocation.invoke_structured(chat, content, validate_output, task="layout", schema=schema, invoke=pipeline._invoke_text)
    # A model blank judgment is only accepted when image and OCR independently agree.
    if result["content_kind"] == "blank":
        result["auto_split"] = pipeline._is_blank_page(native_path or image_path, ink_thresh=.0001) and not ocr_text.strip()
    elif result["content_kind"] in ("supporting", "non_voucher"):
        import re
        role_supported = (doc_types.is_supporting(result["type"]) if result["content_kind"] == "supporting" else
                          bool(re.search(r"要保書|保障(?:額度|內容)|保險契約|使用說明|產品說明|說明頁", ocr_text)))
        if re.search(r"電子發票證明聯|統一發票|會計傳票|實收金額|應繳金額", ocr_text):
            role_supported = False
        result["auto_split"] = role_supported and not any(a.get("source") == "qr" for a in evidence["anchors"])
    if result["content_kind"] != "document" and not result["auto_split"]:
        result["issues"].append("page role lacks independent evidence; source retained for review")
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
