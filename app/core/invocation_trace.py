"""Context-local model/stage tracing used by dataset validation."""
from __future__ import annotations

import time
import json
import os
import re
import uuid
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

_collector: ContextVar[list | None] = ContextVar("model_invocation_trace", default=None)
_sink: ContextVar[Path | None] = ContextVar("trace_sink", default=None)


def sink_path():
    return _sink.get()


def _safe(value):
    if isinstance(value, str) and value.startswith("data:image/"):
        return f"<embedded image: {len(value)} chars>"
    if isinstance(value, str):
        return re.sub(r"data:image/[^;,\s\"']+;base64,[A-Za-z0-9+/=\r\n]+", "<embedded image>", value)
    if isinstance(value, list):
        return [_safe(x) for x in value]
    if isinstance(value, dict):
        return {key: (f"<embedded image: {len(val)} chars>"
                      if key == "url" and isinstance(val, str) and val.startswith("data:image/")
                      else _safe(val)) for key, val in value.items()}
    return value


def atomic_json(path, value):
    """Same-directory replacement leaves the last complete journal readable after termination."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(_safe(value), stream, ensure_ascii=False, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(8):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            # Windows scanners may briefly hold a newly replaced file open.
            if attempt == 7:
                raise
            time.sleep(.025 * (attempt + 1))


@contextmanager
def capture(path=None):
    records = []
    token = _collector.set(records)
    sink_token = _sink.set(Path(path) if path else None)
    try:
        if path:
            atomic_json(path, records)
        yield records
    except BaseException as exc:
        add({"kind": "failure", "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if path:
            atomic_json(path, records)
        _sink.reset(sink_token)
        _collector.reset(token)


def add(record: dict) -> None:
    target = _collector.get()
    if target is not None:
        target.append(_safe(record))
        if _sink.get():
            atomic_json(_sink.get(), target)


@contextmanager
def timed_call(content, attempt: int, parameters=None):
    started = time.perf_counter()
    record = {"kind": "model_call", "attempt": attempt, "input": _safe(content)}
    record["parameters"] = parameters or {}
    add({**record, "kind": "model_call_start"})
    try:
        yield record
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["error_code"] = getattr(exc,"code",None)
        record["status_code"] = getattr(exc,"status_code",None)
        try:
            backend = json.loads(getattr(exc,"error", ""))
            record["backend_error"] = backend.get("error",backend)
        except (ValueError, TypeError, AttributeError):
            pass
        raise
    finally:
        record["seconds"] = round(time.perf_counter() - started, 6)
        add(record)


def record_stage(name: str, note: str, seconds: float) -> None:
    add({"kind": "stage", "stage": name, "note": note,
         "seconds": round(seconds, 6)})
