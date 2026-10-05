"""Context-local model/stage tracing used by dataset validation."""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar

_collector: ContextVar[list | None] = ContextVar("model_invocation_trace", default=None)


def _safe(value):
    if isinstance(value, list):
        return [_safe(x) for x in value]
    if isinstance(value, dict):
        return {key: (f"<embedded image: {len(val)} chars>"
                      if key == "url" and isinstance(val, str) and val.startswith("data:image/")
                      else _safe(val)) for key, val in value.items()}
    return value


@contextmanager
def capture():
    records = []
    token = _collector.set(records)
    try:
        yield records
    finally:
        _collector.reset(token)


def add(record: dict) -> None:
    target = _collector.get()
    if target is not None:
        target.append(_safe(record))


@contextmanager
def timed_call(content, attempt: int):
    started = time.perf_counter()
    record = {"kind": "model_call", "attempt": attempt, "input": _safe(content)}
    try:
        yield record
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record["seconds"] = round(time.perf_counter() - started, 6)
        add(record)


def record_stage(name: str, note: str, seconds: float) -> None:
    add({"kind": "stage", "stage": name, "note": note,
         "seconds": round(seconds, 6)})
