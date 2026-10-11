"""Prepare the pinned tokenizer assets and local backend profile for token budgeting."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen2.5vl:7b", help="Ollama model name")
    parser.add_argument("--backend-url", help="Ollama base URL")
    parser.add_argument("--asset-dir", help="directory for pinned tokenizer assets and manifest")
    args = parser.parse_args(argv)

    try:
        from app.core.request_budget import prepare_profile

        profile = prepare_profile(
            args.model,
            backend_url=args.backend_url,
            asset_dir=args.asset_dir,
        )
    except Exception as exc:  # Keep backend/HTTP metadata out of routine CLI output.
        print(f"ERROR: profile preparation failed ({type(exc).__name__})", file=sys.stderr)
        return 1

    profile_id = "unknown"
    if isinstance(profile, dict):
        candidate = profile.get("profile_id", profile.get("id"))
        if isinstance(candidate, str) and candidate.strip():
            profile_id = candidate.strip().split()[0][:80]
    print(f"ready model={args.model} profile={profile_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
