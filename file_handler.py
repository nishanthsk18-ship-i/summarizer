"""
file_handler.py — Robust local file management for uploaded media (video + audio).

Responsibilities:
  - Validate file type, size, and integrity on upload.
  - Save uploaded bytes to a secure temporary location, with optional
    chunk-by-chunk progress reporting.
  - Guarantee cleanup of local temp files via a context manager.
  - Detect whether a file is audio-only vs video.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Generator

from config import (
    ACCEPTED_AUDIO_EXTENSIONS,
    ACCEPTED_MEDIA_EXTENSIONS,
    ACCEPTED_VIDEO_EXTENSIONS,
    config,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class FileTooLargeError(Exception):
    """Raised when the uploaded file exceeds the configured size limit."""

    @property
    def ui_message(self) -> str:
        return f"File Too Large: {self}"


class InvalidFileTypeError(Exception):
    """Raised when the uploaded file is not a recognised media type."""

    @property
    def ui_message(self) -> str:
        return f"Invalid File Type: {self}"


# ---------------------------------------------------------------------------
# Media type detection
# ---------------------------------------------------------------------------


def is_audio_file(filename: str) -> bool:
    """Return True if ``filename`` has a recognised audio extension."""
    return Path(filename).suffix.lower() in ACCEPTED_AUDIO_EXTENSIONS


def is_video_file(filename: str) -> bool:
    """Return True if ``filename`` has a recognised video extension."""
    return Path(filename).suffix.lower() in ACCEPTED_VIDEO_EXTENSIONS


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_stream_size(file_obj: BinaryIO) -> int | None:
    """
    Try to determine the total byte size of a file-like object by seeking.
    Returns None if the object is not seekable.
    """
    try:
        pos = file_obj.tell()
        end = file_obj.seek(0, 2)   # seek to end
        file_obj.seek(pos)           # reset to original position
        return end
    except (AttributeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_media_file(
    filename: str,
    file_size_bytes: int,
    file_obj: BinaryIO | None = None,
) -> None:
    """
    Perform pre-save validation on an incoming media file.

    Args:
        filename: Original filename (used for extension check).
        file_size_bytes: Size of the file in bytes.
        file_obj: Optional seekable binary file stream for magic bytes header inspection.

    Raises:
        InvalidFileTypeError: Extension not in accepted list or magic bytes mismatch.
        FileTooLargeError: File exceeds the configured max size.
    """
    ext = Path(filename).suffix.lower()
    if ext not in ACCEPTED_MEDIA_EXTENSIONS:
        raise InvalidFileTypeError(
            f"Unsupported file type '{ext}'. "
            f"Accepted: {', '.join(ACCEPTED_MEDIA_EXTENSIONS)}"
        )

    if file_size_bytes > config.max_video_size_bytes:
        size_mb = file_size_bytes / (1024 * 1024)
        raise FileTooLargeError(
            f"File size {size_mb:.1f} MB exceeds the "
            f"{config.max_video_size_mb} MB limit."
        )

    if file_obj is not None:
        try:
            pos = file_obj.tell()
            header = file_obj.read(2048)
            file_obj.seek(pos)

            mime: str | None = None
            try:
                import magic
                mime = magic.from_buffer(header, mime=True)
            except Exception:
                pass

            if mime and not (
                mime.startswith("video/")
                or mime.startswith("audio/")
                or mime in ("application/ogg", "application/x-mpegURL", "application/octet-stream")
            ):
                raise InvalidFileTypeError(
                    f"File header signature ({mime}) does not match a valid video or audio format."
                )
        except InvalidFileTypeError:
            raise
        except Exception as exc:
            logger.debug("Magic byte inspection skipped: %s", exc)


# Backward-compatible alias
validate_video_file = validate_media_file


# ---------------------------------------------------------------------------
# File saving
# ---------------------------------------------------------------------------


def save_uploaded_file(
    file_obj: BinaryIO,
    original_filename: str,
    progress_callback: Callable[[float], None] | None = None,
) -> Path:
    """
    Save an uploaded file-like object to the configured temp directory.

    A unique UUID prefix is prepended to avoid filename collisions.
    Reads the stream in 4 MB chunks so memory stays bounded even for
    large files, and reports chunk-level progress to ``progress_callback``
    if provided.

    Args:
        file_obj: A seekable, binary file-like object (e.g. Streamlit
                  ``UploadedFile`` or ``io.BytesIO``).
        original_filename: The original name of the file (for extension).
        progress_callback: Optional callable that receives a float in
                           [0.0, 1.0] after each chunk is written.

    Returns:
        Path to the saved temporary file.
    """
    ext = Path(original_filename).suffix.lower()
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = config.temp_dir / unique_name
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Determine total size for progress reporting (may be None)
    total_size = _get_stream_size(file_obj)

    chunk_size = 4 * 1024 * 1024   # 4 MB chunks
    written = 0

    with open(dest, "wb") as out:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)

            if progress_callback and total_size:
                progress_callback(min(written / total_size, 1.0))

    logger.info(
        "Saved '%s' → '%s' (%d bytes)",
        original_filename,
        dest,
        dest.stat().st_size,
    )
    return dest


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def delete_local_file(path: Path) -> None:
    """
    Delete a local file, suppressing errors if already gone.

    Args:
        path: Absolute path to the file to delete.
    """
    try:
        path.unlink(missing_ok=True)
        logger.info("Deleted local temp file: %s", path)
    except OSError as exc:
        logger.warning("Could not delete local file '%s': %s", path, exc)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@contextmanager
def managed_temp_video(
    file_obj: BinaryIO,
    original_filename: str,
    save_progress_callback: Callable[[float], None] | None = None,
) -> Generator[Path, None, None]:
    """
    Context manager that saves an uploaded media file to disk, yields its
    path, and deletes it on exit — even if an exception is raised.

    Usage::

        with managed_temp_video(uploaded_bytes, "lecture.mp4") as media_path:
            result = client.summarise(media_path)

    Args:
        file_obj: Seekable binary file-like object.
        original_filename: Original file name (for extension detection).
        save_progress_callback: Optional progress callback (0.0 → 1.0)
                                 called during local disk write.

    Yields:
        Path to the saved temporary file.
    """
    temp_path: Path | None = None
    try:
        temp_path = save_uploaded_file(
            file_obj, original_filename, progress_callback=save_progress_callback
        )
        yield temp_path
    finally:
        if temp_path is not None:
            delete_local_file(temp_path)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def human_readable_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable string (e.g. '42.3 MB').

    Uses floating-point division so intermediate values like 1500 bytes
    are rendered as '1.5 KB' rather than the truncated '1.0 KB' that
    integer floor-division (//=) would produce.
    """
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def cleanup_stale_temp_files(max_age_seconds: int = 3600) -> int:
    """
    Purge orphaned temporary files in config.temp_dir older than max_age_seconds.
    Safe to run on app launch to recover disk space after un-graceful server restarts.
    """
    import time
    if not config.temp_dir.exists():
        return 0

    count = 0
    now = time.time()
    for item in config.temp_dir.iterdir():
        if item.is_file():
            try:
                if (now - item.stat().st_mtime) > max_age_seconds:
                    item.unlink(missing_ok=True)
                    count += 1
            except OSError as exc:
                logger.warning("Could not delete stale temp file '%s': %s", item, exc)
    if count > 0:
        logger.info("Cleaned up %d stale temp file(s) from %s", count, config.temp_dir)
    return count

