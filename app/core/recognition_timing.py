"""Non-overlapping leaf-call timings; initialization is a subset of OCR time."""
VERSION = 3
FIELDS = ("orientation_ocr_seconds", "full_ocr_seconds", "supplement_ocr_seconds",
          "ocr_seconds", "model_seconds", "ocr_cache_seconds", "ocr_init_seconds",
          "ocr_cache_hits", "ocr_cache_misses", "classification_seconds", "extraction_seconds",
          "orientation_model_seconds", "orientation_model_init_seconds",
          "resolution_assessment_seconds", "first_result_seconds")
CSV_FIELDS = ("timings_version", *FIELDS, "queue_seconds")


def from_trace(trace):
    result = {key: 0 for key in FIELDS}
    result["first_result_seconds"] = None
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
        elif kind == "orientation_classifier":
            result["orientation_model_seconds"] += float(record.get("seconds", 0))
            result["orientation_model_init_seconds"] += float(record.get("engine_init_seconds", 0))
        elif kind == "resolution_assessment":
            result["resolution_assessment_seconds"] += float(record.get("seconds", 0))
        elif kind == "first_result":
            seconds = float(record.get("seconds", 0))
            if result["first_result_seconds"] is None or seconds < result["first_result_seconds"]:
                result["first_result_seconds"] = seconds
    kinds = [record.get("kind") for record in trace or []]
    incomplete = (kinds.count("ocr_call_start") > kinds.count("ocr_result") or
                  kinds.count("model_call_start") > kinds.count("model_call") or
                  kinds.count("orientation_classifier_start") > kinds.count("orientation_classifier"))
    return {"timings_version": VERSION, "incomplete": incomplete,
            **{key: round(value, 6) if value is not None else None for key, value in result.items()}}


def merge(*parts):
    result = {key: 0 for key in FIELDS}
    first_results = []
    for part in parts:
        for key in FIELDS:
            value = (part or {}).get(key)
            if key == "first_result_seconds":
                if value is not None:
                    first_results.append(value)
            else:
                result[key] += value or 0
    result["first_result_seconds"] = min(first_results) if first_results else None
    return {"timings_version": VERSION, "incomplete": any((part or {}).get("incomplete") for part in parts),
            **{key: round(value, 6) if value is not None else None for key, value in result.items()}}


def csv_values(timings):
    timings = timings or {}
    # Legacy stage measurements are not comparable to leaf-call timings.
    version = timings.get("timings_version")
    if version not in (2, VERSION):
        return {key: None for key in CSV_FIELDS}
    values = {key: timings.get(key) for key in CSV_FIELDS}
    if version == 2:
        for key in FIELDS[11:]:
            values[key] = None
    return values
