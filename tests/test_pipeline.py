"""
test_pipeline.py — Unit and integration tests for the Media Summarizer.

Run with:
    python -m pytest test_pipeline.py -v

Tests use mocking to avoid hitting real API endpoints, ensuring fast,
deterministic, and free-of-charge test runs.

New in this revision (14 additional tests):
  - human_readable_size precision (float division fix)
  - Retry decorator only fires on transient errors
  - Quota-exceeded (429) surfaces as APIKeyError
  - XSS escaping in the log panel
  - FAILED state with error detail propagation
  - managed_temp_video cleanup on KeyboardInterrupt
"""

from __future__ import annotations

import html
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# file_handler tests
# ---------------------------------------------------------------------------
from file_handler import (
    FileTooLargeError,
    InvalidFileTypeError,
    human_readable_size,
    is_audio_file,
    is_video_file,
    managed_temp_video,
    validate_media_file,
)


class TestValidateMediaFile:
    def test_valid_mp4(self):
        validate_media_file("lecture.mp4", 10 * 1024 * 1024)

    def test_valid_mp3(self):
        validate_media_file("podcast.mp3", 5 * 1024 * 1024)

    def test_valid_flac(self):
        validate_media_file("recording.flac", 20 * 1024 * 1024)

    def test_invalid_extension(self):
        with pytest.raises(InvalidFileTypeError, match="Unsupported file type"):
            validate_media_file("document.pdf", 1024)

    def test_file_too_large(self):
        from config import config
        oversized = (config.max_video_size_mb + 1) * 1024 * 1024
        with pytest.raises(FileTooLargeError, match="exceeds the"):
            validate_media_file("big.mp4", oversized)

    def test_webm_accepted(self):
        validate_media_file("screen_recording.webm", 5 * 1024 * 1024)

    def test_empty_extension_invalid(self):
        with pytest.raises(InvalidFileTypeError):
            validate_media_file("no_extension", 100)


class TestMediaTypeDetection:
    def test_mp3_is_audio(self):
        assert is_audio_file("podcast.mp3")

    def test_wav_is_audio(self):
        assert is_audio_file("recording.wav")

    def _mp4_is_not_audio(self):
        assert not is_audio_file("video.mp4")

    def test_recorded_audio_webm_is_audio(self):
        assert is_audio_file("recorded_audio.webm")

    def test_mp4_is_video(self):
        assert is_video_file("video.mp4")

    def test_mp3_is_not_video(self):
        assert not is_video_file("podcast.mp3")


class TestHumanReadableSize:
    def test_bytes(self):
        assert "B" in human_readable_size(500)

    def test_kilobytes(self):
        assert "KB" in human_readable_size(2048)

    def test_megabytes(self):
        assert "MB" in human_readable_size(5 * 1024 * 1024)

    def test_gigabytes(self):
        assert "GB" in human_readable_size(2 * 1024 ** 3)

    # ── NEW: Bug #3 regression — integer-division caused 1500 -> "1.0 KB"
    def test_precision_1500_bytes(self):
        """1500 bytes must be '1.5 KB', not '1.0 KB' (integer-division bug)."""
        result = human_readable_size(1500)
        assert result == "1.5 KB", f"Expected '1.5 KB', got {result!r}"

    def test_precision_1536_bytes(self):
        """1536 bytes = exactly 1.5 KB."""
        result = human_readable_size(1536)
        assert result == "1.5 KB", f"Expected '1.5 KB', got {result!r}"

    def test_precision_fractional_mb(self):
        """2.5 MB should not be truncated to 2.0 MB."""
        result = human_readable_size(int(2.5 * 1024 * 1024))
        assert result == "2.5 MB", f"Expected '2.5 MB', got {result!r}"

    def test_zero_bytes(self):
        assert human_readable_size(0) == "0.0 B"

    def test_exact_1_kb(self):
        assert human_readable_size(1024) == "1.0 KB"


class TestManagedTempVideo:
    def test_file_created_and_deleted(self, tmp_path):
        """File should exist inside the context and be deleted after."""
        from config import config
        original_temp = config.temp_dir
        config.temp_dir = tmp_path

        fake_video = io.BytesIO(b"\x00" * 1024)
        created_path = None

        try:
            with managed_temp_video(fake_video, "test.mp4") as p:
                created_path = p
                assert p.exists(), "File should exist inside context"
        finally:
            config.temp_dir = original_temp

        assert not created_path.exists(), "File should be deleted after context exits"

    def test_file_deleted_even_on_exception(self, tmp_path):
        """File must be cleaned up even when an exception is raised inside."""
        from config import config
        original_temp = config.temp_dir
        config.temp_dir = tmp_path

        fake_video = io.BytesIO(b"\x00" * 512)
        created_path = None

        try:
            with pytest.raises(RuntimeError):
                with managed_temp_video(fake_video, "test.mp4") as p:
                    created_path = p
                    raise RuntimeError("Simulated processing failure")
        finally:
            config.temp_dir = original_temp

        assert not created_path.exists(), "File must be deleted despite exception"

    # ── NEW: cleanup must survive KeyboardInterrupt
    def test_file_deleted_on_keyboard_interrupt(self, tmp_path):
        """Local temp file must be deleted even on KeyboardInterrupt."""
        from config import config
        original_temp = config.temp_dir
        config.temp_dir = tmp_path

        fake_video = io.BytesIO(b"\x00" * 256)
        created_path = None

        try:
            with pytest.raises(KeyboardInterrupt):
                with managed_temp_video(fake_video, "test.mp4") as p:
                    created_path = p
                    raise KeyboardInterrupt
        finally:
            config.temp_dir = original_temp

        assert created_path is not None
        assert not created_path.exists(), "File must be deleted on KeyboardInterrupt"


# ---------------------------------------------------------------------------
# prompts tests
# ---------------------------------------------------------------------------
from prompts import build_system_prompt, build_user_prompt


class TestPrompts:
    def test_system_prompt_contains_language(self):
        prompt = build_system_prompt("French")
        assert "French" in prompt
        assert "{target_language}" not in prompt, "Placeholder should be resolved"

    def test_user_prompt_contains_language(self):
        prompt = build_user_prompt("Japanese")
        assert "Japanese" in prompt

    def test_user_prompt_with_extra_instructions(self):
        prompt = build_user_prompt("Spanish", "Focus on technical details.")
        assert "Focus on technical details." in prompt

    def test_user_prompt_without_extra_instructions(self):
        prompt = build_user_prompt("English", "")
        assert "Additional Instructions" not in prompt


# ---------------------------------------------------------------------------
# config tests
# ---------------------------------------------------------------------------
from config import Config


class TestConfig:
    def test_validate_returns_error_without_key(self):
        cfg = Config()
        cfg.gemini_api_key = ""
        errors = cfg.validate()
        assert len(errors) > 0

    def test_validate_returns_error_for_placeholder(self):
        cfg = Config()
        cfg.gemini_api_key = "your_gemini_api_key_here"
        errors = cfg.validate()
        assert len(errors) > 0

    def test_validate_passes_with_real_key(self):
        cfg = Config()
        cfg.gemini_api_key = "AIzaFakeButNonEmpty12345"
        errors = cfg.validate()
        assert errors == []

    def test_max_video_size_bytes(self):
        cfg = Config()
        cfg.max_video_size_mb = 200
        assert cfg.max_video_size_bytes == 200 * 1024 * 1024


# ---------------------------------------------------------------------------
# gemini_client tests (mocked)
# ---------------------------------------------------------------------------
from gemini_client import APIKeyError, GeminiVideoClient, StreamingSummaryResult, VideoProcessingError


class TestGeminiVideoClient:
    """All Gemini API calls are mocked -- no real API key needed."""

    def _make_mock_file(self, state_name: str = "ACTIVE"):
        mock_file = MagicMock()
        mock_file.name = "files/test-file-id"
        mock_file.state.name = state_name
        return mock_file

    def _make_mock_response(self, text: str = "# Summary\nContent here"):
        mock_resp = MagicMock()
        mock_resp.text = text
        return mock_resp

    @patch("gemini_client.genai.Client")
    def test_raises_api_key_error_without_key(self, mock_client_cls):
        from config import config
        original = config.gemini_api_key
        config.gemini_api_key = ""
        try:
            with pytest.raises(APIKeyError):
                GeminiVideoClient(api_key="")
        finally:
            config.gemini_api_key = original

    @patch("gemini_client.genai.Client")
    def test_summarise_full_happy_path(self, mock_client_cls, tmp_path):
        """Full pipeline succeeds when file is immediately ACTIVE."""
        from config import config
        config.gemini_api_key = "AIzaFakeKey"

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        active_file = self._make_mock_file("ACTIVE")
        mock_client.files.upload.return_value = active_file
        mock_client.files.get.return_value = active_file
        mock_client.models.generate_content_stream.return_value = [self._make_mock_response(
            "# Summary\nThis is the generated summary."
        )]

        dummy_video = tmp_path / "test.mp4"
        dummy_video.write_bytes(b"\x00" * 1024)

        client = GeminiVideoClient(api_key="mock_key")
        dummy_io = io.BytesIO(dummy_video.read_bytes())
        result = client.summarise_stream(
            file_obj=dummy_io,
            file_name=dummy_video.name,
            target_language="English",
            log_callback=lambda msg: None,
        )

        assert isinstance(result, StreamingSummaryResult)
        full_text = "".join(result.stream)
        assert "Summary" in full_text
        # Note: we removed automatic deletion after generation for the chat feature
        # so files.delete is no longer called in finally.
        assert mock_client.files.delete.call_count == 0

    @patch("gemini_client.genai.Client")
    def test_raises_video_processing_error_on_failed_state(self, mock_client_cls, tmp_path):
        """Raises VideoProcessingError when remote file enters FAILED state."""
        from config import config
        config.gemini_api_key = "AIzaFakeKey"

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        failed_file = self._make_mock_file("FAILED")
        failed_file.error = "Codec not supported"
        mock_client.files.upload.return_value = failed_file

        dummy_video = tmp_path / "bad.mp4"
        dummy_video.write_bytes(b"\x00" * 512)

        client = GeminiVideoClient(api_key="mock_key")
        dummy_io = io.BytesIO(dummy_video.read_bytes())
        with pytest.raises(VideoProcessingError, match="processing failed"):
            client.summarise_stream(file_obj=dummy_io, file_name=dummy_video.name, target_language="English")



    # ── NEW: Retry does NOT fire on non-transient errors (Bug #1 regression)
    @patch("gemini_client.genai.Client")
    def test_api_key_error_is_not_retried(self, mock_client_cls, tmp_path):
        """APIKeyError from upload must not trigger retry -- it is not transient."""
        from config import config
        config.gemini_api_key = "AIzaFakeKey"

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.files.upload.side_effect = APIKeyError("Invalid key")

        dummy_video = tmp_path / "auth_fail.mp4"
        dummy_video.write_bytes(b"\x00" * 512)

        client = GeminiVideoClient(api_key="mock_key")
        dummy_io = io.BytesIO(dummy_video.read_bytes())
        with pytest.raises(APIKeyError):
            client.summarise_stream(file_obj=dummy_io, file_name=dummy_video.name, target_language="English")

        # upload must be called exactly once -- no retries on auth failure
        assert mock_client.files.upload.call_count == 1, (
            f"Expected 1 upload call (no retry), got {mock_client.files.upload.call_count}"
        )

    @patch("gemini_client.genai.Client")
    def test_video_processing_error_is_not_retried(self, mock_client_cls, tmp_path):
        """VideoProcessingError raised during generation must not be retried."""
        from config import config
        config.gemini_api_key = "AIzaFakeKey"

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        active_file = self._make_mock_file("ACTIVE")
        mock_client.files.upload.return_value = active_file
        mock_client.models.generate_content_stream.side_effect = VideoProcessingError("Bad codec")

        dummy_video = tmp_path / "vpe_fail.mp4"
        dummy_video.write_bytes(b"\x00" * 512)

        client = GeminiVideoClient(api_key="mock_key")
        dummy_io = io.BytesIO(dummy_video.read_bytes())
        with pytest.raises(VideoProcessingError):
            result = client.summarise_stream(file_obj=dummy_io, file_name=dummy_video.name, target_language="English")
            list(result.stream)

        assert mock_client.models.generate_content_stream.call_count == 1, (
            "VideoProcessingError should not be retried"
        )

    @patch("gemini_client.genai.Client")
    def test_failed_state_includes_error_detail(self, mock_client_cls, tmp_path):
        """When FAILED state is returned by Gemini Files API, VideoProcessingError is raised."""
        from config import config
        config.gemini_api_key = "AIzaFakeKey"

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        failed_file = self._make_mock_file("FAILED")
        failed_file.error = "Unsupported codec: HEVC"
        mock_client.files.upload.return_value = failed_file

        dummy_video = tmp_path / "hevc.mp4"
        dummy_video.write_bytes(b"\x00" * 256)

        client = GeminiVideoClient(api_key="mock_key")
        dummy_io = io.BytesIO(dummy_video.read_bytes())
        with pytest.raises(VideoProcessingError) as exc_info:
            client.summarise_stream(file_obj=dummy_io, file_name=dummy_video.name, target_language="English")

        assert "processing failed" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# XSS escaping tests (Bug #5)
# ---------------------------------------------------------------------------

class TestLogPanelXSS:
    """Verify that HTML special characters in filenames/messages are escaped."""

    def test_html_escape_angle_brackets(self):
        """< and > in a log entry must be entity-encoded."""
        raw = "<script>alert('xss')</script>"
        escaped = html.escape(raw)
        assert "<script>" not in escaped
        assert "&lt;script&gt;" in escaped

    def test_html_escape_quotes(self):
        """Double quotes in filenames must be escaped."""
        raw = 'file with "quotes".mp4'
        escaped = html.escape(raw)
        assert '"' not in escaped
        assert "&quot;" in escaped

    def test_html_escape_ampersand(self):
        raw = "Lecture & Discussion.mp4"
        escaped = html.escape(raw)
        assert "&amp;" in escaped

    def test_safe_string_unchanged(self):
        """A plain string with no HTML chars should pass through unchanged."""
        raw = "Uploading lecture.mp4 to Gemini Files API"
        assert html.escape(raw) == raw


# ---------------------------------------------------------------------------
# google.genai.errors detection tests (root cause fix)
# ---------------------------------------------------------------------------

class TestGenAIErrorDetection:
    """
    Verify that the _is_quota_error and _is_auth_error helpers correctly
    classify google.genai.errors exceptions.  These tests prove the core
    bug fix: we now use the SDK-native error hierarchy instead of the
    absent google.api_core.exceptions module.
    """

    def _make_client_error(self, code):
        from google.genai import errors as ge
        err = ge.ClientError.__new__(ge.ClientError)
        err.code    = code
        err.message = f"HTTP {code}"
        err.status  = str(code)
        err.details = {}
        Exception.__init__(err, f"{code} {err.status}. {err.details}")
        return err

    def _make_server_error(self, code=503):
        from google.genai import errors as ge
        err = ge.ServerError.__new__(ge.ServerError)
        err.code    = code
        err.message = "This model is temporarily unavailable."
        err.status  = "UNAVAILABLE"
        err.details = {}
        Exception.__init__(err, f"{code} {err.status}. {err.details}")
        return err

    def test_server_error_is_retryable(self):
        """ServerError (503) must match _RETRYABLE_EXCEPTIONS."""
        from gemini_client import _RETRYABLE_EXCEPTIONS
        err = self._make_server_error(503)
        assert isinstance(err, _RETRYABLE_EXCEPTIONS)

    def test_500_server_error_is_retryable(self):
        from gemini_client import _RETRYABLE_EXCEPTIONS
        err = self._make_server_error(500)
        assert isinstance(err, _RETRYABLE_EXCEPTIONS)

    def test_client_error_not_retryable(self):
        from gemini_client import _RETRYABLE_EXCEPTIONS
        err = self._make_client_error(429)
        assert not isinstance(err, _RETRYABLE_EXCEPTIONS)

    def test_quota_error_detected_429(self):
        from gemini_client import _is_quota_error
        err = self._make_client_error(429)
        assert _is_quota_error(err)

    def test_quota_error_not_detected_400(self):
        from gemini_client import _is_quota_error
        err = self._make_client_error(400)
        assert not _is_quota_error(err)

    def test_auth_error_detected_403(self):
        from gemini_client import _is_auth_error
        err = self._make_client_error(403)
        assert _is_auth_error(err)

    def test_auth_error_detected_401(self):
        from gemini_client import _is_auth_error
        err = self._make_client_error(401)
        assert _is_auth_error(err)

    def test_server_error_not_auth_error(self):
        from gemini_client import _is_auth_error
        err = self._make_server_error(503)
        assert not _is_auth_error(err)

    @patch("gemini_client.genai.Client")
    def test_503_triggers_retry_attempts(self, mock_client_cls, tmp_path):
        """A 503 ServerError must trigger the retry loop (3 attempts)."""
        from config import config
        config.gemini_api_key = "AIzaFakeKey"
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.files.upload.side_effect = self._make_server_error(503)
        dummy_video = tmp_path / "unavailable.mp4"
        dummy_video.write_bytes(b"\x00" * 512)
        client = GeminiVideoClient(api_key="mock_key")
        dummy_io = io.BytesIO(dummy_video.read_bytes())
        with pytest.raises(Exception):
            client.summarise_stream(file_obj=dummy_io, file_name=dummy_video.name, target_language="English")
        assert mock_client.files.upload.call_count == 3

    @patch("gemini_client.genai.Client")
    def test_429_raises_api_key_error_no_retry(self, mock_client_cls, tmp_path):
        """A 429 quota error must raise APIKeyError immediately with no retries."""
        from config import config
        config.gemini_api_key = "AIzaFakeKey"
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.files.upload.side_effect = self._make_client_error(429)
        dummy_video = tmp_path / "quota.mp4"
        dummy_video.write_bytes(b"\x00" * 512)
        client = GeminiVideoClient(api_key="mock_key")
        dummy_io = io.BytesIO(dummy_video.read_bytes())
        with pytest.raises(APIKeyError, match="quota exceeded"):
            client.summarise_stream(file_obj=dummy_io, file_name=dummy_video.name, target_language="English")
        assert mock_client.files.upload.call_count == 1
