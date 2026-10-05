"""Compare the two saved CSV validation runs against reviewed page annotations.

Run after gold_*.json have been prepared. Empty gold values mean unverified and
are excluded from accuracy denominators. Model disagreement is not accuracy.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
RESULT = ROOT / "app/data/validation/result"
RUNS = {
    "qwen": RESULT / "validation-f89d0bf3f54d4e93ab91e9b82c41beb5-csv_qwen2.5vl",
    "gemma": RESULT / "validation-e0b0cf8e8040411cbdb0b70d8d54f25e-csv_gemma4e4b",
}
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


def load_gold() -> dict[tuple[str, int], dict]:
    gold = {}
    base_paths = [HERE / name for name in (
        "gold_voucher_cases.json", "gold_other_sources.json", "gold_jpg.json")]
    override_paths = [HERE / name for name in (
        "gold_extra_review.json", "gold_extra_review_2.json")]
    for path in base_paths + [p for p in override_paths if p.exists()]:
        for row in json.loads(path.read_text(encoding="utf-8")):
            key = (row["source"], int(row["page"]))
            if key in gold and path in base_paths:
                raise ValueError(f"Duplicate gold {key}: {path}")
            if not row.get("page_role"):
                row["page_role"] = ("journal_voucher" if row.get("doc_type_key") == "journal_voucher"
                                    else "document")
            gold[key] = row
    return gold


def main() -> None:
    runs = {name: load_run(path) for name, path in RUNS.items()}
    common = set(runs["qwen"]) & set(runs["gemma"])
    gold = load_gold()
    (HERE / "gold_standard.json").write_text(
        json.dumps([gold[key] for key in sorted(gold)], ensure_ascii=False, indent=2),
        encoding="utf-8")
    gold_fields = ("source", "page", "page_role", "doc_type", "doc_type_key", "period",
                   "date", "voucher_id", "total_amount", "subtotal", "tax_amount",
                   "seller_name", "seller_tax_id", "buyer_name", "buyer_tax_id",
                   "items", "entries", "confidence", "remarks")
    with (HERE / "gold_standard.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=gold_fields)
        writer.writeheader()
        for key in sorted(gold):
            record = gold[key]
            writer.writerow({field: (json.dumps(record.get(field), ensure_ascii=False)
                                     if field in ("items", "entries") else record.get(field))
                             for field in gold_fields})
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
            q = runs["qwen"][key].get(field)
            g = runs["gemma"][key].get(field)
            row[f"gold_{field}"] = "" if expected is None else str(expected)
            row[f"qwen_{field}"] = "" if q is None else str(q)
            row[f"gemma_{field}"] = "" if g is None else str(g)
            if normalized(field, q) != normalized(field, g):
                disagreement[field]["different"] += 1
            else:
                disagreement[field]["same"] += 1
            for model, predicted in (("qwen", q), ("gemma", g)):
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

    output_csv = HERE / "validation_comparison.csv"
    with output_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(detail[0]))
        writer.writeheader()
        writer.writerows(detail)
    page_db = ROOT / "app/data/validation/validation.db"
    duplicate_image_groups = []
    router_requested_image = {}
    if page_db.exists():
        con = sqlite3.connect(page_db)
        image_groups = defaultdict(list)
        for source, page, path in con.execute(
                "SELECT relative_path,page_no,page_path FROM pages WHERE dataset_id=?",
                ("d7033a034f3840a0be62a4f71516b60b",)):
            key = (source, page)
            if key in common and Path(path).exists():
                image_groups[hashlib.sha256(Path(path).read_bytes()).hexdigest()].append(key)
        duplicate_image_groups = [[list(k) for k in keys]
                                  for keys in image_groups.values() if len(keys) > 1]
        for name, run_id in (("qwen", "f89d0bf3f54d4e93ab91e9b82c41beb5"),
                             ("gemma", "e0b0cf8e8040411cbdb0b70d8d54f25e")):
            count = 0
            for (raw,) in con.execute(
                    "SELECT trace_json FROM results WHERE run_id=? AND status='succeeded'",
                    (run_id,)):
                calls = [x for x in json.loads(raw or "[]") if x.get("kind") == "model_call"]
                if calls and "請提供" in calls[0].get("response", "") and "圖片" in calls[0].get("response", ""):
                    count += 1
            router_requested_image[name] = count
        con.close()
    summary = {
        "common_pages": len(common),
        "run_pages": {name: len(records) for name, records in runs.items()},
        "success_on_common": {name: sum(records[k]["status"] == "succeeded" for k in common)
                              for name, records in runs.items()},
        "other_on_common": {name: sum(records[k]["doc_type_key"] == "other" for k in common)
                            for name, records in runs.items()},
        "recognition_median_seconds_on_common": {
            name: round(statistics.median(float(records[k]["recognition_seconds"])
                                          for k in common if records[k]["recognition_seconds"]), 1)
            for name, records in runs.items()},
        "router_requested_image": router_requested_image,
        "duplicate_image_groups": duplicate_image_groups,
        "unique_image_pages": len(common) - sum(len(group) - 1 for group in duplicate_image_groups),
        "gold_pages": len(set(gold) & common),
        "uncovered_pages": [list(k) for k in sorted(common - set(gold))],
        "gold_outside_common": [list(k) for k in sorted(set(gold) - common)],
        "qwen_extra_pages": [list(k) for k in sorted(set(runs["qwen"]) - common)],
        "gemma_extra_pages": [list(k) for k in sorted(set(runs["gemma"]) - common)],
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
    for name, path in RUNS.items():
        by_result = defaultdict(list)
        for entry in read_csv(path / "entries.csv"):
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
    (HERE / "validation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
