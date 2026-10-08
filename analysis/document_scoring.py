"""Offline scoring for multi-document voucher exports.

The document scorer is deliberately independent of OCR and of gold authoring. It
matches reviewed document regions to predicted regions by maximum total IoU.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


def _csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _page_key(row: dict) -> tuple[str, int]:
    source = row.get("source") or row.get("relative_path") or row.get("source_path")
    page = row.get("page") or row.get("page_no")
    if not source or page in (None, ""):
        raise ValueError("Multi-document rows require source/page (or relative_path/page_no)")
    return str(source), int(page)


def _box(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        if "x1" in value:
            coords = [value.get("x1"), value.get("y1"), value.get("x2"), value.get("y2")]
        elif "left" in value:
            coords = [value.get("left"), value.get("top"), value.get("right"), value.get("bottom")]
        elif "x" in value and "width" in value:
            coords = [value["x"], value["y"], value["x"] + value["width"], value["y"] + value["height"]]
        else:
            return None
    elif isinstance(value, (list, tuple)):
        if len(value) == 4 and not isinstance(value[0], (list, tuple, dict)):
            coords = value
        elif value and all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in value):
            xs, ys = [p[0] for p in value], [p[1] for p in value]
            coords = [min(xs), min(ys), max(xs), max(ys)]
        else:
            return None
    else:
        return None
    try:
        x1, y1, x2, y2 = map(float, coords)
    except (TypeError, ValueError):
        return None
    if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
        return None
    return x1, y1, x2, y2


def _iou(a, b):
    if a is None or b is None:
        return 0.0
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union else 0.0


def _assignment(gold_boxes, predicted_boxes, threshold):
    """Return maximum-total-IoU qualifying pairs using a bitmask DP."""
    # Process predictions one at a time; unmatched is always an allowed choice.
    states = {0: (0.0, ())}
    for pred_index, pred in enumerate(predicted_boxes):
        next_states = dict(states)
        for mask, (score, pairs) in states.items():
            for gold_index, gold in enumerate(gold_boxes):
                overlap = _iou(gold, pred)
                if overlap < threshold or mask & (1 << gold_index):
                    continue
                new_mask = mask | (1 << gold_index)
                candidate = (score + overlap, pairs + ((gold_index, pred_index),))
                incumbent = next_states.get(new_mask)
                if incumbent is None or candidate[0] > incumbent[0]:
                    next_states[new_mask] = candidate
        states = next_states
    # Total overlap is the objective; pair count breaks equal-score ties.
    return max(states.values(), key=lambda item: (item[0], len(item[1])))[1]


def _is_reviewed(row):
    confidence = row.get("confidence")
    return confidence in (None, "", "high", "medium")


def _load_run(path: Path):
    vouchers = _csv(path / "vouchers.csv")
    fields = defaultdict(dict)
    fields_path = path / "fields.csv"
    if fields_path.exists():
        for row in _csv(fields_path):
            result_id = row.get("result_id") or row.get("page_result_id") or row.get("parent_result_id")
            if result_id:
                fields[result_id][row.get("field", "")] = row.get("value", "")
    item_rows = defaultdict(list)
    items_path = path / "items.csv"
    if items_path.exists():
        for row in _csv(items_path):
            item_rows[row.get("result_id", "")].append({key: row.get(key, "")
                                                          for key in ("description", "quantity", "unit_price", "amount")})
    pages_path = path / "pages.csv"
    pages = _csv(pages_path) if pages_path.exists() else []
    page_meta = {}
    for row in pages:
        key = _page_key(row)
        if key in page_meta:
            raise ValueError(f"Duplicate page metadata: {path.name} {key}")
        page_meta[key] = row
    page_by_result = {row.get("result_id"): key for key, row in page_meta.items()
                      if row.get("result_id")}
    docs = defaultdict(list)
    failed_docs = Counter()
    for row in vouchers:
        if row.get("source") or row.get("relative_path") or row.get("source_path"):
            key = _page_key(row)
        else:
            parent_id = row.get("parent_result_id")
            if not parent_id or parent_id not in page_by_result:
                raise ValueError(f"Cannot map voucher parent_result_id to a page in {path.name}")
            key = page_by_result[parent_id]
        status = (row.get("status") or "").strip().lower()
        if status in {"failed", "timeout", "error", "skipped", "cancelled", "canceled"}:
            failed_docs[key] += 1
            continue
        identifier = row.get("result_id") or row.get("document_id") or row.get("page_result_id")
        if not identifier:
            raise ValueError("Voucher rows require result_id, document_id, or page_result_id")
        linked_fields = {}
        for linked_id in (row.get("parent_result_id"), row.get("page_result_id"),
                          row.get("document_id"), row.get("result_id")):
            if linked_id:
                linked_fields.update(fields.get(linked_id, {}))
        record = {**linked_fields, **row}
        origin = row.get("origin_json") or "{}"
        try:
            origin = json.loads(origin) if isinstance(origin, str) else origin
        except json.JSONDecodeError:
            origin = {}
        region = row.get("source_region") or row.get("region") or (
            (origin.get("region") or origin.get("source_region"))
            if isinstance(origin, dict) else None)
        record["_box"] = _box(region)
        record["_id"] = identifier
        record["items"] = item_rows.get(identifier, [])
        docs[key].append(record)
    # The page manifest is authoritative for failed/missing recognition pages.
    for key in page_meta:
        docs.setdefault(key, [])
    for key, page_docs in docs.items():
        if key not in page_meta:
            page_meta[key] = {}
        if not page_meta[key].get("image_sha256") and not page_meta[key].get("sha256"):
            page_meta[key]["image_sha256"] = next((doc.get("sha256") for doc in page_docs
                                                    if doc.get("sha256")), "")
    for key, count in failed_docs.items():
        page_meta.setdefault(key, {})["failed_prediction_rows"] = count
    for key, meta in page_meta.items():
        if (meta.get("status") or "").strip().lower() in {"failed", "timeout", "error", "skipped",
                                                          "cancelled", "canceled"}:
            failed_docs[key] += len(docs.get(key, ()))
            docs[key] = []
            meta["failed_prediction_rows"] = failed_docs[key]
    return docs, page_meta, bool(pages_path.exists())


def _gold_documents(gold_page):
    documents = gold_page.get("documents")
    if documents is None:
        # A legacy page annotation can safely represent one document only.
        return [dict(gold_page)] if any(gold_page.get(k) not in (None, "") for k in
                                       ("doc_type_key", "total_amount", "date", "voucher_id")) else []
    output = []
    for document in documents:
        origin = document.get("origin") or {}
        region = origin.get("region") or document.get("source_region")
        output.append({**document, "_box": _box(region)})
    return output


def _gold_item_amounts(document):
    items = document.get("items") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            return None
    if not items or any(item.get("amount") in (None, "") for item in items):
        return None
    return sorted(_amount(item["amount"]) for item in items)


def _amount(value):
    try:
        return f"{round(float(str(value).replace(',', '').replace('$', '').strip()), 2):.2f}"
    except (TypeError, ValueError):
        return str(value).strip()


_FIELD_KEYS = ("doc_type_key", "date", "voucher_id", "subtotal", "tax_amount", "total_amount")
_TYPE_VALUES = {
    "統一發票／電子發票": "invoice", "支票": "cheque", "匯款單／銀行轉帳水單": "remittance",
    "海關進口貨物稅費繳納證明": "customs_tax", "出口報單": "export_declaration",
    "進口報單": "import_declaration", "報關行收費通知單": "broker_note",
    "進出口報單（未分進出口）": "declaration", "稅款繳款書": "tax_bill",
    "水電費／公用事業帳單": "utility", "收據／雜項開銷": "receipt",
    "對帳單／應收帳款明細表": "statement", "訂購單／採購單": "purchase_order",
    "食品輸入許可通知": "fda_import", "勞健保費繳款／繳費證明": "social_insurance",
    "帳單費用明細（附頁佐證）": "bill_detail", "會計傳票（記帳憑證）": "journal_voucher",
    "其他憑證": "other",
}


def _gold_fields(document):
    nested = document.get("fields") if isinstance(document.get("fields"), dict) else {}
    keys = set(nested)
    keys.update(field for field in _FIELD_KEYS if field in document)
    # Include any explicitly reviewed top-level field while excluding metadata.
    excluded = {"source", "page", "confidence", "remarks", "origin", "origin_json",
                "source_region", "documents", "fields", "items", "entries", "page_role",
                "document_id", "parent_result_id", "page_result_id", "result_id", "status",
                "quality_json", "sha256", "image_sha256"}
    keys.update(key for key in document if key not in excluded and not key.startswith("_")
                and not isinstance(document[key], (dict, list)))
    return {key: nested[key] if key in nested else document[key]
            for key in keys if key in nested or key in document}


def _prediction_value(document, field):
    if field == "doc_type_key":
        value = document.get("doc_type_key")
        if value in (None, ""):
            value = document.get("doc_type", "")
        return _TYPE_VALUES.get(value, value)
    return document.get(field, "")


def _normalized(field, value):
    if value is None:
        return ""
    value = str(value).strip()
    if not value:
        return ""
    if field in {"amount", "subtotal", "tax_amount", "total_amount"}:
        return _amount(value)
    if field in {"voucher_id", "seller_tax_id", "buyer_tax_id"}:
        import re
        return re.sub(r"[^A-Z0-9]", "", value.upper())
    if field == "date":
        from app.core.normalization import norm_date
        return norm_date(value)
    return "".join(value.split())


def score_documents(run_paths, gold_path, iou_threshold=0.5):
    """Score against every reviewed gold document, including unlisted pages."""
    if not 0 < iou_threshold <= 1:
        raise ValueError("IoU threshold must be in (0, 1]")
    gold_rows = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    if not isinstance(gold_rows, list):
        raise ValueError("Gold must be a JSON list")
    gold = {}
    for row in gold_rows:
        key = _page_key(row)
        if key in gold:
            raise ValueError(f"Duplicate gold {key}")
        gold[key] = row
    runs = {name: _load_run(Path(path)) for name, path in run_paths.items()}
    if len(runs) != 2:
        raise ValueError("Exactly two named runs are required")
    raw_pages = set.union(*(set(run[0]) for run in runs.values()))
    raw_common = set.intersection(*(set(run[0]) for run in runs.values()))
    reviewed_gold = {key for key, row in gold.items()
                     if _is_reviewed(row) and any(_is_reviewed(doc) for doc in _gold_documents(row))}
    hashes = {}
    for name, (_, metadata, _) in runs.items():
        for key, row in metadata.items():
            digest = row.get("image_sha256") or row.get("sha256")
            if digest:
                hashes[(name, key)] = digest.lower()
    by_digest = defaultdict(list)
    for (_, key), digest in hashes.items():
        by_digest[digest].append(key)
    duplicate_groups = []
    scored_pages = raw_pages | reviewed_gold
    for digest, keys in by_digest.items():
        group = sorted(set(keys))
        if len(group) < 2:
            continue
        fingerprints = []
        for key in group:
            row = gold.get(key)
            if row is None:
                fingerprints.append(None)
                continue
            normalized_gold = {k: v for k, v in row.items()
                               if k not in ("source", "page", "remarks", "documents")}
            if isinstance(row.get("documents"), list):
                normalized_gold["documents"] = sorted(
                    row["documents"], key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
            fingerprints.append(json.dumps(normalized_gold, sort_keys=True, ensure_ascii=False))
        if len(set(fingerprints)) > 1:
            raise ValueError(f"Conflicting gold in duplicate image group: {group}")
        duplicate_groups.append(group)
        scored_pages.difference_update(group[1:])

    totals = {name: Counter() for name in runs}
    field_totals = {name: defaultdict(Counter) for name in runs}
    item_totals = {name: Counter() for name in runs}
    reviewed_gold_documents = 0
    page_details = []
    for key in sorted(scored_pages):
        truth_page = gold.get(key, {})
        expected = [doc for doc in _gold_documents(truth_page)
                    if _is_reviewed(doc) and _is_reviewed(truth_page)]
        page_reviewed = key in gold and _is_reviewed(truth_page) and (
            "documents" in truth_page or bool(expected))
        if page_reviewed:
            reviewed_gold_documents += len(expected)
        if not page_reviewed:
            continue
        for name, (predictions, metadata, _) in runs.items():
            predicted = predictions.get(key, [])
            has_boxes = all(doc.get("_box") is not None for doc in expected)
            annotation_insufficient = bool(expected and predicted and not has_boxes
                                           and not (len(expected) == 1 and len(predicted) == 1))
            if annotation_insufficient:
                pairs = ()
                totals[name]["unscorable_gold_count"] += len(expected)
            elif expected and not has_boxes and len(predicted) == 1:
                pairs = ((0, 0),)
            else:
                pairs = _assignment([doc.get("_box") for doc in expected],
                                    [doc.get("_box") for doc in predicted], iou_threshold)
            if not annotation_insufficient:
                tp, fp, fn = len(pairs), len(predicted) - len(pairs), len(expected) - len(pairs)
                totals[name].update(tp=tp, fp=fp, fn=fn)
            else:
                tp = fp = fn = None
            matched = dict(pairs)
            for gold_i, gold_doc in enumerate(expected):
                fields = _gold_fields(gold_doc)
                for field, value in fields.items():
                    if annotation_insufficient:
                        field_totals[name][field]["unscorable"] += 1
                    else:
                        prediction = predicted[matched[gold_i]] if gold_i in matched else {}
                        correct = (gold_i in matched and
                                   _normalized(field, _prediction_value(prediction, field)) ==
                                   _normalized(field, value))
                        field_totals[name][field]["correct" if correct else "incorrect"] += 1
                expected_amounts = _gold_item_amounts(gold_doc)
                if expected_amounts is not None:
                    if annotation_insufficient:
                        item_totals[name]["unscorable"] += 1
                    else:
                        actual = _gold_item_amounts(predicted[matched[gold_i]]) if gold_i in matched else None
                        item_totals[name]["correct" if actual == expected_amounts else "incorrect"] += 1
            page_details.append({"source": key[0], "page": key[1], "run": name,
                                 "page_status": metadata.get(key, {}).get("status", ""),
                                 "complete_seconds": metadata.get(key, {}).get("complete_seconds", ""),
                                 "failed_prediction_rows": metadata.get(key, {}).get("failed_prediction_rows", 0),
                                 "gold_documents": len(expected), "predicted_documents": len(predicted),
                                 "annotation_coverage_insufficient": annotation_insufficient,
                                 "matches": [list(pair) for pair in pairs],
                                 "true_positive": tp, "false_positive": fp, "false_negative": fn})

    metrics = {}
    for name, counts in totals.items():
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        metrics[name] = {"true_positive": tp, "false_positive": fp, "false_negative": fn,
                         "reviewed_gold_count": reviewed_gold_documents,
                         "scorable_gold_count": reviewed_gold_documents - counts["unscorable_gold_count"],
                         "unscorable_gold_count": counts["unscorable_gold_count"],
                         "precision": tp / (tp + fp) if tp + fp else None,
                         "recall": tp / (tp + fn) if tp + fn else None,
                         "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None}
    reviewed_documents = reviewed_gold_documents
    covered_gold = reviewed_gold & raw_pages
    coverage_by_run = {
        name: {"covered_pages": len(reviewed_gold & set(run[0])),
               "uncovered_pages": [list(key) for key in sorted(reviewed_gold - set(run[0]))]}
        for name, run in runs.items()}
    unique_common = raw_common & scored_pages
    hash_complete = all((name, key) in hashes for name in runs for key in scored_pages)
    item_metrics = {name: {**dict(counts), "total": counts["correct"] + counts["incorrect"],
                           "reviewed": counts["correct"] + counts["incorrect"] + counts["unscorable"],
                           "unscorable": counts["unscorable"]}
                    for name, counts in item_totals.items()}
    field_metrics = {name: {field: {**dict(counts), "total": counts["correct"] + counts["incorrect"],
                                    "reviewed": sum(counts.values())}
                            for field, counts in fields.items()}
                     for name, fields in field_totals.items()}
    return {"iou_threshold": iou_threshold, "raw_pages": len(raw_pages),
            "run_raw_pages": {name: len(run[0]) for name, run in runs.items()},
            "raw_common_pages": len(raw_common), "unique_common_pages": len(unique_common),
            "image_hash_coverage": sum(all((name, key) in hashes for name in runs) for key in raw_pages),
            "unique_scored_pages": len(scored_pages),
            "unique_image_pages": len(scored_pages) if hash_complete else None,
            "duplicate_image_groups": [[list(k) for k in group] for group in duplicate_groups],
            "gold_coverage": {"reviewed_pages": len(reviewed_gold),
                              "reviewed_documents": reviewed_documents,
                              "covered_pages": len(covered_gold),
                              "uncovered_pages": [list(k) for k in sorted(reviewed_gold - raw_pages)],
                              "by_run": coverage_by_run},
            "document_metrics": metrics, "field_metrics": field_metrics,
            "null_gold_field_policy": "An explicitly present gold field, including null or empty, is scored against the empty normalized prediction.",
            "item_amount_metrics": item_metrics,
            "annotation_coverage_insufficient": sorted({(row["source"], row["page"])
                for row in page_details if row["annotation_coverage_insufficient"]}),
            "page_details": page_details}
