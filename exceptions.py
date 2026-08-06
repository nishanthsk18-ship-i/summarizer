"""
exceptions.py — Custom exception hierarchy for the AI Media Summarizer.

All user-facing exceptions expose a ``ui_message`` property that returns
a clean, actionable string suitable for st.error() display.
"""

from __future__ import annotations


class TranscodeError(Exception):
    """
    Raised when FFmpeg fails all three transcoding tiers.
    """

    def __init__(self, input_path: str, reasons: list[str]) -> None:
        super().__init__(f"Transcoding failed for {input_path}")
        self.input_path = input_path
        self.reasons = reasons

    @property
    def ui_message(self) -> str:
        return (
            "This mobile recording could not be processed automatically. "
            "Please open your phone's camera settings and set video format "
            "to 'Most Compatible' (iPhone) or 'Standard' (Android), "
            "then re-record and upload again."
        )

    @property
    def handbrake_url(self) -> str:
        return "https://handbrake.fr"


class InspectionError(Exception):
    """
    Raised when ffprobe fails to read the file's stream information.
    """

    def __init__(self, input_path: str, stderr: str) -> None:
        super().__init__(f"Inspection failed for {input_path}: {stderr}")
        self.input_path = input_path
        self.stderr = stderr

    @property
    def ui_message(self) -> str:
        return (
            "Could not inspect this media file — it may be corrupted, "
            "DRM-protected, or in an unreadable format. "
            "Try re-exporting or re-downloading the file."
        )


class UnsupportedFormatError(Exception):
    """
    Raised when the file format is fundamentally incompatible.
    """

    def __init__(self, format_name: str, codec_name: str) -> None:
        super().__init__(f"Unsupported format: {format_name} / {codec_name}")
        self.format_name = format_name
        self.codec_name = codec_name

    @property
    def ui_message(self) -> str:
        fmt = f" ({self.format_name}/{self.codec_name})" if self.format_name else ""
        return (
            f"Unsupported file format{fmt}. "
            "Supported formats: MP4 (H.264), MOV, MKV, AVI, WEBM, "
            "MP3, WAV, M4A, AAC, OGG, FLAC. "
            "Files must not be DRM-encrypted or password-protected."
        )
