"""Versioned, execution-scoped OCR policy. Safe defaults need no new model."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from app import config

VERSION = 1
_override = ContextVar("ocr_policy", default=None)
RESOLUTIONS = ("fixed_1600", "fixed_2000", "fixed_2400", "adaptive")


def get_policy():
    value = {"version": VERSION, "orientation_mode": os.getenv("OCR_ORIENTATION_MODE", "legacy"),
             "resolution": os.getenv("OCR_RESOLUTION_POLICY", "fixed_2400"),
             "orientation_threshold": .90,
             "orientation_model_dir": os.getenv("OCR_DOC_ORIENTATION_MODEL_DIR", str(config.DATA_DIR / "models" / "doc_ori"))}
    value.update(_override.get() or {})
    if value["resolution"] not in RESOLUTIONS or value["orientation_mode"] not in ("legacy", "auto"):
        raise ValueError("Invalid OCR policy")
    if not 0 <= float(value["orientation_threshold"]) <= 1:
        raise ValueError("Invalid orientation threshold")
    return value


@contextmanager
def isolated_policy(values):
    token = _override.set({**get_policy(), **(values or {})})
    try:
        yield get_policy()
    finally:
        _override.reset(token)
