"""
ui/queue_status.py — Non-blocking queue status renderer.

Uses @st.fragment(run_every="2s") to poll the background QueueManager every
2 seconds without blocking the Streamlit/Tornado WebSocket thread.

When a job finishes (DONE or FAILED):
  1. Writes result / error into st.session_state.
  2. Calls st.rerun(scope="app") to trigger a FULL-PAGE rerun so the
     summary panel or error panel becomes visible.

Design decisions:
  - scope="app" is required for a full rerun from inside a fragment.
    Plain st.rerun() inside a fragment only reruns the fragment itself.
  - A 10-minute hard timeout prevents infinite polling if the job stalls.
  - No time.sleep() is ever used — the fragment scheduler handles timing.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

import streamlit as st

import ui.pipeline_state as ps
from ui.loading import show_loading_ui
from queue_worker import JobStatus, get_queue_manager

logger = logging.getLogger(__name__)

# Maximum wall-clock seconds to poll before giving up (safety net)
_POLL_TIMEOUT_SECONDS = 600


# ---------------------------------------------------------------------------
# Resolve fragment decorator (Streamlit ≥1.37 native / ≥1.33 experimental)
# ---------------------------------------------------------------------------

_fragment: Callable | None = getattr(st, "fragment", getattr(st, "experimental_fragment", None))


# ---------------------------------------------------------------------------
# Core polling body
# ---------------------------------------------------------------------------

def _queue_status_body() -> None:
    """
    Poll the active queue job and render the appropriate loading card.
    Called every 2 seconds by the fragment scheduler.
    Handles job completion by writing to session_state and triggering a
    full-page app rerun.
    """
    job_id: str | None = st.session_state.get("queue_job_id")
    if not job_id:
        return

    manager = get_queue_manager()
    job = manager.get_job(job_id)

    # Job evicted from registry (shouldn't happen within TTL, but be defensive)
    if not job:
        logger.warning("Job %s not found in registry — clearing queue_job_id", job_id[:8])
        st.session_state.queue_job_id = None
        st.session_state.processing = False
        st.session_state.error_msg = (
            "Processing job lost (server may have restarted). Please try again."
        )
        _full_rerun()
        return

    # ── Hard timeout guard ──────────────────────────────────────────────────
    poll_start: float = st.session_state.get("_queue_poll_start", 0.0)
    if poll_start == 0.0:
        st.session_state["_queue_poll_start"] = time.monotonic()
    elif time.monotonic() - poll_start > _POLL_TIMEOUT_SECONDS:
        logger.error("Job %s timed out after %ds — aborting poll", job_id[:8], _POLL_TIMEOUT_SECONDS)
        st.session_state.queue_job_id = None
        st.session_state.processing = False
        st.session_state["_queue_poll_start"] = 0.0
        st.session_state.error_msg = (
            f"Processing timed out after {_POLL_TIMEOUT_SECONDS // 60} minutes. "
            "Please try again with a shorter file."
        )
        _full_rerun()
        return

    # ── Render status cards ─────────────────────────────────────────────────
    if job.status == JobStatus.PENDING:
        _render_pending_panel(job.position)
        return

    if job.status == JobStatus.PROCESSING:
        _render_active_pipeline(job_id)
        return

    # ── DONE or FAILED ──────────────────────────────────────────────────────
    st.session_state.queue_job_id = None
    st.session_state.processing = False
    st.session_state["_queue_poll_start"] = 0.0   # reset timeout counter

    if job.status == JobStatus.DONE and job.result is not None:
        st.session_state.result = job.result
        st.session_state.current_remote_file = job.result.remote_file_name
        logger.info("Job %s completed — summary ready (%d chars)",
                    job_id[:8], len(job.result.summary_markdown))
    else:
        error_detail = job.error or "Unknown error"
        st.session_state.error_msg = error_detail
        st.session_state.result = None
        logger.error("Job %s failed: %s", job_id[:8], error_detail)

    _full_rerun()



# ---------------------------------------------------------------------------
# Exported function: render_queue_status()
# ---------------------------------------------------------------------------

if _fragment is not None:
    render_queue_status: Callable = _fragment(run_every="2s")(_queue_status_body)
else:
    # Fallback for very old Streamlit: call directly without fragment scheduler.
    # The caller in app.py is responsible for triggering reruns in this case.
    render_queue_status = _queue_status_body


# ---------------------------------------------------------------------------
# Helper: full-page rerun from inside or outside a fragment
# ---------------------------------------------------------------------------

def _full_rerun() -> None:
    """
    Trigger a full Streamlit app rerun.

    When called inside a fragment, plain st.rerun() only reruns the fragment.
    st.rerun(scope="app") (Streamlit ≥1.37) forces a full-page rerun.
    We try scope="app" first and fall back gracefully.
    """
    try:
        st.rerun(scope="app")
    except TypeError:
        # Older Streamlit: st.rerun() has no scope parameter.
        # If called outside a fragment this is a full rerun anyway.
        st.rerun()


# ---------------------------------------------------------------------------
# Stage renderers
# ---------------------------------------------------------------------------

def _render_pending_panel(position: int) -> None:
    """Render the 'Waiting in queue' loading card."""
    show_loading_ui(
        stage=1,
        progress=0.0,
        stage_message=f"Waiting in server processing queue (position {position})…",
        queue_position=position,
    )


def _render_active_pipeline(job_id: str | None = None) -> None:
    """Read pipeline_state snapshot and render the appropriate progress card."""
    state = ps.snapshot(job_id=job_id)
    stage: float | int = state.get("stage", 1)
    skipped: list[int] = [2] if state.get("skipped_transcode", False) else []
    file_name: str = state.get("sub_message", "")
    file_size_bytes: int = state.get("bytes_total", 0)

    from file_handler import human_readable_size
    size_str = human_readable_size(file_size_bytes) if file_size_bytes > 0 else ""

    _STAGE_RENDERERS = {
        1:   _stage_upload,
        1.5: _stage_audio_extract,
        2:   _stage_transcode,
        3:   _stage_cloud_upload,
        4:   _stage_ai_generate,
        5:   _stage_cleanup,
    }

    renderer = _STAGE_RENDERERS.get(stage)
    if renderer:
        renderer(state, file_name, size_str, skipped)
    else:
        # Unknown / intermediate stage — generic fallback
        show_loading_ui(
            stage=stage,
            progress=state.get("pct", 0.0),
            stage_message=state.get("stage_label", "Processing…"),
            skipped_stages=skipped,
        )


def _stage_upload(state: dict, file_name: str, size_str: str, skipped: list[int]) -> None:
    show_loading_ui(
        stage=1,
        progress=state.get("pct", 100.0),
        stage_message=f"Uploaded '{file_name or 'media file'}'",
        file_name=file_name,
        file_size=size_str,
        file_format=Path(file_name).suffix.lstrip(".").upper() or "AUDIO",
        is_compatible=True,
        skipped_stages=skipped,
    )


def _stage_audio_extract(state: dict, file_name: str, size_str: str, skipped: list[int]) -> None:
    show_loading_ui(
        stage=1.5,
        progress=50.0,
        stage_message="Extracting audio track from video (MP4 → MP3)…",
        file_name=file_name,
        file_size=size_str,
        file_format="MP4",
        conversion_mode="mp3",
        skipped_stages=skipped,
    )


def _stage_transcode(state: dict, file_name: str, size_str: str, skipped: list[int]) -> None:
    show_loading_ui(
        stage=2,
        progress=state.get("ffmpeg_pct", 0.0),
        stage_message="Converting mobile video (HEVC → H.264) for AI processing…",
        file_name=file_name,
        file_size=size_str,
        file_format="HEVC",
        is_compatible=False,
        ffmpeg_speed=state.get("ffmpeg_speed", ""),
        ffmpeg_eta=state.get("ffmpeg_eta", ""),
        skipped_stages=skipped,
    )


def _stage_cloud_upload(state: dict, file_name: str, size_str: str, skipped: list[int]) -> None:
    retry_state: dict = state.get("retry_state", {})
    show_loading_ui(
        stage=3,
        progress=state.get("pct", 50.0),
        stage_message="Streaming media to High-Speed AI Cloud…",
        file_name=file_name,
        file_size=size_str,
        retry_attempt=retry_state.get("attempt", 0),
        retry_wait=retry_state.get("wait_seconds", 0),
        skipped_stages=skipped,
    )


def _stage_ai_generate(state: dict, file_name: str, size_str: str, skipped: list[int]) -> None:
    word_count = state.get("word_count", 0)
    show_loading_ui(
        stage=4,
        progress=50.0,
        stage_message=f"AI is generating summary… ({word_count:,} words so far)",
        file_name=file_name,
        file_size=size_str,
        skipped_stages=skipped,
    )


def _stage_cleanup(state: dict, file_name: str, size_str: str, skipped: list[int]) -> None:
    show_loading_ui(
        stage=5,
        progress=100.0,
        stage_message="✓ Zero Footprint Teardown complete",
        file_name=file_name,
        file_size=size_str,
        skipped_stages=skipped,
    )
