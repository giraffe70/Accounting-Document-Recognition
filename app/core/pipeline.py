"""辨識管線：影像準備、分類、契約擷取、證據合併及程式驗證。

圖片與純 OCR 文字共用擷取契約；會計分類由 services.classify 接續處理。
"""
import base64
import json
import logging
import os
import threading
from functools import lru_cache
from pathlib import Path

from app.core import activity, doc_router, doc_types, model_factory, settings_store, ocr_cache
from app.models.schemas import Voucher

logger = logging.getLogger("pdf_scan.pipeline")
_PDF_LOCK = threading.RLock()

def render_to_images(file_path: str) -> list[str]:
    """PDF → 逐頁 PNG 路徑（輸出至暫存區，不污染來源目錄）；圖片 → 原路徑。"""
    p = Path(file_path)
    if p.suffix.lower() != ".pdf":
        return [str(p)]
    import pypdfium2 as pdfium

    from app import config

    render_dir = config.UPLOAD_DIR / "_pages"
    render_dir.mkdir(parents=True, exist_ok=True)
    out = []
    with _PDF_LOCK, pdfium.PdfDocument(str(p)) as pdf:
        for i in range(len(pdf)):
            img = pdf[i].render(scale=200 / 72).to_pil()  # ~200dpi
            out_path = render_dir / f"{p.stem}_p{i}.png"
            img.save(out_path)
            out.append(str(out_path))
    return out


def analyze_pages(file_path: str) -> list[Voucher]:
    """PDF 逐頁辨識為多張憑證（掃描 PDF 常一頁一張）；圖片回單張。
    單頁失敗僅略過該頁，不影響其他頁。"""
    images = render_to_images(file_path)
    n = len(images)
    out = []
    for i, img in enumerate(images):
        try:
            out.append(_structure_image(img, file_path, i, n))
        except Exception as exc:  # noqa: BLE001
            logger.warning("第 %d/%d 頁辨識失敗，略過：%s", i + 1, n, exc)
    return out


def managed_upload_name(file_path: str) -> str:
    """Name of an upload this app saved directly under UPLOAD_DIR, else "".

    Only such files may be deleted with their records; files imported from an
    external scan directory belong to the user and are never recorded."""
    import re

    from app import config
    try:
        path = Path(file_path).resolve()
        if path.parent == config.UPLOAD_DIR.resolve() and re.fullmatch(r"[a-f0-9]{32}\.[a-z0-9]+", path.name):
            return path.name
    except OSError:
        pass
    return ""


def prepare_pages(file_path: str) -> list[Voucher]:
    """Route immutable native pages and prepare separate documents for appearance review."""
    import uuid
    out = []
    for vouchers in iter_prepare_pages(file_path):
        out.extend(vouchers)
    return out


def iter_prepare_pages(file_path):
    import uuid
    group_id = uuid.uuid4().hex
    for index, source in enumerate(render_to_images(file_path), 1):
        yield prepare_page(source, file_path, index, group_id=group_id)


def page_count(file_path):
    if Path(file_path).suffix.lower() != ".pdf":
        from PIL import Image
        with Image.open(file_path) as image:
            image.verify()
        return 1
    import pypdfium2 as pdfium
    with _PDF_LOCK, pdfium.PdfDocument(str(file_path)) as pdf:
        return len(pdf)


def render_page(file_path, page_no, token):
    from app import config
    folder = config.UPLOAD_DIR / "_pages" / token
    folder.mkdir(parents=True, exist_ok=True)
    if Path(file_path).suffix.lower() != ".pdf":
        import uuid
        from PIL import Image, ImageOps
        target = folder / "source.png"
        temporary = folder / ("r" + uuid.uuid4().hex[:12] + ".png")
        try:
            with Image.open(file_path) as image:
                ImageOps.exif_transpose(image).convert("RGB").save(temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return str(target)
    import pypdfium2 as pdfium
    target = folder / "source.png"
    valid = False
    if target.is_file():
        from PIL import Image
        try:
            with Image.open(target) as image:
                image.verify()
            valid = True
        except OSError:
            pass
    if not valid:
        import uuid
        temporary = folder / ("r" + uuid.uuid4().hex[:12] + ".png")
        try:
            with _PDF_LOCK, pdfium.PdfDocument(str(file_path)) as pdf:
                pdf[page_no - 1].render(scale=200 / 72).to_pil().save(temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return str(target)


def prepare_manual_page(source, file_path, page_no, *, token, group_id=None):
    """Render-only preparation: never instantiate OCR, a router or a model."""
    import uuid
    from copy import deepcopy
    from app import config
    from app.core import page_layout, preparation, text_evidence
    folder = config.UPLOAD_DIR / "_pages" / token
    folder.mkdir(parents=True, exist_ok=True)
    text_evidence.pdf_descriptor(source, file_path, page_no, folder)
    origin = {**page_layout.origin(source, {"box":[0,0,1000,1000]}, page_no),
              "source_token":token, "source_group_id":group_id or uuid.uuid4().hex}
    if managed_upload_name(file_path):
        origin["upload_file"] = managed_upload_name(file_path)
    origin["preparation"] = preparation.fresh(pdf=Path(file_path).suffix.lower()==".pdf")
    parent = Voucher(origin={**deepcopy(origin),"record_role":"source_page",
                             "layout":{"auto_split":False,"content_kind":"unknown","regions":[],"issues":[]}},
                     quality={"version":1,"state":"unassessed","content_kind":"unknown","issues":[]})
    child = Voucher(origin=deepcopy(origin), quality={"version":1,"state":"unassessed","content_kind":"unknown","issues":[]})
    for voucher in (parent, child):
        voucher.source_image = str(source)
        voucher.source_file = f"{Path(file_path).name} p{page_no}"
    return [parent, child]


def prepare_page(source, file_path, page_no, *, token=None, group_id=None, confirmed_preparation=None):
    """One immutable page, with stable identities supplied by resumable imports."""
    import uuid
    from app import config
    from app.core import page_layout, invocation_trace, model_invocation, work_progress, image_evidence
    token = token or uuid.uuid4().hex
    group = {"source_group_id": group_id or uuid.uuid4().hex}
    if managed_upload_name(file_path):
        group["upload_file"] = managed_upload_name(file_path)
    folder = config.UPLOAD_DIR / "_pages" / token
    folder.mkdir(parents=True, exist_ok=True)
    with invocation_trace.capture(folder / "trace.json"), ocr_cache.scope(folder / "ocr_cache"):
        invocation_trace.add({"kind": "snapshot", "versions": model_invocation.source_versions(), "settings": settings_store.get_settings()})
        input_path = page_layout.crop(source, {"box": [0, 0, 1000, 1000]}, folder / "input.png", max_edge=1180)
        if confirmed_preparation is not None:
            from app.core import text_evidence
            from PIL import Image
            evidence = text_evidence.read(source, folder=folder, preparation=confirmed_preparation, purpose="layout")
            with Image.open(input_path) as preview:
                lines = image_evidence.map_lines(evidence["lines"], evidence["size"], preview.size)
            layout = page_layout.detect(input_path, native_path=source, ocr_lines=lines, ocr_text=evidence["text"], orientation_confirmed=True)
        else:
            layout = page_layout.detect(input_path, native_path=source)
        work_progress.emit("saving")
        source_origin = {**page_layout.origin(source, {"box": [0, 0, 1000, 1000]}, page_no),
                         "record_role": "source_page", "source_token": token, "layout": layout, **group}
        parent = Voucher.model_validate({"doc_type_key": layout["type"], "origin": source_origin,
            "quality": {"version": 1, "state": "needs_review" if not layout["auto_split"] else "usable", "content_kind": layout["content_kind"], "issues": layout["issues"]}})
        parent.source_file = f"{Path(file_path).name} p{page_no}"
        parent.source_image = source
        out = [parent]
        if layout["auto_split"] and layout["content_kind"] == "document":
            for document_index, region in enumerate(layout["regions"]):
                crop_path = page_layout.crop(source, region, folder / f"document_{document_index}.png")
                child = Voucher.model_validate({"doc_type_key": region["type"], "doc_type": doc_types.get(region["type"])["label"],
                    "origin": {**page_layout.origin(source, region, page_no), "source_token": token, "document_index": document_index,
                               "layout_fallback": layout.get("fallback_reason"), **group},
                    "quality": {"version": 1, "state": "unassessed", "content_kind": layout["content_kind"], "issues": []}})
                child.source_file, child.source_image = parent.source_file, crop_path
                if confirmed_preparation is not None:
                    from app.core import preparation, text_evidence
                    child.origin["preparation"] = preparation.fresh()
                    text_evidence.inherit(source, crop_path, region, folder=folder)
                child.field_evidence["doc_type_key"] = [{"source": "route", "value": region["type"], "reason": region["reason"], "selected": True}]
                if confirmed_preparation is not None:
                    child.field_evidence["doc_type_key"][0].update(text_identity_sha256=ocr_cache.digest(text_evidence.identity(crop_path)),structure_setting=settings_store.get_settings()["structure"])
                out.append(child)
        return out


def _is_blank_page(image_path: str, ink_thresh: float = 0.004) -> bool:
    """判斷是否近乎空白頁（如網頁列印多出的頁尾頁）：暗色像素比例極低即視為空白。"""
    from PIL import Image

    try:
        im = Image.open(image_path).convert("L")
    except OSError:
        return False
    im.thumbnail((400, 400))
    px = list(im.getdata())
    if not px:
        return False
    dark = sum(1 for p in px if p < 200)
    return (dark / len(px)) < ink_thresh


def prepare_image(image_path: str, hint: str = "", auto_rotate: bool = True) -> str:
    """外觀準備：Router 判類型 + 自動轉正（影像就地覆寫轉正）；回類型代碼。
    不做欄位萃取——留待使用者確認外觀後再 extract_for_type。"""
    settings = settings_store.get_settings()
    ocr_provider, _ = settings_store.parse_setting(settings["ocr"], ("paddle", "pp-ocrv5"))
    ocr_text, ocr_rotation = "", 0
    if ocr_provider not in ("disabled", "none"):
        try:
            from app.core import page_ocr
            from PIL import Image
            with Image.open(image_path) as image:
                size = image.size
            with activity.stage("ocr", "原頁分類 OCR"):
                _, ocr_text, ocr_rotation, _ = page_ocr.read(image_path,size,folder=Path(image_path).parent / (Path(image_path).stem + "_page_ocr"),setting=settings["ocr"])
        except Exception as exc:
            logger.warning("分類 OCR 不可用：%s", exc)
    type_key, rotate_hint = doc_router.classify(image_path, hint=hint, ocr_text=ocr_text)
    if auto_rotate:
        if ocr_text:
            if ocr_rotation:
                _apply_rotation(image_path, ocr_rotation)
            return type_key
        ocr = None
        try:
            if ocr_provider not in ("disabled", "none"):
                ocr = model_factory.get_ocr(ocr_provider)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 不可用，改用 VLM 方向提示：%s", exc)
        if ocr is not None:
            with activity.stage("ocr", settings["ocr"] + "（自動轉正）"):
                _, deg = _auto_orient_ocr(image_path, ocr)
            if deg:
                logger.info("OCR 自動轉正 %d 度", deg)
        elif rotate_hint:
            _apply_rotation(image_path, rotate_hint)
    return type_key


@ocr_cache.scoped
def extract_for_type(image_path: str, type_key: str, hint: str = "", company: str = "", *, confirmed_fields=None, native_path=None, confirmed_type=False, confirmed_paths=None, text_record=None, fast_mode=False) -> Voucher:
    """依「已確認的類型」在「已準備好的影像」上萃取欄位＋精準框。
    不再判類型／不再自動轉正（外觀確認階段已完成）。"""
    settings = settings_store.get_settings()
    ocr_provider, _ = settings_store.parse_setting(settings["ocr"], ("paddle", "pp-ocrv5"))
    struct_provider, struct_model = settings_store.parse_setting(settings["structure"])

    ocr = None
    ocr_used = "（無，VLM 直讀）"
    try:
        if ocr_provider not in ("disabled", "none") and text_record is None:
            ocr = model_factory.get_ocr(ocr_provider)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR 不可用，改用 VLM 直讀：%s", exc)

    ocr_lines, ocr_text, image_conditions = [], "", None
    if text_record is not None:
        from app.core import text_evidence, image_evidence
        from PIL import Image
        if not text_evidence.validate(text_record, native_path or image_path):
            raise ValueError("文字證據與目前影像不一致")
        with Image.open(image_path) as image:
            ocr_lines = image_evidence.map_lines(text_record["lines"], text_record["size"], image.size)
        ocr_text = text_record["text"]
        ocr_used = "pdf_text/pdfium" if text_record["source"] == "pdf_text" else "（無，VLM 直讀）" if text_record["source"] == "none" else settings["ocr"]
    elif ocr is not None:
        try:
            with activity.stage("ocr", settings["ocr"]):
                from app.core import image_evidence
                from PIL import Image
                from app.core import ocr_resolution
                native_lines, ocr_input = ocr_resolution.read(native_path or image_path,
                    Path(image_path).with_name(Path(image_path).stem + "_ocr.png"), ocr, purpose="extraction")
                _, image_conditions = image_evidence.enhancement(ocr_input, native_lines, None)
                from app.core import invocation_trace
                invocation_trace.add({"kind":"image_assessment", **image_conditions})
                with Image.open(ocr_input) as original, Image.open(image_path) as target:
                    ocr_lines = image_evidence.map_lines(native_lines, original.size, target.size)
            ocr_text = ocr.plain_text(ocr_lines)
            ocr_used = settings["ocr"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 辨識失敗，改 VLM 直讀：%s", exc)
            ocr_lines = []

    chat = model_factory.get_chat_model(struct_provider, struct_model, temperature=0.0)
    voucher = extract_evidence(image_path, type_key, chat, ocr_lines, ocr_text, hint=hint, company=company,
                               confirmed_fields=confirmed_fields, native_path=native_path, image_conditions=image_conditions,
                               confirmed_type=confirmed_type, confirmed_paths=confirmed_paths, fast_mode=fast_mode)
    voucher.ocr_provider = ocr_used
    voucher.structure_model = settings["structure"]
    voucher.source_image = image_path
    if ocr is not None and not ocr_lines:
        voucher.validation_warnings.append("OCR 未取得文字，使用圖像擷取")
        voucher.ocr_provider = "（無，VLM 直讀）"
    return voucher


def _company_context(company: str) -> dict:
    if not company:
        return {}
    from app.services import taxid
    matches = [c for c in taxid.load_companies() if company in (c.get("name"), c.get("short"))]
    return {"company": matches[0]} if len(matches) == 1 else {"company_name": company}


def _record_evidence(data: dict, source: str, mode: str = "image") -> None:
    evidence = data.setdefault("field_evidence", {})
    values = {"fields." + k: v for k, v in (data.get("fields") or {}).items()}
    for group in ("items", "entries"):
        for index, row in enumerate(data.get(group) or []):
            values.update({f"{group}[{index}].{k}": v for k, v in row.items()})
    for path, value in values.items():
        if value is not None and value != "":
            evidence.setdefault(path, []).append({"source": source, "value": value, "input_mode": mode, "selected": True})


def _merge_evidence_fields(data: dict, values: dict, source: str, preserve_existing: bool = False) -> None:
    evidence = data.setdefault("field_evidence", {})
    warnings = data.setdefault("validation_warnings", [])
    fields = data.setdefault("fields", {})
    for key, value in values.items():
        if source != "manual" and value in (None, "") or preserve_existing and fields.get(key) not in (None, ""):
            continue
        old = fields.get(key)
        if old not in (None, "") and _norm_amount(old) != _norm_amount(value) and _norm_roc_date(str(old)) != _norm_roc_date(str(value)):
            warnings.append(f"{key} 證據衝突，採用 {source}，原讀值保留於證據")
        path = "fields." + key
        for record in evidence.get(path, []):
            record["selected"] = False
        evidence.setdefault(path, []).append({"source": source, "value": value, "selected": True})
        fields[key] = value


def reread_fields(image_path: str, type_key: str, chat, fields: list[str], *, mode="image",
                  ocr_text="", hint="", evidence=None) -> dict:
    """A subset of the same extraction task, without side effects on other fields."""
    from app.core import prompts, model_invocation
    from app.core import model_coordinates, request_budget
    coordinates = model_coordinates.for_image(image_path, settings_store.get_settings()["structure"], max_edge=2400, deferred=True) if mode == "image" else None
    content = [{"type": "text", "text": prompts.build_extract_prompt(type_key, mode=mode, ocr_text=ocr_text,
                                       hint=hint, fields=fields, evidence=evidence, coordinate_context=coordinates, image_path=image_path)}]
    if mode == "image":
        content.append(request_budget.image_part(coordinates))
    with activity.stage("structure", "指定欄位補讀"):
        result = model_invocation.invoke_structured(chat, content,
                    lambda obj: _validate_image_extraction(obj, type_key, mode, image_path, fields, coordinate_context=coordinates), task="supplement", invoke=_invoke_text,
                    schema=prompts.extraction_schema(type_key, mode, fields, coordinate_context=coordinates), diagnostics=(evidence or {}).get("recognition_diagnostics"))
    return result["fields"]


def reread_document(image_path, type_key, chat, *, mode="image", ocr_text="", hint="", evidence=None):
    """Full contract reread; deliberately does not invoke extraction/supplement again."""
    from app.core import prompts, model_invocation, model_coordinates, request_budget
    coordinates = model_coordinates.for_image(image_path, settings_store.get_settings()["structure"], max_edge=2400, deferred=True) if mode == "image" else None
    content = [{"type":"text", "text":prompts.build_extract_prompt(type_key,mode=mode,ocr_text=ocr_text,hint=hint,evidence=evidence,coordinate_context=coordinates,image_path=image_path)}]
    if mode == "image":
        content.append(request_budget.image_part(coordinates))
    with activity.stage("structure","欄位與表格補讀"):
        return model_invocation.invoke_structured(chat,content,lambda obj:_validate_image_extraction(obj,type_key,mode,image_path,coordinate_context=coordinates),
                    task="supplement",invoke=_invoke_text,schema=prompts.extraction_schema(type_key,mode,coordinate_context=coordinates),diagnostics=(evidence or {}).get("recognition_diagnostics"))


def _validate_image_extraction(obj: dict, type_key: str, mode: str, image_path: str, fields=None, *, coordinate_context=None) -> dict:
    from app.core import prompts
    return prompts.validate_content_and_boxes(obj, type_key, mode, fields, coordinate_context=coordinate_context)


def extract_evidence(image_path: str, type_key: str, chat, ocr_lines: list, ocr_text: str,
                     *, mode="image", hint="", company="", confirmed_fields=None, native_path=None, image_conditions=None, confirmed_type=False, confirmed_paths=None, fast_mode=False) -> Voucher:
    from app.core import barcode, prompts, model_invocation, model_coordinates, classification_evidence, request_budget
    from copy import deepcopy
    type_key, classification = classification_evidence.resolve(type_key,ocr_text,protected=confirmed_type or bool(confirmed_fields) or bool(confirmed_paths))
    model_invocation.invocation_trace.add({"kind":"classification_evidence",**classification})
    coordinates = model_coordinates.for_image(image_path,settings_store.get_settings()["structure"],deferred=True) if mode == "image" else None
    context = _company_context(company)
    diagnostics = context["recognition_diagnostics"] = {"version":1,"requests":[],"issues":[]}
    model_invocation.invocation_trace.add({"kind": "ocr", "lines": ocr_lines, "text": ocr_text})
    context["ocr_enabled"] = settings_store.parse_setting(settings_store.get_settings()["ocr"])[0] not in ("disabled", "none")
    context["image_conditions"] = image_conditions or {}
    context["confirmed_type"] = confirmed_type or bool(confirmed_fields) or bool(confirmed_paths)
    context["confirmed_paths"] = list(confirmed_paths or [])
    context["ocr_lines"] = ocr_lines
    context["fast_mode"] = fast_mode
    if ocr_lines:
        from PIL import Image
        with Image.open(image_path) as source_image:
            context["ocr_size"] = list(source_image.size)
    qr_info = barcode.decode_diagnostics(native_path or image_path) if mode == "image" and type_key == "invoice" else None
    candidates = qr_info["candidates"] if qr_info else []
    if qr_info:
        model_invocation.invocation_trace.add({"kind": "qr_diagnostic", **qr_info})
    if len(candidates) == 1:
        context["qr"] = candidates[0]
    content = [{"type": "text", "text": prompts.build_extract_prompt(type_key, mode=mode, ocr_text=ocr_text, hint=hint, evidence=context,coordinate_context=coordinates,image_path=image_path)}]
    if mode == "image":
        content.append(request_budget.image_part(coordinates))
    with activity.stage("structure", "欄位萃取"):
        data = model_invocation.invoke_structured(chat, content,
                lambda obj: _validate_image_extraction(obj, type_key, mode, image_path,coordinate_context=coordinates), task="extract", invoke=_invoke_text,
                schema=prompts.extraction_schema(type_key, mode,coordinate_context=coordinates),diagnostics=diagnostics)
    initial_ai = deepcopy(data)
    _record_evidence(data, "model", mode)
    data["validation_warnings"] = []
    if (image_conditions or {}).get("blur_warning"):
        data["validation_warnings"].append("文字區域可能模糊，請覆核原圖；未進行生成式修復")
    if classification["conflict"] and classification["protected"]:
        data["validation_warnings"].append("人工確認類型與文件證據衝突，保留人工類型待覆核")
    if len(candidates) == 1:
        _merge_evidence_fields(data, candidates[0], "qr")
    elif len(candidates) > 1:
        data["validation_warnings"].append("多個有效 QR 內容衝突，保留圖像擷取值，請人工確認")
        for candidate in candidates:
            for key, value in candidate.items():
                data["field_evidence"].setdefault("fields." + key, []).append({"source": "qr", "value": value, "selected": False})
    if confirmed_fields:
        confirmed_fields = prompts.validate_extraction({"fields": confirmed_fields}, type_key, mode, list(confirmed_fields))["fields"]
        _merge_evidence_fields(data, confirmed_fields, "manual")
    from app.core import supplement, date_evidence
    evidence_text = ocr_text + "\n" + supplement.spatial_text(ocr_lines)
    model_invocation.invocation_trace.add({"kind":"ocr_semantic_rows", "text":evidence_text})
    date_evidence.record(data, evidence_text, type_key)  # evidence only; the model value is not rewritten
    supplement.normalize_candidates(data, type_key, evidence_text, context.get("qr"), confirmed_fields, confirmed_paths)
    normalized = _build_voucher(deepcopy(data), type_key, normalize_word_total=False)
    data["fields"] = normalized.fields
    data["validation_warnings"] = list(dict.fromkeys(data["validation_warnings"] + normalized.validation_warnings))
    data["recognition_diagnostics"] = diagnostics
    from app.core import recognition_checkpoint
    normalized.initial_ai = initial_ai
    normalized.field_evidence = data.get("field_evidence", {})
    normalized.coordinate_context = coordinates
    normalized.recognition_diagnostics = diagnostics
    checkpoint = recognition_checkpoint.save(image_path, native_path or image_path, normalized, initial_ai,
                                              confirmed_fields, confirmed_paths)
    context["checkpoint_path"] = str(checkpoint)
    first_read = deepcopy(data)
    try:
        supplement.apply(data, type_key, chat, image_path, mode=mode, ocr_text=evidence_text,
                         context=context, confirmed_fields=confirmed_fields, native_path=native_path)
    except Exception as exc:
        data = first_read
        supplement.failed(data, context, exc)
    recognition_checkpoint.complete(checkpoint)
    data["recognition_diagnostics"] = diagnostics
    type_key = context.get("effective_type", type_key)
    if doc_types.get(type_key).get("has_entries") and ocr_lines and not any(p == "entries" or p.startswith("entries[") for p in (confirmed_paths or [])):
        from copy import deepcopy
        before = deepcopy(data.get("entries") or [])
        _fix_entry_sides_with_ocr(data, ocr_lines)
        for index, row in enumerate(data.get("entries") or []):
            for key in ("debit", "credit"):
                if before[index].get(key) != row.get(key):
                    path = f"entries[{index}].{key}"
                    for record in data["field_evidence"].get(path, []):
                        record["selected"] = False
                    data["field_evidence"].setdefault(path, []).append({"source": "ocr", "value": row[key], "selected": True})
        _filter_entry_rows_with_ocr(data, ocr_lines)
    vlm_boxes = _normalize_boxes(data.get("boxes"), image_path) if mode == "image" else {}
    dense = type_key in doc_types.NO_VLM_BOX_TYPES
    data["boxes"] = _refine_boxes_with_ocr(data["fields"], vlm_boxes, ocr_lines, image_path,
                        keep_unmatched=mode == "image" and not dense) if ocr_lines else ({} if dense else vlm_boxes)
    voucher = _build_voucher(data, type_key)
    voucher.initial_ai = initial_ai
    voucher.recognition_diagnostics = diagnostics
    if data.get("previous_contract_candidate"):
        voucher.previous_contract_candidate = data["previous_contract_candidate"]
    voucher.coordinate_context = coordinates
    voucher.classification_evidence = context.get("classification_evidence", classification)
    if type_key == "other":
        voucher.validation_warnings.append("文件類型未知或無法分類，使用通用契約，請人工確認")
    from app.core.quality import evaluate_quality
    voucher.quality = evaluate_quality(voucher.model_dump())
    return voucher


def analyze(file_path: str) -> Voucher:
    """單張（相容既有呼叫）：回第一頁。"""
    return analyze_pages(file_path)[0]


def recognize_image(image_path: str, hint: str = "", *, company: str = "") -> Voucher:
    """對既有單張影像重新辨識（供手動旋轉後重辨識；不再自動旋轉）。
    hint：人工修正提示（來自備註欄），會餵給 Router 與辨識提示詞作為修正指引。"""
    v = _structure_image(image_path, image_path, 0, 1, auto_rotate=False, hint=hint, company=company)
    v.source_image = ""   # 影像已在原位，不需再複製
    return v


@ocr_cache.scoped
def _structure_image(image_path: str, source_file: str, page_idx: int, page_count: int,
                     auto_rotate: bool = True, hint: str = "", company: str = "") -> Voucher:
    """單張全流程：準備（判類型+轉正）＋內容萃取。供批次與重新辨識沿用。"""
    type_key = prepare_image(image_path, hint=hint, auto_rotate=auto_rotate)
    voucher = extract_for_type(image_path, type_key, hint=hint, company=company)
    voucher.field_evidence["doc_type_key"] = [{"source": "route", "value": voucher.doc_type_key,
                "reason": doc_router.last_result().get("reason", ""), "selected": True}]
    name = Path(source_file).name
    voucher.source_file = f"{name} p{page_idx + 1}" if page_count > 1 else name
    return voucher


def _norm_roc_date(s: str) -> str:
    from app.core.normalization import norm_date
    return norm_date(s)


def _clean_taxid(v):
    """統一編號防呆：8 位純數字才算統編；含電話符號或 9~10 位數（電話）則清空。
    非數字或其他長度（可能為國外稅籍）則原樣保留。"""
    import re

    import unicodedata
    s = unicodedata.normalize("NFKC", str(v or "").strip())
    if not s:
        return s
    if any(c in s for c in "-()（）"):      # 電話樣式
        return ""
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        return digits
    if len(digits) in (9, 10):             # 電話號碼位數
        return ""
    return s


@lru_cache(maxsize=1)
def _s2t():
    """簡→繁轉換器（OpenCC s2t：純字元轉換，不做詞彙轉換）。

    刻意不用 s2twp／s2tw：那會把「智能」改成「智慧」、把大陸買方公司登記名稱一起改掉，
    等於竄改單據上的專有名詞。未安裝 opencc 時回 None，僅靠提示詞要求繁體。
    """
    try:
        import opencc

        return opencc.OpenCC("s2t")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenCC 未安裝，簡轉繁略過：%s", exc)
        return None


def to_traditional(s: str) -> str:
    """把 VLM 偶爾吐出的簡體字轉回繁體。

    台/臺 例外：OpenCC 視「台」為「臺」的簡化字，會把台灣公司登記名稱
    （如「台灣順豐速運股份有限公司」）改成「臺灣…」。原文寫「台」就保留「台」。
    """
    cc = _s2t()
    if not cc or not isinstance(s, str) or not s:
        return s
    out = cc.convert(s)
    if len(out) == len(s):          # 長度一致才能逐字還原例外字
        out = "".join(o if c != "台" else "台" for c, o in zip(s, out))
    return out


def _norm_amount(v):
    from app.core.normalization import norm_amount
    return norm_amount(v)


def _clean_fields(f: dict) -> dict:
    """清洗 fields 內層值：
    - VLM 偶把欄位值吐成框陣列（如 [501,268,743,291]）→ 疑似框直接捨棄；其餘陣列攤平。
    - 統編欄位（*tax_id）套統編防呆，避免把電話當統編。
    - 中文一律轉繁體（VLM 偶爾把報單印章、人名吐成簡體）。"""
    import re

    out = {}
    for k, v in (f or {}).items():
        if isinstance(v, (list, tuple)):
            nums = []
            for x in v:
                try:
                    nums.append(float(x))
                except (TypeError, ValueError):
                    nums = None
                    break
            if nums is not None and len(nums) == 4 and all(0 <= n <= 1000 for n in nums):
                out[k] = ""          # 疑似 bounding box，非欄位值
                continue
            scal = [str(x).strip() for x in v if x not in (None, "", [])]
            out[k] = scal[0] if len(scal) == 1 else " ".join(scal)
        else:
            out[k] = v
        # 字串型的框（VLM 把座標塞進欄位，如 "[28,46,63,61]"）→ 捨棄
        sv = out.get(k)
        if isinstance(sv, str) and re.match(r"^\s*[\[\(]?\s*\d{1,4}(\s*,\s*\d{1,4}){3}\s*[\]\)]?\s*$", sv):
            nums = [int(x) for x in re.findall(r"\d+", sv)]
            if all(0 <= n <= 1000 for n in nums):
                out[k] = ""
        if "tax_id" in k and isinstance(out.get(k), str):
            out[k] = _clean_taxid(out[k])
        elif isinstance(out.get(k), str):
            out[k] = to_traditional(out[k])
    return out


def _fix_entry_sides_with_ocr(data: dict, ocr_lines: list) -> int:
    """用 OCR 座標校正傳票分錄的借貸方向，回傳更正筆數。

    VLM 判斷「這個數字印在借方欄還是貸方欄」並不可靠（實測把借方的進項稅額
    120 判成貸方，導致借貸不平衡），而這正是 OCR 擅長的：傳票的「借方金額」與
    「貸方金額」是兩個固定欄位，用表頭的 x 座標就能決定分界線。

    只在 OCR 能**明確**定位該金額時才更正：同一個數字若在多處出現且分屬兩邊
    （如金額同時等於合計），視為無法判定，保留模型原判。
    """
    import re

    rows = [r for r in (data.get("entries") or []) if isinstance(r, dict)]
    if not rows or not ocr_lines:
        return 0

    def xmid(line):
        xs = [p[0] for p in line.get("box") or []]
        return sum(xs) / len(xs) if xs else None

    hd = hc = None
    for l in ocr_lines:
        t = str(l.get("text", "")).replace(" ", "")
        if hd is None and "借方" in t:
            hd = xmid(l)
        if hc is None and "貸方" in t:
            hc = xmid(l)
    if hd is None or hc is None or hd >= hc:
        return 0
    mid = (hd + hc) / 2

    def ymid(line):
        ys = [p[1] for p in line.get("box") or []]
        return sum(ys) / len(ys) if ys else None

    # OCR 行裡所有可解析為金額的數字 → {值: [(x 中心, y 中心), ...]}
    seen: dict[float, list] = {}
    for l in ocr_lines:
        s = str(l.get("text", "")).strip()
        if not re.fullmatch(r"[\d,]+(?:\.\d+)?", s):
            continue
        try:
            v = float(s.replace(",", ""))
        except ValueError:
            continue
        x, y = xmid(l), ymid(l)
        if x is not None:
            seen.setdefault(v, []).append((x, y))

    # 科目代號 → y 座標（供同列定位）。借貸金額相同時（如勞退兩邊都是 6,468），
    # 數字在兩側都出現，只看 x 無法判定；改以「與該列科目代號同一橫列」消歧義。
    code_y: dict[str, list] = {}
    for l in ocr_lines:
        s = str(l.get("text", "")).strip()
        if re.fullmatch(r"\d{4,5}", s):
            y = ymid(l)
            if y is not None:
                code_y.setdefault(s, []).append(y)

    fixed = 0
    for r in rows:
        d = _norm_amount(r.get("debit")) or 0
        c = _norm_amount(r.get("credit")) or 0
        value = d or c or 0
        if not isinstance(value, (int, float)):
            continue
        amt = float(value)
        if not amt or amt not in seen:
            continue
        cands = seen[amt]
        code = str(r.get("account_code", "")).strip()
        # 先試「與本列科目代號同一橫列」的金額
        rows_y = code_y.get(code) or []
        if rows_y:
            near = [(x, y) for (x, y) in cands
                    if y is not None and any(abs(y - cy) < 26 for cy in rows_y)]
            if len(near) == 1:
                cands = near
        sides = {"debit" if x < mid else "credit" for x, _ in cands}
        if len(sides) != 1:          # 仍無法判定（多半撞上合計列）→ 保留模型原判
            warning = "原傳票金額位置無法唯一判定，請對照原圖核對明細"
            if warning not in data.setdefault("validation_warnings", []):
                data["validation_warnings"].append(warning)
            continue
        side = sides.pop()
        if side == "debit" and (not d or c == amt):
            r["debit"], r["credit"] = amt, None
            r["source_direction"] = side
            fixed += 1
        elif side == "credit" and (not c or d == amt):
            r["debit"], r["credit"] = None, amt
            r["source_direction"] = side
            fixed += 1
        else:
            r["source_direction"] = side
        if len(cands) == 1:
            r["source_row_y"] = cands[0][1]
    return fixed


def _filter_entry_rows_with_ocr(data, ocr_lines):
    """Discard only empty rows or repeated/footer rows anchored to OCR geometry."""
    import re
    from copy import deepcopy
    def center(line):
        box = line.get("box") or []
        return sum(p[1] for p in box) / len(box) if box else None
    headers = [center(line) for line in ocr_lines if any(word in str(line.get("text", "")) for word in ("借方", "貸方"))]
    headers = [y for y in headers if y is not None]
    footer = [center(line) for line in ocr_lines if re.sub(r"\s+", "", str(line.get("text", ""))) in ("合計", "總計", "本頁合計")]
    footer = [y for y in footer if y is not None and headers and y > max(headers)]
    keep, seen, removed = [], set(), []
    for index, row in enumerate(data.get("entries") or []):
        if not isinstance(row, dict):
            continue
        empty = all(row.get(key) in (None, "") for key in ("account_code", "account_name", "description", "debit", "credit"))
        y = row.get("source_row_y")
        is_footer = y is not None and any(abs(y - footer_y) <= 6 for footer_y in footer)
        marker = re.sub(r"[\s/／]+", "", str(row.get("description") or ""))
        is_blank_marker = marker in ("以下空白", "本頁以下空白", "以下無資料", "以下無明細") and any(
            marker in re.sub(r"[\s/／]+", "", str(line.get("text") or "")) for line in ocr_lines)
        physical = (y, row.get("source_direction"), row.get("debit"), row.get("credit")) if y is not None else None
        duplicate = physical is not None and physical in seen
        if empty or is_footer or is_blank_marker or duplicate:
            removed.append(index)
            continue
        if physical is not None:
            seen.add(physical)
        keep.append((index, row))
    if not removed:
        return 0
    evidence = data.get("field_evidence") or {}
    updated = {path: records for path, records in evidence.items() if not path.startswith("entries[")}
    for new_index, (old_index, row) in enumerate(keep):
        for path, records in evidence.items():
            prefix = f"entries[{old_index}]."
            if path.startswith(prefix):
                updated[f"entries[{new_index}]." + path[len(prefix):]] = deepcopy(records)
    data["entries"] = [row for _, row in keep]
    data["field_evidence"] = updated
    data.setdefault("validation_warnings", []).append("已依原表格位置排除合計、空白或重複擷取列，請核對明細")
    return len(removed)


def _entries_from_voucher_form(data: dict, spec: dict) -> dict:
    """Convert observed voucher rows, without inventing a balancing side."""
    if not spec.get("has_entries"):
        return {}
    entries, dr, cr = [], 0.0, 0.0
    incomplete = False
    for index, row in enumerate(data.get("entries") or []):
        if not isinstance(row, dict):
            continue
        d, c = _norm_amount(row.get("debit")), _norm_amount(row.get("credit"))
        d = d if isinstance(d, (int, float)) and not isinstance(d, bool) else None
        c = c if isinstance(c, (int, float)) and not isinstance(c, bool) else None
        code, name = str(row.get("account_code") or ""), to_traditional(row.get("account_name") or "")
        base = {"description": to_traditional(row.get("description") or ""), "dept": row.get("dept") or "",
                "offset_code": row.get("offset_code") or ""}
        sides = [("debit", d), ("credit", c)]
        # Legacy outputs used zero for the unused side; prefer an actual nonzero side.
        if any(value not in (None, 0) for _, value in sides):
            sides = [(side, value) for side, value in sides if value not in (None, 0)]
        else:
            sides = [(side, value) for side, value in sides if value is not None]
        if not sides:
            if not any(row.get(key) for key in ("account_code", "account_name", "description")):
                continue
            incomplete = True
            entries.append({**base, "amount": None, "raw_account_code": code, "raw_account_name": name,
                            "evidence_paths": [f"entries[{index}]"]})
            continue
        for side, value in sides:
            entry = {**base, "amount": value, side + "_account": code, side + "_account_name": name,
                     "source_direction": side,
                     "evidence_paths": [f"entries[{index}].{side}"]}
            entries.append(entry)
            if side == "debit": dr += value
            else: cr += value
    kind = str((data.get("fields") or {}).get("voucher_kind") or "")
    one_sided = any(k in kind for k in ("現支", "收入", "支出")) and bool(dr) != bool(cr)
    return {"entries": entries, "debit_total": dr, "credit_total": cr,
            "balance_check": bool(entries) and not incomplete and abs(dr-cr) < .01,
            "classification_meta": {**(data.get("classification_meta") or {}), "original_checks": {
                "complete": bool(entries) and not incomplete, "single_sided": one_sided,
                "observed_debit_total": dr, "observed_credit_total": cr}}}


def _build_voucher(data: dict, type_key: str, *, derive_entries: bool = True, normalize_word_total: bool = True) -> Voucher:
    """Normalize raw fields and map aliases from the document contract."""
    from app.core.normalization import first_present
    import math
    raw_fields = data.get("fields") or {}
    f = _clean_fields(raw_fields)
    spec = doc_types.get(type_key)
    # Refresh checks of current values after edits; keep acquisition/conflict diagnostics.
    generated_prefixes = ("金額兜不攏：", "營業稅額 ", "日期格式無法解析：", "日期在未來（", "日期距今超過 5 年（",
                          "日期與 OCR 原文不一致：", "日期與發票期別不一致：")
    warnings = [w for w in data.get("validation_warnings") or []
                if not w.startswith(generated_prefixes) and "金額無法解析" not in w
                and w not in ("seller 統編檢查碼錯誤", "buyer 統編檢查碼錯誤",
                              "舊收據稅額語意未確認，請區分營業稅與扣繳稅額",
                              "中文大寫與數字金額衝突，保留數字欄與大寫原文供人工核對")]
    evidence = data.get("field_evidence") or {}
    manual_fields = {key: raw_fields.get(key) for key in raw_fields if any(
        record.get("source") == "manual" and record.get("selected") for record in evidence.get("fields." + key, []))}
    f.update(manual_fields)
    if doc_types.receipt_tax_ambiguous(type_key, raw_fields, evidence):
        path = "fields.tax_amount"
        if not any(r.get("selected") and r.get("semantic_role") == "unconfirmed_tax" for r in evidence.get(path, [])):
            for record in evidence.get(path, []):
                record["selected"] = False
            evidence.setdefault(path, []).append({"source": "legacy", "value": raw_fields["tax_amount"],
                                        "semantic_role": "unconfirmed_tax", "selected": True})

    def selected_raw(path):
        records = [r for r in evidence.get(path, []) if r.get("selected")]
        return records[-1].get("value") if records else None

    def invalid_number(value):
        number = _norm_amount(value)
        return isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number)

    for field in spec["fields"]:
        key, kind = field["key"], field["kind"]
        if key in manual_fields:
            continue
        value = f.get(key)
        if value in (None, ""):
            f[key] = None
            raw = selected_raw("fields." + key)
            if kind == "number" and raw not in (None, "") and invalid_number(raw):
                warnings.append(f"{key} 金額無法解析，原文保留於證據")
            continue
        evidence.setdefault("fields." + key, [{"source": "model", "value": raw_fields.get(key), "selected": True}])
        if kind == "date":
            f[key] = _norm_roc_date(value)
        elif kind == "number":
            number = _norm_amount(value)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                f[key] = None
                warnings.append(f"{key} 金額無法解析，原文保留於證據")
            else:
                f[key] = number
    if type_key in ("export_declaration", "import_declaration"):
        f["direction"] = "出口" if type_key == "export_declaration" else "進口"
    if type_key == "import_declaration":
        f["buyer"] = f.get("taxpayer")
        f["buyer_tax_id"] = f.get("taxpayer_tax_id")
    # A generic declaration's direction must be explicit, never inferred from notes.
    if type_key == "declaration" and f.get("direction") not in ("進口", "出口"):
        f["direction"] = None
    words = _norm_amount(f.get("amount_words"))
    total = f.get("total_amount")
    if isinstance(words, (int, float)):
        if total is not None and total != words:
            warnings.append("中文大寫與數字金額衝突，保留數字欄與大寫原文供人工核對")
        elif total is None and normalize_word_total and not any(e.get("source") == "manual" and e.get("selected") for e in evidence.get("fields.total_amount", [])):
            f["total_amount"] = words
            evidence.setdefault("fields.total_amount", []).append({"source": "normalization", "value": words,
                                           "source_path": "fields.amount_words", "selected": True})
    for index, item in enumerate(data.get("items") or []):
        for field in spec["item_fields"]:
            key = field["key"]
            if any(r.get("source") == "manual" and r.get("selected") for r in evidence.get(f"items[{index}].{key}", [])):
                continue
            if field["kind"] == "number" and item.get(key) in (None, ""):
                raw = selected_raw(f"items[{index}].{key}")
                if raw not in (None, "") and invalid_number(raw):
                    warnings.append(f"items[{index}].{key} 金額無法解析")
            if field["kind"] == "number" and item.get(key) not in (None, ""):
                number = _norm_amount(item[key])
                if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                    warnings.append(f"items[{index}].{key} 金額無法解析")
                    item[key] = None
                else:
                    item[key] = number
    common = {key: first_present(f, aliases) for key, aliases in spec["common_fields"].items()}
    if common.get("date"):
        common["date"] = _norm_roc_date(common["date"])
    for key in ("subtotal", "tax_amount", "total_amount"):
        if key in common and common[key] not in (None, ""):
            candidate = _norm_amount(common[key])
            common[key] = None if invalid_number(candidate) else candidate
    if doc_types.receipt_tax_ambiguous(type_key, f, evidence):
        common["tax_amount"] = None
    payload = {
        "doc_type_key": type_key, "doc_type": spec["label"], "fields": f,
        "note": to_traditional(data.get("note") or ""), "confidence": data.get("confidence"),
        "boxes": data.get("boxes") or {}, "items": data.get("items") or [],
        "details_version": data.get("details_version") or 0,
        "box_issues": data.get("box_issues") or [], "origin": data.get("origin") or {},
        "field_evidence": evidence, "validation_warnings": list(dict.fromkeys(warnings)),
        "classification_meta": data.get("classification_meta") or {},
        "recognition_diagnostics": data.get("recognition_diagnostics") or {},
        **(_entries_from_voucher_form(data, spec) if derive_entries else {}),
        "currency": "TWD" if type_key in ("import_declaration", "export_declaration") and common["total_amount"] is not None else f.get("currency") or "TWD", "voucher_id": common["voucher_id"] or "",
        "date": common["date"] or "", "subtotal": common["subtotal"],
        "tax_amount": common["tax_amount"], "total_amount": common["total_amount"],
        "seller": {"name": common["seller_name"] or "", "tax_id": common["seller_tax_id"] or ""},
        "buyer": {"name": common["buyer_name"] or "", "tax_id": common["buyer_tax_id"] or ""},
    }
    from app.core.normalization import normalize_payment
    payload["payment_state"] = normalize_payment(f.get("payment_status"), f.get("payment_method"))
    total_contract = spec["common_money"].get("total_amount")
    if common["total_amount"] is not None and total_contract and total_contract["currency"] != "$document":
        payload["currency"] = total_contract["currency"]
    from app.services import taxid
    checks = taxid.check_voucher(payload)
    amount_check = taxid.check_amounts(payload)
    if amount_check.get("ok") is False:
        payload["validation_warnings"].append(amount_check["warn"])
    date_check = taxid.check_date(payload["date"], evidence)
    if date_check.get("ok") is False:
        payload["validation_warnings"].extend(date_check.get("warnings") or [date_check["warn"]])
    for side, check in checks.items():
        if check.get("checksum_valid") is False:
            payload["validation_warnings"].append(side + " 統編檢查碼錯誤")
    if spec.get("has_entries") and derive_entries:
        dr, cr = payload["debit_total"], payload["credit_total"]
        printed_checks = []
        for key, computed in (("debit_total", dr), ("credit_total", cr)):
            printed = f.get(key)
            if printed is not None:
                printed_checks.append(abs(printed - computed) < .01)
            if printed is not None and abs(printed - computed) >= .01:
                payload["validation_warnings"].append(f"{key} 印刷合計與逐列加總不一致")
        original = payload["classification_meta"]["original_checks"]
        original["printed_totals_match"] = all(printed_checks) if printed_checks else None
        if dr != cr and not original["single_sided"]:
            payload["validation_warnings"].append("原始傳票借貸不平衡，未補造分錄")
    payload["validation_warnings"] = list(dict.fromkeys(payload["validation_warnings"]))
    from app.core.quality import evaluate_quality
    payload["quality"] = evaluate_quality(payload)
    return Voucher.model_validate(payload)


def canonicalize_voucher(data: dict) -> dict:
    """One mapping path for form saves; retain stored metadata/unknown attributes."""
    if not data.get("doc_type_key") or not data.get("fields"):
        from app.core.quality import evaluate_quality
        return {**data, "quality": evaluate_quality(data)}
    normalized = _build_voucher(data, data["doc_type_key"], derive_entries=False, normalize_word_total=False).model_dump()
    mapped = {key: normalized[key] for key in ("fields", "voucher_id", "date", "subtotal", "tax_amount", "total_amount", "currency", "seller", "buyer", "items", "validation_warnings", "field_evidence", "payment_state", "quality")}
    return {**data, **mapped}


def _auto_orient_ocr(image_path: str, ocr) -> tuple[str, int]:
    """以 OCR 信心度在 0/90/180/270 四方向擇優（文字最多且最清晰＝正立），
    對原圖實體旋轉並覆寫，回 (image_path, 旋轉角度)。"""
    from PIL import Image

    try:
        base = Image.open(image_path)
    except OSError:
        return image_path, 0
    small = base.copy()
    base.close()
    small.thumbnail((720, 720))
    import uuid
    tmp = str(Path(image_path).with_name("_orient_" + uuid.uuid4().hex + ".png"))
    tp = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}

    def _score(img):
        try:
            img.save(tmp)
            with ocr_cache.purpose("orientation"):
                lines = ocr.recognize(tmp)
            return sum(len(l["text"]) * max(l.get("score", 0), 0.1) for l in lines)
        except Exception:  # noqa: BLE001
            return 0.0

    s0 = _score(small)
    best_deg, best_score = 0, s0
    # 0° 已足夠清晰（文字多且信心高）→ 視為正立，省下其他三方向
    if s0 < 40:
        for deg in (90, 180, 270):
            sc = _score(small.transpose(tp[deg]))
            if sc > best_score:
                best_score, best_deg = sc, deg
    try:
        os.remove(tmp)
    except OSError:
        pass
    if best_deg:
        _apply_rotation(image_path, best_deg)
    return image_path, best_deg


def _match_ocr_line(s: str, ocr_lines: list):
    """把欄位值對上一條 OCR 行，回傳該行（找不到回 None）。

    只比對「整串值落在單一 OCR 行內」太嚴格：報單的 exporter 是中文名＋英文名＋
    地址三行、unit_price 的「USD 3,100」幣別與金額分行、date 已正規化為西元但
    單據印的是民國 115/06/04——這些全部配不上，欄位就沒有框可點。

    故加兩層退階，但維持密集表單「寧缺勿錯」：退階候選必須**唯一命中**才採用，
    命中多行代表不夠獨特（如 USD 出現十幾次），寧可不給框。
    """
    import re

    def norm(t):
        return str(t).replace(" ", "").replace(",", "")

    lines_n = [(l, norm(l.get("text", ""))) for l in ocr_lines]

    # ① 整串值（原樣或去空白逗號）
    sn = norm(s)
    for l, ln in lines_n:
        if s in str(l.get("text", "")) or (len(sn) >= 3 and sn in ln):
            return l

    # ② 民國日期：值已轉西元，單據上仍是民國
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    cands = []
    if m:
        y, mo, d = int(m.group(1)) - 1911, m.group(2), m.group(3)
        cands += [f"{y}/{mo}/{d}", f"{y}.{mo}.{d}", f"{y}{mo}{d}"]

    # ③ 多行值：改用值裡最長、最獨特的片段定位（長的先試，避免用 USD 這種常見詞）
    cands += sorted({t for t in re.split(r"[\s，、；;]+", s) if len(t) >= 4},
                    key=len, reverse=True)[:3]

    for cand in cands:
        cn = norm(cand)
        if len(cn) < 3:
            continue
        found = [l for l, ln in lines_n if cn in ln]
        if len(found) == 1:          # 唯一命中才算數
            return found[0]
    return None


def _refine_boxes_with_ocr(fields: dict, vlm_boxes: dict, ocr_lines: list, image_path: str,
                           keep_unmatched: bool = True) -> dict:
    """用 OCR 精確行框修正欄位框：欄位值文字對上某 OCR 行，就採用該行的精確框。
    keep_unmatched=False 時，對不上 OCR 的欄位不保留 VLM 框（密集表單寧缺勿錯）。"""
    from PIL import Image

    try:
        with Image.open(image_path) as source:
            w, h = source.size
    except OSError:
        return vlm_boxes
    if not w or not h:
        return vlm_boxes
    refined = dict(vlm_boxes or {}) if keep_unmatched else {}
    for key, val in (fields or {}).items():
        s = str(val).strip()
        if len(s) < 2:
            continue
        hit = _match_ocr_line(s, ocr_lines)
        box = hit.get("box") if hit else None
        if not box:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        from app.core.geometry import convert_box
        try:
            refined[key] = convert_box([min(xs), min(ys), max(xs), max(ys)], unit="pixels", width=w, height=h)
        except ValueError:
            continue
    return refined


def _apply_rotation(image_path: str, degrees_cw: int) -> str:
    """順時針旋轉 90/180/270 度並覆寫存檔（無損 transpose）。"""
    from PIL import Image

    tp = {90: Image.Transpose.ROTATE_270,   # PIL transpose 為逆時針，90°CW = ROTATE_270
          180: Image.Transpose.ROTATE_180,
          270: Image.Transpose.ROTATE_90}.get(degrees_cw)
    if tp is None:
        return image_path
    try:
        im = Image.open(image_path).transpose(tp)
        im.save(image_path)
    except OSError:
        pass
    return image_path


def _normalize_boxes(raw, image_path: str, *, unit="normalized_1000") -> dict:
    from app.core.geometry import convert_box
    from PIL import Image
    if not isinstance(raw, dict):
        return {}
    width, height = 1, 1
    if unit != "normalized_1000":
        with Image.open(image_path) as image:
            width, height = image.size
    result = {}
    for key, box in raw.items():
        try:
            result[key] = convert_box(box, unit=unit, width=width, height=height)
        except (ValueError, TypeError):
            continue
    return result


def _data_uri(image_path: str, *, max_edge=1180) -> str:
    from PIL import Image
    import io
    with Image.open(image_path) as image:
        image = image.convert("RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        raw = buffer.getvalue()
    b64 = base64.b64encode(raw).decode()
    return f"data:image/png;base64,{b64}"


# Compatibility entry points for existing tools/tests.
from app.core.model_invocation import _invoke_text, _parse_json
