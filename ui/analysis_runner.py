"""
ui/analysis_runner.py — Background analysis job builder.

Builds and submits the five-stage processing pipeline as a queued job:
  Stage 1  : File received (in-memory)
  Stage 1.5: MP4 → MP3 audio extract  (optional)
  Stage 2  : Video transcode (HEVC/VFR → H.264, if needed)
  Stage 3  : Cloud AI upload + indexing
  Stage 4  : Gemini summary generation (streaming)
  Stage 5  : Temp-file cleanup

Call `submit_analysis_job()` from the UI thread. All heavy I/O runs in the
QueueManager daemon thread — no Streamlit callbacks are used inside the
worker, so the thread-safety constraint is fully satisfied.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from pathlib import Path
from typing import Any, Callable



import ui.pipeline_state as _ps
from gemini_client import GeminiVideoClient, SummaryResult
from exceptions import TranscodeError, InspectionError
from transcoder import inspect_media, transcode_with_fallback, is_ffmpeg_available
from queue_worker import get_queue_manager
from file_handler import is_audio_file


logger = logging.getLogger(__name__)


def build_analysis_fn(
    *,
    file_bytes: bytes,
    file_name: str,
    target_language: str,
    source_language: str,
    combined_instructions: str,
    conversion_mode: str | None,
) -> Callable[[], Any]:
    """
    Build and return the worker callable for a single analysis job.

    All captured values are plain Python objects (no Streamlit references),
    making the closure fully thread-safe.

    Returns:
        A zero-argument callable that runs the full pipeline and returns
        a SummaryResult, or raises on failure.
    """

    def _run() -> SummaryResult:
        import uuid as _uuid

        gemini = GeminiVideoClient()
        media_bytes = io.BytesIO(file_bytes)
        effective_file_name = file_name
        transcoded_path: str | None = None
        mp3_path: str | None = None

        _tmp = Path(".tmp")
        _tmp.mkdir(exist_ok=True)
        _tmp_in = _tmp / f"inspect_{_uuid.uuid4().hex}_{Path(file_name).name}"

        try:
            # ── Stage 1: Write to disk for ffprobe ─────────────────────────
            _tmp_in.write_bytes(file_bytes)
            _ps.update(stage=1, pct=100.0, bytes_total=len(file_bytes), sub_message=file_name)

            # ── Inspect media ───────────────────────────────────────────────
            _is_audio_input = is_audio_file(file_name)
            try:
                inspection = asyncio.run(inspect_media(str(_tmp_in)))
            except InspectionError as exc:
                logger.warning("ffprobe inspection failed: %s — using conservative defaults", exc)
                inspection = {
                    "needs_transcode": False if _is_audio_input else True,
                    "needs_audio_transcode": False,
                    "is_video_file": not _is_audio_input,
                    "detected_video_codec": "" if _is_audio_input else "unknown",
                    "detected_audio_codec": "opus" if _is_audio_input else "",
                    "is_variable_framerate": False,
                    "detected_container": "webm" if _is_audio_input else "",
                    "detected_pix_fmt": "",
                    "detected_profile": "",
                    "is_iphone": False,
                    "is_android": False,
                    "incompatibility_reasons": [] if _is_audio_input else ["ffprobe inspection failed — transcoding as precaution"],
                    "duration_seconds": 0.0,
                    "file_size_bytes": len(file_bytes),
                }

            is_iphone   = inspection.get("is_iphone", False)
            is_android  = inspection.get("is_android", False)
            is_whatsapp = inspection.get("is_whatsapp", False)

            # Defensive: pure audio files natively accepted by Gemini API never need video transcoding
            if _is_audio_input or not inspection.get("is_video_file"):
                inspection["is_video_file"] = False
                # If audio is already a standard format, avoid unnecessary transcode
                ext_low = Path(file_name).suffix.lower()
                if ext_low in {".webm", ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"} or file_name.startswith("recorded_audio"):
                    inspection["needs_transcode"] = False

            if inspection.get("needs_transcode"):
                _ps.update(
                    stage=2,
                    stage_label="Preparing…",
                    skipped_transcode=False,
                    sub_message=(
                        "WhatsApp media export" if is_whatsapp
                        else ("iPhone recording" if is_iphone
                              else ("Android recording" if is_android else "Converting format"))
                    ),
                )
            else:
                _ps.update(skipped_transcode=True)

            # ── Stage 1.5: MP4 → MP3 extract (optional) ────────────────────
            if conversion_mode == "mp3":
                from transcoder import mp4_to_mp3
                _ps.update(stage=1.5, stage_label="Extracting Audio…")
                # If input file is already an audio recording or audio track, bypass video extraction
                if _is_audio_input or not inspection.get("is_video_file") and not inspection.get("video_codec"):
                    logger.info("Input '%s' is already an audio file — skipping video extraction", effective_file_name)
                    inspection["needs_transcode"] = False
                else:
                    try:
                        mp3_path = asyncio.run(mp4_to_mp3(str(_tmp_in)))
                        if mp3_path == str(_tmp_in):
                            raise RuntimeError("SAFETY BLOCK: mp3_path must differ from source")
                        effective_file_name = Path(mp3_path).name
                        media_bytes = io.BytesIO(Path(mp3_path).read_bytes())
                        inspection["needs_transcode"] = False
                    except TranscodeError as exc:
                        raise TranscodeError(
                            input_path=str(_tmp_in),
                            reasons=["Audio extraction failed. Try '🚀 Analyse Media' to process the full video instead."],
                        ) from exc



            # ── Stage 2: Transcode if needed ────────────────────────────────
            if inspection["needs_transcode"] and is_ffmpeg_available():
                transcoded_path, _ = asyncio.run(
                    transcode_with_fallback(str(_tmp_in), inspection, file_size_bytes=len(file_bytes))
                )
                effective_file_name = Path(transcoded_path).name
                media_bytes = io.BytesIO(Path(transcoded_path).read_bytes())
            elif inspection["needs_transcode"] and not is_ffmpeg_available():
                logger.warning("File needs transcoding but ffmpeg is unavailable — sending original (may fail)")

            # ── Safety assertion: never send incompatible file ──────────────
            assert (
                not inspection["needs_transcode"]
                or transcoded_path is not None
                or not is_ffmpeg_available()
            ), (
                f"SAFETY BLOCK: Attempted to send incompatible file '{file_name}' to Cloud AI. "
                f"Reasons: {inspection.get('incompatibility_reasons')}"
            )

            # Cleanup inspection temp before upload
            try:
                _tmp_in.unlink(missing_ok=True)
            except OSError:
                pass

            # ── Stage 3 & 4: Upload → Index → Generate ──────────────────────
            from gemini_client import VideoProcessingError

            try:
                stream_result = gemini.summarise_stream(
                    file_obj=media_bytes,
                    file_name=effective_file_name,
                    target_language=target_language,
                    source_language=source_language,
                    extra_instructions=combined_instructions,
                    log_callback=None,      # callbacks are not thread-safe; omitted in queued mode
                    progress_callback=None,
                )
            except VideoProcessingError as vpe:
                logger.warning(
                    "Cloud AI rejected media file '%s' with VideoProcessingError (%s). Attempting reactive auto-repair transcode...",
                    effective_file_name, vpe
                )
                if transcoded_path is None and is_ffmpeg_available():
                    _ps.update(
                        stage=2,
                        stage_label="Converting format…",
                        skipped_transcode=False,
                        sub_message="Auto-repairing format for Cloud AI",
                    )
                    inspection["needs_transcode"] = True
                    if not _tmp_in.exists():
                        _tmp_in.write_bytes(file_bytes)
                    transcoded_path, _ = asyncio.run(
                        transcode_with_fallback(str(_tmp_in), inspection, file_size_bytes=len(file_bytes))
                    )
                    effective_file_name = Path(transcoded_path).name
                    media_bytes = io.BytesIO(Path(transcoded_path).read_bytes())
                    stream_result = gemini.summarise_stream(
                        file_obj=media_bytes,
                        file_name=effective_file_name,
                        target_language=target_language,
                        source_language=source_language,
                        extra_instructions=combined_instructions,
                        log_callback=None,
                        progress_callback=None,
                    )
                elif mp3_path is None and is_ffmpeg_available():
                    from transcoder import mp4_to_mp3
                    _ps.update(stage=1.5, stage_label="Extracting Audio Fallback…")
                    if not _tmp_in.exists():
                        _tmp_in.write_bytes(file_bytes)
                    mp3_path = asyncio.run(mp4_to_mp3(str(_tmp_in)))
                    effective_file_name = Path(mp3_path).name
                    media_bytes = io.BytesIO(Path(mp3_path).read_bytes())
                    stream_result = gemini.summarise_stream(
                        file_obj=media_bytes,
                        file_name=effective_file_name,
                        target_language=target_language,
                        source_language=source_language,
                        extra_instructions=combined_instructions,
                        log_callback=None,
                        progress_callback=None,
                    )
                else:
                    raise


            try:
                full_text = "".join(stream_result.stream)
            except Exception as stream_exc:
                logger.error("Streaming generation failed mid-stream: %s", stream_exc)
                if stream_result and stream_result.remote_file_name:
                    try:
                        gemini.delete_remote_file(stream_result.remote_file_name)
                    except Exception as del_exc:
                        logger.warning("Failed to delete remote file %s on stream error: %s", stream_result.remote_file_name, del_exc)

                raise stream_exc

            return SummaryResult(
                summary_markdown=full_text,
                remote_file_name=stream_result.remote_file_name,
                video_filename=stream_result.video_filename,
                target_language=stream_result.target_language,
            )

        finally:
            # ── Stage 5: Cleanup temp files ─────────────────────────────────
            _ps.update(stage=5, stage_label="Cleaning up…")
            for path in [str(_tmp_in), transcoded_path, mp3_path]:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                        logger.info("Cleaned up temp file: %s", path)
                    except OSError as exc:
                        logger.warning("Could not delete temp file: %s", exc)
            _ps.update(local_deleted=True, cloud_deleted=bool(transcoded_path or mp3_path))

    return _run


def submit_analysis_job(
    *,
    uploaded_file: Any,
    target_language: str,
    source_language: str,
    extra_instructions: str,
    conversion_mode: str | None,
    classroom_mode: bool,
) -> str:
    """
    Read the uploaded file, build the analysis pipeline, and submit it to the
    background QueueManager.

    Args:
        uploaded_file:       Streamlit UploadedFile object (read immediately on the UI thread).
        target_language:     Summary output language.
        source_language:     Audio source language hint.
        extra_instructions:  Optional user-defined prompt additions.
        conversion_mode:     "mp3" to extract audio-only; None for full video.
        classroom_mode:      If True, prepend classroom noise-isolation directive.

    Returns:
        job_id: UUID string for polling with get_queue_manager().get_job(job_id).

    Raises:
        Any exception if file reading or initial validation fails.
    """
    # Read bytes immediately on the UI thread — UploadedFile is not thread-safe
    uploaded_file.seek(0)
    file_bytes: bytes = uploaded_file.read()
    file_name: str = uploaded_file.name

    # Build combined instructions
    instructions = extra_instructions or ""
    if classroom_mode:
        classroom_directive = (
            "CLASSROOM NOISE ISOLATION MODE ACTIVE: Filter out all ambient classroom noise, "
            "coughing, student chatter, paper rustling, door slams, and room echo. Focus 100% "
            "exclusively on the professor's / instructor's voice and lecture contents."
        )
        instructions = f"{classroom_directive}\n\n{instructions}".strip()

    runner = build_analysis_fn(
        file_bytes=file_bytes,
        file_name=file_name,
        target_language=target_language,
        source_language=source_language,
        combined_instructions=instructions,
        conversion_mode=conversion_mode,
    )

    job_id = get_queue_manager().submit(runner)
    logger.info("Analysis job %s submitted for file '%s'", job_id[:8], file_name)
    return job_id
