"""Execution-local progress callback; never shares job state between users."""
from contextvars import ContextVar
from contextlib import contextmanager

_callback = ContextVar("import_progress", default=None)


@contextmanager
def capture(callback):
    token = _callback.set(callback)
    try:
        yield
    finally:
        _callback.reset(token)


def emit(phase):
    callback = _callback.get()
    if callback:
        callback(phase)
