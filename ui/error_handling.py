"""
ui/error_handling.py — Renders user-facing error states from session_state.

Handles all known exception types with actionable guidance, including
the new TranscodeError and UnsupportedFormatError from exceptions.py.
"""

from __future__ import annotations

import streamlit as st


def render_error_state() -> None:
    """Render the error panel from st.session_state.error_msg."""
    error_msg: str | None = st.session_state.get("error_msg")
    if not error_msg:
        return

    st.markdown("---")
    st.error(f"**Error:** {error_msg}", icon="🚨")

    # ── Determine which help tip to show based on error content ─────────
    is_transcode_failure = (
        "Most Compatible" in error_msg
        or "Standard" in error_msg
        or "camera settings" in error_msg.lower()
    )
    is_inspection_error = (
        "corrupted" in error_msg.lower()
        or "inspect" in error_msg.lower()
        or "ffprobe" in error_msg.lower()
        or "DRM" in error_msg
    )
    is_unsupported_format = (
        "Unsupported format" in error_msg
        or "Unsupported file format" in error_msg
    )

    if is_transcode_failure:
        st.warning(
            "**🎬 Mobile Video Conversion Failed**\n\n"
            "This recording could not be processed automatically. "
            "Please apply the fix for your device, re-record, and upload again:\n\n"
            "📱 **iPhone fix:** Settings → Camera → Formats → Most Compatible\n"
            "🤖 **Android fix:** Camera App → Settings → Video Quality → Standard",
            icon="🔧",
        )

    elif is_inspection_error:
        st.info(
            "**Tips to fix:**\n"
            "- The file may be **corrupted** — try re-exporting from your source application\n"
            "- If the file is **DRM-protected** (e.g. Netflix download), it cannot be processed\n"
            "- Try downloading or recording the file again\n"
            "- Ensure ffmpeg and ffprobe are installed on the server",
            icon="💡",
        )

    elif is_unsupported_format:
        st.info(
            "**Tips to fix common errors:**\n"
            "- **Unsupported Format** — Your file uses a format we cannot process. "
            "Convert manually to H.264 MP4 using "
            "[HandBrake](https://handbrake.fr) or MP3 using [Audacity](https://www.audacityteam.org)\n"
            "- **iPhone recordings** — Use iOS Files app to share as 'Most Compatible' format\n"
            "- **Android recordings** — Trim or export via Google Photos before uploading",
            icon="💡",
        )

    else:
        st.info(
            "**Tips to fix common errors:**\n"
            "- **API Key Error** — Make sure your API key is configured correctly in `.env` or the sidebar.\n"
            "- **Generation Error** — The media may be silent or contain no analysable content\n"
            "- **503 Unavailable** — High server demand detected. The system automatically retries with backup models.",
            icon="💡",
        )
