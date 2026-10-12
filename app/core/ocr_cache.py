"""Work-scoped OCR evidence in input pixels; no model-derived or mapped boxes."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from weakref import WeakValueDictionary

from PIL import Image
from app.core import invocation_trace

VERSION = 1
_current = ContextVar("ocr_cache_scope", default=None)
_purpose = ContextVar("ocr_purpose", default="full")
_registry_guard = threading.Lock()
_locks = WeakValueDictionary()


@dataclass
class _Scope:
    directory: Path | None
    memory: dict = field(default_factory=dict)


@contextmanager
def scope(directory=None):
    """Nested recognition uses its parent's scope, including across copied images."""
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    token = _current.set(_Scope(Path(directory) if directory is not None else None))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def scoped(function):
    """Standalone pipeline calls get bounded in-memory reuse, never a global cache."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with scope():
            return function(*args, **kwargs)
    return wrapped


@contextmanager
def purpose(name):
    token = _purpose.set(name)
    try:
        yield
    finally:
        _purpose.reset(token)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def pixels(image_path):
    # All production OCR inputs are physically oriented RGB copies. The frame is
    # local to these exact pixels; source/display transforms are applied later.
    with Image.open(image_path) as image:
        rgb = image.convert("RGB")
        return {"pixel_sha256": hashlib.sha256(rgb.tobytes()).hexdigest(),
                "size": list(rgb.size), "mode": "RGB", "frame": "ocr_input_pixels",
                "exif_orientation": image.getexif().get(274, 1)}


def seal(record):
    return {**record, "checksum": digest(record)}


def valid(record, identity):
    if not isinstance(record, dict) or record.get("identity") != identity:
        return False
    try:
        return record.get("checksum") == digest({k: v for k, v in record.items() if k != "checksum"})
    except (ValueError, TypeError):
        return False


def _valid_lines(lines):
    if not isinstance(lines, list):
        return False
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            return False
        if not isinstance(line.get("box"), list) or not isinstance(line.get("score"), (int, float)):
            return False
        if not math.isfinite(line["score"]):
            return False
        for point in line["box"]:
            if not isinstance(point, list) or len(point) != 2:
                return False
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in point):
                return False
    return True


def _error(operation, exc):
    invocation_trace.add({"kind": "ocr_cache_error", "operation": operation,
                          "error": f"{type(exc).__name__}: {exc}"})


def _mutex(name):
    with _registry_guard:
        mutex = _locks.get(name)
        if mutex is None:
            mutex = threading.RLock()
            _locks[name] = mutex
    return mutex


@contextmanager
def _key_lock(path):
    """OS locks release on process termination; the lock file is never unlinked."""
    with _mutex(str(path.resolve())):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"0")
                    stream.flush()
                while True:
                    stream.seek(0)
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in (13, 11, 36):
                            raise
                        time.sleep(.05)
                unlock = lambda: (stream.seek(0), msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1))
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            try:
                yield
            finally:
                try:
                    unlock()
                except OSError as exc:
                    _error("unlock", exc)


def recognize(image_path, signature, compute, metrics):
    """Look up before compute (and before Paddle imports); errors are not cached."""
    started = time.perf_counter()
    current = _current.get()
    identity, key, record = None, None, None
    execution_seconds = 0.0
    attempted = False
    source = {"image": str(image_path), "purpose": _purpose.get(),
              "trace": str(invocation_trace.sink_path() or "")}
    invocation_trace.add({"kind": "ocr_call_start", **source})
    try:
        if current is not None:
            try:
                identity = {"version": VERSION, **pixels(image_path), "engine": deepcopy(signature)}
                key = digest(identity)
            except (OSError, ValueError, TypeError) as exc:
                _error("identity", exc)
        with ExitStack() as locks:
            saved = None
            if key:
                locks.enter_context(_mutex(f"scope:{id(current)}:{key}"))
            if key and current.directory is not None:
                try:
                    saved = current.directory / f"{key}.json"
                    locks.enter_context(_key_lock(saved.with_suffix(".lock")))
                except OSError as exc:
                    saved = None
                    _error("lock", exc)
            if key:
                record = current.memory.get(key)
                if record is None and saved is not None:
                    try:
                        candidate = json.loads(saved.read_text(encoding="utf-8"))
                        if valid(candidate, identity) and _valid_lines(candidate.get("lines")):
                            record = candidate
                        else:
                            _error("read", ValueError("invalid OCR cache record"))
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError, TypeError) as exc:
                        _error("read", exc)
            if record is None:
                attempted = True
                compute_started = time.perf_counter()
                try:
                    lines = compute()
                finally:
                    execution_seconds = time.perf_counter() - compute_started
                if key and _valid_lines(lines):
                    record = seal({"identity": identity, "lines": deepcopy(lines),
                                   "text": "\n".join(line["text"] for line in lines), "source": source})
                    current.memory[key] = record
                    if saved is not None:
                        try:
                            invocation_trace.atomic_json(saved, record)
                        except OSError as exc:
                            _error("write", exc)
            else:
                lines = deepcopy(record["lines"])
    except BaseException as exc:
        _trace(image_path, key, source, record, attempted, execution_seconds, started, metrics,
               [], f"{type(exc).__name__}: {exc}")
        raise
    _trace(image_path, key, source, record, attempted, execution_seconds, started, metrics, lines)
    return lines


def _trace(image, key, source, record, executed, seconds, started, metrics, lines, error=None):
    invocation_trace.add({"kind": "ocr_result", "provider": "paddle", "image": str(image),
                          "purpose": source["purpose"], "seconds": round(seconds, 6),
                          "cache_seconds": round(max(0, time.perf_counter() - started - seconds), 6),
                          "cache_hit": not executed and record is not None, "cache_enabled": key is not None,
                          "cache_key": key, "result_source": (record or {}).get("source", source),
                          "coordinate_frame": "ocr_input_pixels", "lines": lines,
                          "engine_init_seconds": round(metrics.get("engine_init_seconds", 0), 6),
                          "predict_seconds": round(metrics.get("predict_seconds", 0), 6),
                          **({"error": error} if error else {})})
