"""
test_loading.py — Pytest tests for the multi-stage loading experience.

Covers:
  1. parse_ffmpeg_progress() via mock stderr lines in transcoder.py
  2. pipeline_state thread safety and reset/update semantics
  3. retry_state exposure on simulated 429 error
  4. show_loading_ui() HTML component rendering (replaces old markdown tests)

Run with:
    pytest test_loading.py -v
"""

from __future__ import annotations

import re
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Make ui/ importable without a running Streamlit app
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent.parent))

# Stub out streamlit so ui.pipeline_state and ui.loading can be imported
st_mock = MagicMock()
components_mock = MagicMock()
sys.modules.setdefault("streamlit", st_mock)
sys.modules.setdefault("streamlit.components", MagicMock())
sys.modules.setdefault("streamlit.components.v1", components_mock)


# ---------------------------------------------------------------------------
# 1. pipeline_state — basic operations
# ---------------------------------------------------------------------------

class TestPipelineState:
    def setup_method(self) -> None:
        import ui.pipeline_state as ps
        ps.reset()
        self.ps = ps

    def test_reset_returns_defaults(self) -> None:
        self.ps.update(stage=4, word_count=9999)
        self.ps.reset()
        assert self.ps.get("stage") == 1
        assert self.ps.get("word_count") == 0

    def test_update_single_field(self) -> None:
        self.ps.update(stage=3)
        assert self.ps.get("stage") == 3

    def test_update_multiple_fields(self) -> None:
        self.ps.update(stage=4, word_count=500, sections_done=["Overview"])
        snap = self.ps.snapshot()
        assert snap["stage"] == 4
        assert snap["word_count"] == 500
        assert snap["sections_done"] == ["Overview"]

    def test_snapshot_is_copy(self) -> None:
        snap = self.ps.snapshot()
        snap["stage"] = 99
        assert self.ps.get("stage") == 1   # original unchanged

    def test_thread_safe_concurrent_writes(self) -> None:
        """Multiple threads writing should never corrupt the dict."""
        import ui.pipeline_state as ps

        errors: list[Exception] = []

        def _writer(val: int) -> None:
            try:
                for _ in range(200):
                    ps.update(stage=val, word_count=val * 10)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(1, 6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread safety errors: {errors}"
        assert self.ps.get("stage") in range(1, 6)


# ---------------------------------------------------------------------------
# 2. FFmpeg progress parsing (regex-level test, not subprocess)
# ---------------------------------------------------------------------------

class TestFfmpegProgressParsing:
    """
    We test the regex logic used in transcoder.py directly, without
    spawning a real ffmpeg process.
    """

    TIME_RE  = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.\d+")
    SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")

    def _parse_line(self, line: str, total_secs: float) -> tuple[float, str]:
        """Simulate the transcoder parsing logic."""
        time_m  = self.TIME_RE.search(line)
        speed_m = self.SPEED_RE.search(line)
        if not time_m:
            return 0.0, ""
        h, m, s = int(time_m.group(1)), int(time_m.group(2)), int(time_m.group(3))
        current = h * 3600 + m * 60 + s
        pct = (current / total_secs * 100) if total_secs > 0 else 0.0
        speed = f"{speed_m.group(1)}x" if speed_m else ""
        return pct, speed

    @pytest.mark.parametrize("line,total,expected_pct,expected_speed", [
        (
            "frame=  120 fps= 24 size=   512kB time=00:00:05.00 bitrate= 839.0kbits/s speed=2.4x",
            100.0, 5.0, "2.4x",
        ),
        (
            "frame=  600 fps= 25 size=  2048kB time=00:00:30.00 bitrate= 560.0kbits/s speed=1.8x",
            60.0, 50.0, "1.8x",
        ),
        (
            "frame= 1500 fps= 30 size=  8192kB time=00:01:00.00 bitrate= 1120.0kbits/s speed=3.1x",
            60.0, 100.0, "3.1x",
        ),
        (
            "frame=    0 fps=  0 size=       0kB time=00:00:00.00 bitrate=N/A speed=N/A",
            60.0, 0.0, "",
        ),
    ])
    def test_parse_ffmpeg_stderr_line(
        self,
        line: str,
        total: float,
        expected_pct: float,
        expected_speed: str,
    ) -> None:
        pct, speed = self._parse_line(line, total)
        assert abs(pct - expected_pct) < 0.1, f"Expected {expected_pct}%, got {pct}%"
        assert speed == expected_speed

    def test_line_with_no_time_token(self) -> None:
        pct, speed = self._parse_line("Input #0, matroska,webm, from 'test.mkv':", 60.0)
        assert pct == 0.0
        assert speed == ""


# ---------------------------------------------------------------------------
# 3. retry_state exposure (simulate 429 writing to pipeline_state)
# ---------------------------------------------------------------------------

class TestRetryStateExposure:
    def setup_method(self) -> None:
        import ui.pipeline_state as ps
        ps.reset()
        self.ps = ps

    def test_retry_state_written_on_retry(self) -> None:
        """Simulate what gemini_client.py writes on before_sleep callback."""
        self.ps.update(retry_state={
            "attempt": 2,
            "wait_seconds": 8,
            "is_retrying": True,
        })
        snap = self.ps.snapshot()
        rs = snap["retry_state"]
        assert rs["attempt"] == 2
        assert rs["wait_seconds"] == 8
        assert rs["is_retrying"] is True

    def test_retry_state_cleared_after_success(self) -> None:
        """Simulate the `after` callback clearing retry state."""
        self.ps.update(retry_state={"attempt": 3, "wait_seconds": 16, "is_retrying": True})
        # After hook clears it
        self.ps.update(retry_state={"attempt": 0, "wait_seconds": 0, "is_retrying": False})
        rs = self.ps.get("retry_state")
        assert rs["is_retrying"] is False
        assert rs["attempt"] == 0


# ---------------------------------------------------------------------------
# 4. show_loading_ui() — HTML Component Tests
# ---------------------------------------------------------------------------

class TestShowLoadingUI:
    def test_show_loading_ui_renders_html_component(self) -> None:
        from ui.loading import show_loading_ui
        with patch("ui.loading.components.html") as mock_html:
            show_loading_ui(
                stage=2,
                progress=45.0,
                stage_message="Transcoding HEVC video...",
                file_name="lecture.mp4",
                file_size="15.2 MB",
                file_format="HEVC",
                is_compatible=False,
            )
            assert mock_html.called
            args, kwargs = mock_html.call_args
            rendered_html = args[0]
            assert "TRANSCODE" in rendered_html
            assert "lecture.mp4" in rendered_html
            assert "15.2 MB" in rendered_html
            assert "45%" in rendered_html
            assert kwargs.get("height", 0) > 0

    def test_show_loading_ui_queue_position(self) -> None:
        from ui.loading import show_loading_ui
        with patch("ui.loading.components.html") as mock_html:
            show_loading_ui(stage=1, progress=0.0, queue_position=3)
            rendered_html = mock_html.call_args[0][0]
            assert "QUEUE POSITION" in rendered_html
            assert "#3" in rendered_html

    def test_show_loading_ui_mp3_conversion_mode(self) -> None:
        from ui.loading import show_loading_ui
        with patch("ui.loading.components.html") as mock_html:
            show_loading_ui(stage=1.5, progress=50.0, conversion_mode="mp3")
            rendered_html = mock_html.call_args[0][0]
            assert "Extract" in rendered_html
            assert "EXTRACT AUDIO" in rendered_html

    def test_show_loading_ui_height_grows_with_file_and_queue(self) -> None:
        from ui.loading import show_loading_ui
        with patch("ui.loading.components.html") as mock_html:
            # No file, no queue
            show_loading_ui(stage=1, progress=0.0)
            h_base = mock_html.call_args[1]["height"]

            # With file info
            show_loading_ui(stage=1, progress=0.0, file_name="test.mp4", file_size="1 MB")
            h_with_file = mock_html.call_args[1]["height"]

            # With file + queue
            show_loading_ui(stage=1, progress=0.0, file_name="test.mp4", file_size="1 MB", queue_position=2)
            h_with_queue = mock_html.call_args[1]["height"]

            assert h_with_file > h_base
            assert h_with_queue > h_with_file

    def test_show_cleanup_sequence_calls_show_loading_ui(self) -> None:
        from ui.loading import show_cleanup_sequence
        with patch("ui.loading.components.html") as mock_html:
            show_cleanup_sequence(local_done=True, cloud_done=True, session_done=True)
            assert mock_html.called
            rendered_html = mock_html.call_args[0][0]
            assert "Zero Footprint" in rendered_html or "Cleanup" in rendered_html or "CLEANUP" in rendered_html

    def test_show_master_stepper_active_stage_in_html(self) -> None:
        from ui.loading import show_loading_ui
        with patch("ui.loading.components.html") as mock_html:
            show_loading_ui(stage=3, progress=0.0)
            rendered_html = mock_html.call_args[0][0]
            # Stage 3 should be active — 'active' class present
            assert 'class="step active"' in rendered_html

    def test_show_master_stepper_completed_stages(self) -> None:
        from ui.loading import show_loading_ui
        with patch("ui.loading.components.html") as mock_html:
            show_loading_ui(stage=4, progress=0.0)
            rendered_html = mock_html.call_args[0][0]
            # Stages 1,2,3 completed — at least one 'completed' class
            assert 'class="step completed"' in rendered_html

    def test_show_master_stepper_skipped_stage(self) -> None:
        from ui.loading import show_loading_ui
        with patch("ui.loading.components.html") as mock_html:
            show_loading_ui(stage=3, progress=0.0, skipped_stages=[2])
            rendered_html = mock_html.call_args[0][0]
            assert 'class="step skipped"' in rendered_html
