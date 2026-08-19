"""
gemini_client.py — Robust Gemini API client for media (video + audio)
summarisation.

Responsibilities:
  1. Upload media files to the Gemini Files API via a background thread,
     reporting smooth real-time progress to the caller.
  2. Poll for ACTIVE state with sub-tick animation.
  3. Generate a structured summary using the Multilingual Synthesizer prompt.
  4. Delete the remote file from the Files API in a guaranteed finally block.

Progress curves use asymptotic functions (1 - 1/(1+kx)) instead of hard
ceilings, so the bar never freezes — it smoothly decelerates.
"""

from __future__ import annotations

import logging
import math
import mimetypes
import threading
import time
import io
from pathlib import Path
from typing import Callable, Any, Iterator
import httpx

from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from config import config
from prompts import build_system_prompt, build_user_prompt


# Lazy import — pipeline_state only exists when running inside Streamlit
try:
    import ui.pipeline_state as _ps  # type: ignore[import]
except ImportError:
    _ps = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# SDK-native exception types for retry / quota detection
# ---------------------------------------------------------------------------
# The google-genai SDK uses google.genai.errors — google.api_core is NOT
# installed in this project. Mapping:
#   ServerError (5xx) — transient; safe to retry
#   ClientError (4xx) — permanent; check .code for 429 quota exceeded
# ---------------------------------------------------------------------------
from google.genai import errors as _genai_errors

_RETRYABLE_EXCEPTIONS = (_genai_errors.ServerError, httpx.RequestError)   # 500 / 503 and Network failures
_CLIENT_ERROR_CLASS    = _genai_errors.ClientError      # 429 quota, 403 auth


def _is_quota_error(exc: BaseException) -> bool:
    """Return True if exc is an HTTP 429 ResourceExhausted (quota exceeded)."""
    return isinstance(exc, _CLIENT_ERROR_CLASS) and getattr(exc, "code", 0) == 429


def _is_auth_error(exc: Exception) -> bool:
    """Return True if exc is an HTTP 401/403 authentication error."""
    return isinstance(exc, _CLIENT_ERROR_CLASS) and getattr(exc, "code", 0) in (401, 403)


def _is_503_error(exc: BaseException) -> bool:
    """Return True if exc represents a transient 503 / UNAVAILABLE server error."""
    return _is_fallback_candidate_error(exc)


def _is_fallback_candidate_error(exc: BaseException) -> bool:
    """
    Return True if exc represents a 503 UNAVAILABLE, 429 RATE_LIMIT, 404 NOT_FOUND,
    400 model deprecated, or high demand error that should trigger fallback to the next model.
    """
    err_str = str(exc).lower()
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (404, 429, 500, 502, 503, 504):
        return True
    if isinstance(exc, _genai_errors.ServerError):
        return True
    if isinstance(exc, httpx.RequestError):
        return True
    if any(k in err_str for k in (
        "503", "unavailable", "high demand", "not_found", "404", "429", "quota",
        "resource_exhausted", "rate limit", "no longer available", "not found",
        "is not supported", "overloaded", "capacity"
    )):
        return True
    return False



logger = logging.getLogger(__name__)

# ── Global atexit cleanup hook for active remote Google Cloud AI files ────────
import atexit as _atexit
_ACTIVE_REMOTE_FILES: set[str] = set()
_ACTIVE_CLIENTS: list[Any] = []

def _cleanup_all_active_remote_files() -> None:
    for _cl in list(_ACTIVE_CLIENTS):
        for _fn in list(_ACTIVE_REMOTE_FILES):
            try:
                _cl.files.delete(name=_fn)
            except Exception:
                pass
    _ACTIVE_REMOTE_FILES.clear()

_atexit.register(_cleanup_all_active_remote_files)
# ─────────────────────────────────────────────────────────────────────────────

_UPLOAD_END   = 0.25   # upload   occupies   0% →  25%
_PROCESS_END  = 0.55   # polling  occupies  25% →  55%
_GENERATE_END = 0.97   # generation         55% →  97%
# Final cleanup bump: 97% → 100%


# ---------------------------------------------------------------------------
# Asymptotic progress helper
# ---------------------------------------------------------------------------

def _asymptotic_progress(
    elapsed: float,
    estimated: float,
    ceiling: float = 0.98,
) -> float:
    """
    Compute a smooth progress value that approaches ``ceiling`` without
    ever reaching it.  Uses `ceiling * (1 - e^(-k*t))` where k is tuned
    so that progress reaches ~90% of ceiling at ``estimated`` seconds.

    This eliminates hard-ceiling freezes: the bar always moves, just
    progressively slower.
    """
    if estimated <= 0:
        return ceiling
    # k chosen so that at t=estimated, progress ≈ 0.90 * ceiling
    k = -math.log(0.10) / estimated   # ≈ 2.3 / estimated
    return ceiling * (1 - math.exp(-k * elapsed))


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class VideoProcessingError(Exception):
    """Raised when the Gemini Files API fails to process the uploaded media."""

    @property
    def ui_message(self) -> str:
        return f"Media Processing Error: {self}"


class SummaryGenerationError(Exception):
    """Raised when the content-generation request fails or returns empty."""

    @property
    def ui_message(self) -> str:
        return f"Generation Error: {self}"


class APIKeyError(Exception):
    """Raised when the API key is missing or rejected."""

    @property
    def ui_message(self) -> str:
        return f"API Key Error: {self}"


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
LogCallback      = Callable[[str], None]
ProgressCallback = Callable[[float, str], None]   # (fraction 0-1, label)


# ---------------------------------------------------------------------------
# Programmatic JSON Schema Fallback
# ---------------------------------------------------------------------------

class KeyConcept(BaseModel):
    name: str
    definition: str
    context: str
    example: str | None = None

class Section(BaseModel):
    title: str
    timestamp_range: str
    main_points: list[str]
    notable_quote: str | None = None

class SummaryJSON(BaseModel):
    media_type: str
    detected_language: str
    target_language: str
    estimated_duration: str
    primary_domain: str
    difficulty_level: str
    content_type: str
    speaker_count: str
    executive_summary: str
    key_concepts: list[KeyConcept]
    sections: list[Section]
    insights: list[str]
    questions: list[str]
    tags: list[str]


# ---------------------------------------------------------------------------
# GeminiVideoClient
# ---------------------------------------------------------------------------


class GeminiVideoClient:
    """
    High-level client wrapping the Gemini SDK for media-based summarisation.

    Supports both video and audio files. The Gemini Files API handles
    both media types natively.

    Usage::

        client = GeminiVideoClient()
        result = client.summarise(
            video_path=Path("lecture.mp4"),
            target_language="French",
            log_callback=lambda msg: print(msg),
            progress_callback=lambda frac, label: update_bar(frac, label),
        )
        print(result.summary_markdown)
    """

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or config.gemini_api_key
        if not key:
            raise APIKeyError("API key cannot be empty. Please configure your API key in .env.")

        self._client = genai.Client(api_key=key)
        _ACTIVE_CLIENTS.append(self._client)
        self._model  = config.gemini_model

        logger.info("GeminiVideoClient initialised with model: %s", self._model)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _get_fallback_models(self) -> list[str]:
        """Return fallback model chain starting with configured model, followed by valid Google Gemini models."""
        candidates = [
            self._model,
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-pro",
        ]
        valid_candidates: list[str] = []
        for m in candidates:
            if not m:
                continue
            # Normalize legacy/mock model names to valid models
            if m.startswith("gemini-2.5") or m.startswith("gemini-3."):
                m = "gemini-2.0-flash"
            if m not in valid_candidates:
                valid_candidates.append(m)
        return valid_candidates



    def summarise_stream(
        self,
        file_obj: io.IOBase,
        file_name: str,
        target_language: str = "English",
        source_language: str = "Auto-detect",
        extra_instructions: str = "",
        log_callback: LogCallback | None = None,
        progress_callback: ProgressCallback | None = None,
        job_id: str | None = None,
    ) -> StreamingSummaryResult:
        """
        Full pipeline: upload → wait for ACTIVE → generate → cleanup.

        Args:
            file_obj: The media file stream in memory.
            file_name: The original filename of the media.
            target_language: Language for the output summary.
            source_language: The expected language of the input media.
            extra_instructions: Optional user additions to the prompt.
            log_callback: Called with human-readable status strings.
            progress_callback: Called with (fraction, label) as processing
                               advances. ``fraction`` is in [0.0, 1.0].
            as_json: Whether to return structured JSON according to schema.

        Returns:
            SummaryResult containing the markdown summary and metadata.

        Raises:
            VideoProcessingError: If cloud processing fails or times out.
            SummaryGenerationError: If content generation fails.
        """

        def _log(msg: str) -> None:
            logger.info(msg)
            if log_callback:
                log_callback(msg)

        def _progress(fraction: float, label: str) -> None:
            if progress_callback:
                progress_callback(min(fraction, 1.0), label)

        remote_file = None
        try:
            # ── Stage 3: Upload (0 → _UPLOAD_END) ─────────────────────
            if _ps is not None:
                _ps.update(job_id=job_id, stage=3, stage_label="Uploading to Cloud AI…", pct=0.0,
                           bytes_sent=0, bytes_total=0)
            _log(f"📤 Uploading '{file_name}' to Cloud AI…")
            remote_file = self._upload_with_progress(
                file_obj,
                file_name,
                progress_callback=lambda frac, lbl: _progress(
                    frac * _UPLOAD_END, lbl
                ),
                log_callback=_log,
                job_id=job_id,
            )
            _log(f"✅ Upload complete — remote ID: {remote_file.name}")
            _progress(_UPLOAD_END, "Upload complete ✓")

            # ── Stage 3b: Wait for ACTIVE (_UPLOAD_END → _PROCESS_END) ─
            if _ps is not None:
                _ps.update(job_id=job_id, stage=3, stage_label="Indexing media…",
                           sub_message="AI Cloud is indexing your file…")
            _log("⏳ Waiting for Cloud AI to index the media…")
            remote_file = self._wait_for_active(
                remote_file,
                log_callback=_log,
                progress_callback=lambda frac, lbl: _progress(
                    _UPLOAD_END + frac * (_PROCESS_END - _UPLOAD_END), lbl
                ),
            )
            _log("✅ Media indexed and ready for analysis.")
            _progress(_PROCESS_END, "Media indexed ✓")

            # ── Stage 4: Generate (_PROCESS_END → _GENERATE_END) ──────
            if _ps is not None:
                _ps.update(job_id=job_id, stage=4, stage_label="AI Processing…",
                           word_count=0, sections_done=[])
            _log(f"🤖 Generating {target_language} educational summary…")
            stream = self._generate_summary_stream(
                remote_file,
                target_language,
                source_language,
                extra_instructions,
            )
            _progress(_GENERATE_END, "Summary streaming…")

            assert remote_file.name is not None
            return StreamingSummaryResult(
                stream=stream,
                remote_file_name=remote_file.name,
                video_filename=file_name,
                target_language=target_language,
            )

        except VideoProcessingError:
            # Proactive transcoding now happens BEFORE this call in app.py's
            # _run_analysis worker. If we still hit a VideoProcessingError here,
            # the file was already inspected and transcoded (or transcoding was
            # not possible). Re-raise directly — no further fallback.
            if remote_file and remote_file.name:
                try:
                    self.delete_remote_file(remote_file.name)
                except Exception as del_exc:
                    logger.warning("Failed to delete remote file %s during error cleanup: %s", remote_file.name, del_exc)
            raise
        except Exception:
            if remote_file and remote_file.name:
                try:
                    self.delete_remote_file(remote_file.name)
                except Exception as del_exc:
                    logger.warning("Failed to delete remote file %s during cleanup: %s", remote_file.name, del_exc)
            raise

    # ------------------------------------------------------------------
    # Upload with smooth real-time progress
    # ------------------------------------------------------------------

    def _upload_with_progress(
        self,
        file_obj: io.IOBase,
        file_name: str,
        progress_callback: Callable[[float, str], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
        job_id: str | None = None,
    ) -> genai_types.File:
        """
        Upload a media file in a daemon background thread while the main
        thread animates upload progress using an asymptotic curve.

        The bar never freezes — it smoothly decelerates towards 98% and
        jumps to 100% when the thread completes.
        """
        if _ps is not None:
            _ps.update(job_id=job_id, stage=3, stage_label="Uploading to Cloud AI…", pct=0.0)

        result_holder: list[genai_types.File | None] = [None]
        error_holder:  list[BaseException | None]    = [None]

        def _do_upload() -> None:
            try:
                result_holder[0] = self._upload_media_retry(file_obj, file_name)
            except BaseException as exc:  # noqa: BLE001
                error_holder[0] = exc

        file_size_mb = getattr(file_obj, "size", 0) / (1024 * 1024)
        if file_size_mb == 0:
            if isinstance(file_obj, io.BytesIO):
                file_size_mb = file_obj.getbuffer().nbytes / (1024 * 1024)
            elif hasattr(file_obj, "fileno"):
                import os
                try:
                    file_size_mb = os.fstat(file_obj.fileno()).st_size / (1024 * 1024)
                except Exception as stat_exc:
                    logger.debug("Could not determine file size via fstat: %s", stat_exc)

        # Assume configurable upload speed — default 8 MB/s
        estimated_seconds = max(3.0, file_size_mb / config.upload_speed_mbps)
        _bytes_total_approx = int(file_size_mb * 1024 * 1024)

        thread = threading.Thread(target=_do_upload, daemon=True)
        thread.start()

        start = time.monotonic()
        _last_frac: float = -1.0   # throttle: skip callbacks with < 0.5% change
        while thread.is_alive():
            elapsed   = time.monotonic() - start
            simulated = _asymptotic_progress(elapsed, estimated_seconds, ceiling=0.98)
            mb_done   = simulated * file_size_mb
            bytes_done = int(simulated * _bytes_total_approx)

            # Compute simulated speed
            speed_str = ""
            if elapsed > 1.0:
                speed_mbps = mb_done / elapsed
                speed_str = f"{speed_mbps:.1f} MB/s"

            if _ps is not None:
                _ps.update(
                    job_id=job_id,
                    bytes_sent=bytes_done,
                    bytes_total=_bytes_total_approx,
                    upload_speed=speed_str,
                    pct=simulated * 100,
                )

            if progress_callback and (simulated - _last_frac) >= 0.005:
                progress_callback(
                    simulated,
                    f"Uploading  {mb_done:.1f} / {file_size_mb:.1f} MB"
                    f"  ({simulated * 100:.0f}%)",
                )
                _last_frac = simulated
            time.sleep(0.25)

        thread.join()

        # Unwrap tenacity RetryError to expose the underlying root cause.
        # Use a local variable so Pylance can narrow BaseException | None -> BaseException.
        err = error_holder[0]
        if err is not None:
            cause: BaseException = err
            if isinstance(cause, RetryError) and cause.last_attempt.failed:
                inner = cause.last_attempt.exception()
                # inner is BaseException | None; assert narrows type for Pylance
                assert inner is not None, "RetryError.last_attempt.exception() was None"
                raise inner from cause
            raise cause

        if progress_callback:
            progress_callback(1.0, f"Uploaded {file_size_mb:.1f} MB ✓")

        if _ps is not None:
            _ps.update(job_id=job_id, bytes_sent=_bytes_total_approx, pct=100.0, upload_speed="")

        if result_holder[0] is None:
            raise RuntimeError("Upload returned None without raising — this is a bug.")
        return result_holder[0]

    @retry(
        retry=retry_if_exception(
            lambda e: isinstance(e, _RETRYABLE_EXCEPTIONS) or _is_quota_error(e)
        ),
        # Lambda reads config lazily at retry time — NOT at decoration time.
        # If evaluated at decoration time the value is frozen before st.secrets injection.
        wait=wait_exponential(
            multiplier=1.5, min=2, max=30
        ),
        stop=stop_after_attempt(3),
        reraise=True,
        before_sleep=lambda rs: (
            _ps.update(retry_state={
                "attempt": rs.attempt_number,
                "wait_seconds": int(getattr(rs.next_action, 'sleep', 0)),
                "is_retrying": True,
            })
        ) if _ps is not None else None,
        after=lambda rs: (
            _ps.update(retry_state={"attempt": 0, "wait_seconds": 0, "is_retrying": False})
        ) if _ps is not None else None,
    )
    def _upload_media_retry(self, file_obj: io.IOBase, file_name: str) -> genai_types.File:
        """
        Inner upload call, decorated with tenacity retry.

        Only retries on transient ServiceUnavailable / InternalServerError.
        Non-retryable errors (bad key, quota exceeded) are raised immediately.
        Supports both video/* and audio/* MIME types.
        """
        try:
            fn_lower = file_name.lower()
            ext = Path(file_name).suffix.lower()

            # Check if this is an audio file (single or double/compound extension)
            is_audio = False
            for ae in [".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".opus", ".weba", ".wma", ".mp2", ".mp1"]:
                if ext == ae or f"{ae}." in fn_lower or f"{ae}_" in fn_lower or file_name.startswith("recorded_audio"):
                    is_audio = True
                    break

            if is_audio:
                if ".wav" in fn_lower:
                    mime_type = "audio/wav"
                elif ".flac" in fn_lower:
                    mime_type = "audio/flac"
                elif ".ogg" in fn_lower or ".opus" in fn_lower:
                    mime_type = "audio/ogg"
                elif ".m4a" in fn_lower or ".aac" in fn_lower:
                    mime_type = "audio/mp4"
                else:
                    mime_type = "audio/mpeg"  # Default for .mp3, .mp3.mpeg, etc.
            else:
                mime_type, _ = mimetypes.guess_type(file_name)
                if not mime_type:
                    mime_fallbacks = {
                        ".mp4": "video/mp4", ".mov": "video/quicktime",
                        ".avi": "video/x-msvideo", ".webm": "video/webm",
                        ".mp3": "audio/mpeg", ".wav": "audio/wav",
                        ".flac": "audio/flac", ".aac": "audio/aac",
                        ".ogg": "audio/ogg", ".m4a": "audio/mp4",
                        ".wma": "audio/x-ms-wma", ".opus": "audio/opus",
                        ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
                    }
                    mime_type = mime_fallbacks.get(ext, "application/octet-stream")

            # Always seek to the beginning before uploading, especially vital for Tenacity retries
            file_obj.seek(0)
            
            uploaded_file = self._client.files.upload(
                file=file_obj,
                config=genai_types.UploadFileConfig(
                    display_name=file_name,
                    mime_type=mime_type,
                ),
            )
            if uploaded_file and getattr(uploaded_file, "name", None):
                _ACTIVE_REMOTE_FILES.add(str(uploaded_file.name))
            return uploaded_file

        except Exception as exc:
            # 429 quota exceeded
            if _is_quota_error(exc):
                raise APIKeyError(
                    "API quota exceeded. Please wait a minute and try again, "
                    "or check your API plan."
                ) from exc
            # 401/403 invalid key
            if _is_auth_error(exc):
                raise APIKeyError(
                    f"API authentication failed (HTTP {getattr(exc, 'code', '?')}). "
                    "Please check your API key in the .env file."
                ) from exc
            raise

    # Backward-compatible alias
    _upload_video_retry = _upload_media_retry

    # ------------------------------------------------------------------
    # Poll until ACTIVE
    # ------------------------------------------------------------------

    def _wait_for_active(
        self,
        remote_file: genai_types.File,
        log_callback: LogCallback,
        progress_callback: ProgressCallback | None = None,
    ) -> genai_types.File:
        """
        Poll the Files API until the file reaches ACTIVE or FAILED state.

        Animates the progress bar at 0.25s sub-intervals between each API
        poll so the bar moves smoothly instead of jumping every 2s.
        """
        max_attempts  = config.max_poll_attempts
        poll_interval = config.poll_interval_seconds
        TICK          = 0.25   # UI animation tick (seconds)
        ticks_per_poll = max(1, int(poll_interval / TICK))

        for attempt in range(max_attempts):
            state_name = remote_file.state.name if remote_file.state else "PROCESSING"

            if state_name == "ACTIVE":
                if progress_callback:
                    progress_callback(1.0, "Media indexed ✓")
                return remote_file

            if state_name == "FAILED":
                # FAILED state implies backend error (e.g. unsupported codec like HEVC/H.265 or corrupted container)
                error = getattr(remote_file, "error", None)
                err_str = str(error)
                if "code=13" in err_str or "failed to be processed" in err_str.lower():
                    raise VideoProcessingError(
                        "Cloud AI failed to process this media codec (code=13). "
                        "If recorded on an iPhone/Android, the file is likely using HEVC/H.265 or variable frame rates. "
                        "Please convert the video to standard H.264 MP4 (or extract as MP3) before uploading."
                    )
                raise VideoProcessingError(
                    f"Cloud AI processing failed: {error}"
                )

            # Still PROCESSING — animate bar between polls
            elapsed_s = (attempt + 1) * poll_interval

            # Only log every 5 attempts to avoid flooding the log panel
            if attempt % 5 == 0:
                log_callback(
                    f"⏳ Server processing… {elapsed_s:.0f}s elapsed "
                    f"(attempt {attempt + 1}/{max_attempts})"
                )

            # Animate progress bar at TICK intervals between API polls
            base_frac = min(0.95, attempt / max(max_attempts, 20))
            next_frac = min(0.95, (attempt + 1) / max(max_attempts, 20))

            for tick in range(ticks_per_poll):
                interp = base_frac + (next_frac - base_frac) * (tick / ticks_per_poll)
                if progress_callback:
                    progress_callback(
                        interp,
                        f"Indexing media… {elapsed_s:.0f}s",
                    )
                time.sleep(TICK)

            if not remote_file.name:
                raise VideoProcessingError("Remote file is missing name.")
            remote_file = self._get_file_retry(remote_file.name)

        raise VideoProcessingError(
            f"Processing timed out after "
            f"{max_attempts * poll_interval:.0f} seconds. "
            "Try a shorter or lower-quality file."
        )

    @retry(
        retry=retry_if_exception(
            lambda e: isinstance(e, _RETRYABLE_EXCEPTIONS) or _is_quota_error(e)
        ),
        wait=wait_exponential(multiplier=config.tenacity_retry_multiplier, min=2, max=10),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get_file_retry(self, name: str) -> genai_types.File:
        return self._client.files.get(name=name)

    # ------------------------------------------------------------------
    # Generate content (with animated progress)
    # ------------------------------------------------------------------

    def _generate_summary_stream(
        self,
        remote_file: genai_types.File,
        target_language: str,
        source_language: str,
        extra_instructions: str,
    ) -> Iterator[str]:
        """
        Yields the generated summary text chunk-by-chunk using the streaming API.
        Automatically falls back to secondary models if 503 UNAVAILABLE (high demand) occurs.
        """
        if _ps is not None:
            _ps.update(stage=4, stage_label="AI Processing…", word_count=0, sections_done=[])

        system_instruction = build_system_prompt(target_language)
        user_prompt        = build_user_prompt(target_language, source_language, extra_instructions)

        fallback_chain = self._get_fallback_models()
        last_exc: Exception | None = None

        for attempt_idx, model_name in enumerate(fallback_chain):
            try:
                response_stream = self._client.models.generate_content_stream(
                    model=model_name,
                    contents=[remote_file, user_prompt],  # type: ignore[arg-type]
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.2,
                    ),
                )

                chunk_received = False
                for chunk in response_stream:
                    if chunk.text:
                        chunk_received = True
                        yield chunk.text

                if chunk_received:
                    return  # Stream completed successfully

            except Exception as exc:
                last_exc = exc
                if _is_auth_error(exc):
                    raise APIKeyError(
                        f"API authentication failed (HTTP {getattr(exc, 'code', '?')}). "
                        "Please check your GEMINI_API_KEY in secrets."
                    ) from exc

                logger.warning(
                    "Model '%s' failed (%s). Retrying with next model in fallback chain...",
                    model_name,
                    exc,
                )
                time.sleep(1.0 + (attempt_idx * 0.5))
                continue

        if last_exc is not None:
            if _is_quota_error(last_exc):
                raise APIKeyError(
                    "Google Gemini API quota limit reached for your key. "
                    "Please wait a minute before trying again or use an upgraded API key."
                ) from last_exc
            raise SummaryGenerationError(
                f"Summary generation error ({type(last_exc).__name__}): {last_exc}. "
                "Please wait a moment and click 'Analyse Media' again."
            ) from last_exc

    def _generate_summary(
        self,
        remote_file: genai_types.File,
        target_language: str,
        source_language: str,
        extra_instructions: str,
        as_json: bool = False,
    ) -> str:
        """
        Call the generative model with the media file reference and return
        the raw summary string, with automatic model fallback for unavailable models.
        """
        fallback_chain = self._get_fallback_models()
        last_exc: Exception | None = None

        for attempt_idx, model_name in enumerate(fallback_chain):
            try:
                system_instruction = build_system_prompt(target_language)
                user_prompt        = build_user_prompt(target_language, source_language, extra_instructions)

                response = self._client.models.generate_content(
                    model=model_name,
                    contents=[remote_file, user_prompt],  # type: ignore[arg-type]
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.3,
                        max_output_tokens=8192,
                        response_mime_type="application/json" if as_json else None,
                        response_schema=SummaryJSON if as_json else None,
                    ),
                )

                text = getattr(response, "text", None)
                if text and text.strip():
                    return text

            except Exception as exc:
                last_exc = exc
                if _is_auth_error(exc):
                    raise APIKeyError(
                        f"API authentication failed (HTTP {getattr(exc, 'code', '?')}). "
                        "Please check your GEMINI_API_KEY."
                    ) from exc

                logger.warning(
                    "Model '%s' failed (%s). Retrying with next model in chain...",
                    model_name,
                    exc,
                )
                time.sleep(1.0 + (attempt_idx * 0.5))
                continue

        if last_exc is not None:
            if _is_quota_error(last_exc):
                raise APIKeyError(
                    "Google Gemini API quota limit reached for your key. "
                    "Please wait a minute before trying again or use an upgraded API key."
                ) from last_exc
            raise SummaryGenerationError(
                f"Summary generation error ({type(last_exc).__name__}): {last_exc}. "
                "Please wait a moment and try again."
            ) from last_exc
        raise SummaryGenerationError("Failed to generate summary: No response received from AI models.")

    # ------------------------------------------------------------------
    # Interactive Q&A
    # ------------------------------------------------------------------

    def ask_question(
        self,
        remote_file_name: str,
        chat_history: list[dict[str, str]],
        prompt: str,
    ) -> str:
        """
        Ask a follow-up question about an already-uploaded and indexed remote media file.
        Maintains conversational history for multi-turn Q&A.
        """
        try:
            remote_file = self._get_file_retry(remote_file_name)
        except Exception as exc:
            logger.warning("Could not retrieve remote file '%s' for Q&A: %s", remote_file_name, exc)
            remote_file = None

        # Build contents
        contents: list[Any] = []
        if remote_file:
            contents.append(remote_file)

        for msg in chat_history:
            role = "user" if msg.get("role") == "user" else "model"
            content = msg.get("content", "")
            if content:
                contents.append(
                    genai_types.Content(
                        role=role,
                        parts=[genai_types.Part.from_text(text=content)],
                    )
                )

        contents.append(
            genai_types.Content(
                role="user",
                parts=[genai_types.Part.from_text(text=prompt)],
            )
        )

        for model_name in self._get_fallback_models():
            try:
                response = self._client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=(
                            "You are a helpful, accurate multimodal AI assistant answering questions about the provided media file. "
                            "Ground your answers in the media context accurately. If the media does not mention something, state that clearly."
                        ),
                        temperature=0.4,
                    ),
                )
                text = (response.text or "").strip()
                if text:
                    return text
            except Exception as exc:
                if _is_auth_error(exc):
                    raise APIKeyError("API authentication failed. Please check your GEMINI_API_KEY.") from exc
                logger.warning("Model '%s' error during Q&A (%s). Retrying with fallback...", model_name, exc)
                time.sleep(1.0)
                continue

        raise SummaryGenerationError("All AI models are currently experiencing high demand. Please try asking again in a moment.")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def delete_remote_file(self, file_name: str, job_id: str | None = None) -> None:
        """
        Public method to manually delete a file from the Gemini API.
        Useful when `delete_after=False` was passed to summarise().
        """
        if _ps is not None:
            _ps.update(job_id=job_id, stage=5, stage_label="Cleaning up…")
        def _dummy_log(msg: str) -> None:
            pass
        self._delete_remote_file(file_name, _dummy_log)
        if _ps is not None:
            _ps.update(job_id=job_id, cloud_deleted=True)

    def _delete_remote_file(
        self, file_name: str, log_callback: LogCallback
    ) -> None:
        """
        Delete a file from the Gemini Files API.

        Failures are logged as warnings and never re-raised, so they
        cannot mask a real processing error from the caller.
        """
        try:
            self._client.files.delete(name=file_name)
            _ACTIVE_REMOTE_FILES.discard(file_name)
            log_callback("🗑️  Remote file deleted from Cloud AI storage.")
        except Exception as exc:
            _ACTIVE_REMOTE_FILES.discard(file_name)
            logger.warning("Could not delete remote file '%s': %s", file_name, exc)
            log_callback(
                "⚠️  Remote file deletion skipped "
                "(it expires automatically within 48 h)."
            )




# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


class StreamingSummaryResult:
    """Immutable container for a streaming media summary."""

    __slots__ = (
        "stream",
        "remote_file_name",
        "video_filename",
        "target_language",
    )

    def __init__(
        self,
        stream: Iterator[str],
        remote_file_name: str,
        video_filename: str,
        target_language: str,
    ) -> None:
        self.stream = stream
        self.remote_file_name = remote_file_name
        self.video_filename   = video_filename
        self.target_language  = target_language


class SummaryResult:
    """Immutable container for a completed media summary."""

    __slots__ = (
        "summary_markdown",
        "remote_file_name",
        "video_filename",
        "target_language",
    )

    def __init__(
        self,
        summary_markdown: str,
        remote_file_name: str,
        video_filename: str,
        target_language: str,
    ) -> None:
        self.summary_markdown = summary_markdown
        self.remote_file_name = remote_file_name
        self.video_filename   = video_filename
        self.target_language  = target_language

    def __repr__(self) -> str:
        return (
            f"SummaryResult(file={self.video_filename!r}, "
            f"language={self.target_language!r}, "
            f"chars={len(self.summary_markdown):,})"
        )
