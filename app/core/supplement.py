"""One bounded reread; independent evidence gates every adopted candidate."""
from __future__ import annotations
import re
from pathlib import Path
from app.core import doc_types, normalization, invocation_trace

LABELS = {"subtotal": r"未稅(?:合計|金額)?|銷售額|小計|subtotal",
          "tax_amount": r"稅額|營業稅|tax", "total_amount": r"含稅(?:合計|總額)?|總計|總額|應付(?:金額)?|total"}


def numeric(value):
    parsed = normalization.norm_amount(value)
    return parsed if isinstance(parsed, (int, float)) and not isinstance(parsed, bool) else None


def arithmetic(fields, mapping):
    values = {key: numeric(next((fields.get(a) for a in mapping.get(key, []) if fields.get(a) not in (None, "")), None)) for key in LABELS}
    if all(v is not None for v in values.values()):
        return abs(values["subtotal"] + values["tax_amount"] - values["total_amount"]) < .01
    return None


def independent_money(key, value, ocr_text, qr):
    expected = numeric(value)
    if expected is None:
        return False
    if qr and numeric(qr.get(key)) == expected:
        return True
    label = LABELS.get(key)
    if not label:
        return False
    for line in ocr_text.splitlines():
        if re.search(label, line, re.I):
            numbers = re.findall(r"(?<!\w)-?\d[\d,]*(?:\.\d+)?", line)
            if any(numeric(n) == expected for n in numbers):
                return True
    return False


def apply(data, type_key, chat, image_path, *, mode, ocr_text, context, confirmed_fields=None, native_path=None):
    from app.core import pipeline, page_layout
    fields = data["fields"]
    spec = doc_types.get(type_key)
    mapping = spec["common_fields"]
    protected = set(confirmed_fields or {})
    identity = {"invoice": ("seller_tax_id", "seller_name"), "receipt": ("tax_id", "vendor")}.get(type_key, ())
    identity = tuple(dict.fromkeys([*identity, *(a for k in ("voucher_id", "date") for a in mapping.get(k, []) if a in fields)]))
    missing = [k for k in identity if fields.get(k) in (None, "") and k not in protected]
    money = []
    if arithmetic(fields, mapping) is False:
        money = [a for k in LABELS for a in mapping.get(k, []) if a in fields and a not in protected]
    requested = list(dict.fromkeys(missing + money))
    if not requested:
        return
    roi_path = image_path
    if mode == "image":
        source = native_path or image_path
        region = {"box": [0, 0, 1000, 1000] if missing else [0, 400, 1000, 1000], "rotate": 0}
        try:
            roi_path = page_layout.crop(source, region, Path(image_path).with_name(Path(image_path).stem + "_supplement.png"), max_edge=2400)
        except (OSError, ValueError) as exc:
            data["validation_warnings"].append("補讀來源不可用：" + str(exc)[:120])
            return
        # Independent OCR is read from the native ROI, never from the model candidate.
        try:
            provider, _ = pipeline.settings_store.parse_setting(pipeline.settings_store.get_settings()["ocr"])
            ocr = pipeline.model_factory.get_ocr(provider)
            lines = ocr.recognize(roi_path)
            ocr_text = ocr.plain_text(lines)
            invocation_trace.add({"kind": "supplement_ocr", "lines": lines, "text": ocr_text})
        except Exception as exc:
            invocation_trace.add({"kind": "supplement_ocr_error", "error": str(exc)})
    try:
        values = pipeline.reread_fields(roi_path, type_key, chat, requested, mode=mode, ocr_text=ocr_text,
            hint="金額只讀合計區，區分明細金額與未稅合計；識別欄位讀文件本身名稱及統編", evidence=context)
    except (ValueError, RuntimeError) as exc:
        data["validation_warnings"].append("指定欄位補讀失敗：" + str(exc)[:120])
        return
    candidate = {**fields, **{k: v for k,v in values.items() if v not in (None, "")}}
    accepted = {}
    currency = fields.get("currency") or ("TWD" if type_key == "invoice" else None)
    qr = context.get("qr")
    for key, value in values.items():
        if key in protected or value in (None, ""):
            continue
        common_key = next((k for k in LABELS if key in mapping.get(k, [])), None)
        if common_key:
            allow = bool(currency) and independent_money(common_key, value, ocr_text, qr) and arithmetic(candidate, mapping) is not False
        else:
            allow = str(value).replace(" ", "") in ocr_text.replace(" ", "")
            if "tax_id" in key or key == "tax_id":
                allow = allow and len(pipeline._clean_taxid(value)) == 8
        data.setdefault("field_evidence", {}).setdefault("fields." + key, []).append({"source": "supplement", "value": value, "selected": False, "independent_evidence": allow})
        if allow:
            accepted[key] = value
        else:
            data["validation_warnings"].append(f"{key} 補讀候選缺乏獨立證據，待人工確認")
    if currency and re.search(r"USD|美[元金]|EUR|歐元|CNY|人民幣|JPY|日[元圓]", ocr_text, re.I) and currency == "TWD":
        accepted = {k: v for k,v in accepted.items() if k not in money}
        data["validation_warnings"].append("補讀金額幣別證據衝突，未採用")
    # Arithmetic must hold for the actual accepted subset, not only the candidate bundle.
    if money and arithmetic({**fields, **accepted}, mapping) is False:
        accepted = {k: v for k,v in accepted.items() if k not in money}
    pipeline._merge_evidence_fields(data, accepted, "supplement_verified")
    invocation_trace.add({"kind": "supplement_result", "requested": requested, "candidates": values, "adopted": accepted})
