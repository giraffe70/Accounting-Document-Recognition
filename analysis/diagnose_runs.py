"""Read saved model traces to diagnose router behavior."""
import json
import sqlite3
from collections import Counter
from pathlib import Path

db = Path(__file__).resolve().parents[1] / "app/data/validation/validation.db"
con = sqlite3.connect(db)
for run in ("f89d0bf3f54d4e93ab91e9b82c41beb5", "e0b0cf8e8040411cbdb0b70d8d54f25e"):
    rows = con.execute("SELECT trace_json FROM results WHERE run_id=? AND status='succeeded'", (run,)).fetchall()
    responses = []
    for (raw,) in rows:
        trace = json.loads(raw or "[]")
        responses.append(next((x.get("response", "") for x in trace if x.get("kind") == "model_call"), ""))
    print(run, "n", len(responses),
          "no_image", sum("未提供圖片" in r for r in responses),
          "JSON_type", sum('"type"' in r for r in responses),
          "empty", sum(not r.strip() for r in responses))
    print(*Counter(r[:90] for r in responses).most_common(8), sep="\n")
    extraction = []
    no_image_router = 0
    for (raw,) in rows:
        calls = [x for x in json.loads(raw or "[]") if x.get("kind") == "model_call"]
        if calls and "請提供" in calls[0].get("response", "") and "圖片" in calls[0].get("response", ""):
            no_image_router += 1
        if len(calls) > 1:
            extraction.append(calls[1].get("response", ""))
    print("router_requested_image", no_image_router,
          "extraction_requested_image", sum("請提供" in r and "圖片" in r for r in extraction),
          "extraction_json", sum('"fields"' in r for r in extraction))
