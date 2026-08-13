"""
transcoder.py — Bulletproof media inspection and transcoding pipeline.

Architecture
------------
1. inspect_media(file_path)      — ffprobe JSON scan; returns full diagnosis
2. transcode_with_fallback(...)  — three-tier FFmpeg pipeline:
     Tier 1: Full nuclear H.264 transcode (video + audio)
     Tier 2: Audio-only AAC extraction (if video transcode fails)
     Tier 3: Raise TranscodeError with HandBrake guidance (last resort)

The proactive inspect → transcode → upload order eliminates the reactive
"upload first, transcode on rejection" pattern that caused the code:13 bug.

FFmpeg flags (each documented with its purpose):
  -profile:v baseline -level 3.1  → Maximum Gemini compatibility
  -pix_fmt yuv420p                 → Forces 8-bit color; rejects 10-bit HDR
  -vf "fps=30,scale=..."           → Converts VFR to CFR (fixes code:13 on VFR)
                                     scale=trunc(iw/2)*2 ensures even dimensions
  -movflags +faststart             → Moves MP4 metadata to front for cloud upload
  -avoid_negative_ts make_zero     → Fixes timestamp issues in iPhone recordings
  -map_metadata -1                 → Strips all metadata (privacy + compatibility)
  -tag:v avc1                      → Fixes iPhone HEVC container tag issue
  -bsf:a aac_adtstoasc             → Fixes iPhone AAC audio stream format
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, AsyncGenerator

from exceptions import InspectionError, TranscodeError, UnsupportedFormatError

logger = logging.getLogger(__name__)

# Lazy import of pipeline_state so this module stays importable outside Streamlit
try:
    import ui.pipeline_state as _ps  # type: ignore[import]
except ImportError:
    _ps = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Codec / container incompatibility tables
# ---------------------------------------------------------------------------

_INCOMPATIBLE_VIDEO_CODECS: set[str] = {
    "hevc", "h265", "x265",          # iPhone / modern Android standard
    "vp9", "vp8",                     # Chrome / Android recording
    "av1",                            # Next-gen; Gemini rejects
    "mjpeg",                          # DSLR / GoPro motion JPEG
    "prores", "prores_ks",            # Apple ProRes (professional cameras)
    "dnxhd", "dnxhr",                 # Avid DNxHD/HR
    "mpeg4", "msmpeg4v3",             # Legacy MPEG-4 Part 2 / DivX / Xvid
    "xvid", "divx",
    "h263", "h263p", "flv1",         # Legacy mobile / Flash video
    "mpeg1video", "mpeg2video",      # MPEG-1 / MPEG-2
    "vp6", "vp6f", "svq3",           # Flash / Sorenson
    "wmv3", "wmv2", "wmv1",          # Windows Media Video
    "vc1",                            # VC-1 (Blu-ray / WMV HD)
    "theora",                         # OGG Theora
    "rv40", "rv30", "rv20",          # RealVideo
    "cinepak", "rpza", "smc",        # Ancient QuickTime codecs
}


_INCOMPATIBLE_AUDIO_CODECS: set[str] = {
    "opus",                           # Opus in MP4 container
    "vorbis",                         # Vorbis in MP4 container
    "ac3", "eac3",                    # Dolby AC-3 / E-AC-3
    "dts", "dts-hd",                  # DTS
    "truehd", "mlp",                  # Dolby TrueHD
    "flac",                           # FLAC in MP4 container
    "alac",                           # Apple Lossless
    "wmalossless", "wmapro",          # Windows Media Audio lossless/pro
    "mp1", "mp2",                     # Old MPEG audio layers
}

_INCOMPATIBLE_CONTAINERS: set[str] = {
    "matroska", "webm",               # MKV / WebM
    "ogg",                            # OGG container
    "flv",                            # Flash Video
    "avi",                            # AVI (often with incompatible audio)
    "wmv", "asf",                     # Windows Media
    "rm", "rmvb",                     # RealMedia
    "3gp", "3g2",                     # Mobile / old Android
    "mpeg", "mpg", "mpegts", "m2ts",  # MPEG Program / Transport Streams
}

_COMPATIBLE_PIX_FMTS: set[str] = {"yuv420p", "yuvj420p"}

_INCOMPATIBLE_PROFILES: set[str] = {
    "High 10",       # 10-bit HEVC HDR
    "High 4:2:2",    # 4:2:2 chroma (Gemini requires 4:2:0)
    "High 4:4:4",    # 4:4:4 chroma
    "High 4:4:4 Predictive",
}

_AUDIO_EXTENSIONS: set[str] = {
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus",
}

_TMP_DIR = Path(".tmp")


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def is_ffmpeg_available() -> bool:
    """Return True if the ffmpeg binary is available on the system PATH."""
    return shutil.which("ffmpeg") is not None


def is_ffprobe_available() -> bool:
    """Return True if the ffprobe binary is available on the system PATH."""
    return shutil.which("ffprobe") is not None


def _parse_fraction(s: str) -> Fraction | None:
    """Parse '30000/1001' or '30' as a Fraction. Returns None on failure."""
    try:
        if "/" in s:
            num, den = s.split("/", 1)
            den_int = int(den)
            if den_int == 0:
                return None
            return Fraction(int(num), den_int)
        return Fraction(int(s))
    except (ValueError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------
# FIX 1 — Bulletproof codec detection via ffprobe
# ---------------------------------------------------------------------------


async def inspect_media(file_path: str) -> dict[str, Any]:
    """
    Run ffprobe on the file and return a full incompatibility diagnosis.

    Uses ``-show_streams -show_format`` JSON output to inspect every stream.
    Does NOT transcode — only reads metadata.

    Args:
        file_path: Absolute or relative path to the media file.

    Returns:
        Dict with keys:
          needs_transcode (bool)          — True if any incompatibility found
          needs_audio_only (bool)         — True if only audio codec is bad
          is_vfr (bool)                   — True when r_frame_rate≠avg_frame_rate
          is_iphone (bool)                — True when Apple metadata detected
          is_android (bool)               — True when Android metadata detected
          video_codec (str)               — e.g. "hevc", "h264", ""
          audio_codec (str)               — e.g. "aac", "opus", ""
          container (str)                 — ffprobe format_name
          reasons (list)                  — Human-readable list of issues
          duration_seconds (float)        — Duration in seconds (0 if unknown)
          file_size_bytes (int)           — File size in bytes
          is_video_file (bool)            — False for pure audio files
    """
    if not is_ffprobe_available():
        raise InspectionError(
            input_path=file_path,
            stderr="ffprobe is not installed or not on PATH.",
        )

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        file_path,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError as exc:
        raise InspectionError(
            input_path=file_path,
            stderr=f"ffprobe timed out after 30s inspecting '{file_path}'",
        ) from exc
    except Exception as exc:
        raise InspectionError(
            input_path=file_path,
            stderr=f"ffprobe subprocess failed: {exc}",
        ) from exc

    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace")[:500]
        raise InspectionError(
            input_path=file_path,
            stderr=f"ffprobe returned code {proc.returncode}. stderr: {stderr_text}",
        )

    try:
        data: dict[str, Any] = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise InspectionError(
            input_path=file_path,
            stderr=f"ffprobe output was not valid JSON: {exc}",
        ) from exc

    streams: list[dict[str, Any]] = data.get("streams", [])
    fmt: dict[str, Any] = data.get("format", {})
    tags: dict[str, str] = {
        k.lower(): str(v)
        for k, v in {**fmt.get("tags", {}), **(
            next((s.get("tags", {}) for s in streams), {})
        )}.items()
    }

    # Separate video and audio streams
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    # Primary video/audio stream (first one)
    v = video_streams[0] if video_streams else {}
    a = audio_streams[0] if audio_streams else {}

    video_codec    = (v.get("codec_name") or "").lower().strip()
    audio_codec    = (a.get("codec_name") or "").lower().strip()
    pix_fmt        = (v.get("pix_fmt") or "").lower().strip()
    profile        = (v.get("profile") or "").strip()
    r_frame_rate   = (v.get("r_frame_rate") or "0/1").strip()
    avg_frame_rate = (v.get("avg_frame_rate") or "0/1").strip()
    container      = (fmt.get("format_name") or "").lower().split(",")[0].strip()

    # Duration
    try:
        duration_seconds = float(fmt.get("duration") or v.get("duration") or 0)
    except (ValueError, TypeError):
        duration_seconds = 0.0

    # File size
    try:
        file_size_bytes = int(fmt.get("size") or os.path.getsize(file_path))
    except (ValueError, OSError):
        file_size_bytes = 0

    # ── VFR detection ──────────────────────────────────────────────────
    rfps = _parse_fraction(r_frame_rate)
    afps = _parse_fraction(avg_frame_rate)
    is_vfr = False
    if rfps is not None and afps is not None and afps > 0:
        is_vfr = abs(rfps - afps) > Fraction(1, 10)

    # ── Device detection ───────────────────────────────────────────────
    make_tag      = tags.get("com.apple.quicktime.make", "").lower()
    handler_tag   = tags.get("handler_name", "").lower()
    compat_brands = tags.get("compatible_brands", "").lower()

    is_iphone = (
        "apple" in make_tag
        or "apple" in handler_tag
        or "qt  " in compat_brands
        or (video_codec == "hevc" and container in {"mov", "mp4", "qt"})
        or (r_frame_rate in {"60000/1001", "30000/1001", "24000/1001"} and video_codec == "hevc")
    )
    is_android = (
        "android" in tags.get("encoder", "").lower()
        or container in {"3gp", "3g2"}
    )

    # ── Incompatibility collection ─────────────────────────────────────
    reasons: list[str] = []
    is_video_file = bool(video_streams)

    # Video codec
    video_bad = False
    if video_codec in _INCOMPATIBLE_VIDEO_CODECS:
        reasons.append(f"Video codec '{video_codec}' is not supported by the Cloud AI engine")
        video_bad = True

    # Pixel format
    if is_video_file and pix_fmt and pix_fmt not in _COMPATIBLE_PIX_FMTS:
        reasons.append(
            f"Pixel format '{pix_fmt}' is incompatible — Cloud AI requires yuv420p (8-bit)"
        )
        video_bad = True

    # Color profile (10-bit HDR)
    if profile in _INCOMPATIBLE_PROFILES:
        reasons.append(f"Color profile '{profile}' (10-bit) not supported — requires 8-bit")
        video_bad = True

    # VFR & High FPS detection
    if is_vfr:
        reasons.append(
            f"Variable frame rate detected (r_fps={r_frame_rate} vs avg_fps={avg_frame_rate}) "
            "— Cloud AI requires constant frame rate"
        )
        video_bad = True

    if (rfps is not None and rfps > 30) or (afps is not None and afps > 30):
        reasons.append(
            f"High frame rate detected (r_fps={r_frame_rate}, avg_fps={avg_frame_rate}) "
            "— Cloud AI requires standard 30fps max"
        )
        video_bad = True


    # Audio codec
    audio_bad = False
    if audio_codec in _INCOMPATIBLE_AUDIO_CODECS:
        reasons.append(f"Audio codec '{audio_codec}' is not supported in this container")
        audio_bad = True

    # Container
    container_bad = False
    for bad_c in _INCOMPATIBLE_CONTAINERS:
        if bad_c in container:
            reasons.append(f"Container format '{container}' may not be compatible with the AI engine")
            container_bad = True
            break

    needs_transcode = bool(video_bad or container_bad or is_vfr)
    needs_audio_only = audio_bad and not video_bad and not container_bad and not is_vfr

    return {
        "needs_transcode":        needs_transcode or needs_audio_only,
        "needs_audio_only":       needs_audio_only,
        "is_vfr":                 is_vfr,
        "is_iphone":              is_iphone,
        "is_android":             is_android,
        "video_codec":            video_codec,
        "audio_codec":            audio_codec,
        "container":              container,
        "reasons":                reasons,
        "duration_seconds":       duration_seconds,
        "file_size_bytes":        file_size_bytes,
        "is_video_file":          is_video_file,
        # Backward compatibility aliases
        "detected_video_codec":   video_codec,
        "detected_audio_codec":   audio_codec,
        "detected_container":     container,
        "detected_pix_fmt":       pix_fmt,
        "detected_profile":       profile,
        "incompatibility_reasons": reasons,
    }


# ---------------------------------------------------------------------------
# FIX 2 — Nuclear FFmpeg commands
# ---------------------------------------------------------------------------


def _build_nuclear_video_cmd(
    in_path: Path,
    out_path: Path,
    is_iphone: bool = False,
    is_android: bool = False,
) -> list[str]:
    """
    Build the full nuclear video + audio transcode command.

    -profile:v baseline -level 3.1  → Maximum Gemini compatibility
    -pix_fmt yuv420p                 → Forces 8-bit color; rejects 10-bit HDR
    -vf fps=30,scale=...             → Converts VFR→CFR; ensures even dimensions
    -movflags +faststart             → Moves MP4 moov atom to front for upload
    -avoid_negative_ts make_zero     → Fixes negative timestamps in iPhone files
    -map_metadata -1                 → Strips metadata (privacy + compat)
    -tag:v avc1                      → Fixes iPhone HEVC container tag issue
    -bsf:a aac_adtstoasc             → Fixes iPhone AAC ADTS→ASC conversion
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_path),
        # Video encoding — H.264 baseline for maximum compatibility
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",          # Force 8-bit YUV420 — no 10-bit HDR
        "-vf", "fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2",
                                         # fps=30: CFR (fixes VFR code:13)
                                         # scale: even dimensions (FFmpeg crash guard)
        # Audio encoding
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        # Container flags
        "-movflags", "+faststart",       # Move moov atom to front for streaming
        "-avoid_negative_ts", "make_zero",  # Fix iPhone negative timestamps
        "-map_metadata", "-1",           # Strip all metadata
    ]

    if is_iphone:
        # iPhone-specific fixes
        cmd += [
            "-tag:v", "avc1",           # Fix HEVC container tag → H.264 tag
            "-bsf:a", "aac_adtstoasc",  # Convert AAC ADTS to ASC bitstream format
            "-vsync", "cfr",            # Forces constant frame rate (kills VFR)
        ]

    if is_android:
        # Android-specific fixes
        cmd += [
            "-vsync", "cfr",            # Kills VFR
            "-r", "30",                 # Forces exactly 30fps CFR output
        ]

    cmd.append(str(out_path))
    return cmd


def _build_audio_only_video_cmd(
    in_path: Path,
    out_path: Path,
) -> list[str]:
    """
    Build audio-only transcode command (copy video stream, re-encode audio).
    Used when video is compatible but audio codec is not.
    """
    return [
        "ffmpeg", "-y",
        "-i", str(in_path),
        "-c:v", "copy",                 # Video stream is already compatible
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        "-movflags", "+faststart",
        "-avoid_negative_ts", "make_zero",
        "-map_metadata", "-1",
        str(out_path),
    ]


def _build_audio_extract_cmd(
    in_path: Path,
    out_path: Path,
) -> list[str]:
    """
    Build Tier 2 audio extraction command.
    Used as fallback when full video transcode fails.
    Output is .m4a (AAC).
    """
    return [
        "ffmpeg", "-y",
        "-i", str(in_path),
        "-vn",                          # Drop video stream entirely
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        str(out_path),
    ]


# ---------------------------------------------------------------------------
# FIX 3 — Three-tier fallback pipeline
# ---------------------------------------------------------------------------


async def transcode_with_fallback(
    input_path: str,
    inspection: dict[str, Any],
    log_callback: Any = None,
    file_size_bytes: int = 0,
    job_id: str | None = None,
) -> tuple[str, str]:
    """
    Three-tier transcoding pipeline for guaranteed Gemini compatibility.

    Tier 1 — Nuclear H.264 transcode (video + audio):
        Timeout: 300s for <500MB files, 600s for larger.
        If successful: return (output_path, "full_video")

    Tier 2 — Audio extraction (AAC m4a):
        Triggered if Tier 1 fails for any reason.
        If successful: return (output_path, "audio_only")
        Caller should warn user that only audio was processed.

    Tier 3 — Graceful rejection:
        If both tiers fail: raise TranscodeError with HandBrake guidance.
        The original file is NOT sent to Gemini under any circumstances.

    Args:
        input_path:       Path to the source media file on disk.
        inspection:       Dict returned by inspect_media().
        log_callback:     Optional callable(str) for progress messages.
        file_size_bytes:  File size in bytes (used for timeout calculation).

    Returns:
        (output_path, processing_mode) where processing_mode is
        "full_video" | "audio_only".

    Raises:
        TranscodeError: Both tiers failed; user must convert manually.
    """
    def _log(msg: str) -> None:
        logger.info(msg)
        if log_callback and callable(log_callback):
            log_callback(msg)

    _TMP_DIR.mkdir(exist_ok=True)
    in_path = Path(input_path)
    stem = in_path.stem

    # Compute timeout based on file size
    size_mb = (file_size_bytes or in_path.stat().st_size) / (1024 * 1024)
    tier1_timeout = 600.0 if size_mb > 100.0 else 300.0

    is_iphone = inspection.get("is_iphone", False)
    is_android = inspection.get("is_android", False)
    audio_only_mode = inspection.get("needs_audio_only", False)

    if is_iphone:
        _log("📱 iPhone video detected — applying device-specific conversion flags…")
    elif is_android:
        _log("📱 Android video detected — applying device-specific conversion flags…")

    # ── Tier 1: Nuclear video + audio transcode ────────────────────────
    uid = uuid.uuid4().hex
    if audio_only_mode:
        tier1_out = _TMP_DIR / f"{uid}_{stem}_audio_fixed.mp4"
        tier1_cmd = _build_audio_only_video_cmd(in_path, tier1_out)
        _log("🔄 Audio codec incompatible — re-encoding audio stream only…")
    else:
        tier1_out = _TMP_DIR / f"{uid}_{stem}_transcoded.mp4"
        tier1_cmd = _build_nuclear_video_cmd(in_path, tier1_out, is_iphone=is_iphone, is_android=is_android)
        reasons = inspection.get("reasons", [])
        _log(f"🔄 Transcoding to H.264 — reasons: {'; '.join(reasons) or 'container compatibility'}")

    if _ps is not None:
        _ps.update(job_id=job_id, stage=2, stage_label="Converting Format…", ffmpeg_pct=0.0)

    tier1_success = False
    tier1_proc: asyncio.subprocess.Process | None = None
    try:
        tier1_proc = await asyncio.create_subprocess_exec(
            *tier1_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Stream progress while waiting
        await _stream_ffmpeg_progress(tier1_proc, inspection.get("duration_seconds", 0.0), job_id=job_id)
        await asyncio.wait_for(tier1_proc.wait(), timeout=tier1_timeout)

        if tier1_proc.returncode == 0 and tier1_out.exists() and tier1_out.stat().st_size > 0:
            tier1_success = True
            if _ps is not None:
                _ps.update(job_id=job_id, ffmpeg_pct=100.0, stage_label="Conversion complete")
            out_mb = tier1_out.stat().st_size / (1024 * 1024)
            in_mb = size_mb
            _log(
                f"✅ Transcode complete — {in_mb:.1f} MB → {out_mb:.1f} MB "
                f"({'audio only' if audio_only_mode else 'full video'})"
            )
            mode = "audio_only" if audio_only_mode else "full_video"
            return str(tier1_out), mode
        else:
            _log(f"⚠️ Tier 1 transcode failed (exit code {tier1_proc.returncode})")

    except asyncio.TimeoutError:
        _log(f"⚠️ Tier 1 transcode timed out after {tier1_timeout:.0f}s")
        if tier1_proc is not None:
            try:
                tier1_proc.kill()
            except Exception:
                pass
    except Exception as exc:
        _log(f"⚠️ Tier 1 transcode error: {exc}")

    # ── Tier 2: Audio extraction fallback ─────────────────────────────
    _log("⚠️ Video transcode failed — extracting audio track for AI processing…")
    tier2_out = _TMP_DIR / f"{uid}_{stem}_audio.m4a"
    tier2_cmd = _build_audio_extract_cmd(in_path, tier2_out)

    try:
        tier2_proc = await asyncio.create_subprocess_exec(
            *tier2_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, tier2_stderr = await asyncio.wait_for(
            tier2_proc.communicate(), timeout=180.0
        )

        if tier2_proc.returncode == 0 and tier2_out.exists() and tier2_out.stat().st_size > 0:
            out_mb = tier2_out.stat().st_size / (1024 * 1024)
            _log(f"✅ Audio extraction complete — {out_mb:.1f} MB audio track")
            return str(tier2_out), "audio_only"
        else:
            stderr_text = tier2_stderr.decode("utf-8", errors="replace")[:300]
            _log(f"⚠️ Tier 2 audio extraction failed: {stderr_text}")

    except asyncio.TimeoutError:
        _log("⚠️ Tier 2 audio extraction timed out after 180s")
    except Exception as exc:
        _log(f"⚠️ Tier 2 audio extraction error: {exc}")

    # ── Tier 3: Graceful rejection ─────────────────────────────────────
    _log("❌ All transcode tiers failed — raising TranscodeError")
    raise TranscodeError(
        input_path=str(in_path),
        reasons=inspection.get("reasons", []),
    )


async def parse_ffmpeg_progress(
    stderr_stream: asyncio.StreamReader,
    total_seconds: float
) -> AsyncGenerator[float, None]:
    """
    Parses 'time=HH:MM:SS.xx' tokens from FFmpeg stderr stream.
    Yields a progress float from 0.0 to 1.0.
    """
    time_re = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.\d+")
    
    async for line_bytes in stderr_stream:
        line = line_bytes.decode("utf-8", errors="replace")
        tm = time_re.search(line)
        
        if tm:
            h, m, s = int(tm.group(1)), int(tm.group(2)), int(tm.group(3))
            cur = h * 3600 + m * 60 + s
            pct = (cur / total_seconds) if total_seconds > 0 else 0.0
            yield min(pct, 1.0)


async def _stream_ffmpeg_progress(
    process: asyncio.subprocess.Process,
    total_duration: float,
    job_id: str | None = None,
) -> None:
    """
    Iterate over parse_ffmpeg_progress and write progress to pipeline_state.
    """
    if process.stderr is None:
        return

    start_time = time.monotonic()
    
    async for frac in parse_ffmpeg_progress(process.stderr, total_duration):
        elapsed = time.monotonic() - start_time
        speed_str = ""
        eta_str = ""
        if elapsed > 1.0 and frac > 0.0:
            speed_val = (frac * total_duration) / elapsed
            speed_str = f"{speed_val:.1f}x"
            if speed_val > 0:
                eta_sec = int((total_duration - (frac * total_duration)) / speed_val)
                eta_str = f"~{eta_sec}s"

        if _ps is not None:
            _ps.update(
                job_id=job_id,
                ffmpeg_pct=min(frac * 100.0, 99.5),
                ffmpeg_speed=speed_str,
                ffmpeg_eta=eta_str,
            )


# ---------------------------------------------------------------------------
# Backward-compatible aliases (keep existing callers from breaking)
# ---------------------------------------------------------------------------


def needs_transcoding(file_name: str) -> bool:
    """
    Deprecated — use inspect_media() for real codec detection.
    Kept for backward compatibility with existing callers.
    """
    ext = Path(file_name).suffix.lower()
    return ext in {".hevc", ".mov", ".mkv", ".webm", ".avi", ".3gp", ".wmv", ".flv"}


# ---------------------------------------------------------------------------
# Dedicated MP4 → MP3 Extraction
# ---------------------------------------------------------------------------


async def mp4_to_mp3(
    input_path: str,
    output_path: str | None = None,
) -> str:
    """
    Extracts audio from an MP4/video file and saves it as high-quality MP3.

    Args:
        input_path: Absolute or relative path to the source video file in .tmp/
        output_path: Optional custom output path. If None, auto-generates
                     a path in .tmp/ using uuid4() prefix.

    Returns:
        Absolute path to the converted MP3 file.

    Raises:
        FileNotFoundError: If input_path does not exist.
        UnsupportedFormatError: If input is already an audio file (no video stream).
        InspectionError: If ffprobe cannot read the input file.
        TranscodeError: If FFmpeg extraction fails.
    """
    in_p = Path(input_path)
    if not in_p.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    # Verify input has video stream via inspect_media
    inspection = await inspect_media(str(in_p))
    if not inspection.get("is_video_file") and not inspection.get("video_codec"):
        raise UnsupportedFormatError(
            format_name="audio",
            codec_name="unknown",
        )

    _TMP_DIR.mkdir(exist_ok=True)
    if output_path is None:
        out_p = _TMP_DIR / f"{uuid.uuid4().hex}_{in_p.stem}.mp3"
    else:
        out_p = Path(output_path)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(in_p),
        "-vn",
        "-c:a", "libmp3lame",
        "-q:a", "2",
        "-ar", "44100",
        "-ac", "2",
        "-map_metadata", "-1",
        str(out_p),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        logger.debug("mp4_to_mp3 FFmpeg stderr: %s", stderr_text)

        if proc.returncode != 0:
            raise TranscodeError(
                input_path=str(in_p),
                reasons=[f"MP4→MP3 extraction failed: {stderr_text[:500]}"],
            )

        return str(out_p.resolve())

    except asyncio.TimeoutError as exc:
        logger.error("mp4_to_mp3 timed out after 120s")
        raise TranscodeError(
            input_path=str(in_p),
            reasons=["MP4→MP3 extraction timed out after 120 seconds"],
        ) from exc

