"""
tests/test_analysis_runner.py — Unit tests for pipeline builder and status fragment.
"""

from unittest.mock import MagicMock, patch
import pytest

from ui.analysis_runner import build_analysis_fn, submit_analysis_job
from ui.queue_status import _render_pending_panel, _full_rerun


def test_build_analysis_fn_returns_callable():
    fn = build_analysis_fn(
        file_bytes=b"dummy audio content",
        file_name="test.mp3",
        target_language="English",
        source_language="English",
        combined_instructions="",
        conversion_mode=None,
    )
    assert callable(fn)


@patch("ui.analysis_runner.get_queue_manager")
def test_submit_analysis_job(mock_qm_getter):
    mock_qm = MagicMock()
    mock_qm.submit.return_value = "job-uuid-12345678"
    mock_qm_getter.return_value = mock_qm

    mock_file = MagicMock()
    mock_file.read.return_value = b"test bytes"
    mock_file.name = "lecture.mp3"

    job_id = submit_analysis_job(
        uploaded_file=mock_file,
        target_language="English",
        source_language="English",
        extra_instructions="Summarize key points",
        conversion_mode=None,
        classroom_mode=False,
    )

    assert job_id == "job-uuid-12345678"
    mock_qm.submit.assert_called_once()


@patch("ui.queue_status.show_loading_ui")
def test_render_pending_panel(mock_loading_ui):
    _render_pending_panel(position=3)
    mock_loading_ui.assert_called_once_with(
        stage=1,
        progress=0.0,
        stage_message="Waiting in server processing queue (position 3)…",
        queue_position=3,
    )


@patch("ui.queue_status.st")
def test_full_rerun_scope_app_fallback(mock_st):
    # First call with scope="app" raises TypeError, second call without args succeeds
    mock_st.rerun.side_effect = [TypeError("Unexpected scope parameter"), None]
    _full_rerun()
    assert mock_st.rerun.call_count == 2
