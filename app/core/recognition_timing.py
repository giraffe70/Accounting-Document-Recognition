"""Non-overlapping leaf-call timings; initialization is a subset of OCR time."""
VERSION = 2
FIELDS = ("orientation_ocr_seconds", "full_ocr_seconds", "supplement_ocr_seconds",
          "ocr_seconds", "model_seconds", "ocr_cache_seconds", "ocr_init_seconds",
          "ocr_cache_hits", "ocr_cache_misses", "classification_seconds", "extraction_seconds")
CSV_FIELDS = ("timings_version", *FIELDS, "queue_seconds")


def from_trace(trace):
    result = {key: 0 for key in FIELDS}
    task = None
    for record in trace or []:
        kind = record.get("kind")
        if kind in ("structured_round", "output_mode"):
            task = record.get("task")
        if kind == "ocr_result":
            seconds = float(record.get("seconds", 0))
            purpose = record.get("purpose", "full")
            field = ("orientation_ocr_seconds" if purpose == "orientation" else
                     "supplement_ocr_seconds" if purpose.startswith("supplement") else "full_ocr_seconds")
            result[field] += seconds
            result["ocr_seconds"] += seconds
            result["ocr_cache_seconds"] += float(record.get("cache_seconds", 0))
            result["ocr_init_seconds"] += float(record.get("engine_init_seconds", 0))
            if record.get("cache_enabled"):
                result["ocr_cache_hits" if record.get("cache_hit") else "ocr_cache_misses"] += 1
        elif kind == "model_call":
            seconds = float(record.get("seconds", 0))
            result["model_seconds"] += seconds
            if task in ("layout", "classify"):
                result["classification_seconds"] += seconds
            else:
                result["extraction_seconds"] += seconds
    kinds = [record.get("kind") for record in trace or []]
    incomplete = (kinds.count("ocr_call_start") > kinds.count("ocr_result") or
                  kinds.count("model_call_start") > kinds.count("model_call"))
    return {"timings_version": VERSION, "incomplete": incomplete,
            **{key: round(value, 6) for key, value in result.items()}}


def merge(*parts):
    result = {key: 0 for key in FIELDS}
    for part in parts:
        for key in FIELDS:
            result[key] += (part or {}).get(key, 0) or 0
    return {"timings_version": VERSION, "incomplete": any((part or {}).get("incomplete") for part in parts),
            **{key: round(value, 6) for key, value in result.items()}}


def csv_values(timings):
    timings = timings or {}
    # Legacy stage measurements are not comparable to leaf-call timings.
    return {key: timings.get(key) if timings.get("timings_version") == VERSION else None
            for key in CSV_FIELDS}
