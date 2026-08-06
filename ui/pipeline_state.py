"""
ui/pipeline_state.py — Thread-safe shared progress state for the pipeline.

Supports multi-tenant / multi-job state isolation by keying progress states
by `job_id`. Fully backward compatible with single-job usage.

Schema
------
stage        int         1-5, maps to pipeline stage
stage_label  str         Human-readable current stage name
pct          float       0.0-100.0  overall progress within current stage
sub_message  str         Rotating tip / status detail
ffmpeg_speed str         e.g. "2.4x" (Stage 2 only)
ffmpeg_eta   str         e.g. "~42s" (Stage 2 only)
ffmpeg_pct   float       0.0-100.0 transcoding progress (Stage 2)
retry_state  dict        {attempt:int, wait_seconds:int, is_retrying:bool}
word_count   int         Streamed words so far (Stage 4)
sections_done list[str]  Markdown section titles completed (Stage 4)
bytes_sent   int         Upload bytes sent so far (Stage 3)
bytes_total  int         Total upload bytes (Stage 3)
upload_speed str         e.g. "4.2 MB/s" (Stage 3)
skipped_transcode bool   True when Stage 2 was not needed
done         bool        Set True by cleanup to trigger crossfade
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_DEFAULT_KEY = "global"


def _default_state() -> dict[str, Any]:
    return {
        "stage":            1,
        "stage_label":      "Uploading…",
        "pct":              0.0,
        "sub_message":      "",
        "ffmpeg_speed":     "",
        "ffmpeg_eta":       "",
        "ffmpeg_pct":       0.0,
        "retry_state":      {"attempt": 0, "wait_seconds": 0, "is_retrying": False},
        "word_count":       0,
        "sections_done":    [],
        "bytes_sent":       0,
        "bytes_total":      0,
        "upload_speed":     "",
        "skipped_transcode": False,
        "done":             False,
    }


# Map of job_id -> state dict
_STATES: dict[str, dict[str, Any]] = {
    _DEFAULT_KEY: _default_state()
}


def reset(job_id: str | None = None) -> None:
    """Reset all fields for the given job_id (or global default) to defaults."""
    key = job_id or _DEFAULT_KEY
    with _lock:
        _STATES[key] = _default_state()


def update(job_id: str | None = None, **kwargs: Any) -> None:
    """Atomically update one or more fields for job_id (or global default)."""
    if "job_id" in kwargs:
        key = kwargs.pop("job_id") or _DEFAULT_KEY
    elif job_id and isinstance(job_id, str):
        key = job_id
    else:
        key = _DEFAULT_KEY

    with _lock:
        if key not in _STATES:
            _STATES[key] = _default_state()
        _STATES[key].update(kwargs)


def update_job(job_id: str, **kwargs: Any) -> None:
    """Explicit multi-tenant update helper keyed by job_id."""
    key = job_id or _DEFAULT_KEY
    with _lock:
        if key not in _STATES:
            _STATES[key] = _default_state()
        _STATES[key].update(kwargs)


def get(key: str, default: Any = None, job_id: str | None = None) -> Any:
    """Thread-safe single-field read."""
    j_key = job_id or _DEFAULT_KEY
    with _lock:
        state = _STATES.get(j_key, _STATES.get(_DEFAULT_KEY, {}))
        return state.get(key, default)


def snapshot(job_id: str | None = None) -> dict[str, Any]:
    """Return a shallow copy of the full state (safe to read from UI thread)."""
    j_key = job_id or _DEFAULT_KEY
    with _lock:
        state = _STATES.get(j_key, _STATES.get(_DEFAULT_KEY, {}))
        return dict(state)


def cleanup_job(job_id: str) -> None:
    """Remove completed job state from memory."""
    with _lock:
        _STATES.pop(job_id, None)
