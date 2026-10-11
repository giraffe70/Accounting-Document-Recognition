"""Atomic first-read recovery, restricted to optional supplement interruption."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from app.core import invocation_trace


def save(image_path, source_path, voucher, initial_ai, confirmed_fields=None, confirmed_paths=None):
    from app.core.model_invocation import source_versions
    sink = invocation_trace.sink_path()
    path = sink.parent / "first_read_checkpoint.json" if sink else Path(image_path).with_name(Path(image_path).stem + "_first_read_checkpoint.json")
    value = {"version":1,"phase":"first_read_complete", "execution_version_sha256":source_versions()["execution_version_sha256"],
             "source_sha256":hashlib.sha256(Path(source_path).read_bytes()).hexdigest(), "initial_ai":initial_ai,
             "voucher":voucher.model_dump(mode="json"), "manual_provenance":{"fields":confirmed_fields or {},"paths":confirmed_paths or []}}
    invocation_trace.atomic_json(path,value)
    invocation_trace.add({"kind":"first_read_checkpoint","path":str(path),"phase":value["phase"]})
    return path


def mark_supplement(path):
    path = Path(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["phase"] = "supplement"
    invocation_trace.atomic_json(path,value)


def complete(path):
    path = Path(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["phase"] = "supplement_complete"
    invocation_trace.atomic_json(path,value)


def recover(path, *, execution_version, source_path, reason):
    from app.models.schemas import Voucher
    from app.core.quality import evaluate_quality
    from app.core import prompts
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        if saved.get("version") != 1 or saved.get("phase") != "supplement" or saved.get("execution_version_sha256") != execution_version or saved.get("source_sha256") != hashlib.sha256(Path(source_path).read_bytes()).hexdigest():
            return None
        raw = dict(saved["initial_ai"])
        raw.pop("box_issues", None)  # Localization diagnostics are not content members.
        value = saved["voucher"]
        mode = "image" if "boxes" in raw else "ocr_text"
        prompts.validate_content_and_boxes(raw,value["doc_type_key"],mode)
        # A valid all-null contract is not a usable recovered first read.
        if not any(v is not None and v != "" for k,v in value.get("fields",{}).items() if k not in ("currency","direction")) and not value.get("items") and not value.get("entries"):
            return None
        diagnostics = value.setdefault("recognition_diagnostics", {"version":1,"requests":[],"issues":[]})
        diagnostics.setdefault("issues",[]).append({"code":"supplement_failed","stage":"supplement","reason":reason,"recovered_checkpoint":True})
        value["quality"] = evaluate_quality(value)
        return Voucher.model_validate(value).model_dump(mode="json")
    except (OSError, ValueError, TypeError, KeyError):
        return None
