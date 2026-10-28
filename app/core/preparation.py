"""Versioned human preparation, bound to the exact working image pixels."""
from copy import deepcopy
from datetime import datetime, timezone
from app.core import ocr_cache

VERSION = 1
MODES = ("manual_review", "automatic")
DOCUMENT_MODES = ("single", "auto_split", "manual_split")


def state(data):
    value = (data.get("origin") or {}).get("preparation") or {}
    return value if value.get("version") == VERSION else {}


def manual(data):
    return state(data).get("mode") == "manual_review"


def fresh(*, pdf=False):
    return {"version": VERSION, "mode": "manual_review", "direction_confirmed": False,
            "document_mode": "single", "type_confirmed": False, "pdf_enabled": bool(pdf),
            "transforms": [], "preview_ready_at": datetime.now(timezone.utc).isoformat()}


def fingerprint(path):
    return ocr_cache.pixels(path)["pixel_sha256"]


def confirmed(data, path):
    value = state(data)
    return bool(value.get("direction_confirmed") and value.get("image_sha256") == fingerprint(path))


def changed(data, operation=None):
    value = state(data)
    if not value:
        return
    value = deepcopy(value)
    value.update(direction_confirmed=False, image_sha256=None)
    value.pop("text_evidence", None)
    if not value.get("type_confirmed"):
        data["doc_type_key"], data["doc_type"] = "", ""
        data.setdefault("field_evidence",{}).pop("doc_type_key",None)
    kind = (operation or {}).get("kind")
    # 第一次動影像時記住原本能否沿用 PDF 文字層，復原後才能還原。
    value.setdefault("pdf_baseline", bool(value.get("pdf_enabled")))
    if kind in ("rotate", "crop"):
        value.setdefault("transforms", []).append(operation)
        value["fine_angle"] = 0   # 90° 與裁切會把已套用的微調定稿
    elif kind == "fine":
        # 微調只記目前累計角度，不逐次追加 transform。
        value["fine_angle"] = float(operation.get("angle") or 0)
        value["pdf_enabled"] = False
    else:
        value["pdf_enabled"] = False
    data.setdefault("origin", {})["preparation"] = value


def restored(data):
    """影像回到復原點：撤銷旋轉、裁切、微調與方向確認。"""
    changed(data)
    value = state(data)
    if not value:
        return
    value["transforms"] = []
    value["fine_angle"] = 0
    value["pdf_enabled"] = bool(value.pop("pdf_baseline", value.get("pdf_enabled")))


def confirm(data, path, *, document_mode="single", type_confirmed=False, actor_id=None):
    if document_mode not in DOCUMENT_MODES:
        raise ValueError("未知的單據張數處理方式")
    value = deepcopy(state(data))
    value.update(direction_confirmed=True, image_sha256=fingerprint(path),
                 document_mode=document_mode, type_confirmed=bool(type_confirmed),
                 confirmed_at=datetime.now(timezone.utc).isoformat(), confirmed_by=actor_id)
    data.setdefault("origin", {})["preparation"] = value
    return value
