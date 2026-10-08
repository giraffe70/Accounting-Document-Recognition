"""Compare the two saved CSV validation runs against reviewed page annotations.

Provide explicit CSV runs, a frozen gold JSON and a new output directory. Empty gold values mean unverified and
are excluded from accuracy denominators. Model disagreement is not accuracy.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path


try:
    from .gold_io import load_frozen
    from .document_scoring import score_documents
except ImportError:
    from gold_io import load_frozen
    from document_scoring import score_documents

FIELDS = (
    "doc_type_key", "date", "voucher_id", "total_amount", "subtotal",
    "tax_amount", "seller_name", "seller_tax_id", "buyer_name", "buyer_tax_id",
)
AMOUNTS = {"total_amount", "subtotal", "tax_amount"}
IDS = {"voucher_id", "seller_tax_id", "buyer_tax_id"}
TYPE_LABELS = {
    "統一發票／電子發票": "invoice", "支票": "cheque",
    "匯款單／銀行轉帳水單": "remittance", "海關進口貨物稅費繳納證明": "customs_tax",
    "出口報單": "export_declaration", "進口報單": "import_declaration",
    "報關行收費通知單": "broker_note", "進出口報單（未分進出口）": "declaration",
    "稅款繳款書": "tax_bill", "水電費／公用事業帳單": "utility",
    "收據／雜項開銷": "receipt", "對帳單／應收帳款明細表": "statement",
    "訂購單／採購單": "purchase_order", "食品輸入許可通知": "fda_import",
    "勞健保費繳款／繳費證明": "social_insurance", "帳單費用明細（附頁佐證）": "bill_detail",
    "會計傳票（記帳憑證）": "journal_voucher", "其他憑證": "other",
}


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def load_run(path: Path) -> dict[tuple[str, int], dict]:
    vouchers = read_csv(path / "vouchers.csv")
    fields = defaultdict(dict)
    items = defaultdict(list)
    for row in read_csv(path / "fields.csv"):
        fields[row["result_id"]][row["field"]] = row["value"]
    for row in read_csv(path / "items.csv"):
        items[row["result_id"]].append({
            key: row.get(key, "") for key in ("description", "quantity", "unit_price", "amount")
        })
    result = {}
    for row in vouchers:
        key = (row["source"], int(row["page"]))
        if key in result:
            raise ValueError(f"Duplicate result: {path.name} {key}")
        record = {**fields[row["result_id"]], **row,
                  "doc_type_key": TYPE_LABELS.get(row["doc_type"], ""),
                  "items": items[row["result_id"]]}
        # Journal pages print a balanced debit/credit total rather than a
        # purchase gross amount. The CSV puts it in debit_total/credit_total.
        if record["doc_type_key"] == "journal_voucher" and not record["total_amount"]:
            record["total_amount"] = record.get("debit_total") or record.get("credit_total") or ""
        result[key] = record
    return result


def _has_multi_document_pages(run_paths, gold_path=None):
    """Detect the additive vouchers/pages export without changing legacy loading."""
    if gold_path is not None:
        gold_rows = json.loads(Path(gold_path).read_text(encoding="utf-8"))
        if any("documents" in row for row in gold_rows):
            return True
    for path in run_paths.values():
        voucher_path = Path(path) / "vouchers.csv"
        if not voucher_path.is_file():
            continue
        with voucher_path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
        seen = set()
        for row in rows:
            key = (row.get("source") or row.get("relative_path") or row.get("parent_result_id"),
                   row.get("page") or row.get("page_no"))
            if key[0] and key in seen:
                return True
            if key[0]:
                seen.add(key)
            origin = row.get("origin_json") or "{}"
            try:
                origin = json.loads(origin) if isinstance(origin, str) else origin
            except json.JSONDecodeError:
                origin = {}
            has_region = bool(row.get("source_region") or
                              (isinstance(origin, dict) and (origin.get("region") or origin.get("source_region"))))
            if row.get("document_id") or row.get("parent_result_id") or has_region:
                return True
    return False


def normalized(field: str, value: object) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if field in AMOUNTS:
        try:
            return str(round(float(s.replace(",", "").replace("$", "")), 2))
        except ValueError:
            return s
    if field in IDS:
        return re.sub(r"[^A-Z0-9]", "", s.upper())
    if field == "date":
        nums = re.findall(r"\d+", s)
        if len(nums) >= 3:
            year, month, day = map(int, nums[:3])
            if year < 1911:
                year += 1911
            try:
                return f"{year:04d}-{month:02d}-{day:02d}"
            except ValueError:
                pass
    return re.sub(r"\s+", "", s)


def evaluate(run_paths, gold_path, output_dir, dataset_id, page_db=None, run_ids=None, deduplicate=False):
    if len(run_paths) != 2:
        raise ValueError("Exactly two named runs are required")
    output_dir = Path(output_dir).resolve()
    gold_path = Path(gold_path).resolve()
    if output_dir.exists():
        raise ValueError("Output directory must be new; existing results are never overwritten")
    if len(run_paths) != 2:
        raise ValueError("Exactly two named runs are required")
    if _has_multi_document_pages(run_paths, gold_path):
        # A page can contain several independently located documents. Preserve
        # the legacy evaluator for its one-row-per-page contract and publish the
        # additive document metrics for this export shape.
        inputs = [Path(gold_path).resolve()]
        for path in run_paths.values():
            inputs.extend(p for p in (Path(path) / "vouchers.csv", Path(path) / "pages.csv",
                                      Path(path) / "fields.csv", Path(path) / "items.csv") if p.is_file())
        for path in inputs:
            if path.resolve().is_relative_to(output_dir):
                raise ValueError("Output directory must not contain input files")
        hashes = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
        document_scores = score_documents(run_paths, gold_path)
        output_dir.mkdir(parents=True, exist_ok=False)
        detail = document_scores.pop("page_details")
        if detail:
            with (output_dir / "document_comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(detail[0]))
                writer.writeheader()
                writer.writerows(detail)
        for path in inputs:
            if hashlib.sha256(path.read_bytes()).hexdigest() != hashes[str(path.resolve())]:
                raise ValueError(f"Input changed during evaluation: {path}")
        summary = {"common_pages": document_scores["raw_common_pages"],
                   "comparability_verified": False, "csv_run_identity_verified": False,
                   "raw_common_pages": document_scores["raw_common_pages"],
                   "unique_image_pages": document_scores["unique_image_pages"],
                   "legacy_page_metrics_available": False,
                   "document_scoring": document_scores,
                   "provenance": {"dataset_id": dataset_id,
                                  "runs": {name: str(Path(path).resolve()) for name, path in run_paths.items()},
                                  "input_sha256": hashes,
                                  "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                  "created_at_utc": datetime.now(timezone.utc).isoformat()}}
        (output_dir / "validation_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary
    inputs = [gold_path] + [Path(p) / f for p in run_paths.values()
                           for f in ("vouchers.csv", "fields.csv", "items.csv", "entries.csv")]
    for path in inputs:
        if path.resolve().is_relative_to(output_dir):
            raise ValueError("Output directory must not contain input files")
    input_hashes = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    runs = {name: load_run(Path(path)) for name, path in run_paths.items()}
    names = list(runs)
    common = set.intersection(*(set(records) for records in runs.values()))
    if not common:
        raise ValueError("Runs have no common source/page keys")
    raw_common_count = len(common)
    gold = load_frozen(gold_path)
    duplicate_image_groups = []
    router_requested_image = {}
    image_coverage = None
    batch_settings = {}
    comparable = ("max_edge", "repeats", "warmup", "prompt_version_sha256", "code_version_sha256", "model_parameters")
    modes = {name: {record.get("mode") for record in records.values()} for name, records in runs.items()}
    mode_verified = all(len(values) == 1 and None not in values and "" not in values for values in modes.values())
    if mode_verified and modes[names[0]] != modes[names[1]]:
        raise ValueError("Batch conditions differ: recognition mode")
    run_ids = run_ids or {}
    if page_db is not None:
        page_db = Path(page_db).resolve()
        with closing(sqlite3.connect(page_db.as_uri() + "?mode=ro", uri=True)) as con:
            image_groups = defaultdict(list)
            covered = set()
            for source, page, path in con.execute(
                    "SELECT relative_path,page_no,page_path FROM pages WHERE dataset_id=?", (dataset_id,)):
                key = (source, page)
                if key in common and Path(path).is_file():
                    covered.add(key)
                    image_groups[hashlib.sha256(Path(path).read_bytes()).hexdigest()].append(key)
            image_coverage = len(covered)
            duplicate_image_groups = [sorted(keys) for keys in image_groups.values() if len(keys) > 1]
            for name, run_id in run_ids.items():
                if name not in runs:
                    raise ValueError(f"Unknown run name: {name}")
                row = con.execute("SELECT dataset_id,settings_json FROM runs WHERE id=?", (run_id,)).fetchone()
                if not row or row[0] != dataset_id:
                    raise ValueError(f"Run {name} does not belong to dataset {dataset_id}")
                batch_settings[name] = json.loads(row[1])
                count = 0
                for (raw,) in con.execute("SELECT trace_json FROM results WHERE run_id=? AND status='succeeded'", (run_id,)):
                    calls = [x for x in json.loads(raw or "[]") if x.get("kind") == "model_call"]
                    if calls and "請提供" in calls[0].get("response", "") and "圖片" in calls[0].get("response", ""):
                        count += 1
                router_requested_image[name] = count
        if batch_settings and set(batch_settings) != set(runs):
            raise ValueError("Provide run IDs for both CSV batches")
        if len(batch_settings) == 2:
            for key in comparable:
                if batch_settings[names[0]].get(key) != batch_settings[names[1]].get(key):
                    raise ValueError(f"Batch conditions differ: {key}")
    if deduplicate:
        if image_coverage != len(common):
            raise ValueError("Deduplication requires images for every common page via --database")
        for group in duplicate_image_groups:
            truths = [gold.get(k) for k in group]
            fingerprints = [json.dumps({k:v for k,v in t.items() if k not in ("source", "page", "remarks")},
                                       sort_keys=True, ensure_ascii=False) if t else None for t in truths]
            if len(set(fingerprints)) != 1:
                raise ValueError(f"Conflicting gold in duplicate group: {group}")
            common.difference_update(group[1:])
    # All preflight checks precede output creation; frozen gold is only read.
    output_dir.mkdir(parents=True, exist_ok=False)
    stats = {scope: {name: {field: Counter() for field in FIELDS} for name in runs}
             for scope in ("high", "high_medium", "all")}
    core = {scope: {name: Counter() for name in runs}
            for scope in ("high", "high_medium", "all")}
    full_page = {scope: {name: Counter() for name in runs}
                 for scope in ("high", "high_medium", "all")}
    disagreement = {field: Counter() for field in FIELDS}
    detail = []
    for source, page in sorted(common):
        key = (source, page)
        truth = gold.get(key, {})
        confidence = truth.get("confidence", "")
        row = {"source": source, "page": page, "gold_present": bool(truth),
               "page_role": truth.get("page_role", ""),
               "gold_confidence": truth.get("confidence", ""),
               "gold_remarks": truth.get("remarks", "")}
        for field in FIELDS:
            expected = truth.get(field)
            q = runs[names[0]][key].get(field)
            g = runs[names[1]][key].get(field)
            row[f"gold_{field}"] = "" if expected is None else str(expected)
            row[f"{names[0]}_{field}"] = "" if q is None else str(q)
            row[f"{names[1]}_{field}"] = "" if g is None else str(g)
            if normalized(field, q) != normalized(field, g):
                disagreement[field]["different"] += 1
            else:
                disagreement[field]["same"] += 1
            for model, predicted in ((names[0], q), (names[1], g)):
                if (expected is None or str(expected).strip() == ""
                        or (row["page_role"] == "journal_voucher"
                            and field in ("subtotal", "tax_amount"))):
                    row[f"{model}_{field}_match"] = ""
                    continue
                ok = normalized(field, predicted) == normalized(field, expected)
                row[f"{model}_{field}_match"] = int(ok)
                for scope in stats:
                    if scope == "high" and confidence != "high":
                        continue
                    if scope == "high_medium" and confidence not in ("high", "medium"):
                        continue
                    stats[scope][model][field]["correct" if ok else "incorrect"] += 1
        for model in runs:
            row[f"{model}_status"] = runs[model][key]["status"]
            row[f"{model}_items"] = json.dumps(runs[model][key]["items"], ensure_ascii=False)
        row["gold_items"] = json.dumps(truth.get("items", []), ensure_ascii=False)
        for model in runs:
            marks = [row[f"{model}_{field}_match"] for field in FIELDS
                     if row[f"{model}_{field}_match"] != ""]
            if len(marks) >= 2:
                ok = all(mark == 1 for mark in marks)
                row[f"{model}_full_page_match"] = int(ok)
                for scope in full_page:
                    if scope == "high" and confidence != "high":
                        continue
                    if scope == "high_medium" and confidence not in ("high", "medium"):
                        continue
                    full_page[scope][model]["correct" if ok else "incorrect"] += 1
            else:
                row[f"{model}_full_page_match"] = ""
        if (row["page_role"] != "journal_voucher"
                and all(truth.get(field) not in (None, "") for field in
                        ("date", "voucher_id", "total_amount"))):
            for model in runs:
                ok = all(row[f"{model}_{field}_match"] == 1
                         for field in ("date", "voucher_id", "total_amount"))
                row[f"{model}_core_match"] = int(ok)
                for scope in core:
                    if scope == "high" and confidence != "high":
                        continue
                    if scope == "high_medium" and confidence not in ("high", "medium"):
                        continue
                    core[scope][model]["correct" if ok else "incorrect"] += 1
        else:
            for model in runs:
                row[f"{model}_core_match"] = ""
        detail.append(row)

    output_csv = output_dir / "validation_comparison.csv"
    with output_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(detail[0]))
        writer.writeheader()
        writer.writerows(detail)
    summary = {
        "common_pages": len(common),
        "run_pages": {name: len(records) for name, records in runs.items()},
        "success_on_common": {name: sum(records[k]["status"] == "succeeded" for k in common)
                              for name, records in runs.items()},
        "other_on_common": {name: sum(records[k]["doc_type_key"] == "other" for k in common)
                            for name, records in runs.items()},
        "recognition_median_seconds_on_common": {
            name: (round(statistics.median(float(records[k]["recognition_seconds"])
                                          for k in common if records[k].get("recognition_seconds")), 1)
             if any(records[k].get("recognition_seconds") for k in common) else None)
            for name, records in runs.items()},
        "router_requested_image": router_requested_image,
        "duplicate_image_groups": [[list(k) for k in group] for group in duplicate_image_groups],
        "raw_common_pages": raw_common_count,
        "deduplicated": deduplicate,
        "excluded_duplicate_pages": [list(k) for group in duplicate_image_groups for k in group[1:]] if deduplicate else [],
        "image_hash_coverage": image_coverage,
        "unique_image_pages": (raw_common_count - sum(len(group)-1 for group in duplicate_image_groups)
                               if image_coverage == raw_common_count else None),
        "gold_pages": len(set(gold) & common),
        "uncovered_pages": [list(k) for k in sorted(common - set(gold))],
        "gold_outside_common": [list(k) for k in sorted(set(gold) - common)],
        "extra_pages": {name: [list(k) for k in sorted(set(records) - set.intersection(*(set(r) for r in runs.values())))] for name, records in runs.items()},
        "gold_confidence": dict(Counter(row.get("confidence", "") for key, row in gold.items()
                                        if key in common)),
        "field_scores": {scope: {name: {field: dict(counts) for field, counts in scores.items()}
                                  for name, scores in model_scores.items()}
                         for scope, model_scores in stats.items()},
        "core_three_scores": {scope: {name: dict(counts) for name, counts in model_scores.items()}
                              for scope, model_scores in core.items()},
        "full_page_scores": {scope: {name: dict(counts) for name, counts in model_scores.items()}
                             for scope, model_scores in full_page.items()},
        "model_disagreement": {field: dict(counts) for field, counts in disagreement.items()},
    }
    by_type = defaultdict(lambda: {name: Counter() for name in runs})
    for row in detail:
        if row["gold_confidence"] not in ("high", "medium"):
            continue
        type_key = row["gold_doc_type_key"]
        if not type_key:
            continue
        for name in runs:
            by_type[type_key][name]["correct" if row[f"{name}_doc_type_key_match"] == 1
                                     else "incorrect"] += 1
    summary["type_scores_by_gold_category"] = {
        type_key: {name: dict(counts) for name, counts in scores.items()}
        for type_key, scores in sorted(by_type.items())}
    amount_by_role = {role: {name: Counter() for name in runs}
                      for role in ("journal_voucher", "other_documents")}
    for row in detail:
        if row["gold_confidence"] not in ("high", "medium"):
            continue
        role = ("journal_voucher" if row["page_role"] == "journal_voucher"
                else "other_documents")
        for name in runs:
            mark = row[f"{name}_total_amount_match"]
            if mark != "":
                amount_by_role[role][name]["correct" if mark == 1 else "incorrect"] += 1
    summary["total_amount_scores_by_role"] = {
        role: {name: dict(counts) for name, counts in scores.items()}
        for role, scores in amount_by_role.items()}
    invoice_items = {name: Counter() for name in runs}
    for key, truth in gold.items():
        if key not in common or truth.get("confidence") not in ("high", "medium"):
            continue
        if truth.get("doc_type_key") != "invoice":
            continue
        expected = truth.get("items") or []
        if not expected or any(item.get("amount") in (None, "") for item in expected):
            continue
        gold_amounts = sorted(normalized("total_amount", item["amount"]) for item in expected)
        for name, records in runs.items():
            predicted = records[key]["items"]
            actual_amounts = sorted(normalized("total_amount", item.get("amount"))
                                    for item in predicted if item.get("amount") not in (None, ""))
            ok = actual_amounts == gold_amounts
            invoice_items[name]["correct" if ok else "incorrect"] += 1
    summary["invoice_item_amount_multiset_scores"] = {
        name: dict(counts) for name, counts in invoice_items.items()}
    journal_scores = {name: {"amount_side": Counter(), "account_exact": Counter()}
                      for name in runs}
    for name, path in run_paths.items():
        by_result = defaultdict(list)
        for entry in read_csv(Path(path) / "entries.csv"):
            side = "debit" if entry["debit_account"] or entry["debit_account_name"] else "credit"
            account = entry["debit_account_name"] if side == "debit" else entry["credit_account_name"]
            by_result[entry["result_id"]].append({"side": side, "amount": entry["amount"],
                                                   "account_name": account})
        for key, truth in gold.items():
            if key not in common or truth.get("doc_type_key") != "journal_voucher":
                continue
            if truth.get("confidence") not in ("high", "medium"):
                continue
            expected = truth.get("entries") or truth.get("items") or []
            # A cash voucher can leave the balancing cash side implicit. Gold
            # records it for accounting completeness, but OCR cannot extract
            # a line that is not printed on the page.
            expected = [item for item in expected
                        if item.get("account_name", item.get("name")) is not None]
            if not expected or any(item.get("side") not in ("debit", "credit")
                                   or item.get("amount") in (None, "") for item in expected):
                continue
            predicted = by_result[runs[name][key]["result_id"]]
            expected_amounts = sorted((item["side"], normalized("total_amount", item["amount"]))
                                      for item in expected)
            actual_amounts = sorted((item["side"], normalized("total_amount", item["amount"]))
                                    for item in predicted)
            amount_ok = expected_amounts == actual_amounts
            journal_scores[name]["amount_side"]["correct" if amount_ok else "incorrect"] += 1
            if all(item.get("account_name") or item.get("name") for item in expected):
                expected_accounts = sorted((item["side"], normalized("total_amount", item["amount"]),
                                            normalized("seller_name", item.get("account_name") or item.get("name")))
                                           for item in expected)
                actual_accounts = sorted((item["side"], normalized("total_amount", item["amount"]),
                                          normalized("seller_name", item.get("account_name")))
                                         for item in predicted)
                journal_scores[name]["account_exact"]["correct" if expected_accounts == actual_accounts
                                                      else "incorrect"] += 1
    summary["journal_entry_scores"] = {
        name: {metric: dict(counts) for metric, counts in scores.items()}
        for name, scores in journal_scores.items()}
    # For new exports with a single document on each page, add document metrics
    # while retaining every legacy page-level metric and output column.
    if any((Path(path) / "pages.csv").is_file() for path in run_paths.values()):
        summary["document_scoring"] = score_documents(run_paths, gold_path)
    for path in inputs:
        if hashlib.sha256(path.read_bytes()).hexdigest() != input_hashes[str(path.resolve())]:
            raise ValueError(f"Input changed during evaluation: {path}")
    summary["provenance"] = {"dataset_id": dataset_id, "run_ids": run_ids,
        "runs": {name: str(Path(path).resolve()) for name, path in run_paths.items()},
        "batch_settings": batch_settings,
        "comparability_verified": (len(batch_settings) == 2 and mode_verified
                                   and all(key in settings and settings[key] is not None
                                           for settings in batch_settings.values() for key in comparable)),
        "csv_run_identity_verified": False,
        "comparability_note": "CSV/run ID association must be checked by the caller; missing mode or snapshot fields leave comparability unverified.",
        "input_sha256": input_hashes, "scorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "gold_loader_sha256": hashlib.sha256(Path(load_frozen.__code__.co_filename).read_bytes()).hexdigest(),
        "created_at_utc": datetime.now(timezone.utc).isoformat()}
    (output_dir / "validation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def named_values(values):
    result = {}
    for value in values:
        name, separator, target = value.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]*", name) or not target or name in result:
            raise ValueError("Use unique lowercase NAME=VALUE arguments")
        result[name] = target
    return result


def main():
    parser = argparse.ArgumentParser(description="Score two saved CSV runs against frozen gold; no model calls or gold writes.")
    parser.add_argument("--run", action="append", required=True, metavar="NAME=CSV_DIR")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory only")
    parser.add_argument("--database", type=Path, help="Read-only validation DB for image hashes and settings")
    parser.add_argument("--run-id", action="append", default=[], metavar="NAME=ID")
    parser.add_argument("--deduplicate", action="store_true")
    args = parser.parse_args()
    try:
        run_paths = {k: Path(v) for k,v in named_values(args.run).items()}
        run_ids = named_values(args.run_id)
        if run_ids and args.database is None:
            raise ValueError("--run-id requires --database")
        summary = evaluate(run_paths, args.gold, args.output_dir, args.dataset_id,
                           args.database, run_ids, args.deduplicate)
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
