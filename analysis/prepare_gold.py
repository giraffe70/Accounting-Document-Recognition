"""Explicit export of reviewed gold to a new directory, separate from scoring."""
import argparse
import hashlib
import json
from pathlib import Path

try:
    from .gold_io import combine
except ImportError:
    from gold_io import combine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, action="append", required=True)
    parser.add_argument("--override", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        gold = combine(args.base, args.override)
        hashes = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in args.base + args.override}
        args.output_dir.mkdir(parents=True, exist_ok=False)
        (args.output_dir / "gold_standard.json").write_text(
            json.dumps([gold[k] for k in sorted(gold)], ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output_dir / "manifest.json").write_text(
            json.dumps({"input_sha256": hashes, "override_order": [str(p) for p in args.override]},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(args.output_dir / "gold_standard.json")


if __name__ == "__main__":
    main()
