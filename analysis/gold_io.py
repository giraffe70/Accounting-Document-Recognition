"""Read frozen gold or explicitly combine reviewed annotations; never write inputs."""
import json
from pathlib import Path


def load_frozen(path: Path) -> dict:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Gold must be a JSON list")
    result = {}
    for row in rows:
        key = (row["source"], int(row["page"]))
        if key in result:
            raise ValueError(f"Duplicate gold {key}: {path}")
        result[key] = {**row, "page_role": row.get("page_role") or (
            "journal_voucher" if row.get("doc_type_key") == "journal_voucher" else "document")}
    return result


def combine(base_paths, override_paths=()) -> dict:
    result = {}
    for path in base_paths:
        rows = load_frozen(Path(path))
        overlap = result.keys() & rows.keys()
        if overlap:
            raise ValueError(f"Duplicate base gold {sorted(overlap)}")
        result.update(rows)
    for path in override_paths:
        result.update(load_frozen(Path(path)))
    return result
