"""
config.py — Centralised configuration loader for the Media Summarizer.

Reads values from the .env file (or real environment variables) and
exposes them as a validated, typed Config dataclass so the rest of
the application always receives sensible defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root — override=True ensures .env wins over stale
# system-level env vars that may have been set before the process started.
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
# NOTE: On Streamlit Cloud, app.py injects st.secrets into os.environ BEFORE
# importing this module, so all os.getenv() calls below transparently pick up
# cloud secrets without needing to call st.secrets here at import time.


# ---------------------------------------------------------------------------
# Supported languages
# ---------------------------------------------------------------------------
SUPPORTED_LANGUAGES: list[str] = [
    "English",
    "Spanish",
    "French",
    "German",
    "Portuguese",
    "Italian",
    "Dutch",
    "Russian",
    "Japanese",
    "Korean",
    "Chinese (Simplified)",
    "Chinese (Traditional)",
    "Arabic",
    "Hindi",
    "Turkish",
    "Polish",
    "Swedish",
    "Danish",
    "Norwegian",
    "Finnish",
    "Tamil",
]

# ---------------------------------------------------------------------------
# Accepted media types (video + audio)
# ---------------------------------------------------------------------------
ACCEPTED_VIDEO_EXTENSIONS: list[str] = [
    ".mp4", ".mpeg", ".mov", ".avi", ".flv",
    ".mpg", ".webm", ".wmv", ".3gp",
]

ACCEPTED_AUDIO_EXTENSIONS: list[str] = [
    ".mp3", ".wav", ".flac", ".aac", ".ogg",
    ".wma", ".m4a", ".opus", ".weba",
]

# Combined list for validation
ACCEPTED_MEDIA_EXTENSIONS: list[str] = (
    ACCEPTED_VIDEO_EXTENSIONS + ACCEPTED_AUDIO_EXTENSIONS
)


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------
@dataclass
class Config:
    """Application-wide configuration values."""

    # Gemini credentials & model
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.5-flash"))

    # File limits
    max_video_size_mb: int = field(
        default_factory=lambda: int(os.getenv("MAX_VIDEO_SIZE_MB", "500"))
    )

    # Temp directory (resolved relative to project root)
    temp_dir: Path = field(
        default_factory=lambda: Path(os.getenv("TEMP_DIR", "./tmp_uploads")).resolve()
    )

    # Output defaults
    default_language: str = field(
        default_factory=lambda: os.getenv("DEFAULT_LANGUAGE", "English")
    )
    default_source_language: str = field(
        default_factory=lambda: os.getenv("DEFAULT_SOURCE_LANGUAGE", "Auto-detect")
    )

    # UI strings
    app_title: str = field(
        default_factory=lambda: os.getenv("APP_TITLE", "Multilingual AI Media Summarizer")
    )
    app_icon: str = field(default_factory=lambda: os.getenv("APP_ICON", "🎬"))

    # Debug flag
    debug: bool = field(
        default_factory=lambda: os.getenv("DEBUG", "false").lower() == "true"
    )

    # Gemini polling and retry settings
    poll_interval_seconds: float = 1.0   # poll every 1s — aggressive UI feedback
    max_poll_attempts: int = 300         # 1s × 300 = 5 min ceiling
    tenacity_retry_multiplier: float = 1.5
    
    # Assumptions for progress bars
    upload_speed_mbps: float = 8.0

    def __post_init__(self) -> None:
        """Create temp directory if it doesn't exist; fall back to system temp on failure."""
        import tempfile
        try:
            self.temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            import logging as _log
            _log.getLogger(__name__).warning(
                "Could not create temp_dir '%s' (%s). Falling back to system temp.", self.temp_dir, exc
            )
            self.temp_dir = Path(tempfile.mkdtemp(prefix="mediasummarizer_"))

    @property
    def max_video_size_bytes(self) -> int:
        return self.max_video_size_mb * 1024 * 1024

    def validate(self) -> list[str]:
        """Return a list of validation error strings (empty = valid)."""
        errors: list[str] = []
        if not self.gemini_api_key or self.gemini_api_key == "your_gemini_api_key_here":
            errors.append(
                "GEMINI_API_KEY is not set. "
                "Copy .env.example → .env and add your key."
            )
        return errors


# Singleton instance used across the app
config = Config()
