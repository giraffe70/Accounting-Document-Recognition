"""One bounded reread; independent evidence gates every adopted candidate."""
from __future__ import annotations
import re
import math
from pathlib import Path
from app.core import doc_types, normalization, invocation_trace

LABELS = {"subtotal": r"未稅(?:銷售額|合計|金額)?|銷售額(?:[（(]未稅[）)])?|小計|\bsubtotal\b",
          "tax_amount": r"稅額(?:[（(]營業稅[）)])?|營業稅(?:額)?|\btax\b", "total_amount": r"含稅(?:合計|總額)?|總計(?:[（(]含稅[）)])?|總額|應付(?:金額)?|\btotal\b"}
CORE_MISSING = {"invoice": ("voucher_id", "date", "seller_name", "seller_tax_id", "total_amount"),
                "receipt": ("total_amount",)}
RECEIPT_PRINTED = {"date": r"收據日期|開立日期|日期", "vendor": r"廠商|收款人|店家|營業人"}
POLICY_VERSION = 2


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


def labeled_money_values(key, ocr_text):
    """Only an unambiguous value after a semantic amount label can conflict."""
    pattern = LABELS.get(key)
    if not pattern:
        return set()
    values = set()
    for line in ocr_text.splitlines():
        match = re.search(pattern, line, re.I)
        if not match:
            continue
        suffix = line[match.end():]
        next_label = re.search("|".join(LABELS.values()), suffix, re.I)
        if next_label:
            suffix = suffix[:next_label.start()]
        numbers = re.findall(r"(?<!\w)-?\d[\d,]*(?:\.\d+)?", suffix)
        if len(numbers) == 1 and numeric(numbers[0]) is not None:
            values.add(numeric(numbers[0]))
    return values


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
    protected.update(path.removeprefix("fields.") for path in confirmed_paths or [] if path.startswith("fields."))
    protected.update(path.removeprefix("fields.") for path, records in (data.get("field_evidence") or {}).items()
                     if path.startswith("fields.") and any(record.get("source") == "manual" and record.get("selected") for record in records))
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


def select_targets(data, type_key, text, context, protected):
    """A blank nullable field is not, by itself, a reason for a model call."""
    fields = data["fields"]
    spec = doc_types.get(type_key)
    mapping = spec["common_fields"]
    selected, skipped = [], {}
    for key in CORE_MISSING.get(type_key, ()):
        if key in fields and fields[key] in (None, ""):
            if key in protected:
                skipped[key] = "manual"
            else:
                selected.append(key)
    if type_key == "receipt":
        for key, pattern in RECEIPT_PRINTED.items():
            if key in fields and fields[key] in (None, ""):
                if key in protected:
                    skipped[key] = "manual"
                elif re.search(pattern, text):
                    selected.append(key)
                else:
                    skipped[key] = "no_printed_anchor"
    if type_key in ("invoice", "receipt") and "date" in fields and "date" not in protected and fields["date"] not in (None, ""):
        from datetime import date
        try:
            date.fromisoformat(normalization.norm_date(fields["date"]))
        except (TypeError, ValueError):
            selected.append("date")
            data.setdefault("validation_warnings", []).append("date 日期格式無效，待人工確認")
    if type_key == "invoice" and "voucher_id" in fields and "voucher_id" not in protected and fields["voucher_id"] not in (None, ""):
        if not re.fullmatch(r"[A-Z]{2}[- ]?\d{8}", str(fields["voucher_id"]).strip(), re.I):
            selected.append("voucher_id")
            data.setdefault("validation_warnings", []).append("voucher_id 發票號碼格式無效，待人工確認")
    from app.services.taxid import validate_tax_id
    if type_key in ("invoice", "receipt"):
        identity = ("seller_tax_id", "seller_name", "buyer_tax_id", "buyer_name") if type_key == "invoice" else ("tax_id", "vendor")
        for key in identity:
            value = fields.get(key)
            if key not in fields or key in protected or value in (None, ""):
                continue
            invalid = ("tax_id" in key and not validate_tax_id(str(value))) or (
                (key.endswith("name") or key == "vendor") and bool(re.fullmatch(r"\d{8}", str(value))))
            role_conflict = key.startswith(("buyer", "seller")) and re.search(r"買方|買受人|賣方|銷售人", text) and not party_evidence(key, value, text)
            if invalid or role_conflict:
                selected.append(key)
                data.setdefault("validation_warnings", []).append(f"{key} 欄位語意或識別格式無效，待人工確認")
    money = [a for k in LABELS for a in mapping.get(k, []) if a in fields and a not in protected]
    if arithmetic(fields, mapping) is False:
        selected.extend(money)
    selected.extend(key for key in money if fields[key] not in (None, "") and numeric(fields[key]) is None)
    for common in LABELS:
        for key in mapping.get(common, []):
            if key in fields and key not in protected and numeric(fields[key]) is not None:
                printed = labeled_money_values(common, text)
                if len(printed) == 1 and numeric(fields[key]) not in printed:
                    selected.append(key)
                    data.setdefault("validation_warnings", []).append(f"{key} 與原圖標示金額不同，待人工確認")
    # A missing optional subtotal or tax never starts a read. Invalid printed values can.
    groups = []
    confirmed = context.get("confirmed_paths") or []
    if type_key in ("invoice", "receipt") and spec.get("has_items") and not data.get("items") and \
            re.search(r"品名|項目|明細", text) and re.search(r"數量|單價|金額", text) and \
            re.search(r"\d", text) and not any(p == "items" or p.startswith("items[") for p in confirmed):
        groups.append("items")
    if type_key == "journal_voucher" and spec.get("has_entries") and not any(
            numeric(r.get("debit")) is not None or numeric(r.get("credit")) is not None for r in data.get("entries") or []) and \
            "借方" in text and "貸方" in text and re.search(r"\d", text) and not any(
            p == "entries" or p.startswith("entries[") for p in confirmed):
        groups.append("entries")
    for key, value in fields.items():
        if value in (None, "") and key not in selected and key not in skipped and key in (
                "tax_id", "voucher_id", "subtotal", "tax_amount", "withholding_tax", "buyer_tax_id", "buyer_name"):
            skipped[key] = "nullable_or_unproven"
    return list(dict.fromkeys(selected)), groups, skipped


def apply(data, type_key, chat, image_path, *, mode, ocr_text, context, confirmed_fields=None, native_path=None):
    from app.core import pipeline, page_layout, classification_evidence
    fields = data["fields"]
    mapping = doc_types.get(type_key)["common_fields"]
    protected = set(confirmed_fields or {})
    protected.update(path.removeprefix("fields.") for path in context.get("confirmed_paths", []) if path.startswith("fields."))
    protected.update(path.removeprefix("fields.") for path, records in (data.get("field_evidence") or {}).items()
                     if path.startswith("fields.") and any(record.get("source") == "manual" and record.get("selected") for record in records))
    requested, groups, skipped = select_targets(data, type_key, ocr_text, context, protected)
    money = [key for common in LABELS for key in mapping.get(common, []) if key in requested]
    conditions = context.get("image_conditions") or {}
    if conditions.get("low_text_contrast") or conditions.get("skew_degrees"):
        data.setdefault("validation_warnings", []).append("影像文字對比或傾斜需覆核；僅在目標文字難辨時評估增強")
    corrected_type, classification = classification_evidence.resolve(
        type_key, ocr_text, protected=context.get("confirmed_type", False) or bool(confirmed_fields))
    type_change = corrected_type != type_key
    invocation_trace.add({"kind": "supplement_decision", "requested": requested, "groups": groups,
                          "skipped": skipped, "type_change": type_change,
                          "quality_only": bool(conditions.get("low_text_contrast") or conditions.get("skew_degrees"))})
    if context.get("supplement_done") or not (requested or groups or type_change):
        return
    context["supplement_done"] = True
    if context.get("checkpoint_path"):
        from app.core import recognition_checkpoint
        recognition_checkpoint.mark_supplement(context["checkpoint_path"])
    invocation_trace.add({"kind":"supplement_start", "fields":requested, "groups":groups, "task_limit":1})
    roi_path = image_path
    region = {"box": [0, 0, 1000, 1000], "rotate": 0}
    roi_source = "full_document"
    ocr_requests = 0
    original_ocr_source = "first_pass"
    if mode == "image":
        from PIL import Image
        from app.core import supplement_roi, image_evidence, ocr_cache
        from time import perf_counter
        source = native_path or image_path
        try:
            with Image.open(image_path) as display, Image.open(source) as native:
                display_size, source_size = display.size, native.size
            same_frame = supplement_roi.same_visual_frame(image_path, source)
            first_lines = context.get("ocr_lines") or []
            fallback = "type_change" if type_change else "frame_mismatch" if not same_frame else None
            if not fallback:
                chosen, fallback = supplement_roi.choose(type_key, requested, groups, first_lines, display_size)
                if chosen:
                    region = chosen
                    roi_source = chosen["source"]
            if roi_source != "full_document":
                roi_path = page_layout.crop(source, region, Path(image_path).with_name(Path(image_path).stem + "_supplement.png"), max_edge=2400)
            else:
                roi_path = str(source)
            with Image.open(roi_path) as roi_image:
                roi_size = roi_image.size
            crop_pixels = [round(region["box"][i] * source_size[i % 2] / 1000) for i in range(4)]
            xscale = roi_size[0] / (crop_pixels[2] - crop_pixels[0])
            yscale = roi_size[1] / (crop_pixels[3] - crop_pixels[1])
            context["supplement_region"] = region["box"]
            invocation_trace.add({"kind": "supplement_region", "source": roi_source, "fallback": fallback,
                                  "source_path": str(source), "source_pixels": ocr_cache.pixels(source)["pixel_sha256"],
                                  "box": region["box"], "source_size": source_size, "input_size": roi_size,
                                  "crop_pixels": crop_pixels,
                                  "source_to_input": [[xscale, 0, -crop_pixels[0] * xscale],
                                                      [0, yscale, -crop_pixels[1] * yscale]],
                                  "coordinate_frame": "document_normalized_1000", "rotate": 0})
        except (OSError, ValueError) as exc:
            failed(data, context, exc)
            return
        lines = supplement_roi.map_existing(first_lines, display_size, region, roi_size) if same_frame else []
        mapped_lines = lines
        context["supplement_ocr_lines"] = lines
        context["supplement_prompt_lines"] = lines
        context["supplement_ocr_size"] = list(roi_size)
        context["supplement_ocr_source"] = "mapped_first_pass"
        context["supplement_prompt_source"] = "mapped_first_pass"
        original_ocr_source = "mapped_first_pass" if lines else "none"
        target_ocr_text = "\n".join(line.get("text", "") for line in lines)
        need, need_reason = supplement_roi.needs_fresh_ocr(type_key, requested, groups, lines, region, source_size=source_size)
        if roi_source == "full_document" and same_frame:
            need, need_reason = False, "reuse_full_document"
        elif not same_frame:
            need, need_reason = True, "frame_mismatch_recompute"
        local_info = None
        if roi_source != "full_document" and not need and lines:
            _, local_info = image_evidence.enhancement(roi_path, lines, None)
            if (local_info["low_text_contrast"] or local_info["skew_degrees"]) and need_reason != "existing_ocr_readable":
                need, need_reason = True, "target_quality"
        if not context.get("ocr_enabled", True):
            need, need_reason = False, "ocr_disabled"
        if not lines and not need and not context.get("qr"):
            invocation_trace.add({"kind": "supplement_skip", "reason": "no_readable_original_evidence"})
            data.setdefault("validation_warnings", []).append("補讀目標缺乏可讀原圖證據，保留缺值待人工覆核")
            return
        invocation_trace.add({"kind": "supplement_ocr_decision", "source": original_ocr_source,
                              "mapped_lines": len(lines), "fresh": need, "reason": need_reason})
        if need:
            try:
                provider, _ = pipeline.settings_store.parse_setting(pipeline.settings_store.get_settings()["ocr"])
                ocr = pipeline.model_factory.get_ocr(provider)
                started = perf_counter()
                ocr_requests += 1
                with ocr_cache.purpose("supplement_original"):
                    raw_lines = ocr.recognize(roi_path)
                raw_text = ocr.plain_text(raw_lines)
                invocation_trace.add({"kind": "supplement_ocr", "source": "native_roi_ocr",
                                      "seconds": round(perf_counter() - started, 6), "lines": raw_lines, "text": raw_text})
                if raw_lines:
                    lines = raw_lines
                    original_ocr_source = "native_roi_ocr"
                    context["supplement_ocr_lines"] = lines
                    context["supplement_prompt_lines"] = lines
                    context["supplement_ocr_source"] = original_ocr_source
                    context["supplement_prompt_source"] = original_ocr_source
                    target_ocr_text = raw_text
                _, raw_reason = supplement_roi.needs_fresh_ocr(type_key, requested, groups, lines, region, source_size=source_size)
                if raw_reason != "existing_ocr_readable":
                    enhanced, info = image_evidence.enhancement(
                        roi_path, lines or mapped_lines,
                        Path(roi_path).with_name(Path(roi_path).stem + "_enhanced.png"))
                    if enhanced:
                        _, after = image_evidence.enhancement(enhanced, lines, None)
                        before_contrast, after_contrast = info.get("text_contrast"), after.get("text_contrast")
                        improves = (before_contrast is not None and after_contrast is not None and
                                    after_contrast >= before_contrast * 1.1 + 5) or abs(info.get("skew_degrees") or 0) >= 1
                        info["measured_improvement"] = bool(improves)
                        if improves:
                            started = perf_counter()
                            ocr_requests += 1
                            with ocr_cache.purpose("supplement_enhanced"):
                                extra_lines = ocr.recognize(enhanced)
                            extra_text = ocr.plain_text(extra_lines)
                            invocation_trace.add({"kind": "enhanced_ocr", "seconds": round(perf_counter() - started, 6),
                                                  "lines": extra_lines, "text": extra_text})
                            context["enhanced_ocr_text"] = extra_text
                            context["supplement_prompt_lines"] = extra_lines
                            context["supplement_prompt_source"] = "enhanced_copy"
                            context["supplement_enhance_matrix"] = info["matrix"]
                            if extra_text.strip() != target_ocr_text.strip():
                                context["ocr_copy_conflict"] = True
                                data["validation_warnings"].append("OCR 原圖與增強副本不同，候選待人工確認")
                            roi_path = enhanced
                            if type_key == "invoice":
                                from app.core import barcode
                                qr_info = barcode.decode_diagnostics(enhanced)
                                invocation_trace.add({"kind": "qr_enhanced", **qr_info})
                                if qr_info["status"] == "valid":
                                    candidate_qr = qr_info["candidates"][0]
                                    if not context.get("qr"):
                                        context["qr"] = candidate_qr
                                    elif candidate_qr != context["qr"]:
                                        context["qr_copy_conflict"] = True
                                        data["validation_warnings"].append("QR 原圖與增強副本內容衝突，候選待人工確認")
                                elif qr_info["status"] == "conflict":
                                    context["qr_copy_conflict"] = True
                    invocation_trace.add({"kind": "image_enhancement", **info})
            except Exception as exc:
                invocation_trace.add({"kind": "supplement_ocr_error", "error": str(exc)})
        ocr_text = target_ocr_text + "\n" + spatial_text(context["supplement_ocr_lines"])
        invocation_trace.add({"kind": "supplement_evidence", "source": original_ocr_source,
                              "ocr_requests": ocr_requests, "original_lines": len(context["supplement_ocr_lines"]),
                              "prompt_lines": len(context["supplement_prompt_lines"])})
        if not context["supplement_ocr_lines"] and not context.get("qr"):
            invocation_trace.add({"kind": "supplement_skip", "reason": "no_readable_original_evidence"})
            data.setdefault("validation_warnings", []).append("補讀目標缺乏可讀原圖證據，保留缺值待人工覆核")
            return
    full = bool(groups or type_change)
    try:
        if full:
            result = pipeline.reread_document(roi_path,corrected_type,chat,mode=mode,ocr_text=ocr_text,
                    hint="依文件本身正確類型讀取完整欄位及表格，不推算缺值",evidence=context)
            values = result["fields"] if type_change or roi_source == "full_document" else {
                key: value for key, value in result["fields"].items() if key in requested}
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
    rejected = {}
    for key, value in values.items():
        if key in protected or value in (None, "") or not full and key not in requested:
            rejected[key] = "manual" if key in protected else "empty_candidate" if value in (None, "") else "outside_target"
            continue
        common_key = next((k for k in LABELS if key in mapping.get(k, [])), None)
        note = normalization.amount_annotation(value)
        if common_key and note:
            if note["currency"] and note["currency"] != currency:
                rejected[key] = "currency_mismatch"
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
            rejected[key] = "copy_conflict" if context.get("ocr_copy_conflict") or context.get("qr_copy_conflict") else "independent_evidence_missing"
            data["validation_warnings"].append(f"{key} 補讀候選缺乏獨立證據，待人工確認")
    if currency and re.search(r"USD|美[元金]|EUR|歐元|CNY|人民幣|JPY|日[元圓]", ocr_text, re.I) and currency == "TWD":
        rejected.update({key: "currency_conflict" for key in accepted if key in money})
        accepted = {k: v for k,v in accepted.items() if k not in money}
        data["validation_warnings"].append("補讀金額幣別證據衝突，未採用")
    # Arithmetic must hold for the actual accepted subset, not only the candidate bundle.
    if money and arithmetic({**fields, **accepted}, mapping) is False:
        rejected.update({key: "arithmetic_conflict" for key in accepted if key in money})
        accepted = {k: v for k,v in accepted.items() if k not in money}
    pipeline._merge_evidence_fields(data, accepted, "supplement_verified")
    if mode == "image" and result.get("boxes"):
        from app.core import supplement_roi, geometry
        for key in accepted:
            if key in result["boxes"]:
                try:
                    data.setdefault("boxes", {})[key] = geometry.validate_box(
                        supplement_roi.box_to_document(result["boxes"][key], region,
                            image_size=roi_size if mode == "image" else None,
                            enhancement_matrix=context.get("supplement_enhance_matrix")))
                except (TypeError, ValueError):
                    rejected[key + ".box"] = "invalid_roi_coordinate"
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
            rejected[group] = "independent_row_evidence_missing"
            data["validation_warnings"].append(f"{group} 補讀候選缺乏獨立欄位／位置證據，待人工確認")
    invocation_trace.add({"kind": "supplement_result", "requested": requested, "candidates": values,
                          "adopted": accepted, "rejected": rejected, "ocr_requests": ocr_requests,
                          "region_source": roi_source})


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
