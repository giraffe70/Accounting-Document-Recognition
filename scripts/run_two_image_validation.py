"""Run the live recognition pipeline on two fixed validation images.

The pipeline can rotate images in place.  This runner therefore copies the
source files before inference and records the untouched source filename,
recognition output, and manual ground truth in one JSON evidence file.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SOURCE = ROOT / "ref" / "test_img"
OUT = ROOT / "artifacts" / "two_image_validation"

# Values transcribed from clearly printed fields in the two source images.
# Fields that cannot be read unambiguously from the scans are deliberately not
# scored.  A later, larger validation should replace this with reviewed labels.
GROUND_TRUTH = {
    "test_001.jpg": {
        "voucher_id": "ZP123456789",
        "date": "2022-06-06",
        "invoice_type": "三聯式",
        "tax_type": "應稅",
        "buyer_tax_id": "88888888",
        "item_description": "商品",
        "quantity": 2,
        "unit_price": 500,
        "item_amount": 1000,
        "subtotal": 1000,
        "tax_amount": 50,
        "total_amount": 1050,
    },
    "test_002.jpg": {
        "voucher_id": "ZZ12345678",
        "date": "2025-08-25",
        "invoice_type": "三聯式",
        "tax_type": "應稅",
        "buyer_tax_id": "12345678",
        "item_description": "Excel線上課程",
        "quantity": 1,
        "unit_price": 20000,
        "item_amount": 20000,
        "subtotal": 20000,
        "tax_amount": 1000,
        "total_amount": 21000,
    },
}


def normalize(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(float(value)).rstrip("0").rstrip(".")
    return str(value).replace(" ", "").replace(",", "").replace("（", "(").replace("）", ")").lower()


def extract_scored_fields(voucher):
    fields = voucher.fields or {}
    item = (voucher.items or [None])[0]
    return {
        "voucher_id": fields.get("voucher_id", voucher.voucher_id),
        "date": fields.get("date", voucher.date),
        "invoice_type": fields.get("invoice_type", ""),
        "tax_type": fields.get("tax_type", ""),
        "buyer_tax_id": fields.get("buyer_tax_id", voucher.buyer.tax_id),
        "item_description": item.description if item else "",
        "quantity": item.quantity if item else None,
        "unit_price": item.unit_price if item else None,
        "item_amount": item.amount if item else None,
        "subtotal": fields.get("subtotal", voucher.subtotal),
        "tax_amount": fields.get("tax_amount", voucher.tax_amount),
        "total_amount": fields.get("total_amount", voucher.total_amount),
    }


def main():
    from app.core import pipeline, settings_store
    from app.services import classify

    OUT.mkdir(parents=True, exist_ok=True)
    settings = settings_store.get_settings()
    ocr_runtime = "configured"
    if "--no-ocr" in sys.argv:
        # Diagnostic fallback only.  It preserves saved settings while allowing
        # the VLM path to be evaluated when the local OCR runtime cannot finish.
        from app.core import model_factory
        model_factory.get_ocr = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("OCR disabled for fallback validation")
        )
        ocr_runtime = "disabled for fallback validation"
    results = []
    for source_name, expected in GROUND_TRUTH.items():
        src = SOURCE / source_name
        working = OUT / source_name
        shutil.copy2(src, working)
        started = datetime.now(timezone.utc).isoformat()
        try:
            doc_type_key = pipeline.prepare_image(str(working))
            voucher = pipeline.extract_for_type(str(working), doc_type_key)
            classified = classify.classify(voucher)
            observed = extract_scored_fields(classified)
            comparisons = {
                key: {
                    "expected": value,
                    "observed": observed.get(key),
                    "match": normalize(value) == normalize(observed.get(key)),
                }
                for key, value in expected.items()
            }
            results.append({
                "source_image": str(src.relative_to(ROOT)),
                "working_image": str(working.relative_to(ROOT)),
                "started_at_utc": started,
                "doc_type_key": doc_type_key,
                "recognized": classified.model_dump(mode="json"),
                "scored_fields": observed,
                "ground_truth": expected,
                "comparisons": comparisons,
                "correct_fields": sum(x["match"] for x in comparisons.values()),
                "total_scored_fields": len(comparisons),
            })
            print(f"{source_name}: {results[-1]['correct_fields']}/{results[-1]['total_scored_fields']}")
        except Exception as exc:  # Retain an evidence record even if inference fails.
            results.append({
                "source_image": str(src.relative_to(ROOT)),
                "working_image": str(working.relative_to(ROOT)),
                "started_at_utc": started,
                "ground_truth": expected,
                "error": f"{type(exc).__name__}: {exc}",
                "correct_fields": 0,
                "total_scored_fields": len(expected),
            })
            print(f"{source_name}: ERROR {results[-1]['error']}")

    total = sum(x["total_scored_fields"] for x in results)
    correct = sum(x["correct_fields"] for x in results)
    evidence = {
        "scope": "Initial two-image live-pipeline validation",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "ocr_runtime": ocr_runtime,
        "scoring_rule": "Exact match after removing spaces and thousands separators; only clearly visible fields are scored.",
        "correct_fields": correct,
        "total_scored_fields": total,
        "field_accuracy": correct / total if total else None,
        "results": results,
    }
    out = OUT / "validation_evidence.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Evidence: {out.relative_to(ROOT)}")
    print(f"Overall: {correct}/{total} ({correct / total:.1%})" if total else "Overall: no scored fields")


if __name__ == "__main__":
    main()
