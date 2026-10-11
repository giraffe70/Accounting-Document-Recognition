"""Two-stage page routing. Source evidence authorizes every automatic crop."""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Literal
from PIL import Image
from pydantic import Field, StrictInt
from app.core import prompts, doc_router, doc_types, invocation_trace, model_invocation, model_factory, settings_store
from app.core import image_evidence, model_coordinates, classification_evidence
from app.core.geometry import validate_box, convert_box, iou

MAX_REGIONS = 16
LAYOUT_VERSION = 3


class Region(prompts.RouteOutput):
    confidence: Literal["high", "medium", "low"] = "low"
    box: list[StrictInt] | None = None
    line_ids: list[StrictInt] = Field(default_factory=list)


class PageRoute(prompts.RouteOutput):
    content_kind: Literal["blank", "supporting", "non_voucher", "document", "unknown"]
    layout_kind: Literal["single", "multiple", "uncertain"]
    confidence: Literal["high", "medium", "low"]


def validate_page(raw, mode="image"):
    page = PageRoute.model_validate(raw).model_dump()
    if page["type"] not in doc_types.DOC_TYPES or len(page["reason"]) > 120:
        raise ValueError("invalid page type or reason longer than 120 characters")
    page.update(doc_router.validate_route({k:page[k] for k in ("type","rotate","reason")},mode))
    return page


def validate_layout(raw, mode="image", ocr_lines=None, size=None, evidence=None, *, coordinate_context=None, strict=False):
    """Compatible saved-layout validator; production uses strict repairable checks."""
    route = doc_router.validate_route({k:raw[k] for k in ("type","rotate","reason") if k in raw}, mode)
    if set(raw)-{"type","rotate","reason","content_kind","layout_kind","regions","confidence"}:
        raise ValueError("unknown layout properties")
    kind = raw.get("content_kind","document")
    layout_kind = raw.get("layout_kind","multiple" if len(raw.get("regions",[]))>1 else "single")
    if kind not in ("blank","supporting","non_voucher","document","unknown") or layout_kind not in ("single","multiple","uncertain"):
        raise ValueError("invalid page role or layout kind")
    regions = raw.get("regions",[])
    evidence = evidence or image_evidence.layout_evidence(lines=ocr_lines,size=size)
    issues, valid = [], []
    if not isinstance(regions,list) or len(regions)>MAX_REGIONS:
        raise ValueError("region count must be 0–16")
    if (layout_kind=="single" and len(regions)>1) or (layout_kind=="multiple" and len(regions)<2):
        issues.append("layout kind disagrees with region count")
    for index, value in enumerate(regions):
        try:
            if not isinstance(value,dict) or value.get("type") not in doc_types.DOC_TYPES:
                raise ValueError("region type is not a document type")
            if mode!="ocr_text" and "line_ids" in value:
                raise ValueError("image regions must not contain line_ids")
            region = Region.model_validate(value).model_dump()
            region.update(doc_router.validate_route({k:region[k] for k in ("type","rotate","reason")},mode))
            if len(region["reason"])>120:
                raise ValueError("reason longer than 120 characters")
            if mode=="ocr_text":
                ids = region["line_ids"]
                if not ids or len(set(ids))!=len(ids) or any(type(i) is not int or not 0<=i<len(ocr_lines or []) for i in ids):
                    raise ValueError("invalid OCR line IDs")
                if region["box"] is not None:
                    raise ValueError("text regions must use OCR IDs, not model boxes")
                points = [p for i in ids for p in ocr_lines[i]["box"]]
                region["box"] = convert_box([min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)],unit="pixels",width=size[0],height=size[1])
                region["ocr_region_box"] = region["box"]
                region["box"] = image_evidence.safe_margin(region["box"],evidence,selected_ids=ids)
                region["coordinate_source"] = "ocr"
            else:
                region["raw_box"] = region["box"]
                region["box"] = model_coordinates.convert(region["box"],coordinate_context) if coordinate_context is not None else validate_box(region["box"])
                region["box"] = image_evidence.safe_margin(region["box"],evidence)
                region["coordinate_source"] = "model_calibrated" if coordinate_context is not None else "saved_normalized"
            valid.append(region)
        except (ValueError,TypeError,KeyError) as exc:
            issues.append({"index":index,"raw":value,"reason":str(exc)})
    if not regions and kind=="document" and layout_kind=="single":
        valid=[{**route,"box":[0,0,1000,1000],"confidence":"high","line_ids":list(range(len(ocr_lines or []))),"coordinate_source":"whole_page"}]
    if any(iou(a["box"],b["box"])>.1 for i,a in enumerate(valid) for b in valid[i+1:]):
        issues.append("overlapping document regions")
    issues.extend(image_evidence.verify_regions(valid,evidence))
    auto = bool(valid) and not issues and layout_kind!="uncertain" and kind=="document" and all(r["confidence"]=="high" for r in valid)
    if strict and issues:
        invocation_trace.add({"kind":"layout_validation_error","issues":issues,"raw":raw})
        raise ValueError(str(issues)[:800])
    return {**route,"content_kind":kind,"layout_kind":layout_kind,"regions":valid,"auto_split":auto,
            "issues":issues,"fallback_reason":None,"source_evidence":evidence,"version":LAYOUT_VERSION}


def _page_schema(mode):
    schema=PageRoute.model_json_schema()
    schema["properties"]["type"]["enum"]=list(doc_types.DOC_TYPES)
    schema["properties"]["rotate"]["enum"]=[0] if mode=="ocr_text" else [0,90,180,270]
    schema["properties"]["reason"]["maxLength"]=120
    return schema


def _region_schema(mode, lines, coordinates):
    region=Region.model_json_schema()
    region["required"]=["type","rotate","reason","confidence","box"]
    region["properties"]["type"]["enum"]=list(doc_types.DOC_TYPES)
    region["properties"]["rotate"]["enum"]=[0] if mode=="ocr_text" else [0,90,180,270]
    region["properties"]["reason"]["maxLength"]=120
    if mode=="ocr_text":
        region["required"].append("line_ids")
        region["properties"]["box"]={"type":"null"}
        region["properties"]["line_ids"]={"type":"array","minItems":1,"maxItems":len(lines),"uniqueItems":True,"items":{"type":"integer","minimum":0,"maximum":max(0,len(lines)-1)}}
    else:
        region["properties"].pop("line_ids")
        region["properties"]["box"]=model_coordinates.box_schema(coordinates)
    return {"type":"object","additionalProperties":False,"required":["regions"],
            "properties":{"regions":{"type":"array","minItems":2,"maxItems":MAX_REGIONS,"items":region}}}


def _prompt_evidence(evidence):
    # Text is supplied once; retain positions and complete groups without
    # repeating every OCR line in both the text and evidence sections.
    groups = image_evidence.independent_groups(evidence)
    return {**{k:evidence.get(k) for k in ("frames", "anchors", "coordinate_unit", "source_size", "ocr_source_size")},
            "complete_groups":[{"box":box, "line_ids":[r["id"] for r in evidence.get("text_boxes", [])
                if image_evidence._contains(box, r["box"])]} for box in groups]}


def detect(image_path, *, mode="image", ocr_lines=None, ocr_text="", model_setting=None, native_path=None):
    from app.core import pipeline, activity, barcode, page_ocr
    settings=settings_store.get_settings()
    setting=model_setting or settings["structure"]
    provider,model=settings_store.parse_setting(setting)
    chat=model_factory.get_chat_model(provider,model,temperature=0.0)
    with Image.open(image_path) as image:
        size=image.size
    rotation, transforms=0,{}
    if ocr_lines is None and settings_store.parse_setting(settings.get("ocr","disabled/none"))[0] not in ("disabled","none"):
        try:
            with activity.stage("ocr", "原頁版面 OCR"):
                ocr_lines,ocr_text,rotation,transforms=page_ocr.read(native_path or image_path,size,folder=Path(image_path).parent/"page_ocr",setting=settings.get("ocr","paddle/pp-ocrv5"))
        except Exception as exc:
            invocation_trace.add({"kind":"layout_ocr_error","error":str(exc)})
            ocr_lines=[]
    ocr_lines=ocr_lines or []
    coordinates=model_coordinates.for_image(image_path,setting)
    qr=barcode.decode_diagnostics(native_path or image_path)
    invocation_trace.add({"kind":"layout_qr",**qr})
    evidence=image_evidence.layout_evidence(native_path or image_path,ocr_lines,size,qr)
    invocation_trace.add({"kind":"layout_source_evidence", "evidence":evidence})
    menu="；".join(f"{k}={v['label']}" for k,v in doc_types.DOC_TYPES.items())
    instructions="\n".join([prompts.ROUTE_TEMPLATE,prompts.DATA_BOUNDARY,prompts._mode(mode),"類型："+menu,
        "先只判斷頁面角色及單張／多張／不確定；禁止輸出regions或任何裁切框。",
        "content_kind: blank/supporting/non_voucher/document/unknown；layout_kind: single/multiple/uncertain；confidence: high/medium/low。",
        "多聯、表格欄位、明細列及合計區屬同一單據。佐證會計關係不等同非憑證；有独立付款資訊的收據與車票仍是document。",
        "reason最多120字。rotate順時針0/90/180/270，純文字模式固定0。"])
    data={"ocr_text":ocr_text,"source_evidence":_prompt_evidence(evidence)}
    content=[{"type":"text","text":instructions+prompts.DATA_SEPARATOR+prompts._json(data)}]
    if mode=="image":
        content.append({"type":"image_url","image_url":{"url":pipeline._data_uri(image_path)}})
    page_errors=[]
    def page_validator(raw):
        page=validate_page(raw,mode)
        if page["content_kind"]=="document" and page["layout_kind"]=="single" and len(image_evidence.independent_groups(evidence))>1:
            raise ValueError("single contradicts independent complete transaction groups; repair layout_kind")
        return page
    try:
        with activity.stage("structure",setting+"（頁面角色／判類型）"):
            page=model_invocation.invoke_structured(chat,content,page_validator,task="layout",schema=_page_schema(mode),invoke=pipeline._invoke_text)
    except ValueError as exc:
        page_errors.append(str(exc))
        supported,reason=classification_evidence.supported_type(ocr_text)
        if not supported or image_evidence.single_document(evidence) is None:
            return {"type":"other","rotate":rotation,"reason":"版面契約未修復","content_kind":"unknown","layout_kind":"uncertain","regions":[],"auto_split":False,"issues":page_errors,"fallback_reason":None,"source_evidence":evidence,"version":LAYOUT_VERSION,"coordinate_context":coordinates,"image_transforms":transforms}
        page={"type":supported,"rotate":rotation,"reason":reason,"content_kind":"document","layout_kind":"single","confidence":"high"}
    effective,classification=classification_evidence.resolve(page["type"],ocr_text)
    if classification["adopted"]:
        page["type"]=effective
        page["content_kind"]="document"
    result={**page,"regions":[],"auto_split":False,"issues":page_errors,"fallback_reason":None,"source_evidence":evidence,
            "version":LAYOUT_VERSION,"coordinate_context":coordinates,"image_transforms":transforms,"classification_evidence":classification}
    if mode=="ocr_text" and rotation:
        result["rotate"]=rotation
    if page["content_kind"] in ("blank","supporting","non_voucher"):
        import re
        compact=re.sub(r"\s+","",ocr_text)
        strong_financial=bool(classification_evidence.supported_type(ocr_text)[0] or re.search(r"實[收付](?:金額)?|應[繳付](?:金額)?|[付收]款(?:總額|金額)|繳[費款]金額",compact) or any(a.get("source")=="qr" for a in evidence["anchors"]))
        proof=(pipeline._is_blank_page(native_path or image_path,ink_thresh=.0001) and not ocr_text.strip()) if page["content_kind"]=="blank" else (
            doc_types.is_supporting(page["type"]) and bool(ocr_text.strip()) if page["content_kind"]=="supporting" else bool(re.search(r"要保書|保障(?:額度|內容)|保險契約|使用說明|產品說明|說明頁",compact)))
        result["auto_split"]=bool(proof and not strong_financial)
        if not result["auto_split"]:
            result["issues"].append("page role lacks independent evidence; source retained for review")
    elif page["content_kind"]=="document" and page["layout_kind"]=="single":
        # No model crop for a single document. Table borders cannot remove the header.
        single=image_evidence.single_document(evidence)
        multiple_proof=image_evidence.independent_groups(evidence)
        if len(multiple_proof)>1:
            result["issues"].append("single judgment conflicts with independent complete groups")
        elif single or page["confidence"]=="high":
            result["regions"]=[{"type":page["type"],"rotate":result["rotate"],"reason":page["reason"],"confidence":"high","box":[0,0,1000,1000],"coordinate_source":"whole_page"}]
            result["auto_split"]=True
            if page_errors or not single:
                result["fallback_reason"]="完整原頁擷取；單張版面或範圍需覆核"
    elif page["content_kind"]=="document" and page["layout_kind"]=="multiple":
        region_mode=mode if coordinates.get("verified") or mode=="ocr_text" else "ocr_text"
        if region_mode=="ocr_text" and not ocr_lines:
            result["issues"].append("multiple page lacks calibrated model geometry and OCR groups")
        else:
            schema=_region_schema(region_mode,ocr_lines,coordinates)
            directive="已判斷為多張，僅輸出regions。每個區域必須是一張完整單據；禁止文字行、表格欄位、多聯與合計區碎片。reason最多120字。"
            directive += "文字模式box=null，line_ids只能使用已提供唯一ID；不接收圖片。" if region_mode=="ocr_text" else "圖片模式禁止line_ids。"+model_coordinates.instructions(coordinates)
            region_data={"page":page, "source_evidence":_prompt_evidence(evidence)}
            if region_mode=="ocr_text":
                positions={r["id"]:r["box"] for r in evidence.get("text_boxes", [])}
                region_data["ocr_lines"]=[{"id":i,"text":r.get("text",""),"box":positions.get(i)} for i,r in enumerate(ocr_lines)]
            else:
                region_data["ocr_text"]=ocr_text
            region_content=[{"type":"text","text":prompts.DATA_BOUNDARY+"\n"+directive+"\n類型："+menu+prompts.DATA_SEPARATOR+prompts._json(region_data)}]
            if region_mode=="image":
                region_content.append({"type":"image_url","image_url":{"url":pipeline._data_uri(image_path)}})
            def validator(raw):
                if set(raw)!={"regions"}:
                    raise ValueError("region task requires only regions")
                layout=validate_layout({**{k:page[k] for k in ("type","rotate","reason","content_kind","layout_kind")},"rotate":0 if region_mode=="ocr_text" else page["rotate"],"regions":raw["regions"]},region_mode,ocr_lines,size,evidence,coordinate_context=coordinates,strict=True)
                if not layout["auto_split"]:
                    raise ValueError("regions require high-confidence complete document evidence")
                return layout
            try:
                with activity.stage("structure",setting+"（多單據定位）"):
                    located=model_invocation.invoke_structured(chat,region_content,validator,task="layout",schema=schema,invoke=pipeline._invoke_text)
                result.update(regions=located["regions"],auto_split=True)
                if region_mode=="ocr_text":
                    for region in result["regions"]:
                        region["rotate"]=result["rotate"]
            except ValueError as exc:
                result["issues"].append(str(exc))
                if image_evidence.single_document(evidence) and len(image_evidence.independent_groups(evidence))<2:
                    result.update(layout_kind="single",auto_split=True,fallback_reason="分割未通過；獨立證據支持單張，保留完整原頁",regions=[{"type":page["type"],"rotate":result["rotate"],"reason":page["reason"],"confidence":"high","box":[0,0,1000,1000],"coordinate_source":"whole_page"}])
    if not result["auto_split"] and page["layout_kind"]=="uncertain" and image_evidence.single_document(evidence):
        result.update(auto_split=True,content_kind="document",layout_kind="single",fallback_reason="版面不確定，獨立證據支持單張完整原頁",regions=[{"type":page["type"],"rotate":result["rotate"],"reason":page["reason"],"confidence":"high","box":[0,0,1000,1000],"coordinate_source":"whole_page"}])
    for region in result["regions"]:
        rows=[r["text"] for r in evidence.get("text_boxes",[]) if image_evidence._contains(region["box"],r["box"])]
        region["type"],region["classification_evidence"]=classification_evidence.resolve(region["type"],"\n".join(rows))
        region["classification_evidence"]["page_type"]=page["type"]
        if region["type"]!=page["type"]:
            result["issues"].append("page and region classification conflict")
    invocation_trace.add({"kind":"layout","result":result})
    return result


def crop(source, region, target, *, max_edge=None):
    box=validate_box(region["box"])
    rotation=region.get("rotate",0)
    if rotation not in (0,90,180,270):
        raise ValueError("invalid rotation")
    with Image.open(source) as image:
        w,h=image.size
        pixels=[round(box[0]*w/1000),round(box[1]*h/1000),round(box[2]*w/1000),round(box[3]*h/1000)]
        if pixels[0]>=pixels[2] or pixels[1]>=pixels[3]:
            raise ValueError("region has no source pixels")
        result=image.convert("RGB").crop(pixels).rotate(-rotation,expand=True)
        if max_edge:
            result.thumbnail((max_edge,max_edge),Image.Resampling.LANCZOS)
        Path(target).parent.mkdir(parents=True,exist_ok=True)
        result.save(target)
    return str(target)


def origin(source, region, page_no=1):
    with Image.open(source) as image:
        size=list(image.size)
    box = validate_box(region["box"])
    x0,y0,x1,y1 = [round(box[i]*size[i%2]/1000) for i in range(4)]
    w,h = x1-x0,y1-y0
    rotation = region.get("rotate",0)
    matrix = {0:[[1,0,-x0],[0,1,-y0]], 90:[[0,-1,y0+h],[1,0,-x0]],
              180:[[-1,0,x0+w],[0,-1,y0+h]],270:[[0,1,-y0],[-1,0,x0+w]]}[rotation]
    return {"version":2,"coordinate_unit":"normalized_1000","region":box,"rotation":rotation,"page_no":page_no,
            "source_size":size,"coordinate_source":region.get("coordinate_source","system"),
            "crop_pixels":[x0,y0,x1,y1],"upright_size":[h,w] if rotation in (90,270) else [w,h],"source_to_upright":matrix,"padding":[0,0,0,0],
            "classification_evidence":region.get("classification_evidence"),"image_sha256":hashlib.sha256(Path(source).read_bytes()).hexdigest()}
