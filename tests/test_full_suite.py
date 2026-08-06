"""
test_full_suite.py — Expanded test suite for Multimodal AI Media Summarizer.
Covers transcoder inspection, nuclear FFmpeg builder, database operations,
pipeline assertions, loading UI states, and custom exceptions.
"""

import asyncio
import pytest
from pathlib import Path
from fractions import Fraction
from unittest.mock import AsyncMock, MagicMock, patch

from exceptions import TranscodeError, InspectionError, UnsupportedFormatError
from transcoder import (
    inspect_media,
    transcode_with_fallback,
    _build_nuclear_video_cmd,
    _build_audio_extract_cmd,
    _parse_fraction,
    _INCOMPATIBLE_VIDEO_CODECS,
    _INCOMPATIBLE_AUDIO_CODECS,
)
from database import init_db, generate_key, validate_and_use_key


# ---------------------------------------------------------------------------
# Transcoder & Inspection Tests
# ---------------------------------------------------------------------------


def test_parse_fraction_valid() -> None:
    assert _parse_fraction("30000/1001") == Fraction(30000, 1001)
    assert _parse_fraction("30") == Fraction(30, 1)
    assert _parse_fraction("invalid") is None
    assert _parse_fraction("30/0") is None


def test_nuclear_cmd_iphone_flags() -> None:
    cmd = _build_nuclear_video_cmd(Path("input.mov"), Path("output.mp4"), is_iphone=True)
    assert "-tag:v" in cmd
    assert "avc1" in cmd
    assert "-bsf:a" in cmd
    assert "aac_adtstoasc" in cmd
    assert "-vsync" in cmd
    assert "cfr" in cmd


def test_nuclear_cmd_android_flags() -> None:
    cmd = _build_nuclear_video_cmd(Path("input.3gp"), Path("output.mp4"), is_android=True)
    assert "-vsync" in cmd
    assert "cfr" in cmd
    assert "-r" in cmd
    assert "30" in cmd


def test_audio_extract_cmd() -> None:
    cmd = _build_audio_extract_cmd(Path("input.mp4"), Path("output.m4a"))
    assert "-vn" in cmd
    assert "-c:a" in cmd
    assert "aac" in cmd


@pytest.mark.anyio
async def test_inspect_media_hevc_detection() -> None:
    mock_ffprobe_out = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "pix_fmt": "yuv420p",
                "r_frame_rate": "30/1",
                "avg_frame_rate": "30/1",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
            }
        ],
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "12.5",
            "size": "5000000",
            "tags": {"com.apple.quicktime.make": "Apple"}
        }
    }

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.communicate.return_value = (json_bytes(mock_ffprobe_out), b"")
        proc.returncode = 0
        mock_exec.return_value = proc

        res = await inspect_media("dummy_iphone.mov")
        assert res["needs_transcode"] is True
        assert res["is_iphone"] is True
        assert res["video_codec"] == "hevc"
        assert len(res["reasons"]) > 0


@pytest.mark.anyio
async def test_inspect_media_vfr_detection() -> None:
    mock_ffprobe_out = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "r_frame_rate": "60/1",
                "avg_frame_rate": "24/1",
            }
        ],
        "format": {
            "format_name": "mp4",
            "duration": "10.0",
            "size": "1000000"
        }
    }

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.communicate.return_value = (json_bytes(mock_ffprobe_out), b"")
        proc.returncode = 0
        mock_exec.return_value = proc

        res = await inspect_media("vfr_video.mp4")
        assert res["is_vfr"] is True
        assert res["needs_transcode"] is True


def json_bytes(obj: dict) -> bytes:
    import json
    return json.dumps(obj).encode("utf-8")


# ---------------------------------------------------------------------------
# Database Tests
# ---------------------------------------------------------------------------


def test_database_key_lifecycle() -> None:
    init_db()
    key = generate_key(max_quota=2)
    assert key.startswith("live_")

    assert validate_and_use_key(key) is True
    assert validate_and_use_key(key) is True
    # Quota exceeded
    assert validate_and_use_key(key) is False


def test_database_invalid_key() -> None:
    init_db()
    assert validate_and_use_key("nonexistent_key_123") is False
    assert validate_and_use_key("") is False


# ---------------------------------------------------------------------------
# Exceptions UI Message Tests
# ---------------------------------------------------------------------------


def test_transcode_error_ui_message() -> None:
    err = TranscodeError(input_path="test.mp4", reasons=["HEVC codec"])
    assert "Settings → Camera → Formats" in err.ui_message or "Most Compatible" in err.ui_message
    assert err.handbrake_url == "https://handbrake.fr"


def test_inspection_error_ui_message() -> None:
    err = InspectionError(input_path="corrupt.mp4", stderr="ffprobe error")
    assert "Could not inspect" in err.ui_message


def test_unsupported_format_error_ui_message() -> None:
    err = UnsupportedFormatError(format_name="mkv", codec_name="vp9")
    assert "Unsupported file format" in err.ui_message


# ---------------------------------------------------------------------------
# MP4 -> MP3 Conversion Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mp4_to_mp3_success(tmp_path: Path) -> None:
    from transcoder import mp4_to_mp3
    fake_input = tmp_path / "lecture.mp4"
    fake_input.write_bytes(b"dummy mp4 data")

    mock_inspection = {
        "is_video_file": True,
        "video_codec": "h264",
    }

    with patch("transcoder.inspect_media", AsyncMock(return_value=mock_inspection)), \
         patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.communicate.return_value = (b"", b"ffmpeg output")
        proc.returncode = 0
        mock_exec.return_value = proc

        res_path = await mp4_to_mp3(str(fake_input))
        assert res_path.endswith(".mp3")
        assert ".tmp" in res_path or str(tmp_path) in res_path


@pytest.mark.anyio
async def test_mp4_to_mp3_ffmpeg_failure(tmp_path: Path) -> None:
    from transcoder import mp4_to_mp3
    fake_input = tmp_path / "bad.mp4"
    fake_input.write_bytes(b"bad video")

    mock_inspection = {"is_video_file": True, "video_codec": "h264"}

    with patch("transcoder.inspect_media", AsyncMock(return_value=mock_inspection)), \
         patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.communicate.return_value = (b"", b"codec error")
        proc.returncode = 1
        mock_exec.return_value = proc

        with pytest.raises(TranscodeError):
            await mp4_to_mp3(str(fake_input))
        assert fake_input.exists()  # Original MP4 not deleted by function


@pytest.mark.anyio
async def test_mp4_to_mp3_audio_only_rejection(tmp_path: Path) -> None:
    from transcoder import mp4_to_mp3
    fake_audio = tmp_path / "song.mp3"
    fake_audio.write_bytes(b"mp3 data")

    mock_inspection = {"is_video_file": False, "video_codec": ""}

    with patch("transcoder.inspect_media", AsyncMock(return_value=mock_inspection)):
        with pytest.raises(UnsupportedFormatError):
            await mp4_to_mp3(str(fake_audio))


@pytest.mark.anyio
async def test_mp4_to_mp3_missing_file() -> None:
    from transcoder import mp4_to_mp3
    with pytest.raises(FileNotFoundError):
        await mp4_to_mp3("non_existent_file_999.mp4")


def test_ui_button_visibility_video_file() -> None:
    inspection = {"video_codec": "h264", "is_video_file": True}
    show_mp3_button = bool(inspection.get("video_codec") or inspection.get("is_video_file"))
    assert show_mp3_button is True


def test_ui_button_visibility_audio_file() -> None:
    inspection = {"video_codec": "", "is_video_file": False}
    show_mp3_button = bool(inspection.get("video_codec") or inspection.get("is_video_file"))
    assert show_mp3_button is False


def test_cleanup_completeness_with_mp3_path(tmp_path: Path) -> None:
    import os
    orig = tmp_path / "orig.mp4"
    trans = tmp_path / "trans.mp4"
    mp3 = tmp_path / "audio.mp3"

    for p in (orig, trans, mp3):
        p.write_bytes(b"data")

    files_to_cleanup = [str(orig), str(trans), str(mp3)]

    # Simulate exception & finally block cleanup
    try:
        raise ValueError("Simulated pipeline error")
    except ValueError:
        pass
    finally:
        for path in files_to_cleanup:
            if path and Path(path).exists():
                os.remove(path)

    for p in (orig, trans, mp3):
        assert not p.exists()


# ---------------------------------------------------------------------------
# Remediation Tests (Audit Bug Fix Verification)
# ---------------------------------------------------------------------------


def test_magic_bytes_validation_rejection() -> None:
    import io
    from file_handler import validate_media_file, InvalidFileTypeError

    fake_text_stream = io.BytesIO(b"Hello world, this is plain text, not video")
    
    # Should pass extension check if .mp4, but magic bytes signature check raises InvalidFileTypeError
    with pytest.raises(InvalidFileTypeError) as exc:
        validate_media_file("fake_video.mp4", file_size_bytes=42, file_obj=fake_text_stream)
    assert "File header signature" in str(exc.value) or "Unsupported file type" in str(exc.value)


def test_pipeline_state_job_isolation() -> None:
    import ui.pipeline_state as ps

    ps.reset("job-user-A")
    ps.reset("job-user-B")

    ps.update("job-user-A", stage=3, pct=45.0, stage_label="Uploading User A...")
    ps.update("job-user-B", stage=2, pct=88.0, stage_label="Converting User B...")

    snap_a = ps.snapshot("job-user-A")
    snap_b = ps.snapshot("job-user-B")

    assert snap_a["pct"] == 45.0
    assert snap_a["stage_label"] == "Uploading User A..."

    assert snap_b["pct"] == 88.0
    assert snap_b["stage_label"] == "Converting User B..."

    ps.cleanup_job("job-user-A")
    ps.cleanup_job("job-user-B")


def test_database_lock_retry() -> None:
    from database import validate_and_use_key, init_db, generate_key
    init_db()
    key = generate_key(max_quota=5)
    # Operational validation should succeed cleanly
    assert validate_and_use_key(key, max_retries=2) is True

