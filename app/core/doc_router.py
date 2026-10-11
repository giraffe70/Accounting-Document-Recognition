"""Document routing using the shared structured invocation and three-template builder."""
from __future__ import annotations
import logging
from contextvars import ContextVar
from app.core import doc_types, model_factory, settings_store, prompts

logger = logging.getLogger("pdf_scan.router")
_last_result = ContextVar("last_document_route", default=None)
# Compatibility for external callers; this is a rendered view, not a second template.
ROUTER_PROMPT = prompts.build_route_prompt()


def validate_route(obj: dict, mode="image") -> dict:
    obj = prompts.RouteOutput.model_validate(obj).model_dump()
    candidate = obj["type"].strip().lower()
    key = candidate if candidate in doc_types.DOC_TYPES else next((key for key, spec in doc_types.DOC_TYPES.items()
          if candidate == spec["label"].lower() or candidate in [str(a).lower() for a in spec.get("aliases", [])]), "other")
    reason = obj["reason"].strip()
    if key == "other" and not reason:
        if candidate in ("other", *doc_types.DOC_TYPES["other"]["aliases"]):
            raise ValueError("other requires nonempty reason")
        reason = "未知文件類型：" + candidate
    rotate = obj["rotate"]
    if rotate not in ((0,) if mode == "ocr_text" else (0, 90, 180, 270)):
        raise ValueError("rotate allowed: 0" if mode == "ocr_text" else "rotate allowed: 0,90,180,270")
    return {"type": key, "rotate": rotate, "reason": reason}


def last_result() -> dict:
    return _last_result.get() or {}


def classify(image_path: str = "", hint: str = "", *, mode="image", ocr_text="", model_setting=None) -> tuple[str, int]:
    from app.core import activity, model_invocation, pipeline
    setting = model_setting or settings_store.get_settings()["structure"]
    provider, model = settings_store.parse_setting(setting)
    chat = model_factory.get_chat_model(provider, model, temperature=0.0)
    content = [{"type": "text", "text": prompts.build_route_prompt(mode, ocr_text, hint)}]
    if mode == "image":
        content.append({"type": "image_url", "image_url": {"url": pipeline._data_uri(image_path)}})
    try:
        with activity.stage("structure", setting + "（判類型）"):
            obj = model_invocation.invoke_structured(chat, content, lambda obj: validate_route(obj, mode),
                                                       task="route", invoke=pipeline._invoke_text, schema=prompts.RouteOutput.model_json_schema())
    except Exception as exc:
        logger.warning("Router 失敗，改用 other：%s", exc)
        obj = {"type": "other", "rotate": 0, "reason": "分類失敗：" + str(exc)[:120]}
    from app.core import classification_evidence
    obj["type"], evidence = classification_evidence.resolve(obj["type"], ocr_text)
    obj["classification_evidence"] = evidence
    model_invocation.invocation_trace.add({"kind":"classification_evidence",**evidence})
    _last_result.set(obj)
    return obj["type"], obj["rotate"]
