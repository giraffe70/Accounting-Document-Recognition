"""Compare two frozen OCR pilot result files without invoking any model."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.document_scoring import _gold_item_amounts
from analysis.evaluate_validation import normalized


FIELD_NAMES = ("date", "voucher_id", "total_amount", "subtotal", "tax_amount",
               "seller_tax_id", "buyer_tax_id", "doc_type_key")
TIMING_FIELDS = ("ocr_seconds", "ocr_cache_seconds", "ocr_cache_hits", "ocr_cache_misses",
                 "ocr_init_seconds", "first_result_seconds", "orientation_model_seconds",
                 "orientation_model_init_seconds", "resolution_assessment_seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _results_document(value: Any, label: str, expected_run_id: Any,
                      manifest: dict) -> tuple[list[dict], list[str]]:
    if isinstance(value, list):
        rows, metadata = value, {}
    elif isinstance(value, dict) and isinstance(value.get("results"), list):
        rows, metadata = value["results"], value
    else:
        raise ValueError(f"{label} results must be a JSON list or an object with a results list")
    unverified = []
    expected_commit = manifest.get(label + "_commit") or manifest.get(label + "_frozen_commit")
    expected = {"run_id": expected_run_id, "combo": manifest.get("combo")}
    if expected_commit not in (None, ""):
        expected["frozen_commit"] = expected_commit
    for key, want in expected.items():
        if want in (None, ""):
            unverified.append(f"manifest.{key}")
            continue
        if key not in metadata:
            unverified.append(f"{label}.{key}")
        elif metadata[key] != want:
            raise ValueError(f"{label} metadata mismatch: {key}")
    return rows, unverified


def _align(manifest: dict, rows: list[dict], label: str) -> dict[str, dict]:
    pages = manifest.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("Manifest pages must be a non-empty list")
    expected = {}
    for page in pages:
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id:
            raise ValueError("Manifest page has no id")
        if page_id in expected:
            raise ValueError(f"Duplicate manifest page id: {page_id}")
        expected[page_id] = page
    aligned = {}
    for row in rows:
        page_id = row.get("page_id")
        if page_id not in expected:
            raise ValueError(f"Unexpected {label} page id: {page_id}")
        if page_id in aligned:
            raise ValueError(f"Duplicate {label} page id: {page_id}")
        page = expected[page_id]
        for field in ("source", "page", "image_sha256"):
            if row.get(field) != (page.get("sha256") if field == "image_sha256" else page.get(field)):
                raise ValueError(f"{label} {field} mismatch for page {page_id}")
        aligned[page_id] = row
    missing = sorted(set(expected) - set(aligned))
    if missing:
        raise ValueError(f"Missing {label} pages: {', '.join(missing)}")
    return aligned


def _page_data(row: dict) -> tuple[dict, list[dict]]:
    response = row.get("response") if isinstance(row.get("response"), dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    docs = data.get("documents") if isinstance(data.get("documents"), list) else []
    return {"ok": response.get("ok") is True,
            "status": data.get("page_status", "unknown"),
            "layout": data.get("layout"),
            "image_transforms": data.get("image_transforms"),
            "timings": data.get("timings") if isinstance(data.get("timings"), dict) else {}}, docs


def _single_success_voucher(row: dict) -> tuple[dict | None, str]:
    page, docs = _page_data(row)
    if not page["ok"] or page["status"] != "succeeded":
        return None, "page_incomplete"
    if len(docs) != 1:
        return None, "expected_one_document"
    doc = docs[0]
    if doc.get("status") != "succeeded" or not isinstance(doc.get("voucher"), dict):
        return None, str(doc.get("status") or "document_incomplete")
    return doc["voucher"], "succeeded"


def _voucher_value(voucher: dict | None, field: str) -> Any:
    if voucher is None:
        return ""
    fields = voucher.get("fields") if isinstance(voucher.get("fields"), dict) else {}
    value = voucher.get(field, fields.get(field, ""))
    if field in ("seller_tax_id", "buyer_tax_id") and value in (None, ""):
        party = voucher.get("seller" if field == "seller_tax_id" else "buyer")
        if isinstance(party, dict):
            value = party.get("tax_id", "")
    if field == "doc_type_key" and value in (None, ""):
        value = voucher.get("doc_type", fields.get("doc_type_key", ""))
    if field == "total_amount" and value in (None, ""):
        doc_type = voucher.get("doc_type_key", fields.get("doc_type_key", ""))
        if doc_type == "journal_voucher":
            value = voucher.get("debit_total", fields.get("debit_total", ""))
            if value in (None, ""):
                value = voucher.get("credit_total", fields.get("credit_total", ""))
    return value


def _gold_value(gold: dict, field: str) -> Any:
    return gold.get(field, gold.get("gold_" + field, ""))


def _normalize(field: str, value: Any) -> str:
    return normalized(field, value)


def _status(gold: Any, prediction: Any, field: str) -> str:
    if gold in (None, ""):
        return "unscored"
    return "correct" if _normalize(field, gold) == _normalize(field, prediction) else "incorrect"


def _json_list(value: Any) -> list | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, list) else None


def _gold_collection(value: Any) -> Any:
    """Decode CSV JSON collections while preserving malformed non-empty values."""
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return value
    return decoded if isinstance(decoded, list) or decoded is None else value


def _is_empty_collection(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _entry_rows(value: Any, *, gold: bool) -> list[dict] | None:
    rows = _json_list(value)
    if rows is None:
        return None
    normalized_rows = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        account = row.get("account_name", row.get("name"))
        if gold and account is None:
            # Match the existing evaluator: ignore the unprinted balancing side.
            continue
        side = row.get("side")
        if side not in ("debit", "credit"):
            side = "debit" if (row.get("debit_account") or row.get("debit_account_name")) else "credit"
        amount = row.get("amount")
        if amount in (None, "") or side not in ("debit", "credit"):
            return None
        if not gold and not account:
            account = (row.get("debit_account_name") if side == "debit"
                       else row.get("credit_account_name"))
        normalized_rows.append({"side": side, "amount": _normalize("total_amount", amount),
                                "account": _normalize("seller_name", account) if account else ""})
    return normalized_rows


def _items_comparison(gold: dict, voucher: dict | None) -> str:
    items = _gold_collection(gold.get("items", gold.get("gold_items", "")))
    if _is_empty_collection(items):
        return "unscored"
    truth = _gold_item_amounts({"items": items})
    if truth is None:
        return "unassessed"
    predicted = _gold_item_amounts({"items": (voucher or {}).get("items")})
    return "correct" if predicted == truth else "incorrect"


def _entries_comparison(gold: dict, voucher: dict | None) -> tuple[str, str]:
    doc_type = gold.get("doc_type_key", gold.get("gold_doc_type_key"))
    if doc_type not in (None, "", "journal_voucher"):
        return "unscored", "unscored"
    entries = _gold_collection(gold.get("entries", gold.get("gold_entries", "")))
    if _is_empty_collection(entries):
        entries = _gold_collection(gold.get("items", gold.get("gold_items", "")))
    if _is_empty_collection(entries):
        return "unscored", "unscored"
    truth = _entry_rows(entries, gold=True)
    predicted = _entry_rows((voucher or {}).get("entries"), gold=False)
    if truth is None or not truth or predicted is None:
        return "unassessed", "unassessed"
    expected_sides = sorted((entry["side"], entry["amount"]) for entry in truth)
    actual_sides = sorted((entry["side"], entry["amount"]) for entry in predicted)
    amount_side = "correct" if expected_sides == actual_sides else "incorrect"
    if all(entry["account"] for entry in truth):
        expected_accounts = sorted((entry["side"], entry["amount"], entry["account"]) for entry in truth)
        actual_accounts = sorted((entry["side"], entry["amount"], entry["account"]) for entry in predicted)
        account = "correct" if expected_accounts == actual_accounts else "incorrect"
    else:
        account = "unassessed"
    return amount_side, account


def _snapshot_summary(rows: dict[str, dict]) -> dict:
    digests = {key: set() for key in ("execution_version_sha256", "code_version_sha256",
                                      "prompt_version_sha256", "policy_version_sha256")}
    page_ids = set()
    snapshot_count = 0
    incomplete_snapshots = 0
    for page_id, row in rows.items():
        page, _ = _page_data(row)
        trace = page.get("trace")
        if not isinstance(trace, list):
            # Keep trace separate from timings below; it is carried in response.data.
            response = row.get("response") if isinstance(row.get("response"), dict) else {}
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            trace = data.get("trace")
        snapshots = [event for event in trace or []
                     if isinstance(event, dict) and event.get("kind") == "snapshot"]
        if not snapshots:
            continue
        page_ids.add(page_id)
        snapshot_count += len(snapshots)
        for event in snapshots:
            versions = event.get("versions") if isinstance(event.get("versions"), dict) else {}
            if any(not isinstance(versions.get(key), str) or not versions.get(key) for key in digests):
                incomplete_snapshots += 1
            for key in digests:
                value = versions.get(key)
                if isinstance(value, str) and value:
                    digests[key].add(value)
    complete = (page_ids == set(rows) and snapshot_count > 0 and incomplete_snapshots == 0
                and all(digests[key] for key in digests))
    return {"status": "verified" if complete else "unverified",
            "pages_with_snapshots": len(page_ids), "snapshot_count": snapshot_count,
            "incomplete_snapshots": incomplete_snapshots,
            "version_digest_count": {key: len(values) for key, values in digests.items()},
            "version_digests": {key: sorted(values) for key, values in digests.items()}}


def _layout_signature(row: dict) -> Any:
    page, _ = _page_data(row)
    layout = page["layout"]
    if not isinstance(layout, dict):
        return None
    regions = layout.get("regions") if isinstance(layout.get("regions"), list) else []
    selected_regions = sorted((json.dumps({"type": region.get("type"), "box": region.get("box"),
                                           "rotate": region.get("rotate")},
                                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                              for region in regions if isinstance(region, dict)))
    transforms = page["image_transforms"]
    geometric_transform_fields = ("ocr_size", "target_size", "rotation", "coordinate_unit",
                                  "target_frame", "matrix")
    selected_transforms = ({key: transforms[key] for key in geometric_transform_fields
                            if key in transforms} if isinstance(transforms, dict) else transforms)
    return {"rotate": layout.get("rotate"), "regions": selected_regions,
            "image_transforms": selected_transforms}


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _timing_averages(rows: dict[str, dict]) -> dict:
    output = {}
    for field in TIMING_FIELDS:
        values = []
        for row in rows.values():
            page, _ = _page_data(row)
            value = page["timings"].get(field)
            if isinstance(value, (int, float)) and math.isfinite(value):
                values.append(float(value))
        output[field] = {"average": statistics.mean(values) if values else None,
                         "observed_pages": len(values)}
    return output


def _page_evaluation(manifest_pages: list[dict], baseline: dict[str, dict],
                     candidate: dict[str, dict]) -> tuple[dict, list[dict]]:
    field_counts = {field: {"baseline": {"scored": 0, "correct": 0},
                            "candidate": {"scored": 0, "correct": 0}}
                    for field in FIELD_NAMES}
    pages_out = []
    regressions = []
    new_failures = []
    review_ids = []
    unassessed_items = []
    unassessed_entries = []
    baseline_complete = candidate_complete = 0
    for page in manifest_pages:
        page_id = page["id"]
        base_row, cand_row = baseline[page_id], candidate[page_id]
        base_meta, cand_meta = _page_data(base_row)[0], _page_data(cand_row)[0]
        base_status, cand_status = base_meta["status"], cand_meta["status"]
        if base_meta["ok"] and base_status == "succeeded":
            baseline_complete += 1
        if cand_meta["ok"] and cand_status == "succeeded":
            candidate_complete += 1
        if base_meta["ok"] and base_status == "succeeded" and not (cand_meta["ok"] and cand_status == "succeeded"):
            new_failures.append(page_id)
        base_voucher, base_doc_status = _single_success_voucher(base_row)
        cand_voucher, cand_doc_status = _single_success_voucher(cand_row)
        gold = page.get("gold") if isinstance(page.get("gold"), dict) else {}
        field_status = {}
        for field in FIELD_NAMES:
            expected = _gold_value(gold, field)
            if expected in (None, ""):
                continue
            base_state = _status(expected, _voucher_value(base_voucher, field), field)
            cand_state = _status(expected, _voucher_value(cand_voucher, field), field)
            field_status[field] = {"baseline": base_state, "candidate": cand_state}
            for variant, state in (("baseline", base_state), ("candidate", cand_state)):
                field_counts[field][variant]["scored"] += 1
                field_counts[field][variant]["correct"] += state == "correct"
            if base_state == "correct" and cand_state == "incorrect":
                regressions.append({"page_id": page_id, "field": field})

        base_items = _items_comparison(gold, base_voucher)
        cand_items = _items_comparison(gold, cand_voucher)
        if base_items != "unscored" or cand_items != "unscored":
            field_counts.setdefault("items.amounts", {"baseline": {"scored": 0, "correct": 0},
                                                      "candidate": {"scored": 0, "correct": 0}})
            for variant, state in (("baseline", base_items), ("candidate", cand_items)):
                if state in ("correct", "incorrect"):
                    field_counts["items.amounts"][variant]["scored"] += 1
                    field_counts["items.amounts"][variant]["correct"] += state == "correct"
            if base_items == "correct" and cand_items == "incorrect":
                regressions.append({"page_id": page_id, "field": "items.amounts"})
            if "unassessed" in (base_items, cand_items):
                field_status["items.amounts"] = {"baseline": base_items, "candidate": cand_items}
                unassessed_items.append(page_id)

        base_entry, base_account = _entries_comparison(gold, base_voucher)
        cand_entry, cand_account = _entries_comparison(gold, cand_voucher)
        if base_entry != "unscored" or cand_entry != "unscored":
            field_counts.setdefault("entries.amount_side", {"baseline": {"scored": 0, "correct": 0},
                                                            "candidate": {"scored": 0, "correct": 0}})
            for variant, state in (("baseline", base_entry), ("candidate", cand_entry)):
                if state in ("correct", "incorrect"):
                    field_counts["entries.amount_side"][variant]["scored"] += 1
                    field_counts["entries.amount_side"][variant]["correct"] += state == "correct"
            if base_entry == "correct" and cand_entry == "incorrect":
                regressions.append({"page_id": page_id, "field": "entries.amount_side"})
            field_counts.setdefault("entries.account_exact", {"baseline": {"scored": 0, "correct": 0},
                                                               "candidate": {"scored": 0, "correct": 0}})
            for variant, state in (("baseline", base_account), ("candidate", cand_account)):
                if state in ("correct", "incorrect"):
                    field_counts["entries.account_exact"][variant]["scored"] += 1
                    field_counts["entries.account_exact"][variant]["correct"] += state == "correct"
            if base_account == "correct" and cand_account == "incorrect":
                regressions.append({"page_id": page_id, "field": "entries.account_exact"})
            if "unassessed" in (base_entry, cand_entry):
                unassessed_entries.append(page_id)
            field_status["entries"] = {"baseline_amount_side": base_entry,
                                        "candidate_amount_side": cand_entry,
                                        "baseline_account_exact": base_account,
                                        "candidate_account_exact": cand_account}

        base_layout, cand_layout = _layout_signature(base_row), _layout_signature(cand_row)
        layout_changed = base_layout is None or cand_layout is None or base_layout != cand_layout
        if layout_changed:
            review_ids.append(page_id)
        pages_out.append({"page_id": page_id, "baseline_page_status": base_status,
                          "candidate_page_status": cand_status,
                          "baseline_document_status": base_doc_status,
                          "candidate_document_status": cand_doc_status,
                          "field_status": field_status,
                          "layout_review_required": layout_changed})
    for field, variants in field_counts.items():
        for values in variants.values():
            scored = values["scored"]
            values["accuracy"] = values["correct"] / scored if scored else None
    return ({"field_scores": field_counts,
             "complete_pages": {"baseline": baseline_complete, "candidate": candidate_complete},
             "field_regressions": regressions,
             "new_failures": new_failures,
             "layout_or_transform_review_page_ids": review_ids,
             "unassessed_item_page_ids": unassessed_items,
             "unassessed_entry_page_ids": unassessed_entries}, pages_out)


def _orientation_gate(path: Path | None, expected_ids: set[str]) -> dict:
    if path is None:
        return {"status": "missing", "verified": False,
                "issues": ["orientation_results_missing"], "review_page_ids": [],
                "reference": "unknown"}
    value = _load_json(path)
    rows = value.get("results") if isinstance(value, dict) else value
    if not isinstance(rows, list):
        raise ValueError("Orientation results must be a JSON list or an object with results")
    rotations = (0, 90, 180, 270)
    expected_cases = {(page_id, rotation) for page_id in expected_ids for rotation in rotations}
    seen = set()
    issues = []
    review = []
    references = set()
    for row in rows:
        page_id = row.get("page_id") if isinstance(row, dict) else None
        if page_id not in expected_ids:
            raise ValueError(f"Orientation result page id invalid: {page_id}")
        rotation = row.get("input_rotation_cw", 0)
        if type(rotation) is not int or rotation not in rotations:
            raise ValueError(f"Orientation input rotation invalid for {page_id}: {rotation}")
        case = (page_id, rotation)
        if case in seen:
            raise ValueError(f"Orientation result case duplicated: {page_id} rotation {rotation}")
        seen.add(case)
        for angle_key in ("expected_angle", "angle"):
            angle = row.get(angle_key)
            if angle is not None and (type(angle) is not int or angle not in rotations):
                raise ValueError(f"Orientation {angle_key} invalid for {page_id}: {angle}")
        reference = row.get("reference")
        if reference:
            references.add(reference)
            if reference != "legacy_ocr_orientation":
                issues.append("orientation_reference_unrecognized")
        else:
            issues.append("orientation_reference_missing")
        if row.get("accepted") is True:
            correct = row.get("prediction_correct", row.get("correct"))
            if correct is False:
                issues.append("accepted_prediction_incorrect")
                review.append(page_id)
            elif correct is not True:
                issues.append("accepted_prediction_accuracy_missing")
                review.append(page_id)
    missing_cases = sorted(expected_cases - seen)
    missing_pages = sorted({page_id for page_id, _ in missing_cases})
    if missing_cases:
        issues.append("orientation_case_coverage_incomplete")
        review.extend(missing_pages)
    reference_label = ("legacy_ocr_orientation (not independent human-reviewed direction gold)"
                       if references == {"legacy_ocr_orientation"} else "unverified reference")
    return {"status": "verified" if not issues else "review_required",
            "verified": not issues, "issues": sorted(set(issues)),
            "review_page_ids": sorted(set(review)), "reference": reference_label,
            "expected_case_count": len(expected_cases), "observed_case_count": len(seen),
            "missing_cases": [{"page_id": page_id, "input_rotation_cw": rotation}
                              for page_id, rotation in missing_cases]}


def compare(manifest_path: Path, baseline_path: Path, candidate_path: Path,
            output_dir: Path, orientation_path: Path | None = None) -> dict:
    manifest_path = manifest_path.resolve()
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ValueError("Output directory must be new; existing results are never overwritten")
    if not manifest.get("gold_path") or not manifest.get("gold_sha256"):
        raise ValueError("Manifest must include frozen gold_path and gold_sha256")
    gold_path = Path(manifest["gold_path"])
    if not gold_path.is_absolute():
        gold_path = manifest_path.parent / gold_path
    gold_path = gold_path.resolve()
    if _sha256(gold_path) != manifest["gold_sha256"]:
        raise ValueError("Frozen gold hash mismatch before comparison")

    expected = {page.get("id"): page for page in manifest.get("pages", []) if isinstance(page, dict)}
    if len(expected) != len(manifest.get("pages", [])):
        raise ValueError("Manifest page ids must be present and unique")
    for page in manifest["pages"]:
        if not page.get("id") or not page.get("source") or page.get("page") in (None, ""):
            raise ValueError("Manifest page requires id, source, and page")
        if not isinstance(page.get("gold"), dict):
            raise ValueError(f"Manifest page {page['id']} has no gold field mapping")
        if not page.get("sha256"):
            raise ValueError(f"Manifest page {page['id']} has no image sha256")
        image_path = Path(page.get("path", ""))
        if image_path.is_file() and _sha256(image_path) != page["sha256"]:
            raise ValueError(f"Frozen image hash mismatch for page {page['id']}")

    base_rows, base_unverified = _results_document(_load_json(baseline_path), "baseline",
                                                    manifest.get("baseline_run_id"), manifest)
    cand_rows, cand_unverified = _results_document(_load_json(candidate_path), "candidate",
                                                    manifest.get("candidate_run_id"), manifest)
    base = _align(manifest, base_rows, "baseline")
    candidate = _align(manifest, cand_rows, "candidate")
    source_unverified = sorted(set(base_unverified + cand_unverified))
    base_snapshots = _snapshot_summary(base)
    candidate_snapshots = _snapshot_summary(candidate)
    if base_snapshots["status"] != "verified":
        source_unverified.append("baseline.phase_snapshots")
    if candidate_snapshots["status"] != "verified":
        source_unverified.append("candidate.phase_snapshots")
    source_unverified = sorted(set(source_unverified))
    expected_ids = set(expected)
    orientation = _orientation_gate(orientation_path, expected_ids)
    evaluation, pages_out = _page_evaluation(manifest["pages"], base, candidate)

    base_wall = [float(base[p].get("wall_seconds")) for p in expected_ids
                 if isinstance(base[p].get("wall_seconds"), (int, float)) and
                 math.isfinite(base[p].get("wall_seconds"))]
    cand_wall = [float(candidate[p].get("wall_seconds")) for p in expected_ids
                 if isinstance(candidate[p].get("wall_seconds"), (int, float)) and
                 math.isfinite(candidate[p].get("wall_seconds"))]
    median_base, median_candidate = statistics.median(base_wall) if base_wall else None, statistics.median(cand_wall) if cand_wall else None
    p95_base, p95_candidate = _percentile(base_wall, .95), _percentile(cand_wall, .95)
    improvement = ((median_base - median_candidate) / median_base * 100
                   if median_base not in (None, 0) and median_candidate is not None else None)
    timing_summary = {"baseline": _timing_averages(base), "candidate": _timing_averages(candidate)}
    page_complete = (len(expected) == 12 and evaluation["complete_pages"]["baseline"] == 12 and
                     evaluation["complete_pages"]["candidate"] == 12 and len(base_wall) == 12 and len(cand_wall) == 12)
    speed_ok = improvement is not None and improvement >= 25
    p95_ok = p95_base is not None and p95_candidate is not None and p95_candidate <= p95_base
    gates = {
        "twelve_pages_complete": page_complete,
        "median_improvement_at_least_25_percent": speed_ok,
        "p95_not_increased": p95_ok,
        "no_baseline_correct_to_candidate_incorrect": not evaluation["field_regressions"],
        "no_new_page_failures": not evaluation["new_failures"],
        "no_unreviewed_layout_or_transform_changes": not evaluation["layout_or_transform_review_page_ids"],
        "orientation_accuracy_verified": orientation["verified"],
        "item_amounts_assessed": not evaluation["unassessed_item_page_ids"],
        "journal_entries_assessed": not evaluation["unassessed_entry_page_ids"],
        "source_metadata_verified": not source_unverified,
    }
    summary = {
        "schema_version": 1,
        "provenance": {"frozen_commit": manifest.get("frozen_commit"),
                       "baseline_run_id": manifest.get("baseline_run_id"),
                       "candidate_run_id": manifest.get("candidate_run_id"),
                       "combo": manifest.get("combo"),
                       "phase_snapshots": {"baseline": base_snapshots,
                                           "candidate": candidate_snapshots},
                       "metadata_status": "verified" if not source_unverified else "unverified",
                       "unverified_metadata": source_unverified,
                       "gold_hash_verified_before": True,
                       "gold_hash_verified_after": True},
        "page_count": len(expected),
        "wall_seconds": {"baseline_median": median_base, "candidate_median": median_candidate,
                         "median_improvement_percent": improvement,
                         "baseline_p95_linear": p95_base, "candidate_p95_linear": p95_candidate},
        "average_timings": timing_summary,
        "evaluation": evaluation,
        "orientation": orientation,
        "pages": pages_out,
        "gates": gates,
        "gate_passed": all(gates.values()),
    }
    if _sha256(gold_path) != manifest["gold_sha256"]:
        raise ValueError("Frozen gold changed during comparison")
    output_dir.mkdir(parents=True)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
    (output_dir / "report.md").write_text(_report(summary), encoding="utf-8")
    return summary


def _report(summary: dict) -> str:
    wall = summary["wall_seconds"]
    lines = ["# OCR pilot comparison", "",
             f"Gate: **{'PASS' if summary['gate_passed'] else 'FAIL'}**",
             f"Pages: {summary['page_count']}",
             f"Median wall seconds: baseline {wall['baseline_median']}; candidate {wall['candidate_median']}",
             f"Median improvement: {wall['median_improvement_percent']}%",
             f"Linear P95 wall seconds: baseline {wall['baseline_p95_linear']}; candidate {wall['candidate_p95_linear']}",
             f"Source metadata: {summary['provenance']['metadata_status']}", "",
             f"Orientation reference: {summary['orientation']['reference']}", "",
             "## Gates", ""]
    for name, passed in summary["gates"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} `{name}`")
    lines.extend(["", "## Field score aggregates", "",
                  "| Field | Baseline scored/correct | Candidate scored/correct |",
                  "|---|---:|---:|"])
    for field, variants in summary["evaluation"]["field_scores"].items():
        base, candidate = variants["baseline"], variants["candidate"]
        lines.append(f"| `{field}` | {base['scored']}/{base['correct']} | {candidate['scored']}/{candidate['correct']} |")
    lines.extend(["", "## Page IDs requiring review", ""])
    changes = summary["evaluation"]["field_regressions"]
    lines.extend(f"- `{item['page_id']}` (`{item['field']}` regression)" for item in changes)
    lines.extend(f"- `{page_id}` (layout/transform changed)"
                 for page_id in summary["evaluation"]["layout_or_transform_review_page_ids"])
    lines.extend(f"- `{page_id}` (orientation review)" for page_id in summary["orientation"]["review_page_ids"])
    lines.extend(f"- `{page_id}` (new page failure)" for page_id in summary["evaluation"]["new_failures"])
    lines.extend(f"- `{page_id}` (item amounts not assessable)"
                 for page_id in summary["evaluation"]["unassessed_item_page_ids"])
    lines.extend(f"- `{page_id}` (journal entries not assessable)"
                 for page_id in summary["evaluation"]["unassessed_entry_page_ids"])
    lines.extend(["", "## Average timing fields", "",
                  "| Metric | Baseline average | Candidate average |",
                  "|---|---:|---:|"])
    for field in TIMING_FIELDS:
        base = summary["average_timings"]["baseline"][field]["average"]
        candidate = summary["average_timings"]["candidate"][field]["average"]
        lines.append(f"| `{field}` | {base} | {candidate} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare frozen baseline/candidate OCR pilot outputs; no models or gold writes.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--orientation-results", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = compare(args.manifest, args.baseline, args.candidate, args.output_dir,
                          args.orientation_results)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"OCR pilot gate: {'PASS' if summary['gate_passed'] else 'FAIL'}; report: {args.output_dir / 'report.md'}")
    return 0 if summary["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
