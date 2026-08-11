"""
ui/queue_status.py — Renders queue status and active processing cards.
Integrates with show_loading_ui for zero-glitch HTML component rendering.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import streamlit as st

import ui.pipeline_state as ps
from ui.loading import show_loading_ui
from queue_worker import JobStatus, get_queue_manager

logger = logging.getLogger(__name__)


_fragment = getattr(st, "fragment", getattr(st, "experimental_fragment", None))


def _queue_status_body() -> None:
    """
    Check active queue job and render the unified loading UI.
    Auto-refreshes every 2 seconds via Streamlit fragment (non-blocking).
    When the job finishes, collects the result into session_state and
    triggers a full-page rerun to display the summary or error.
    """
    manager = get_queue_manager()

    job_id = st.session_state.get("queue_job_id")
    if not job_id:
        return

    job = manager.get_job(job_id)
    if not job:
        st.session_state.queue_job_id = None
        return

    if job.status == JobStatus.PENDING:
        _render_pending_panel(job.position)
        return

    if job.status == JobStatus.PROCESSING:
        _render_active_pipeline(job_id)
        return

    # ── DONE or FAILED: collect result and trigger full-page re-render ──
    st.session_state.queue_job_id = None  # clear poll so we don't loop
    if job.status == JobStatus.DONE and job.result is not None:
        st.session_state.result = job.result
        st.session_state.current_remote_file = job.result.remote_file_name
    elif job.error:
        st.session_state.error_msg = job.error
        st.session_state.result = None  # clear stale result

    # st.rerun(scope="app") triggers a FULL page rerun (not just the fragment).
    # Streamlit 1.37+ supports scope parameter; fall back gracefully for older.
    try:
        st.rerun(scope="app")
    except TypeError:
        st.rerun()


if _fragment is not None:
    render_queue_status = _fragment(run_every="2s")(_queue_status_body)
else:
    render_queue_status = _queue_status_body


def _render_pending_panel(position: int) -> None:
    """Render the 'waiting in queue' status card using show_loading_ui."""
    show_loading_ui(
        stage=1,
        progress=0.0,
        stage_message="Waiting in server processing queue...",
        queue_position=position,
    )


def _render_active_pipeline(job_id: str | None = None) -> None:
    """Read the current pipeline_state snapshot for job_id and render matching loading card."""
    state = ps.snapshot(job_id=job_id)
    stage: float | int = state.get("stage", 1)
    skipped: list[int] = [2] if state.get("skipped_transcode", False) else []
    file_name = state.get("sub_message", "")
    file_size = state.get("bytes_total", 0)
    
    from file_handler import human_readable_size
    size_str = human_readable_size(file_size) if file_size > 0 else ""

    if stage == 1:
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

    elif stage == 1.5:
        show_loading_ui(
            stage=1.5,
            progress=50.0,
            stage_message="Extracting audio track from video (MP4 → MP3)...",
            file_name=file_name,
            file_size=size_str,
            file_format="MP4",
            conversion_mode="mp3",
            skipped_stages=skipped,
        )

    elif stage == 2:
        show_loading_ui(
            stage=2,
            progress=state.get("ffmpeg_pct", 0.0),
            stage_message="Converting mobile video (HEVC → H.264) for AI processing...",
            file_name=file_name,
            file_size=size_str,
            file_format="HEVC",
            is_compatible=False,
            ffmpeg_speed=state.get("ffmpeg_speed", ""),
            ffmpeg_eta=state.get("ffmpeg_eta", ""),
            skipped_stages=skipped,
        )

    elif stage == 3:
        retry_state: dict = state.get("retry_state", {})
        show_loading_ui(
            stage=3,
            progress=state.get("pct", 50.0),
            stage_message="Streaming media to High-Speed AI Cloud...",
            file_name=file_name,
            file_size=size_str,
            retry_attempt=retry_state.get("attempt", 0),
            retry_wait=retry_state.get("wait_seconds", 0),
            skipped_stages=skipped,
        )

    elif stage == 4:
        show_loading_ui(
            stage=4,
            progress=50.0,
            stage_message=f"AI is generating summary... ({state.get('word_count', 0):,} words generated)",
            file_name=file_name,
            file_size=size_str,
            skipped_stages=skipped,
        )

    elif stage == 5:
        show_loading_ui(
            stage=5,
            progress=100.0,
            stage_message="✓ Zero Footprint Teardown complete",
            file_name=file_name,
            file_size=size_str,
            skipped_stages=skipped,
        )

    else:
        pct = state.get("pct", 0.0)
        label = state.get("stage_label", "Processing…")
        show_loading_ui(
            stage=stage,
            progress=pct,
            stage_message=label,
            skipped_stages=skipped,
        )
