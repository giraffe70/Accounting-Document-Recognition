"""One bounded reread; independent evidence gates every adopted candidate."""
from __future__ import annotations
import re
import math
from pathlib import Path
from app.core import doc_types, normalization, invocation_trace

LABELS = {"subtotal": r"未稅(?:銷售額|合計|金額)?|銷售額(?:[（(]未稅[）)])?|小計|\bsubtotal\b",
          "tax_amount": r"稅額(?:[（(]營業稅[）)])?|營業稅(?:額)?|\btax\b", "total_amount": r"含稅(?:合計|總額)?|總計(?:[（(]含稅[）)])?|總額|應付(?:金額)?|\btotal\b"}


def spatial_text(lines):
    """Join only OCR boxes on the same baseline, within this native document."""
    rows = []
    for line in sorted(lines or [], key=lambda r: sum(p[1] for p in r["box"])/len(r["box"])):
        box = line["box"]
        center = sum(p[1] for p in box)/len(box)
        height = max(p[1] for p in box)-min(p[1] for p in box)
        if rows and abs(center-rows[-1][0]) <= max(2,min(height, rows[-1][1])*.4):
            rows[-1][2].append(line)
        else:
            rows.append((center,height,[line]))
    return "\n".join(" ".join(r.get("text", "") for r in sorted(group,key=lambda r:min(p[0] for p in r["box"]))) for _,_,group in rows)


def numeric(value):
    parsed = normalization.norm_amount(value)
    return parsed if isinstance(parsed, (int, float)) and not isinstance(parsed, bool) and math.isfinite(parsed) else None


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
        match = re.search(label, line, re.I)
        if match:
            segment = line[match.end():]
            next_label = re.search("|".join(LABELS.values()), segment, re.I)
            if next_label:
                segment = segment[:next_label.start()]
            numbers = re.findall(r"(?<!\w)-?\d[\d,]*(?:\.\d+)?", segment)
            if len(numbers) == 1 and numeric(numbers[0]) == expected:
                return True
    return False


def party_evidence(key, value, text):
    from app.services.taxid import validate_tax_id
    value = str(value).strip()
    if "tax_id" in key and not validate_tax_id(value):
        return False
    if (key.endswith("name") or key == "vendor") and re.fullmatch(r"\d{8}", value):
        return False
    party = r"買方|買受人|客戶" if key.startswith("buyer") else r"賣方|銷售人|營業人|店家|廠商"
    return any(re.search(party, line) and value.replace(" ", "") in line.replace(" ", "") for line in text.splitlines())


def normalize_candidates(data, type_key, ocr_text, qr=None, confirmed_fields=None, confirmed_paths=None):
    """Annotation parsing is a proposed change, independently checked before use."""
    from app.core import pipeline
    fields = data["fields"]
    mapping = doc_types.get(type_key)["common_fields"]
    protected = set(confirmed_fields or {})
    currency = normalization.normalize_currency(fields.get("currency")) or ("TWD" if type_key == "invoice" else None)
    proposed = {}
    records = []
    for key, raw in fields.items():
        note = normalization.amount_annotation(raw)
        common = next((k for k in LABELS if key in mapping.get(k, [])), None)
        if note and common:
            allowed = key not in protected and bool(currency) and (not note["currency"] or note["currency"] == currency) and independent_money(common, note["value"], ocr_text, qr)
            record = {"source": "normalization_candidate", **note, "selected": False, "independent_evidence": allowed}
            data.setdefault("field_evidence", {}).setdefault("fields."+key, []).append(record)
            records.append((key, record))
            if allowed:
                proposed[key] = note["value"]
    if arithmetic({**fields, **proposed}, mapping) is False or (currency == "TWD" and re.search(r"USD|美[元金]|EUR|歐元|CNY|人民幣|JPY|日[元圓]", ocr_text, re.I)):
        proposed = {}
    pipeline._merge_evidence_fields(data, proposed, "normalization_verified")
    for index, item in enumerate(data.get("items") or []):
        note = normalization.amount_annotation(item.get("amount"))
        if not note:
            continue
        description = str(item.get("description") or "").strip()
        verified = bool(description) and any(description in line and str(note["value"]) in line.replace(",", "") for line in ocr_text.splitlines())
        verified = verified and bool(currency) and (not note["currency"] or note["currency"] == currency)
        quantity, price = numeric(item.get("quantity")), numeric(item.get("unit_price"))
        if quantity is not None and price is not None and abs(quantity*price-note["value"]) >= .01:
            verified = False
        path = f"items[{index}].amount"
        if path in (confirmed_paths or []) or "items" in (confirmed_paths or []) or f"items[{index}]" in (confirmed_paths or []):
            verified = False
        data.setdefault("field_evidence", {}).setdefault(path, []).append({"source": "normalization_candidate", **note, "selected": False, "independent_evidence": verified})
        if verified:
            for record in data["field_evidence"][path]:
                record["selected"] = False
            data["field_evidence"][path].append({"source":"normalization_verified", "value":note["value"], "selected":True})
            item["amount"] = note["value"]


def failed(data, context, exc):
    code = getattr(exc, "code", None) or ("context_limit" if "exceed" in str(exc).lower() and "context" in str(exc).lower() else
            "timeout" if isinstance(exc, TimeoutError) else "supplement_error")
    issue = {"code":"supplement_failed", "stage":"supplement", "reason":code,
             "error":f"{type(exc).__name__}: {exc}", "details":getattr(exc,"details",{})}
    context.setdefault("recognition_diagnostics", {"version":1,"requests":[],"issues":[]}).setdefault("issues",[]).append(issue)
    data["recognition_diagnostics"] = context["recognition_diagnostics"]
    data.setdefault("validation_warnings",[]).append("指定欄位補讀失敗，保留首讀結果：" + str(exc)[:120])
    invocation_trace.add({"kind":"supplement_failure", **issue})


def apply(data, type_key, chat, image_path, *, mode, ocr_text, context, confirmed_fields=None, native_path=None):
    from app.core import pipeline, page_layout
    fields = data["fields"]
    spec = doc_types.get(type_key)
    mapping = spec["common_fields"]
    protected = set(confirmed_fields or {})
    identity = {"invoice": ("seller_tax_id", "seller_name"), "receipt": ("tax_id", "vendor")}.get(type_key, ())
    identity = tuple(dict.fromkeys([*identity, *(a for k in ("voucher_id", "date") for a in mapping.get(k, []) if a in fields)]))
    from app.services.taxid import validate_tax_id
    identity = tuple(dict.fromkeys([*identity, *(k for k in fields if k in ("buyer_tax_id", "buyer_name") and
        (fields.get(k) not in (None, "") or re.fullmatch(r"\d{8}", str(fields.get("buyer_name") or ""))))]))
    missing = [k for k in identity if k not in protected and (fields.get(k) in (None, "") or
               ("tax_id" in k and not validate_tax_id(str(fields.get(k) or ""))) or
               ((k.endswith("name") or k == "vendor") and re.fullmatch(r"\d{8}", str(fields.get(k) or ""))))]
    # Labeled OCR disagreement is an evidence conflict even when raw values are nonempty.
    missing.extend(k for k in identity if k not in protected and fields.get(k) not in (None, "") and
                   (k.startswith(("buyer", "seller"))) and re.search(r"買方|買受人|賣方|銷售人", ocr_text) and not party_evidence(k, fields[k], ocr_text))
    for key in missing:
        if fields.get(key) not in (None, ""):
            data["validation_warnings"].append(f"{key} 欄位語意或識別格式無效，待人工確認")
    money = []
    if arithmetic(fields, mapping) is False or any(numeric(fields.get(a)) is None for a in mapping.get("total_amount", [])):
        money = [a for k in LABELS for a in mapping.get(k, []) if a in fields and a not in protected]
    money.extend(a for k in LABELS for a in mapping.get(k, []) if a in fields and a not in protected and fields[a] not in (None, "") and numeric(fields[a]) is None)
    conditions = context.get("image_conditions") or {}
    if conditions.get("low_text_contrast") or conditions.get("skew_degrees"):
        money.extend(a for k in LABELS for a in mapping.get(k, []) if a in fields and a not in protected)
        missing.extend(k for k in identity if k not in protected)
    requested = list(dict.fromkeys(missing + money))
    groups = []
    if spec.get("has_entries") and not any(numeric(r.get("debit")) is not None or numeric(r.get("credit")) is not None for r in data.get("entries") or []):
        groups.append("entries")
    if spec.get("has_items") and not data.get("items") and re.search(r"品名|明細|數量.*單價", ocr_text):
        groups.append("items")
    groups = [group for group in groups if not any(p.startswith(group + "[") or p == group for p in context.get("confirmed_paths", []))]
    if not requested and not groups or context.get("supplement_done"):
        return
    context["supplement_done"] = True
    if context.get("checkpoint_path"):
        from app.core import recognition_checkpoint
        recognition_checkpoint.mark_supplement(context["checkpoint_path"])
    invocation_trace.add({"kind":"supplement_start", "fields":requested, "groups":groups, "task_limit":1})
    roi_path = image_path
    if mode == "image":
        source = native_path or image_path
        region = {"box": [0, 0, 1000, 1000] if missing or groups else [0, 400, 1000, 1000], "rotate": 0}
        try:
            roi_path = page_layout.crop(source, region, Path(image_path).with_name(Path(image_path).stem + "_supplement.png"), max_edge=2400)
        except (OSError, ValueError) as exc:
            failed(data, context, exc)
            return
        # Independent OCR is read from the native ROI, never from the model candidate.
        try:
            if not context.get("ocr_enabled", True):
                raise RuntimeError("VLM direct mode leaves OCR disabled")
            provider, _ = pipeline.settings_store.parse_setting(pipeline.settings_store.get_settings()["ocr"])
            ocr = pipeline.model_factory.get_ocr(provider)
            lines = ocr.recognize(roi_path)
            ocr_text = ocr.plain_text(lines)
            context["supplement_ocr_lines"] = lines
            from PIL import Image
            with Image.open(roi_path) as image:
                context["supplement_ocr_size"] = list(image.size)
            from app.core import image_evidence, barcode
            enhanced, info = image_evidence.enhancement(roi_path, lines, Path(roi_path).with_name(Path(roi_path).stem + "_enhanced.png"))
            invocation_trace.add({"kind":"image_enhancement", **info})
            if enhanced:
                extra_lines = ocr.recognize(enhanced)
                extra_text = ocr.plain_text(extra_lines)
                invocation_trace.add({"kind":"enhanced_ocr", "lines":extra_lines, "text":extra_text})
                context["enhanced_ocr_text"] = extra_text
                context["supplement_prompt_lines"] = extra_lines
                # Conflicting OCR evidence remains separate; it cannot justify replacement.
                if extra_text.strip() != ocr_text.strip():
                    context["ocr_copy_conflict"] = True
                    data["validation_warnings"].append("OCR 原圖與增強副本不同，候選待人工確認")
                roi_path = enhanced
                if type_key == "invoice":
                    qr_info = barcode.decode_diagnostics(enhanced)
                    invocation_trace.add({"kind":"qr_enhanced", **qr_info})
                    if qr_info["status"] == "valid":
                        candidate_qr = qr_info["candidates"][0]
                        if not context.get("qr"):
                            context["qr"] = candidate_qr
                        elif candidate_qr != context["qr"]:
                            context["qr_copy_conflict"] = True
                            data["validation_warnings"].append("QR 原圖與增強副本內容衝突，候選待人工確認")
                    elif qr_info["status"] == "conflict":
                        context["qr_copy_conflict"] = True
            invocation_trace.add({"kind": "supplement_ocr", "lines": lines, "text": ocr_text})
            ocr_text += "\n" + spatial_text(lines)
        except Exception as exc:
            invocation_trace.add({"kind": "supplement_ocr_error", "error": str(exc)})
    from app.core import classification_evidence
    corrected_type, classification = classification_evidence.resolve(type_key, ocr_text, protected=context.get("confirmed_type", False) or bool(confirmed_fields))
    full = bool(groups or corrected_type != type_key)
    try:
        if full:
            result = pipeline.reread_document(roi_path,corrected_type,chat,mode=mode,ocr_text=ocr_text,
                    hint="依文件本身正確類型讀取完整欄位及表格，不推算缺值",evidence=context)
            values = result["fields"]
        else:
            result = {}
            values = pipeline.reread_fields(roi_path, type_key, chat, requested, mode=mode, ocr_text=ocr_text,
                hint="金額只讀合計區，區分明細金額與未稅合計；識別欄位讀文件本身名稱及統編", evidence=context)
    except Exception as exc:
        failed(data, context, exc)
        return
    if corrected_type != type_key:
        # Correct the contract before adopting fields; keep the previous AI data.
        from copy import deepcopy
        data["previous_contract_candidate"] = deepcopy(data)
        for evidence in data.get("field_evidence", {}).values():
            for record in evidence:
                record["selected"] = False
        data["fields"] = fields = {s["key"]:None for s in doc_types.get(corrected_type)["fields"]}
        mapping = doc_types.get(corrected_type)["common_fields"]
        context["effective_type"] = corrected_type
        context["classification_evidence"] = classification
        groups = [g for g,flag in (("items","has_items"),("entries","has_entries")) if doc_types.get(corrected_type).get(flag)]
        data["items"], data["entries"] = [], []
        data["doc_type"] = doc_types.get(corrected_type)["label"]
        data["boxes"] = {}
        invocation_trace.add({"kind":"supplement_classification",**classification})
    candidate = {**fields, **{k: v for k,v in values.items() if v not in (None, "")}}
    accepted = {}
    currency = fields.get("currency") or ("TWD" if corrected_type == "invoice" else None)
    qr = context.get("qr")
    for key, value in values.items():
        if key in protected or value in (None, "") or not full and key not in requested:
            continue
        common_key = next((k for k in LABELS if key in mapping.get(k, [])), None)
        note = normalization.amount_annotation(value)
        if common_key and note:
            if note["currency"] and note["currency"] != currency:
                continue
            value = note["value"]
            candidate[key] = value
        if common_key:
            allow = bool(currency) and independent_money(common_key, value, ocr_text, qr) and arithmetic(candidate, mapping) is not False
        else:
            allow = party_evidence(key, value, ocr_text) if key in ("buyer_name", "seller_name", "buyer_tax_id", "seller_tax_id", "tax_id", "vendor") else str(value).replace(" ", "") in ocr_text.replace(" ", "")
        if context.get("ocr_copy_conflict") or context.get("qr_copy_conflict"):
            allow = False
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
    for group in groups:
        candidates = result.get(group) or []
        accepted_rows = []
        for index, row in enumerate(candidates):
            allow = row_evidence(row, group, ocr_text, context.get("supplement_ocr_lines") or context.get("ocr_lines") or [])
            allow = allow and not context.get("ocr_copy_conflict") and not context.get("qr_copy_conflict")
            data.setdefault("field_evidence", {}).setdefault(f"{group}[{index}]", []).append({"source":"supplement", "value":row, "selected":allow, "independent_evidence":allow})
            if allow:
                accepted_rows.append(row)
        if candidates and len(accepted_rows)==len(candidates):
            data[group] = accepted_rows
        elif candidates:
            data["validation_warnings"].append(f"{group} 補讀候選缺乏獨立欄位／位置證據，待人工確認")
    invocation_trace.add({"kind": "supplement_result", "requested": requested, "candidates": values, "adopted": accepted})


def row_evidence(row, group, text, lines):
    """One printed row must corroborate identifiers, numbers and debit/credit side."""
    if not isinstance(row,dict):
        return False
    name = str(row.get("description") or row.get("account_name") or "").strip()
    code = str(row.get("account_code") or "").strip()
    identifier = code or name
    if not identifier:
        return False
    rows = spatial_text(lines).splitlines() if lines else text.splitlines()
    for printed in rows:
        if identifier.replace(" ","") not in printed.replace(" ",""):
            continue
        numeric_keys = ("debit","credit") if group=="entries" else ("quantity","unit_price","amount")
        values = [(k,numeric(row.get(k))) for k in numeric_keys if row.get(k) not in (None,"")]
        if not values or any(v is None for _,v in values):
            continue
        numbers = [numeric(n) for n in re.findall(r"(?<!\w)-?\d[\d,]*(?:\.\d+)?", printed)]
        if any(value not in numbers for _,value in values):
            continue
        if group=="items":
            q,p,a=numeric(row.get("quantity")),numeric(row.get("unit_price")),numeric(row.get("amount"))
            return q is None or p is None or a is None or abs(q*p-a)<.01
        # An amount alone does not tell us which column it belongs to.
        headers = {side:[r for r in lines if ("借方" if side=="debit" else "貸方") in r.get("text","")] for side in ("debit","credit")}
        if not all(headers.values()):
            return all(("借方" if side=="debit" else "貸方") in printed for side,_ in values)
        xmid=lambda line:sum(p[0] for p in line["box"])/len(line["box"])
        dx,cx=xmid(headers["debit"][0]),xmid(headers["credit"][0])
        id_lines=[r for r in lines if identifier in r.get("text","")]
        for side,value in values:
            amounts=[r for r in lines if numeric(r.get("text"))==value and any(abs(sum(p[1] for p in r["box"])/len(r["box"])-sum(p[1] for p in ident["box"])/len(ident["box"])) <= max(p[1] for p in ident["box"])-min(p[1] for p in ident["box"]) for ident in id_lines)]
            if not amounts or any((abs(xmid(r)-dx)<abs(xmid(r)-cx)) != (side=="debit") for r in amounts):
                return False
        return True
    return False
