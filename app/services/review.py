"""憑證確認流程：辨識結果落地 → 人工修正 → 確認完成，並保留對齊訊號。"""
import json
import os
import shutil
from datetime import datetime
from typing import Optional

from sqlmodel import select
from sqlalchemy import update

from app import config
from app.db.models import ReviewStatus, VoucherRecord
from app.db.session import get_session
from app.models.schemas import Voucher


def _ocr_cache_directory(rec):
    """An uploaded source keeps its scope after its document images are copied."""
    import re
    origin = (json.loads(rec.current_json).get("origin") or {})
    token = origin.get("source_token")
    if isinstance(token, str) and re.fullmatch(r"[a-f0-9]{32}", token):
        return config.UPLOAD_DIR / "_pages" / token / "ocr_cache"
    return config.DATA_DIR / "recognition" / str(rec.source_parent_id or rec.id) / "ocr_cache"


def _period_of(date_str: str) -> str:
    """由 YYYY-MM-DD 取 YYYY-MM，供日記帳彙整。"""
    return date_str[:7] if date_str and len(date_str) >= 7 else ""


def save_voucher(voucher: Voucher, company: str = "",
                 status: ReviewStatus = ReviewStatus.pending, *, source_parent_id=None) -> VoucherRecord:
    """辨識完成後落地紀錄。ai_json 與 current_json 初始相同。
    status 預設 pending（已辨識待確認）；匯入外觀確認階段傳 prepared（僅判類型/轉正）。"""
    payload = voucher.model_dump()
    blob = json.dumps(payload, ensure_ascii=False)
    rec = VoucherRecord(
        source_file=voucher.source_file,
        record_role=voucher.origin.get("record_role", "voucher"),
        source_parent_id=source_parent_id,
        company=company,
        period=_period_of(voucher.date),
        status=status,
        confidence=str(voucher.confidence or ""),
        ai_json=blob,
        current_json=blob,
        ocr_provider=voucher.ocr_provider,
        structure_model=voucher.structure_model,
    )
    with get_session() as s:
        s.add(rec)
        s.commit()
        s.refresh(rec)
        # 落地後以紀錄 id 保存該頁掃描影像，供審核對照
        src = getattr(voucher, "source_image", "")
        if src and os.path.exists(src):
            try:
                pages = config.UPLOAD_DIR / "pages"
                pages.mkdir(parents=True, exist_ok=True)
                ext = os.path.splitext(src)[1].lower() or ".png"
                shutil.copyfile(src, pages / f"{rec.id}{ext}")
                rec.image_path = f"pages/{rec.id}{ext}"
                s.add(rec)
                s.commit()
                s.refresh(rec)
            except OSError:
                pass
    return rec


def list_vouchers(status: Optional[ReviewStatus | list[ReviewStatus]] = None, company: str = "", period: str = "", *, include_sources=False):
    with get_session() as s:
        stmt = select(VoucherRecord)
        if not include_sources:
            stmt = stmt.where(VoucherRecord.record_role != "source_page")
        if status:
            statuses = status if isinstance(status, (list, tuple)) else [status]
            stmt = stmt.where(VoucherRecord.status.in_(statuses))
        if company:
            stmt = stmt.where(VoucherRecord.company == company)
        if period:
            stmt = stmt.where(VoucherRecord.period == period)
        return s.exec(stmt.order_by(VoucherRecord.created_at.desc())).all()


def get_voucher(rec_id: int) -> Optional[VoucherRecord]:
    with get_session() as s:
        return s.get(VoucherRecord, rec_id)


def save_prepared_vouchers(vouchers, company=""):
    parents, records = {}, []
    for voucher in vouchers:
        token = voucher.origin.get("source_token")
        rec = save_voucher(voucher, company, ReviewStatus.prepared, source_parent_id=parents.get(token))
        if rec.record_role == "source_page":
            parents[token] = rec.id
        records.append(rec)
    return records


class RecognitionProtected(ValueError):
    pass


def _check_model_write(rec):
    if rec.record_role == "source_page":
        raise RecognitionProtected("來源頁只保存原圖，請辨識子單據或先人工分割")


def _invalidate_confirmation(rec):
    """實際內容變更才撤銷本次確認；備註與無變動儲存不呼叫此函式。"""
    if getattr(rec, "status", None) == ReviewStatus.confirmed:
        rec.status = ReviewStatus.pending
        rec.confirmed_at = None


def _preserve_source_image(rec_id):
    rec = get_voucher(rec_id)
    if rec and rec.record_role == "source_page":
        raise RecognitionProtected("來源頁原圖保留不變，請对子憑證旋轉或裁切")


def _save_recognition(rec_id, voucher):
    """Append model versions while keeping the first AI result and selected manual values."""
    from copy import deepcopy
    from app.core import pipeline, model_invocation, invocation_trace
    with get_session() as session:
        rec = session.get(VoucherRecord, rec_id)
        _check_model_write(rec)
        previous = json.loads(rec.current_json)
        selected_manual_type = any(r.get("source") == "manual" and r.get("selected") for r in previous.get("field_evidence", {}).get("doc_type_key", []))
        if selected_manual_type and previous.get("doc_type_key") != voucher.doc_type_key:
            raise RecognitionProtected("單據類型已有人工確認，新模型結果類型不同，請重新載入")
        data = voucher.model_dump()
        evidence = deepcopy(previous.get("field_evidence") or {})
        for path, records in evidence.items():
            manual = next((r for r in reversed(records) if r.get("source") == "manual" and r.get("selected")), None)
            if not manual:
                continue
            if path.startswith("fields."):
                key = path[7:]
                data.setdefault("fields", {})[key] = (previous.get("fields") or {}).get(key)
            else:
                # Manual row groups are preserved together so row indices cannot drift.
                group = path.split("[", 1)[0]
                if group in ("items", "entries"):
                    data[group] = previous.get(group, [])
                elif path in previous:
                    data[path] = previous[path]
            data.setdefault("field_evidence", {})[path] = records
        if rec.manual_entry:
            for key in ("fields", "items", "entries", "note", "doc_type_key", "doc_type"):
                if key in previous:
                    data[key] = previous[key]
        data["origin"] = previous.get("origin", {})
        data["source_file"] = rec.source_file
        data = pipeline.canonicalize_voucher(data)
        history = json.loads(rec.recognition_history or "[]")
        history.append({"at": datetime.now().isoformat(), "versions": model_invocation.source_versions(), "model_result": voucher.model_dump()})
        rec.recognition_history = json.dumps(history, ensure_ascii=False)
        if rec.status in (ReviewStatus.prepared, ReviewStatus.queued) and not (json.loads(rec.ai_json).get("fields") or {}):
            rec.ai_json = json.dumps(voucher.model_dump(), ensure_ascii=False)
        rec.current_json = json.dumps(data, ensure_ascii=False)
        rec.confidence, rec.structure_model, rec.ocr_provider = str(voucher.confidence or ""), voucher.structure_model, voucher.ocr_provider
        rec.period = _period_of(data.get("date", "")) or rec.period
        rec.status = ReviewStatus.pending
        rec.confirmed_at = None
        rec.updated_at = datetime.now()
        session.add(rec); session.commit(); session.refresh(rec)
        return rec


def update_current(rec_id: int, voucher_dict: dict, *, mark_manual: bool = True,
                   expected_updated_at: Optional[datetime] = None) -> Optional[VoucherRecord]:
    """人工修正欄位（不動 ai_json，只更新 current_json）。"""
    with get_session() as s:
        rec = s.get(VoucherRecord, rec_id)
        if not rec:
            return None
        if expected_updated_at is not None and rec.updated_at != expected_updated_at:
            raise RecognitionProtected("辨識期間資料已被修改，請重新載入")
        if getattr(rec, "record_role", "voucher") == "source_page":
            raise RecognitionProtected("來源頁只保存原圖與版面診斷，請修改子憑證")
        if not mark_manual:
            _check_model_write(rec)
        from app.core import pipeline
        previous = json.loads(rec.current_json)
        old_fields = previous.get("fields") or {}
        voucher_dict = {**previous, **voucher_dict}
        voucher_dict["origin"] = previous.get("origin", {})
        # Browser copies can lag behind earlier saves; audit history is server-owned.
        from copy import deepcopy
        voucher_dict["field_evidence"] = deepcopy(previous.get("field_evidence") or {})
        if mark_manual:
            voucher_dict["validation_warnings"] = list(previous.get("validation_warnings") or [])
            voucher_dict["classification_meta"] = deepcopy(previous.get("classification_meta") or {})
        if voucher_dict.get("fields") is not None:
            voucher_dict["fields"] = {**old_fields, **voucher_dict["fields"]}
        if mark_manual:
            evidence = voucher_dict.setdefault("field_evidence", {})
            changed = False
            for key, value in (voucher_dict.get("fields") or {}).items():
                if old_fields.get(key) != value:
                    if any(r.get("source") == "manual" and r.get("selected") and r.get("value") == value
                           for r in evidence.get("fields." + key, [])):
                        continue  # same raw input as before normalization, not a new edit
                    changed = True
                    for item in evidence.get("fields." + key, []):
                        item["selected"] = False
                    evidence.setdefault("fields." + key, []).append({"source": "manual", "value": value, "selected": True})
            for index, row in enumerate(voucher_dict.get("items") or []):
                old_rows = previous.get("items") or []
                old_row = old_rows[index] if index < len(old_rows) else {}
                for key, value in row.items():
                    if old_row.get(key) != value:
                        path = f"items[{index}].{key}"
                        if any(r.get("source") == "manual" and r.get("selected") and r.get("value") == value
                               for r in evidence.get(path, [])):
                            continue
                        changed = True
                        for item in evidence.get(path, []):
                            item["selected"] = False
                        evidence.setdefault(path, []).append({"source": "manual", "value": value, "selected": True})
            for key in ("entries", "note", "doc_type_key", "doc_type"):
                if voucher_dict.get(key) != previous.get(key):
                    changed = True
                    for item in evidence.get(key, []):
                        item["selected"] = False
                    evidence.setdefault(key, []).append({"source": "manual", "value": voucher_dict.get(key), "selected": True})
            if (len(voucher_dict.get("items") or []) != len(previous.get("items") or [])
                    or voucher_dict.get("note") != previous.get("note")
                    or voucher_dict.get("doc_type_key") != previous.get("doc_type_key")):
                changed = True
            if changed and voucher_dict.get("classification_meta"):
                meta = voucher_dict["classification_meta"]
                meta.update(status="candidate", stale=True)
                meta["warnings"] = list(dict.fromkeys([*(meta.get("warnings") or []), "資料已人工修改，原分錄待重新確認"]))
        voucher_dict = pipeline.canonicalize_voucher(voucher_dict)
        previous_normalized = pipeline.canonicalize_voucher(deepcopy(previous))
        if not mark_manual or voucher_dict != previous_normalized:
            _invalidate_confirmation(rec)
        rec.current_json = json.dumps(voucher_dict, ensure_ascii=False)
        rec.period = _period_of(voucher_dict.get("date", "")) or rec.period
        rec.updated_at = datetime.now()
        s.add(rec)
        s.commit()
        s.refresh(rec)
        return rec


def confirm_record(rec_id: int, expected_updated_at: str) -> Optional[VoucherRecord]:
    """確認已保存的指定版本；條件 UPDATE 避免查核與寫入之間的競態。"""
    with get_session() as s:
        rec = s.get(VoucherRecord, rec_id)
        if not rec:
            return None
        if rec.record_role == "source_page":
            raise RecognitionProtected("來源頁不參與憑證確認，請操作子憑證")
        if rec.status not in (ReviewStatus.pending, ReviewStatus.confirmed):
            raise RecognitionProtected("請先完成內容辨識，再確認憑證")
        if not rec.updated_at or rec.updated_at.isoformat() != expected_updated_at:
            raise RecognitionProtected("資料已被修改，請重新載入後確認")
        if rec.status == ReviewStatus.confirmed:
            return rec
        now = datetime.now()
        result = s.execute(update(VoucherRecord).where(
            VoucherRecord.id == rec_id, VoucherRecord.updated_at == rec.updated_at,
            VoucherRecord.status == ReviewStatus.pending,
            VoucherRecord.record_role != "source_page",
        ).values(status=ReviewStatus.confirmed, confirmed_at=now, updated_at=now)
          .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            s.rollback()
            raise RecognitionProtected("資料已被修改，請重新載入後確認")
        s.commit()
        s.refresh(rec)
        return rec


def set_company(rec_id: int, company: str) -> Optional[VoucherRecord]:
    """補填／更正歸屬公司。公司只在匯入時由下拉指定，漏選就永遠是空的
    （清單與報表都靠它分公司彙整），故審核階段要能補。"""
    with get_session() as s:
        rec = s.get(VoucherRecord, rec_id)
        if not rec:
            return None
        company = company or ""
        if rec.company != company:
            _invalidate_confirmation(rec)
        rec.company = company
        rec.updated_at = datetime.now()
        s.add(rec)
        s.commit()
        s.refresh(rec)
        return rec


def set_notes(rec_id: int, **notes) -> Optional[VoucherRecord]:
    """只更新備註欄與人工輸入標記，不動狀態與 current_json。
    備註會成為下次辨識的人工修正提示，故必須能單獨儲存。"""
    allowed = {"correction_note", "manual_entry"}
    with get_session() as s:
        rec = s.get(VoucherRecord, rec_id)
        if not rec:
            return None
        for k, v in notes.items():
            if k in allowed and v is not None:
                setattr(rec, k, v)
        rec.updated_at = datetime.now()
        s.add(rec)
        s.commit()
        s.refresh(rec)
        return rec


def _clear_boxes(rec_id: int):
    """影像變動後既有 boxes 不再對齊，清除之。"""
    with get_session() as s:
        r = s.get(VoucherRecord, rec_id)
        cur = json.loads(r.current_json)
        cur["boxes"] = {}
        _invalidate_confirmation(r)
        r.current_json = json.dumps(cur, ensure_ascii=False)
        r.updated_at = datetime.now()
        s.add(r)
        s.commit()


def _image_path_of(rec):
    if not rec or not rec.image_path:
        return None
    p = config.UPLOAD_DIR / rec.image_path
    return p if p.exists() else None


def rotate_image(rec_id: int, direction: str = "cw"):
    _preserve_source_image(rec_id)
    """手動旋轉掃描影像 90 度（無損）並覆寫。"""
    p = _image_path_of(get_voucher(rec_id))
    if not p:
        return None
    from PIL import Image

    tp = Image.Transpose.ROTATE_270 if direction == "cw" else Image.Transpose.ROTATE_90
    try:
        Image.open(p).transpose(tp).save(p)
    except OSError:
        return None
    _clear_boxes(rec_id)
    return True


def rotate_fine(rec_id: int, angle: float):
    _preserve_source_image(rec_id)
    """微調角度旋轉（去歪斜），正值＝順時針；空白處補白。"""
    p = _image_path_of(get_voucher(rec_id))
    if not p:
        return None
    from PIL import Image

    try:
        if float(angle) % 360 == 0:
            return True
        im = Image.open(p).convert("RGB").rotate(-float(angle), expand=True, fillcolor=(255, 255, 255))
        im.save(p)
    except (OSError, ValueError):
        return None
    _clear_boxes(rec_id)
    return True


def _backup_once(path) -> None:
    """首次破壞性修圖（裁切／塗白）前備份為 <name>.orig<ext>，誤裁時仍可取回。
    只備份第一次，之後的修改都以第一次的原圖為還原點。"""
    backup = path.with_suffix(".orig" + path.suffix)
    if not backup.exists():
        try:
            shutil.copyfile(path, backup)
        except OSError:
            pass


def _mask_region(path, lt_rb) -> bool:
    """把已分割出去的區域塗成白底（紙張底色）。

    一頁兩張發票時，若只把區塊複製成新憑證而不動原圖，原圖仍是兩張發票，
    VLM 會混讀兩張的欄位 → 必錯。塗白後原圖只剩其餘憑證。"""
    from PIL import Image, ImageDraw

    try:
        _backup_once(path)
        im = Image.open(path)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        fill = 255 if im.mode == "L" else (255, 255, 255)
        ImageDraw.Draw(im).rectangle(lt_rb, fill=fill)
        im.save(path)
        return True
    except (OSError, ValueError):
        return False


def is_blank_image(rec_id: int) -> bool:
    """紀錄的影像是否已近乎空白（分割光了所有憑證後的空殼）。"""
    from app.core import pipeline

    p = _image_path_of(get_voucher(rec_id))
    return bool(p) and pipeline._is_blank_page(str(p))


def split_voucher(rec_id: int, box):
    """從原憑證影像裁一塊 → 只做外觀準備（判類型/轉正）→ 存成新憑證紀錄。
    切走的區域會在原圖上塗白（見 _mask_region），原圖只留其餘憑證。
    供一頁多張憑證時手動分割。新紀錄以 prepared 落地，重新進入外觀確認漏斗。
    box 為 [x1,y1,x2,y2]，值域 0~1。"""
    from app.core import doc_types, pipeline, ocr_cache
    from app.models.schemas import Voucher

    p = _image_path_of(get_voucher(rec_id))
    if not p or not isinstance(box, (list, tuple)) or len(box) < 4:
        return None
    rec = get_voucher(rec_id)
    from PIL import Image

    try:
        im = Image.open(p)
        w, h = im.size
        x1, y1, x2, y2 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
        L, T = int(min(x1, x2) * w), int(min(y1, y2) * h)
        R, B = int(max(x1, x2) * w), int(max(y1, y2) * h)
        if R - L < 10 or B - T < 10:
            return None
        import uuid
        source_token = uuid.uuid4().hex
        pages = config.UPLOAD_DIR / "_pages" / source_token
        pages.mkdir(parents=True, exist_ok=True)
        tmp = pages / "document.png"
        im.crop((L, T, R, B)).save(tmp)
    except (OSError, ValueError, TypeError):
        return None

    with ocr_cache.scope(pages / "ocr_cache"):
        type_key = pipeline.prepare_image(str(tmp))     # 只判類型＋轉正，不萃取欄位
    v = Voucher.model_validate({
        "doc_type_key": type_key,
        "doc_type": doc_types.get(type_key)["label"],
        "fields": {}, "boxes": {}, "items": [],
    })
    v.source_image = str(tmp)                       # 讓 save_voucher 複製此裁切影像
    v.source_file = f"{rec.source_file}（分割）"
    if rec.record_role == "source_page":
        from app.core import page_layout
        v.origin = page_layout.origin(p, {"box": [round(x1*1000), round(y1*1000), round(x2*1000), round(y2*1000)], "rotate": 0}, json.loads(rec.current_json).get("origin", {}).get("page_no", 1))
    v.origin["source_token"] = source_token
    # Keep the upload group so "delete whole upload" also reaches manually split documents.
    parent_origin = json.loads(rec.current_json).get("origin") or {}
    for key in ("source_group_id", "upload_file"):
        if parent_origin.get(key):
            v.origin[key] = parent_origin[key]
    new_rec = save_voucher(v, company=rec.company, status=ReviewStatus.prepared,
                           source_parent_id=rec.id if rec.record_role == "source_page" else rec.source_parent_id)
    # 新憑證已建立，才把該區從原圖抹掉（先建後抹：中途失敗不會兩邊都沒有）
    if new_rec and rec.record_role != "source_page" and _mask_region(p, (L, T, R, B)):
        _clear_boxes(rec_id)
    return new_rec


def crop_image(rec_id: int, box):
    """裁切影像。box 為 [x1,y1,x2,y2]，值域 0~1（相對影像比例）。"""
    _preserve_source_image(rec_id)
    p = _image_path_of(get_voucher(rec_id))
    if not p or not isinstance(box, (list, tuple)) or len(box) < 4:
        return None
    from PIL import Image

    try:
        im = Image.open(p)
        w, h = im.size
        x1, y1, x2, y2 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
        L, T = int(min(x1, x2) * w), int(min(y1, y2) * h)
        R, B = int(max(x1, x2) * w), int(max(y1, y2) * h)
        if R - L < 10 or B - T < 10:
            return None
        if (L, T, R, B) == (0, 0, w, h):
            return True
        _backup_once(p)
        im.crop((L, T, R, B)).save(p)
    except (OSError, ValueError, TypeError):
        return None
    _clear_boxes(rec_id)
    return True


def re_recognize(rec_id: int) -> Optional[VoucherRecord]:
    """對既有影像重新辨識，以修正備註作為提示。"""
    from app.core import pipeline

    rec = get_voucher(rec_id)
    if not rec or not rec.image_path:
        return None
    _check_model_write(rec)
    hint = rec.correction_note or ""
    from app.core import invocation_trace, model_invocation, ocr_cache
    import uuid
    trace_path = config.DATA_DIR / "recognition" / str(rec_id) / (uuid.uuid4().hex + ".json")
    with invocation_trace.capture(trace_path), ocr_cache.scope(_ocr_cache_directory(rec)):
        invocation_trace.add({"kind": "snapshot", "versions": model_invocation.source_versions(), "record_id": rec_id})
        converted, type_key = _route_unconfirmed_page(rec)
        if converted:
            return converted
        current = json.loads(rec.current_json)
        manual = any(r.get("source") == "manual" and r.get("selected") for records in current.get("field_evidence", {}).values() for r in records)
        if manual or rec.manual_entry or type_key:
            confirmed = {p[7:]: current.get("fields", {}).get(p[7:]) for p, records in current.get("field_evidence", {}).items()
                         if p.startswith("fields.") and any(r.get("source") == "manual" and r.get("selected") for r in records)}
            protected_type = rec.manual_entry or any(r.get("source") == "manual" and r.get("selected") for r in current.get("field_evidence", {}).get("doc_type_key", []))
            protected_paths = [p for p,records in current.get("field_evidence", {}).items() if any(r.get("source") == "manual" and r.get("selected") for r in records)]
            v = pipeline.extract_for_type(str(config.UPLOAD_DIR / rec.image_path), current.get("doc_type_key", "other") if manual else type_key or current.get("doc_type_key", "other"), hint=hint, company=rec.company, confirmed_fields=confirmed, confirmed_type=protected_type, confirmed_paths=protected_paths)
        else:
            v = pipeline.recognize_image(str(config.UPLOAD_DIR / rec.image_path), hint=hint, company=rec.company)
    return _save_recognition(rec_id, v)


def _route_unconfirmed_page(rec):
    """Convert an unconfirmed legacy page while leaving its source pixels intact."""
    from app.core import page_layout, invocation_trace
    current = json.loads(rec.current_json)
    manual = any(r.get("source") == "manual" and r.get("selected") for records in current.get("field_evidence", {}).values() for r in records)
    if rec.source_parent_id or rec.manual_entry or manual or rec.status == ReviewStatus.confirmed:
        return None, None
    source = config.UPLOAD_DIR / rec.image_path
    layout = page_layout.detect(str(source))
    if (layout["content_kind"] == "document" and layout["auto_split"] and len(layout["regions"]) == 1
            and layout["regions"][0]["rotate"] == 0 and layout["regions"][0]["box"] == [0,0,1000,1000]):
        return None, layout["regions"][0]["type"]
    origin = {**page_layout.origin(source, {"box":[0,0,1000,1000]}, current.get("origin", {}).get("page_no", 1)), "record_role":"source_page", "layout": layout}
    # Recheck human state in the transaction after the model call.
    with get_session() as session:
        parent = session.get(VoucherRecord, rec.id)
        if parent.current_json != rec.current_json or parent.status != rec.status:
            raise RecognitionProtected("辨識期間資料已被修改，請重新載入")
        parent.record_role = "source_page"
        parent.current_json = json.dumps({**current, "origin": origin, "quality":{"version":1,"state":"needs_review","content_kind":layout["content_kind"],"issues":[{"code":"manual_split","message":"來源頁保留，請確認子單據"}]}}, ensure_ascii=False)
        session.add(parent); session.commit(); session.refresh(parent)
    if layout["auto_split"] and layout["content_kind"] == "document":
        for index, region in enumerate(layout["regions"]):
            target = config.UPLOAD_DIR / "_pages" / f"source_{rec.id}" / f"document_{index}.png"
            native = page_layout.crop(source, region, target)
            voucher = Voucher.model_validate({"doc_type_key": region["type"], "doc_type": page_layout.doc_types.get(region["type"])["label"],
                "origin": {**page_layout.origin(source, region, origin["page_no"]), "document_index":index, "layout_fallback":layout.get("fallback_reason")},
                "quality":{"version":1,"state":"unassessed","content_kind":layout["content_kind"],"issues":[]}})
            voucher.source_image, voucher.source_file = native, rec.source_file
            save_voucher(voucher, rec.company, ReviewStatus.prepared, source_parent_id=rec.id)
    return get_voucher(rec.id), None


def confirm_appearance(rec_id: int, type_key: str = "") -> Optional[VoucherRecord]:
    """外觀確認完成：prepared → queued（加入掃描清單，待內容提取）。不萃取欄位。"""
    with get_session() as s:
        rec = s.get(VoucherRecord, rec_id)
        if not rec:
            return None
        if rec.record_role == "source_page":
            raise RecognitionProtected("來源頁只保存原圖，請先人工分割")
        if rec.status not in (ReviewStatus.prepared, ReviewStatus.queued):
            raise RecognitionProtected("此憑證已完成內容辨識，請至確認頁操作")
        if type_key:
            _apply_type(rec, type_key)
        rec.status = ReviewStatus.queued
        rec.updated_at = datetime.now()
        s.add(rec); s.commit(); s.refresh(rec)
        return rec


def ids_by_status(status: ReviewStatus) -> list[int]:
    """列舉指定狀態的紀錄 id（供批次提取）。"""
    return [r.id for r in list_vouchers(status=status)]


def merge_vouchers(primary_id: int, other_ids: list) -> Optional[VoucherRecord]:
    """多頁合併為一張：把其他紀錄的影像併為主頁的附頁，刪除其他紀錄。
    供「一份多頁文件被拆成多筆」時（如水費帳單 3 頁）在外觀確認階段合併。"""
    prim = get_voucher(primary_id)
    if not prim:
        return None
    if prim.record_role == "source_page":
        raise RecognitionProtected("來源頁不能合併為財務憑證，請合併子憑證")
    pages = config.UPLOAD_DIR / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    with get_session() as s:
        p = s.get(VoucherRecord, primary_id)
        atts = json.loads(p.attachments or "[]")
        idx = len(atts) + 1
        absorbed = False

        def _absorb(rel_path: str):
            nonlocal idx
            src = config.UPLOAD_DIR / rel_path
            if not src.exists():
                return
            ext = os.path.splitext(rel_path)[1].lower() or ".png"
            dest_rel = f"pages/{primary_id}_att{idx}{ext}"
            try:
                shutil.copyfile(src, config.UPLOAD_DIR / dest_rel)
                atts.append(dest_rel)
                idx += 1
            except OSError:
                pass

        for oid in other_ids:
            if oid == primary_id:
                continue
            o = s.get(VoucherRecord, oid)
            if not o:
                continue
            if o.record_role == "source_page":
                raise RecognitionProtected("來源頁須保留，不可刪除合併")
            if o.image_path:
                _absorb(o.image_path)               # 該頁主影像 → 主頁附頁
            for a in json.loads(o.attachments or "[]"):
                _absorb(a)                           # 該頁自己的附頁也一併搬入
            s.delete(o)
            absorbed = True
        p.attachments = json.dumps(atts, ensure_ascii=False)
        if absorbed:
            _invalidate_confirmation(p)
        p.updated_at = datetime.now()
        s.add(p)
        s.commit()
        s.refresh(p)
        return p


def set_type(rec_id: int, type_key: str) -> Optional[VoucherRecord]:
    """外觀確認階段：修正表單種類（更新 current_json 的 doc_type_key/doc_type）。"""
    with get_session() as s:
        rec = s.get(VoucherRecord, rec_id)
        if not rec:
            return None
        if rec.record_role == "source_page":
            raise RecognitionProtected("來源頁只保存原圖，請修改子憑證類型")
        _apply_type(rec, type_key)
        rec.updated_at = datetime.now()
        s.add(rec)
        s.commit()
        s.refresh(rec)
        return rec


def _apply_type(rec, type_key):
    from app.core import doc_types

    cur = json.loads(rec.current_json)
    if cur.get("doc_type_key") != type_key:
        _invalidate_confirmation(rec)
    cur["doc_type_key"] = type_key
    cur["doc_type"] = doc_types.get(type_key)["label"]
    evidence = cur.setdefault("field_evidence", {})
    selected = any(item.get("source") == "manual" and item.get("selected")
                   and item.get("value") == type_key for item in evidence.get("doc_type_key", []))
    if not selected:
        for item in evidence.get("doc_type_key", []):
            item["selected"] = False
        evidence.setdefault("doc_type_key", []).append({"source": "manual", "value": type_key, "selected": True})
    rec.current_json = json.dumps(cur, ensure_ascii=False)


def recognize_record(rec_id: int) -> Optional[VoucherRecord]:
    """外觀確認完成後：依已確認的類型，在已準備好的影像上做內容辨識（萃取欄位＋框）。
    完成後轉為 pending（待確認）。備註欄作為人工修正提示餵入。"""
    from app.core import pipeline

    rec = get_voucher(rec_id)
    if not rec or not rec.image_path:
        return None
    cur = json.loads(rec.current_json)
    type_key = cur.get("doc_type_key") or "other"
    _check_model_write(rec)
    hint = rec.correction_note or ""
    confirmed = {path[7:]: cur.get("fields", {}).get(path[7:]) for path, records in cur.get("field_evidence", {}).items()
                 if path.startswith("fields.") and any(r.get("source") == "manual" and r.get("selected") for r in records)}
    from app.core import invocation_trace, model_invocation, ocr_cache
    import uuid
    trace_path = config.DATA_DIR / "recognition" / str(rec_id) / (uuid.uuid4().hex + ".json")
    with invocation_trace.capture(trace_path), ocr_cache.scope(_ocr_cache_directory(rec)):
        invocation_trace.add({"kind": "snapshot", "versions": model_invocation.source_versions(), "record_id": rec_id})
        protected_paths = [p for p,records in cur.get("field_evidence", {}).items() if any(r.get("source") == "manual" and r.get("selected") for r in records)]
        v = pipeline.extract_for_type(str(config.UPLOAD_DIR / rec.image_path), type_key, hint=hint, company=rec.company, confirmed_fields=confirmed, confirmed_type=True, confirmed_paths=protected_paths)
        v.field_evidence["doc_type_key"] = cur.get("field_evidence", {}).get("doc_type_key") or [{"source": "manual", "value": type_key, "reason": "已確認單據類型", "selected": True}]
        v.source_image = ""   # 影像已在原位
    return _save_recognition(rec_id, v)


def feedback_log():
    """匯集有任何備註或經人工修正的紀錄，作為修正回饋清單。"""
    out = []
    for rec in list_vouchers():
        changed = rec.ai_json != rec.current_json
        if not rec.correction_note and not changed and not rec.manual_entry:
            continue
        cur = json.loads(rec.current_json)
        out.append({
            "id": rec.id, "source_file": rec.source_file,
            "doc_type": cur.get("doc_type", ""), "doc_type_key": cur.get("doc_type_key", ""),
            "voucher_id": cur.get("voucher_id", ""), "status": rec.status, "changed": changed,
            "manual_entry": bool(rec.manual_entry),
            "correction_note": rec.correction_note,
        })
    return out


def alignment_pairs():
    """已確認紀錄中，AI 原始值與人工修正值不同者 → 模型對齊/微調訊號。"""
    pairs = []
    for rec in list_vouchers(status=ReviewStatus.confirmed):
        if rec.ai_json != rec.current_json:
            pairs.append({"id": rec.id, "ai": json.loads(rec.ai_json),
                          "corrected": json.loads(rec.current_json)})
    return pairs


# ── 刪除 ──────────────────────────────────────────────────────────
class DeleteForbidden(PermissionError):
    """目前使用者不得刪除（例如非管理員刪除已確認憑證）。"""


DELETE_SCOPES = ("record", "upload")


def _origin_of(rec) -> dict:
    try:
        return json.loads(rec.current_json).get("origin") or {}
    except (TypeError, ValueError):
        return {}


def _child_records(session, parent_ids):
    if not parent_ids:
        return []
    return session.exec(select(VoucherRecord).where(VoucherRecord.source_parent_id.in_(list(parent_ids)))).all()


def _delete_targets(session, rec, scope: str) -> dict:
    """刪除 rec 時一併移除的紀錄。

    record：目前單據；若為來源頁則連同其子單據；若刪的是最後一張子單據，
    且來源頁已自動分割（前端不顯示），一併移除，避免留下看不見的孤兒。
    upload：同一個上傳檔（source_group_id）的所有來源頁與單據；舊資料沒有
    source_group_id 時，退回同一來源頁及其子單據，不以檔名猜測群組。"""
    if scope not in DELETE_SCOPES:
        raise ValueError("刪除範圍不合法")
    found = {rec.id: rec}
    if scope == "upload":
        root_id = rec.id if rec.record_role == "source_page" else rec.source_parent_id
        root = session.get(VoucherRecord, root_id) if root_id is not None else None
        if root:
            found[root.id] = root
        group = _origin_of(rec).get("source_group_id")
        if group:
            for other in session.exec(select(VoucherRecord).where(VoucherRecord.current_json.contains(group))).all():
                if _origin_of(other).get("source_group_id") == group:
                    found[other.id] = other
    for child in _child_records(session, [r.id for r in found.values() if r.record_role == "source_page"]):
        found[child.id] = child
    # 最後一張子單據被刪後，已自動分割（隱藏）的來源頁沒有用途，一併移除。
    for parent_id in {r.source_parent_id for r in found.values() if r.source_parent_id is not None} - set(found):
        parent = session.get(VoucherRecord, parent_id)
        if not parent or parent.record_role != "source_page":
            continue
        layout = _origin_of(parent).get("layout") or {}
        if layout.get("auto_split") is True and not [c for c in _child_records(session, [parent_id]) if c.id not in found]:
            found[parent.id] = parent
    return found


def _delete_summary(targets: dict, scope: str, rec) -> dict:
    records = list(targets.values())
    confirmed = [r for r in records if r.status == ReviewStatus.confirmed]
    return {
        "scope": scope, "record_id": rec.id, "ids": sorted(targets),
        "voucher_count": len([r for r in records if r.record_role != "source_page"]),
        "source_page_count": len([r for r in records if r.record_role == "source_page"]),
        "confirmed_count": len(confirmed),
        "attachment_count": sum(len(json.loads(r.attachments or "[]")) for r in records),
        "requires_admin": bool(confirmed),
    }


def delete_preview(rec_id: int, scope: str) -> Optional[dict]:
    """刪除前預覽影響範圍；不改動任何資料。"""
    with get_session() as s:
        rec = s.get(VoucherRecord, rec_id)
        if not rec:
            return None
        return _delete_summary(_delete_targets(s, rec, scope), scope, rec)


def _still_referenced(session, needle: str) -> bool:
    return session.exec(select(VoucherRecord.id).where(VoucherRecord.current_json.contains(needle)).limit(1)).first() is not None


def _cleanup_deleted_files(deleted: list) -> tuple:
    """刪除紀錄後清理應用程式自己管理的檔案；失敗只回報，不中斷。

    只動 uploads/pages、uploads/_pages、data/recognition 內的路徑，以及 uploads/
    頂層由本系統存下的上傳檔（32 位 hex 檔名）；外部掃描來源目錄永不處理。
    其他紀錄仍引用的 token／上傳檔會保留。"""
    import re
    from pathlib import Path

    upload_dir = config.UPLOAD_DIR.resolve()
    roots = [upload_dir / "pages", upload_dir / "_pages", (config.DATA_DIR / "recognition").resolve()]
    removed, failed = 0, []

    def remove(path):
        nonlocal removed
        try:
            target = Path(path).resolve()
            if not any(target != root and target.is_relative_to(root) for root in roots):
                failed.append(f"{path}: 不在可清理範圍")
                return
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            else:
                return
            removed += 1
        except OSError as exc:
            failed.append(f"{path}: {exc}")

    ids = {item["id"] for item in deleted}
    pages = upload_dir / "pages"
    names = [p.name for p in pages.iterdir()] if pages.is_dir() else []
    for item in deleted:
        for rel in [item["image_path"], *item["attachments"]]:
            if rel:
                remove(upload_dir / rel)
        prefix = re.compile(rf"^{item['id']}(?=[._])")
        for name in names:
            if prefix.match(name):
                remove(pages / name)
        remove(upload_dir / "_pages" / f"source_{item['id']}")

    tokens = {item["origin"].get("source_token") for item in deleted}
    uploads = {item["origin"].get("upload_file") for item in deleted}
    with get_session() as s:
        for rec_id in ids:
            if not _child_records(s, [rec_id]):
                remove(config.DATA_DIR / "recognition" / str(rec_id))
        for token in tokens:
            if isinstance(token, str) and re.fullmatch(r"[a-f0-9]{32}", token) and not _still_referenced(s, token):
                remove(upload_dir / "_pages" / token)
        for name in uploads:
            if not (isinstance(name, str) and re.fullmatch(r"[a-f0-9]{32}\.[a-z0-9]+", name)) or _still_referenced(s, name):
                continue
            source = upload_dir / name
            try:
                if source.is_file() and source.parent == upload_dir:
                    source.unlink()
                    removed += 1
            except OSError as exc:
                failed.append(f"{source}: {exc}")
            rendered = re.compile(rf"^{re.escape(Path(name).stem)}_p\d+\.png$")
            render_dir = upload_dir / "_pages"
            for page in (render_dir.iterdir() if render_dir.is_dir() else []):
                if page.is_file() and rendered.match(page.name):
                    remove(page)
    return removed, failed


def _write_delete_audit(actor: str, scope: str, requested_id: int, deleted: list) -> None:
    """只記錄誰、何時、刪了哪些紀錄與狀態；不保留影像或欄位內容。"""
    entry = {"time": datetime.now().isoformat(timespec="seconds"), "actor": actor, "scope": scope,
             "requested_id": requested_id,
             "records": [{"id": d["id"], "role": d["role"], "status": d["status"]} for d in deleted]}
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.LOG_DIR / "deletions.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def delete_vouchers(rec_id: int, scope: str, *, is_admin: bool = False, actor: str = "") -> Optional[dict]:
    """刪除紀錄並清理其檔案。已確認憑證僅管理員可刪（DeleteForbidden）。"""
    import logging
    with get_session() as s:
        rec = s.get(VoucherRecord, rec_id)
        if not rec:
            return None
        targets = _delete_targets(s, rec, scope)
        if not is_admin and any(t.status == ReviewStatus.confirmed for t in targets.values()):
            raise DeleteForbidden("範圍內含已確認的憑證，只有管理員可以刪除")
        deleted = [{"id": t.id, "role": t.record_role, "status": t.status.value, "image_path": t.image_path,
                    "attachments": json.loads(t.attachments or "[]"), "origin": _origin_of(t)}
                   for t in targets.values()]
        # 不在刪除範圍內卻指向被刪來源頁的紀錄：解除關聯，避免懸空外鍵。
        for orphan in _child_records(s, list(targets)):
            if orphan.id not in targets:
                orphan.source_parent_id = None
                s.add(orphan)
        for target in targets.values():
            s.delete(target)
        s.commit()
    removed, failed = _cleanup_deleted_files(deleted)
    _write_delete_audit(actor, scope, rec_id, deleted)
    log = logging.getLogger("pdf_scan.review")
    log.info("刪除憑證 %s（範圍 %s，操作者 %s），清理 %d 個檔案", sorted(targets), scope, actor or "-", removed)
    if failed:
        log.warning("刪除後檔案清理失敗：%s", failed)
    return {"ok": True, "deleted_ids": sorted(targets), "files_removed": removed, "cleanup_failed": failed}
