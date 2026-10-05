"""Score live full-image recognition against the reviewed, visible core fields.

The ground truth deliberately includes only values that are legible on the
source scan.  Empty / uncertain boxes are not counted, so the score measures
recognition accuracy rather than a mixture of recognition and annotation guesswork.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "artifacts" / "full_image_validation" / "recognition_evidence.json"
OUT = ROOT / "artifacts" / "full_image_validation" / "scored_evidence.json"

# Human-reviewed transcription from the visible source scans.  Dates are
# standardized to Gregorian YYYY-MM-DD; numeric fields use plain numbers.
GROUND_TRUTH = {
    "test_001.jpg": {"doc_type_key": "invoice", "voucher_id": "ZP123456789", "date": "2022-06-06", "buyer_tax_id": "88888888", "subtotal": 1000, "tax_amount": 50, "total_amount": 1050},
    "test_002.jpg": {"doc_type_key": "invoice", "voucher_id": "ZZ12345678", "date": "2025-08-25", "buyer_tax_id": "12345678", "subtotal": 20000, "tax_amount": 1000, "total_amount": 21000},
    "test_003.jpg": {"doc_type_key": "invoice", "date": "2021-05-30", "seller_tax_id": "80627602", "buyer_tax_id": "12345678", "subtotal": 300, "tax_amount": 15, "total_amount": 315},
    "test_004.jpg": {"doc_type_key": "invoice", "voucher_id": "01737757", "date": "2005-10-05", "seller_tax_id": "22930866", "subtotal": 387, "tax_amount": 19, "total_amount": 406},
    "test_005.jpg": {"doc_type_key": "invoice", "voucher_id": "08461001", "date": "2017-07-21", "subtotal": 7619, "tax_amount": 381, "total_amount": 8000},
    "test_006.jpg": {"doc_type_key": "invoice", "voucher_id": "AU23857150", "date": "2020-08-27", "buyer_tax_id": "L1234567", "subtotal": 585900, "tax_amount": 29295, "total_amount": 615195},
    "test_007.jpg": {"doc_type_key": "invoice", "voucher_id": "RA25842705", "date": "2010-11-03", "seller_tax_id": "24335250", "total_amount": 512},
    "test_008.jpg": {"doc_type_key": "receipt", "date": "2013-09-08"},
    "test_009.jpg": {"doc_type_key": "receipt", "date": "2015-01-14", "total_amount": 8725},
    "test_010.jpg": {"doc_type_key": "receipt", "date": "2011-12-21", "buyer_tax_id": "04126516", "seller_tax_id": "05169809", "total_amount": 30},
    "test_011.jpg": {"doc_type_key": "invoice", "voucher_id": "12167863", "date": "2018-02-27", "seller_tax_id": "10847152", "total_amount": 7420},
    "test_012.jpg": {"doc_type_key": "receipt", "date": "2018-01-29", "seller_tax_id": "13593857", "total_amount": 85000},
    "test_013.jpg": {"doc_type_key": "invoice", "voucher_id": "CV43689184", "date": "2016-08-23", "buyer_tax_id": "73502634", "seller_tax_id": "16106654", "subtotal": 3333, "tax_amount": 167, "total_amount": 3500},
    "test_014.jpg": {"doc_type_key": "invoice", "voucher_id": "BK52686868", "date": "2014-08-16", "buyer_tax_id": "73502634", "seller_tax_id": "53742663", "total_amount": 3600},
    "test_015.jpg": {"doc_type_key": "invoice", "voucher_id": "NG16471850", "date": "2013-08-01", "buyer_tax_id": "73502634", "seller_tax_id": "69403860", "subtotal": 4580, "tax_amount": 229, "total_amount": 4809},
    "test_016.jpg": {"doc_type_key": "invoice", "voucher_id": "PK34389027", "date": "2013-10-25", "seller_tax_id": "23172516", "total_amount": 102995},
    "test_017.jpg": {"doc_type_key": "invoice", "voucher_id": "YW31692468", "date": "2018-01-10", "seller_tax_id": "53618192", "total_amount": 5925},
    "test_018.jpg": {"doc_type_key": "receipt", "seller_tax_id": "93860530", "total_amount": 460},
    "test_019.jpg": {"doc_type_key": "receipt", "date": "2013-10-14", "total_amount": 300},
    "test_020.jpg": {"doc_type_key": "invoice", "voucher_id": "DA31298502", "date": "2016-08-17", "seller_tax_id": "22808339", "subtotal": 11000, "tax_amount": 550, "total_amount": 11550},
    "test_021.jpg": {"doc_type_key": "invoice", "voucher_id": "SG15974151", "date": "2015-11-27", "buyer_tax_id": "74502634", "seller_tax_id": "28386770", "subtotal": 42857, "tax_amount": 2143, "total_amount": 45000},
    "test_022.jpg": {"doc_type_key": "invoice", "voucher_id": "SG52106184", "buyer_tax_id": "73502634", "seller_tax_id": "22933006", "subtotal": 66667, "tax_amount": 3333, "total_amount": 70000},
    "test_023.jpg": {"doc_type_key": "invoice", "voucher_id": "DA36250219", "date": "2016-08-10", "seller_tax_id": "84683507"},
    "test_024.jpg": {"doc_type_key": "receipt", "date": "2016-08-01", "seller_tax_id": "56974551", "total_amount": 800},
    "test_025.jpg": {"doc_type_key": "receipt", "date": "2013-09-01", "total_amount": 400},
    "test_026.jpg": {"doc_type_key": "receipt", "date": "2014-06-25", "seller_tax_id": "14361095"},
    "test_027.jpg": {"doc_type_key": "receipt", "date": "2014-05-31", "total_amount": 1290},
    "test_028.jpg": {"doc_type_key": "receipt", "date": "2014-08-21", "seller_tax_id": "99120348", "total_amount": 2926},
    "test_029.jpg": {"doc_type_key": "receipt", "total_amount": 210},
    "test_030.jpg": {"doc_type_key": "invoice", "voucher_id": "DS12038456", "date": "2016-10-03", "seller_tax_id": "23205498", "subtotal": 18000, "tax_amount": 900, "total_amount": 18900},
    "test_031.jpg": {"doc_type_key": "journal_voucher", "date": "2014-04-09", "voucher_id": "103040900004", "debit_total": 5000, "credit_total": 5000},
    "test_032.jpg": {"doc_type_key": "journal_voucher", "date": "2000-03-09", "voucher_id": "089030901", "debit_total": 1200, "credit_total": 1200},
    "test_033.jpg": {"doc_type_key": "journal_voucher", "date": "2022-05-26", "voucher_id": "110526001", "debit_total": 10000, "credit_total": 10000},
    "test_034.jpg": {"doc_type_key": "journal_voucher", "date": "2009-04-23", "voucher_id": "980423-001", "debit_total": 35700, "credit_total": 35700},
    "test_035.jpg": {"doc_type_key": "journal_voucher", "date": "2014-04-09", "voucher_id": "103040900004", "debit_total": 5000, "credit_total": 5000},
    "test_036.jpg": {"doc_type_key": "journal_voucher", "date": "2021-02-18", "debit_total": 367673, "credit_total": 367673},
    "test_037.jpg": {"doc_type_key": "journal_voucher", "date": "2012-08-19", "voucher_id": "101080002", "debit_total": 10500, "credit_total": 10500},
    "test_038.jpg": {"doc_type_key": "journal_voucher", "date": "2003-08-16", "voucher_id": "0920816001", "debit_total": 1000, "credit_total": 1000},
    "test_039.jpg": {"doc_type_key": "journal_voucher", "date": "2009-01-31", "voucher_id": "9801310001", "debit_total": 4200, "credit_total": 4200},
    "test_040.jpg": {"doc_type_key": "journal_voucher", "date": "2018-09-26", "voucher_id": "ES1070926003", "debit_total": 51030, "credit_total": 51030},
}


def normalize(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(float(value)).rstrip("0").rstrip(".")
    value = str(value).strip().upper()
    return re.sub(r"[\s,，()（）$]", "", value)


def observed(record: dict, key: str):
    if key == "doc_type_key":
        return record.get("doc_type_key", "")
    data = record.get("recognized") or {}
    fields = data.get("fields") or {}
    return fields.get(key, data.get(key, ""))


def main() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    by_name = {Path(r["source_image"]).name: r for r in evidence.get("results", [])}
    results, all_rows = [], []
    for name in sorted(GROUND_TRUTH):
        record = by_name.get(name, {"source_image": f"ref/test_img/{name}", "error": "No recognition record"})
        comparisons = []
        for key, expected in GROUND_TRUTH[name].items():
            actual = observed(record, key)
            match = normalize(expected) == normalize(actual)
            row = {"field": key, "expected": expected, "observed": actual, "match": match}
            comparisons.append(row)
            all_rows.append(row)
        results.append({"source_image": record.get("source_image"), "doc_type_key": record.get("doc_type_key", ""),
                        "error": record.get("error"), "comparisons": comparisons,
                        "correct_fields": sum(x["match"] for x in comparisons), "total_scored_fields": len(comparisons)})
    by_field = {}
    for row in all_rows:
        b = by_field.setdefault(row["field"], {"correct": 0, "total": 0})
        b["total"] += 1
        b["correct"] += int(row["match"])
    correct, total = sum(x["match"] for x in all_rows), len(all_rows)
    output = {"scope": "40 test_img files; manually reviewed clearly visible core fields", "scoring_rule": "Exact match after space, comma, case and simple number-format normalization", "correct_fields": correct, "total_scored_fields": total, "field_accuracy": correct / total if total else None, "per_field": by_field, "results": results}
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{correct}/{total} {correct / total:.1%}")
    for field, summary in by_field.items():
        print(f"{field}: {summary['correct']}/{summary['total']}")


if __name__ == "__main__":
    main()
