"""
app.py — Streamlit UI for the Multilingual AI Media Summarizer.

Run with:
    streamlit run app.py

UI layout:
  Sidebar  — API key, model selection, language, custom instructions.
  Main     — Upload zone, media preview, 4-stage visual progress bar,
             live log, tabbed summary output, download buttons.
  Footer   — Attribution.
"""

from __future__ import annotations

# ── Streamlit Cloud secret injection ──────────────────────────────────────────
# Safely parse secrets from TOML files at import time, without invoking
# st.secrets before SessionInfo is initialized.
import os as _os
import logging as _logging
from pathlib import Path as _Path

_secret_files = [
    _Path(".streamlit/secrets.toml"),
    _Path.home() / ".streamlit" / "secrets.toml",
]
for _sp in _secret_files:
    if _sp.exists():
        try:
            import tomllib as _tomllib
            with open(_sp, "rb") as _sf:
                _sdata = _tomllib.load(_sf)
                for _k, _v in _sdata.items():
                    if isinstance(_v, (str, int, float, bool)) and not _os.environ.get(_k):
                        _os.environ[_k] = str(_v)
        except Exception as _se:
            _logging.getLogger(__name__).warning("Error reading secrets TOML file %s: %s", _sp, _se)
# ─────────────────────────────────────────────────────────────────────────────

import html
import io
import typing
import logging
import time
from datetime import datetime
from pathlib import Path

import streamlit as st
from ui.recorder import render_audio_recorder
import ui.pipeline_state as _ps
from ui.loading import show_loading_ui

from config import SUPPORTED_LANGUAGES, config
from file_handler import (
    FileTooLargeError,
    InvalidFileTypeError,
    human_readable_size,
    is_audio_file,
    validate_media_file,
)
from gemini_client import (
    APIKeyError,
    GeminiVideoClient,
    SummaryGenerationError,
    VideoProcessingError,
    SummaryResult,
)
from transcoder import inspect_media, transcode_with_fallback, is_ffmpeg_available
from exceptions import TranscodeError, InspectionError
from queue_worker import get_queue_manager, JobStatus
from ui.queue_status import render_queue_status

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if config.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Removed _persist_env_key, now in ui/sidebar.py


# ---------------------------------------------------------------------------
# Page config  ← must be the FIRST Streamlit call
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title=config.app_title,
    page_icon=config.app_icon,
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Get Help": None,
        "Report a bug": None,
        "About": "Multilingual AI Video Summarizer powered by Advanced Multimodal AI.",
    },
)

# Startup disk hygiene — cleanup orphaned files older than 1h from prior crashed runs
from file_handler import cleanup_stale_temp_files
cleanup_stale_temp_files(max_age_seconds=3600)


# ---------------------------------------------------------------------------
# Startup Health Checks
# ---------------------------------------------------------------------------
def _run_startup_health_checks() -> None:
    """Run environment, system binary, database, and temp folder health checks."""
    import time
    from database import init_db
    from transcoder import is_ffmpeg_available, is_ffprobe_available

    # 0. Read st.secrets safely now that st.set_page_config() has executed
    try:
        if hasattr(st, "secrets") and st.secrets:
            for _k, _v in st.secrets.items():
                if isinstance(_v, (str, int, float, bool)) and not os.environ.get(_k):
                    os.environ[_k] = str(_v)
    except Exception:
        pass

    # 1. Initialize SQLite DB
    init_db()

    # 2. Check .tmp/ directory and clean files older than 1 hour
    tmp_dir = Path(".tmp")
    tmp_dir.mkdir(exist_ok=True)
    now = time.time()
    for f in tmp_dir.glob("*"):
        if f.is_file() and (now - f.stat().st_mtime) > 3600:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    # 3. Check FFmpeg / ffprobe availability warning
    if not is_ffmpeg_available() or not is_ffprobe_available():
        st.warning(
            "⚠️ **FFmpeg / ffprobe binary not found on system PATH.** "
            "Media inspection and transcoding features will be limited. "
            "Please install FFmpeg: `winget install FFmpeg` (Windows) or `brew install ffmpeg` (macOS)."
        )

    # 4. Check GEMINI_API_KEY
    if not config.gemini_api_key:
        st.error(
            "❌ **API Key is not configured.**\n\n"
            "- **Streamlit Cloud**: Go to your app → ⚙️ Settings → **Secrets** and add:\n"
            "  ```\n  GEMINI_API_KEY = \"AIzaSy...\"\n  ```\n"
            "- **Local**: Add `GEMINI_API_KEY=AIzaSy...` to your `.env` file."
        )

_run_startup_health_checks()

# ---------------------------------------------------------------------------
# CSS — premium dark-mode
# ---------------------------------------------------------------------------
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Fira+Code:wght@400;500&display=swap');

/* ── Global Design Tokens ─────────────────────────────────────────── */
:root {
    --bg-primary:      #0A0A14;
    --bg-secondary:    #0F0F1E;
    --glass-bg:        rgba(15, 15, 30, 0.85);
    --glass-border:    rgba(255, 255, 255, 0.08);
    --glass-shadow:    0 8px 32px rgba(0, 0, 0, 0.4);
    --accent-cyan:     #63B3ED;
    --accent-violet:   #9F7AEA;
    --accent-green:    #68D391;
    --accent-amber:    #F6AD55;
    --accent-red:      #FC8181;
    --text-primary:    rgba(255, 255, 255, 0.92);
    --text-secondary:  rgba(255, 255, 255, 0.5);
    --text-muted:      rgba(255, 255, 255, 0.25);
    --radius-card:     24px;
    --radius-pill:     100px;
    --font-primary:    "Inter", "SF Pro Display", system-ui, sans-serif;
    --font-mono:       "Fira Code", "SF Mono", monospace;
    --transition:      all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}

html, body, [class*="css"] {
    font-family: var(--font-primary) !important;
    background: linear-gradient(135deg, #0A0A14 0%, #0D0D20 50%, #0A0A14 100%) !important;
    background-attachment: fixed !important;
    color: var(--text-primary) !important;
}

.main .block-container { padding: 2rem 3rem; max-width: 1200px; position: relative; z-index: 1; }

/* ── Scrollbars & Selection ───────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(99, 179, 237, 0.3); border-radius: 100px; }
::-webkit-scrollbar-thumb:hover { background: rgba(99, 179, 237, 0.6); }
::selection { background: rgba(99, 179, 237, 0.3); color: white; }

/* ── Background Drifting Orbs ─────────────────────────────────────── */
@keyframes orb-float-1 {
    0%, 100% { transform: translate(0px, 0px); }
    50%       { transform: translate(120px, 80px); }
}
@keyframes orb-float-2 {
    0%, 100% { transform: translate(0px, 0px); }
    50%       { transform: translate(-120px, -80px); }
}

/* ── Sidebar ──────────────────────────────────────────────────────── */
section[data-testid="stSidebar"] {
    background: rgba(10, 10, 20, 0.95) !important;
    border-right: 1px solid var(--glass-border) !important;
    backdrop-filter: blur(20px);
}
section[data-testid="stSidebar"] .block-container { padding: 1.5rem 1rem; }

/* ── Hero ─────────────────────────────────────────────────────────── */
.hero-header {
    background: var(--glass-bg);
    backdrop-filter: blur(20px);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-card);
    padding: 2.25rem 2rem;
    margin-bottom: 2rem;
    text-align: center;
    box-shadow: var(--glass-shadow);
}
.hero-title {
    font-size: 2.4rem; font-weight: 700;
    background: linear-gradient(135deg, #63B3ED, #9F7AEA);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    margin-bottom: 0.4rem; line-height: 1.2;
}
.hero-subtitle { color: var(--text-secondary); font-size: 1rem; font-weight: 400; }

/* ── Glass Cards ─────────────────────────────────────────────────── */
.card {
    background: var(--glass-bg);
    backdrop-filter: blur(20px);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-card);
    padding: 1.5rem; margin-bottom: 1.25rem;
    box-shadow: var(--glass-shadow);
    transition: var(--transition);
}
.card:hover { border-color: rgba(99,179,237,0.3); }
.card-header {
    font-size: 0.82rem; font-weight: 700; color: var(--accent-cyan);
    text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 1rem;
    display: flex; align-items: center; gap: 0.5rem;
}

/* ── Drag & Drop File Uploader Zone ───────────────────────────────── */
[data-testid="stFileUploader"] {
    border: 2px dashed rgba(99,179,237,0.25) !important;
    border-radius: var(--radius-card) !important;
    background: rgba(15, 15, 30, 0.6) !important;
    padding: 1rem !important;
    transition: var(--transition) !important;
}
[data-testid="stFileUploader"]:hover {
    border-color: var(--accent-cyan) !important;
    background: rgba(99,179,237,0.05) !important;
    transform: scale(1.005);
}

/* ── Fix Streamlit native progress bar & iframe ────────────────────── */
.stProgress, div[data-testid="stProgressBar"] { display: none !important; }
iframe { border: none !important; }

/* ── Button Styling (Analyse & Extract) ───────────────────────────── */
div[data-testid="stButton"] > button {
    height: 56px !important;
    background: linear-gradient(135deg, #63B3ED 0%, #4299E1 100%) !important;
    color: white !important; border: none !important;
    border-radius: var(--radius-pill) !important; padding: 0 2rem !important;
    font-weight: 600 !important; font-size: 1.05rem !important;
    letter-spacing: 0.02em !important; transition: var(--transition) !important;
    box-shadow: 0 4px 24px rgba(99,179,237,0.35) !important; width: 100% !important;
}
div[data-testid="stButton"] > button:hover {
    transform: translateY(-2px) !important;
    box-shadow: 0 8px 32px rgba(99,179,237,0.5) !important;
}
div[data-testid="stButton"] > button:active { transform: translateY(0) !important; }

button[key="extract_mp3_btn"] {
    background: linear-gradient(135deg, #9F7AEA 0%, #805AD5 100%) !important;
    box-shadow: 0 4px 24px rgba(159,122,234,0.35) !important;
}

/* ── Inputs ───────────────────────────────────────────────────────── */
.stSelectbox > div > div,
.stTextArea > div > div > textarea,
.stTextInput > div > div > input {
    background: rgba(255,255,255,0.04) !important;
    border: 1px solid var(--glass-border) !important;
    border-radius: 12px !important;
    color: var(--text-primary) !important;
    transition: var(--transition) !important;
}
.stSelectbox > div > div:focus-within,
.stTextArea > div > div > textarea:focus,
.stTextInput > div > div > input:focus {
    border-color: var(--accent-cyan) !important;
    box-shadow: 0 0 0 3px rgba(99,179,237,0.15) !important;
}

/* ── Reduced motion compliance ────────────────────────────────────── */
@media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
}

/* ── Export / Download Buttons ──────────────────────────────────── */
div[data-testid="stDownloadButton"] > button {
    border-radius: 50px !important;
    padding: 10px 20px !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    letter-spacing: 0.3px !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
    background: rgba(255,255,255,0.04) !important;
    color: rgba(255,255,255,0.75) !important;
    transition: all 0.2s cubic-bezier(0.4,0,0.2,1) !important;
    width: 100% !important;
}

/* PDF button — coral/red accent */
div[data-testid="stDownloadButton"][data-key="export_pdf_btn"] > button {
    background: linear-gradient(135deg, rgba(252,129,74,0.15), rgba(252,129,74,0.06)) !important;
    color: #FC8174 !important;
    border-color: rgba(252,129,74,0.3) !important;
}
div[data-testid="stDownloadButton"][data-key="export_pdf_btn"] > button:hover {
    background: linear-gradient(135deg, rgba(252,129,74,0.28), rgba(252,129,74,0.14)) !important;
    box-shadow: 0 0 22px rgba(252,129,74,0.22) !important;
    transform: translateY(-2px) !important;
}

/* DOCX button — blue accent */
div[data-testid="stDownloadButton"][data-key="export_docx_btn"] > button {
    background: linear-gradient(135deg, rgba(99,179,237,0.15), rgba(99,179,237,0.06)) !important;
    color: #63B3ED !important;
    border-color: rgba(99,179,237,0.3) !important;
}
div[data-testid="stDownloadButton"][data-key="export_docx_btn"] > button:hover {
    background: linear-gradient(135deg, rgba(99,179,237,0.28), rgba(99,179,237,0.14)) !important;
    box-shadow: 0 0 22px rgba(99,179,237,0.22) !important;
    transform: translateY(-2px) !important;
}

/* Markdown button — green accent */
div[data-testid="stDownloadButton"][data-key="export_md_btn"] > button {
    background: linear-gradient(135deg, rgba(104,211,145,0.15), rgba(104,211,145,0.06)) !important;
    color: #68D391 !important;
    border-color: rgba(104,211,145,0.3) !important;
}
div[data-testid="stDownloadButton"][data-key="export_md_btn"] > button:hover {
    background: linear-gradient(135deg, rgba(104,211,145,0.28), rgba(104,211,145,0.14)) !important;
    box-shadow: 0 0 22px rgba(104,211,145,0.22) !important;
    transform: translateY(-2px) !important;
}
</style>

<!-- Background Ambient Orbs -->
<div class="bg-orb-1"></div>
<div class="bg-orb-2"></div>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Helper: Stage indicator HTML
# ---------------------------------------------------------------------------

_STAGES = [
    ("📤", "Upload"),
    ("⚙️", "Process"),
    ("🤖", "Generate"),
    ("✅", "Done"),
]


def _stage_html(active_index: int) -> str:
    """
    Build the 4-step stage indicator.

    Args:
        active_index: 0=Upload, 1=Process, 2=Generate, 3=Done, -1=idle
    """
    items = []
    for i, (icon, label) in enumerate(_STAGES):
        if i < active_index:
            css = "done"
            icon_render = "✓"
        elif i == active_index:
            css = "current"
            icon_render = icon
        else:
            css = ""
            icon_render = icon

        items.append(
            f'<div class="stage-item {css}">'
            f'<span class="stage-dot"></span>'
            f'<span>{icon_render} {label}</span>'
            f"</div>"
        )
        if i < len(_STAGES) - 1:
            items.append('<span class="stage-arrow">›</span>')

    return '<div class="stage-bar">' + "".join(items) + "</div>"


def _progress_fraction_to_stage(fraction: float) -> int:
    """Map a 0–1 fraction to a stage index (0–3)."""
    if fraction < 0.25:
        return 0
    if fraction < 0.55:
        return 1
    if fraction < 0.97:
        return 2
    return 3


# ---------------------------------------------------------------------------
# Session-state initialisation
# ---------------------------------------------------------------------------
_STATE_DEFAULTS: dict = {
    "processing":             False,
    "result":                 None,
    "log_entries":            [],
    "error_msg":              None,
    "progress_frac":          0.0,
    "progress_lbl":           "",
    "chat_history":           [],
    "current_remote_file":    None,
    "queue_job_id":           None,   # Active queue job ID (None = no pending job)
    # ── Recorder session state (populated by ui/recorder.py) ──────────────
    "recorder_clips":         [],     # list[str] MM:SS timestamps of clipping events
    "recorder_quality_score": 0,      # int 0-100 composite quality score
    "recorder_segments":      [],     # list[dict] timeline segments
    # ── Upload info (for Stage 1 card) ─────────────────────────────────
    "upload_file_name":       "",
    "upload_file_size":       0,
    "upload_file_fmt":        "",
    "conversion_mode":        None,
    "mp3_path":               None,
}
for _k, _v in _STATE_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
from ui.sidebar import MODEL_DISPLAY_NAMES, render_sidebar
_, target_language, source_language, extra_instructions = render_sidebar()
# selected_model is stored in session_state by sidebar to avoid mutating the
# global config singleton (race condition on multi-user Streamlit Cloud).
selected_model: str = st.session_state.get("selected_model", config.gemini_model)


# ---------------------------------------------------------------------------
# Hero
# ---------------------------------------------------------------------------
st.markdown(
    """
<div class="hero-header">
    <div class="hero-title">🎬 Multimodal AI Media Summarizer</div>
    <div class="hero-subtitle">
        Upload a lecture, meeting, or recording — get a complete AI-powered summary in seconds.
    </div>
    <div style="display: flex; justify-content: center; gap: 12px; margin-top: 16px; flex-wrap: wrap;">
        <span style="padding: 4px 14px; border-radius: 100px; background: rgba(99, 179, 237, 0.1); border: 1px solid rgba(99, 179, 237, 0.25); color: #63B3ED; font-size: 11px; font-weight: 600;">🎓 Lecture Mode</span>
        <span style="padding: 4px 14px; border-radius: 100px; background: rgba(104, 211, 145, 0.1); border: 1px solid rgba(104, 211, 145, 0.25); color: #68D391; font-size: 11px; font-weight: 600;">🔒 Zero Footprint</span>
        <span style="padding: 4px 14px; border-radius: 100px; background: rgba(159, 122, 234, 0.1); border: 1px solid rgba(159, 122, 234, 0.25); color: #9F7AEA; font-size: 11px; font-weight: 600;">⚡ Advanced AI Engine</span>
    </div>
</div>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Upload + options columns
# ---------------------------------------------------------------------------
col_upload, col_opts = st.columns([3, 2], gap="large")

with col_upload:
    st.markdown('<div class="card-header">📁 Media Input</div>', unsafe_allow_html=True)
    
    tab_upload, tab_record = st.tabs(["📁 File Upload", "🎤 Record Audio"])
    
    with tab_upload:
        raw_uploaded_file = st.file_uploader(
            "Drop your video or audio file here",
            type=[
                "mp4", "mov", "avi", "mpeg", "mpg", "webm", "wmv", "3gp", "flv",
                "mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus",
            ],
            label_visibility="collapsed",
            key="video_uploader",
        )
        
    with tab_record:
        audio_bytes = render_audio_recorder(
            classroom_mode=st.session_state.get("classroom_mode_toggle", False),
            max_duration_seconds=3600,
            noise_gate_default=0.008,
        )
        
    # Determine which file to process
    uploaded_file: typing.Any = None
    if raw_uploaded_file is not None:
        uploaded_file = raw_uploaded_file
        st.session_state["recorded_audio_bytes"] = None
    elif audio_bytes is not None:
        st.session_state["recorded_audio_bytes"] = audio_bytes
        ext = ".webm"
        if audio_bytes.startswith(b"\x1a\x45\xdf\xa3"):
            ext = ".webm"
        elif b"ftyp" in audio_bytes[:32]:
            ext = ".m4a"
        uploaded_file = io.BytesIO(audio_bytes)
        uploaded_file.name = f"recorded_audio{ext}"
    elif st.session_state.get("recorded_audio_bytes") is not None:
        cached_bytes: bytes = st.session_state["recorded_audio_bytes"]
        ext = ".webm"
        if cached_bytes.startswith(b"\x1a\x45\xdf\xa3"):
            ext = ".webm"
        elif b"ftyp" in cached_bytes[:32]:
            ext = ".m4a"
        uploaded_file = io.BytesIO(cached_bytes)
        uploaded_file.name = f"recorded_audio{ext}"

    if uploaded_file is not None:
        # BUG FIX: read size without consuming the stream, then seek back.
        # Streamlit's UploadedFile is seekable, so we use tell/seek.
        uploaded_file.seek(0, 2)          # seek to end
        file_size = uploaded_file.tell()  # byte count
        uploaded_file.seek(0)             # reset for subsequent reads

        st.markdown(
            f'<div style="margin-top:0.75rem;color:#94a3b8;font-size:0.85rem">'
            f"📄 <strong>{uploaded_file.name}</strong> · "
            f"{human_readable_size(file_size)}</div>",
            unsafe_allow_html=True,
        )
        # Audio vs video preview safely using raw bytes (getvalue()) to avoid
        # Streamlit Cloud's UploadedFile MediaFileManager SessionInfo crash.
        try:
            if hasattr(uploaded_file, "getvalue"):
                _preview_data = uploaded_file.getvalue()
            else:
                uploaded_file.seek(0)
                _preview_data = uploaded_file.read()
                uploaded_file.seek(0)

            if is_audio_file(uploaded_file.name):
                st.audio(_preview_data)
            else:
                st.video(_preview_data)
        except Exception as _prev_exc:
            logger.warning("Media preview skipped: %s", _prev_exc)
        finally:
            uploaded_file.seek(0)

        # Cache inspection result in session_state keyed by (filename, filesize)
        # so we don't re-run ffprobe on every Streamlit rerender
        _inspect_cache_key = f"{uploaded_file.name}:{file_size}"
        inspection: dict | None = None

        if st.session_state.get("_inspect_cache_key") == _inspect_cache_key:
            inspection = st.session_state.get("_inspection_cache")
        elif is_audio_file(uploaded_file.name):
            # Audio files do not require HEVC/VFR video inspection — skip main thread ffprobe
            ext = Path(uploaded_file.name).suffix.lower()
            needs_conv = (
                ext in {".mpeg", ".mpg", ".wma", ".aiff"}
                or any(f"{ae}." in uploaded_file.name.lower() for ae in ACCEPTED_AUDIO_EXTENSIONS)
            )
            inspection = {
                "needs_transcode": needs_conv,
                "needs_audio_only": needs_conv,
                "is_vfr": False,
                "is_iphone": False,
                "is_android": False,
                "video_codec": "",
                "audio_codec": "native",
                "container": ext.lstrip("."),
                "reasons": ["Non-standard or compound audio format — converting to AAC/MP3"] if needs_conv else [],
                "duration_seconds": 0.0,
                "file_size_bytes": file_size,
                "is_video_file": False,
            }
            st.session_state["_inspect_cache_key"] = _inspect_cache_key
            st.session_state["_inspection_cache"] = inspection
        else:
            # Run ffprobe in a background thread so it never blocks Streamlit's
            # Tornado WebSocket heartbeat. asyncio.run() is safe inside a
            # ThreadPoolExecutor worker (it has no running event loop).
            import concurrent.futures as _cf
            import uuid as _uuid
            _tmp = Path(".tmp")
            _tmp.mkdir(exist_ok=True)
            _tmp_in = _tmp / f"ui_inspect_{_uuid.uuid4().hex}{Path(uploaded_file.name).suffix}"
            try:
                uploaded_file.seek(0)
                _tmp_in.write_bytes(uploaded_file.read())
                uploaded_file.seek(0)

                def _run_inspect(_path: str) -> dict:
                    import asyncio as _ai
                    return _ai.run(inspect_media(_path))

                with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                    _fut = _ex.submit(_run_inspect, str(_tmp_in))
                    try:
                        inspection = _fut.result(timeout=30)
                    except _cf.TimeoutError:
                        inspection = None  # graceful fallback if ffprobe hangs
                # Cache to avoid repeat ffprobe calls on re-renders
                st.session_state["_inspect_cache_key"] = _inspect_cache_key
                st.session_state["_inspection_cache"] = inspection
            except Exception:
                inspection = None  # fallback if inspection fails
            finally:
                # Always delete the temp file — prevents unbounded disk usage
                try:
                    _tmp_in.unlink(missing_ok=True)
                except OSError:
                    pass

        if inspection:
            if inspection.get("is_iphone"):
                st.info("📱 iPhone video detected (HEVC format). Auto-converting to H.264 for AI processing — takes ~10 seconds.")
            elif inspection.get("is_android"):
                st.info("🤖 Android video detected. Auto-converting to a compatible format — takes ~10 seconds.")
            elif inspection.get("is_vfr"):
                st.info("🎥 Variable frame rate detected. Normalizing to 30fps for AI compatibility.")
            elif inspection.get("needs_transcode"):
                st.info("⚠️ Incompatible format detected. Auto-converting to a compatible format — takes ~10 seconds.")
            else:
                st.success("✅ File format is compatible — processing immediately.")


with col_opts:
    st.markdown('<div class="card-header">⚙️ Processing Options</div>', unsafe_allow_html=True)

    instructions_preview = (
        "<em>(none)</em>"
        if not extra_instructions.strip()
        else extra_instructions[:80] + ("…" if len(extra_instructions) > 80 else "")
    )
    st.markdown(
        f"""
<div class="card">
    <div style="margin-bottom:0.75rem">
        <span style="color:#94a3b8;font-size:0.82rem">🤖 Model</span><br>
        <span style="font-weight:600;color:#c7d2fe">{MODEL_DISPLAY_NAMES.get(selected_model, selected_model)}</span>
    </div>
    <div style="margin-bottom:0.75rem">
        <span style="color:#94a3b8;font-size:0.82rem">🌐 Output Language</span><br>
        <span style="font-weight:600;color:#c7d2fe">{target_language}</span>
    </div>
    <div>
        <span style="color:#94a3b8;font-size:0.82rem">📝 Custom Instructions</span><br>
        <span style="font-weight:600;color:#c7d2fe">{instructions_preview}</span>
    </div>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.expander("ℹ️ How it works", expanded=False):
        st.markdown(
            """
**Step 1 — Save & Upload**  
Your file is saved locally then streamed to the secure Cloud AI Files API.

**Step 2 — Server Processing**  
The AI engine indexes every frame and audio track.

**Step 3 — AI Generation**  
The model synthesises a structured multilingual summary.

**Step 4 — Interactive Chat**  
The cloud file is kept alive temporarily so you can chat with it. It is deleted when you upload a new file.
"""
        )

    with st.expander("📋 Supported formats — click to expand", expanded=False):
        st.markdown(
            """
✅ **Fully Supported** (no conversion needed)  
`MP4 (H.264)` &nbsp; `MP3` &nbsp; `WAV` &nbsp; `M4A` &nbsp; `AAC` &nbsp; `OGG` &nbsp; `WEBM (VP8)` &nbsp; `FLAC`

🔄 **Auto-Converted** (processed automatically)  
`MP4 (HEVC/H.265)` &nbsp; `MOV` &nbsp; `MKV` &nbsp; `AVI` &nbsp; `FLV`  
`WEBM (VP9/AV1)` &nbsp; `iPhone recordings` &nbsp; `Android recordings` &nbsp; `Variable frame rate`

❌ **Not Supported** (manual conversion required)  
Encrypted/DRM files &nbsp;·&nbsp; Corrupted files &nbsp;·&nbsp; Files over {max_mb} MB
""".format(max_mb=getattr(config, 'max_video_size_mb', 2048))
        )


# ---------------------------------------------------------------------------
# Analyse / Extract buttons
# ---------------------------------------------------------------------------
st.markdown("")
show_mp3_button = False
if uploaded_file is not None:
    from pathlib import Path as _P
    _ext = _P(uploaded_file.name).suffix.lower()
    if _ext in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".3gp", ".flv"}:
        show_mp3_button = True
    elif 'inspection' in locals() and isinstance(inspection, dict):
        if inspection.get("video_codec") or inspection.get("is_video_file"):
            show_mp3_button = True

if show_mp3_button:
    col1, col2 = st.columns(2)
    with col1:
        analyse_clicked = st.button(
            "🚀 Analyse Media",
            disabled=uploaded_file is None or st.session_state.processing,
            key="analyse_btn",
            use_container_width=True,
        )
    with col2:
        mp3_clicked = st.button(
            "🎵 Extract Audio & Summarize",
            disabled=uploaded_file is None or st.session_state.processing,
            key="extract_mp3_btn",
            use_container_width=True,
        )
else:
    btn_col, _ = st.columns([2, 3])
    with btn_col:
        analyse_clicked = st.button(
            "🚀 Analyse Media",
            disabled=uploaded_file is None or st.session_state.processing,
            key="analyse_btn",
            use_container_width=True,
        )
    mp3_clicked = False


# ---------------------------------------------------------------------------
# Processing pipeline
# ---------------------------------------------------------------------------
if (analyse_clicked or mp3_clicked) and uploaded_file is not None:
    if mp3_clicked:
        st.session_state.conversion_mode = "mp3"
    else:
        st.session_state.conversion_mode = None
    # Cleanup previous session's remote file if it exists
    if st.session_state.current_remote_file:
        try:
            old_client = GeminiVideoClient()
            old_client.delete_remote_file(st.session_state.current_remote_file)
        except Exception as e:
            logger.warning("Failed to clean up old remote file: %s", e)

    # Reset all state
    st.session_state.processing    = True
    st.session_state.result        = None
    st.session_state.error_msg     = None
    st.session_state.log_entries   = []
    st.session_state.chat_history  = []
    st.session_state.current_remote_file = None
    st.session_state.progress_frac = 0.0
    st.session_state.progress_lbl  = "Starting…"

    # ── Pre-flight validation ───────────────────────────────────────────
    uploaded_file.seek(0, 2)
    _file_size = uploaded_file.tell()
    uploaded_file.seek(0)

    try:
        validate_media_file(uploaded_file.name, _file_size)
    except (InvalidFileTypeError, FileTooLargeError) as _exc:
        st.session_state.error_msg   = _exc.ui_message
        st.session_state.processing  = False
        st.stop()

    # Store upload info for Stage 1 card
    from pathlib import Path as _Path
    _fmt = Path(uploaded_file.name).suffix.lstrip(".").upper() or "MEDIA"
    st.session_state.upload_file_name = uploaded_file.name
    st.session_state.upload_file_size = _file_size
    st.session_state.upload_file_fmt  = _fmt

    # Reset pipeline state for this run
    _ps.reset()
    _ps.update(
        stage=1,
        stage_label="File received",
        sub_message=uploaded_file.name,
        bytes_total=_file_size,
        bytes_sent=_file_size,   # file is already in-memory at this point
        pct=100.0,
    )

    # ── Build progress UI ───────────────────────────────────────────────
    st.markdown("---")

    from ui.loading import show_loading_ui
    show_loading_ui(
        stage=1,
        progress=100.0,
        stage_message=f"Uploaded '{st.session_state.upload_file_name}'",
        file_name=st.session_state.upload_file_name,
        file_size=human_readable_size(st.session_state.upload_file_size),
        file_format=st.session_state.upload_file_fmt,
        is_compatible=True,
        conversion_mode=st.session_state.get("conversion_mode") or "",
    )

    stage_placeholder = st.empty()
    progress_text = st.empty()

    st.markdown("")

    # Log panel
    st.markdown('<div class="card-header">📡 Live Log</div>', unsafe_allow_html=True)
    log_placeholder = st.empty()

    # ── Callback helpers ────────────────────────────────────────────────

    def _render_log() -> None:
        lines = []
        for entry in st.session_state.log_entries:
            # Bug #5 fix: HTML-escape content before injecting into the DOM
            # to prevent XSS via filenames or API response content.
            safe_entry = html.escape(entry)
            if entry.startswith(("✅", "🗑", "🎉")):
                css = "log-s"
            elif entry.startswith("⚠️"):
                css = "log-w"
            elif entry.startswith("❌"):
                css = "log-e"
            else:
                css = "log-i"
            lines.append(f'<span class="{css}">{safe_entry}</span>')

        log_placeholder.markdown(
            '<div class="log-panel">' + "<br>".join(lines) + "</div>",
            unsafe_allow_html=True,
        )

    def on_log(message: str) -> None:
        """Append a text message to the live log panel."""
        logger.info(message)
        st.session_state.log_entries.append(message)
        _render_log()

    def on_progress(fraction: float, label: str) -> None:
        """
        Update the visual progress bar, stage indicator, and label.

        Throttled: DOM writes are skipped when the fraction changes by less
        than 0.5% — this reduces Streamlit render calls from ~480 to ~60
        for a typical 60-second generation without any visible UX degradation.

        Args:
            fraction: Float in [0.0, 1.0] representing overall progress.
            label:    Human-readable description of the current operation.
        """
        prev_frac = st.session_state.progress_frac
        st.session_state.progress_frac = fraction
        st.session_state.progress_lbl  = label

        # Always render at 0%, 100%, and every time label changes
        label_changed = (label != getattr(st.session_state, "_prev_progress_lbl", ""))
        delta_significant = (fraction - prev_frac) >= 0.005

        if not (delta_significant or label_changed or fraction >= 1.0):
            return

        st.session_state["_prev_progress_lbl"] = label
        pct = int(fraction * 100)

        # Update progress text
        progress_text.markdown(f"**{pct}%** — {label}")

        # Stage indicator (legacy bar — hidden by CSS, kept for log alignment)
        stage_idx = _progress_fraction_to_stage(fraction)
        stage_placeholder.markdown(_stage_html(stage_idx), unsafe_allow_html=True)

    # ── Main processing pipeline ────────────────────────────────────────
    start_time = time.monotonic()
    try:
        from database import validate_and_use_key
        custom_key = st.session_state.get("custom_api_key", "")
        is_direct_gemini_key = bool(custom_key and (custom_key.startswith("AIzaSy") or len(custom_key) > 20))
        if not is_direct_gemini_key and not validate_and_use_key(custom_key):
            st.error("Invalid or expired Custom Application Key. Please check your key or request a new one.")
            st.stop()

        on_log(f"🎬 Queuing analysis of '{uploaded_file.name}'…")
        on_progress(0.01, "Queuing…")

        # Read the file bytes NOW (before submitting to the thread, as
        # Streamlit's UploadedFile is not thread-safe).
        uploaded_file.seek(0)
        file_bytes = uploaded_file.read()
        file_name  = uploaded_file.name

        # Combine custom instructions with classroom mode directive if enabled
        combined_instructions = extra_instructions
        if st.session_state.get("classroom_mode_toggle", True):
            classroom_directive = (
                "CLASSROOM NOISE ISOLATION MODE ACTIVE: Filter out all ambient classroom noise, "
                "coughing, student chatter, paper rustling, door slams, and room echo. Focus 100% "
                "exclusively on the professor's / instructor's voice and lecture contents."
            )
            combined_instructions = f"{classroom_directive}\n\n{extra_instructions}".strip()

        # Capture session state values that are needed inside the worker thread
        # (st.session_state is not thread-safe; capture before thread starts)
        _conversion_mode: str | None = st.session_state.get("conversion_mode")

        def _run_analysis() -> SummaryResult:
            """Closure executed by the background worker thread.

            Pipeline order (enforced):
              1. inspect_media()           — ffprobe codec/container scan
              2. transcode_with_fallback() — three-tier FFmpeg pipeline (if needed)
              3. ASSERTION GUARD           — prevents sending incompatible file
              4. summarise_stream()        — Gemini upload + generation
              5. cleanup                   — delete local transcoded temp file

            asyncio.run() is safe here because this function runs in a plain
            daemon thread (QueueManager worker), not inside an existing event loop.
            """
            import asyncio
            import os
            import uuid as _uuid

            gemini = GeminiVideoClient()
            media_bytes = io.BytesIO(file_bytes)
            effective_file_name = file_name
            transcoded_path: str | None = None
            mp3_path: str | None = None  # set if conversion_mode=="mp3"

            try:
                # ── Step 1: Proactive codec inspection ─────────────────────
                # Write bytes to a temp file so ffprobe can inspect it
                from pathlib import Path as _Path
                _tmp = _Path(".tmp")
                _tmp.mkdir(exist_ok=True)
                _tmp_in = _tmp / f"inspect_{_uuid.uuid4().hex}_{file_name}"
                _tmp_in.write_bytes(file_bytes)

                if _ps is not None:
                    _ps.update(
                        stage=1,
                        pct=100.0,
                        bytes_total=len(file_bytes),
                        sub_message=file_name
                    )

                try:
                    inspection = asyncio.run(inspect_media(str(_tmp_in)))
                except InspectionError as exc:
                    logger.warning("ffprobe inspection failed: %s", exc)
                    # Treat as needing transcode (conservative fallback)
                    inspection = {
                        "needs_transcode": True,
                        "needs_audio_transcode": False,
                        "is_video_file": True,
                        "detected_video_codec": "unknown",
                        "detected_audio_codec": "",
                        "is_variable_framerate": False,
                        "detected_container": "",
                        "detected_pix_fmt": "",
                        "detected_profile": "",
                        "is_iphone": False,
                        "is_android": False,
                        "incompatibility_reasons": ["ffprobe inspection failed — transcoding as precaution"],
                        "duration_seconds": 0.0,
                        "file_size_bytes": len(file_bytes),
                    }

                # Update pipeline_state with detection results
                is_iphone  = inspection.get("is_iphone", False)
                is_android = inspection.get("is_android", False)
                skipped = [] if inspection.get("needs_transcode") else [2]
                if inspection.get("needs_transcode"):
                    _ps.update(
                        stage=2,
                        stage_label="Preparing…",
                        skipped_transcode=False,
                        sub_message="iPhone recording" if is_iphone else (
                            "Android recording" if is_android else "Converting format"
                        ),
                    )
                else:
                    _ps.update(skipped_transcode=True)

                # ── Step 1.5: Extract MP3 if requested ─────────────────────
                if _conversion_mode == "mp3":
                    from transcoder import mp4_to_mp3
                    if _ps is not None:
                        _ps.update(stage=1.5, stage_label="Extracting Audio…")
                    try:
                        mp3_path = asyncio.run(mp4_to_mp3(str(_tmp_in)))
                        assert mp3_path != str(_tmp_in), "SAFETY BLOCK: mp3_path must differ from original_path"
                        effective_file_name = _Path(mp3_path).name
                        media_bytes = io.BytesIO(open(mp3_path, "rb").read())
                        inspection["needs_transcode"] = False  # Converted MP3 is compatible
                    except TranscodeError as exc:
                        logger.error("MP4->MP3 extraction failed: %s", exc)
                        raise TranscodeError(
                            input_path=str(_tmp_in),
                            reasons=["Audio extraction failed. Try '🚀 Analyse Media' to process the full video instead."]
                        ) from exc

                # ── Step 2: Transcode if needed ───────────────────────────
                processing_mode = "full_video"
                if inspection["needs_transcode"] and is_ffmpeg_available():
                    transcoded_path, processing_mode = asyncio.run(
                        transcode_with_fallback(
                            str(_tmp_in),
                            inspection,
                            file_size_bytes=len(file_bytes),
                        )
                    )
                    effective_file_name = _Path(transcoded_path).name
                    media_bytes = io.BytesIO(open(transcoded_path, "rb").read())
                elif inspection["needs_transcode"] and not is_ffmpeg_available():
                    # ffmpeg not installed — warn and attempt original (may fail)
                    logger.warning(
                        "File needs transcoding but ffmpeg is not available. "
                        "Attempting Cloud AI upload with original file (may fail)."
                    )

                # ── Step 3: Assertion guard (regression prevention) ──────────
                assert (
                    not inspection["needs_transcode"]
                    or transcoded_path is not None
                    or not is_ffmpeg_available()
                ), (
                    "SAFETY BLOCK: Attempted to send incompatible file to Cloud AI. "
                    f"File: {file_name}, reasons: {inspection.get('reasons')}"
                )

                # Clean up the inspection temp input file
                try:
                    _tmp_in.unlink(missing_ok=True)
                except OSError:
                    pass

                # ── Step 4: Gemini upload + generation ─────────────────────
                stream_result = gemini.summarise_stream(
                    file_obj=media_bytes,
                    file_name=effective_file_name,
                    target_language=target_language,
                    source_language=source_language,
                    extra_instructions=combined_instructions,
                    log_callback=None,    # callbacks not thread-safe; omit in queued mode
                    progress_callback=None,
                )
                
                # Drain the stream into a string inside the worker thread.
                # Wrap iteration in try/except so if streaming fails mid-way,
                # the remote file on Gemini Files API is deleted immediately.
                try:
                    full_text = "".join(stream_result.stream)
                except Exception as stream_exc:
                    logger.error("Error during streaming response generation: %s", stream_exc)
                    if stream_result and stream_result.remote_file_name:
                        try:
                            gemini.delete_remote_file(stream_result.remote_file_name)
                        except Exception:
                            pass
                    raise stream_exc

                return SummaryResult(
                    summary_markdown=full_text,
                    remote_file_name=stream_result.remote_file_name,
                    video_filename=stream_result.video_filename,
                    target_language=stream_result.target_language,
                )

            finally:
                # ── Step 5: Clean up temp files ───────────────────
                if _ps is not None:
                    _ps.update(stage=5, stage_label="Cleaning up…")
                    
                files_to_cleanup = [
                    str(_tmp_in),
                    transcoded_path,
                    mp3_path,
                ]
                for path in files_to_cleanup:
                    if path and os.path.exists(path):
                        try:
                            os.remove(path)
                            logger.info("Cleaned up temp file: %s", path)
                        except OSError as exc:
                            logger.warning("Could not delete temp file: %s", exc)
                if _ps is not None:
                    _ps.update(
                        local_deleted=True,
                        cloud_deleted=bool(transcoded_path or mp3_path),
                    )


        job_id = get_queue_manager().submit(_run_analysis)
        st.session_state.queue_job_id = job_id
        on_log(f"✅ Queued successfully! Job ID: {job_id[:8]}…")
        st.rerun()

    except (APIKeyError, VideoProcessingError, SummaryGenerationError,
            TranscodeError, InspectionError) as exc:
        _msg = getattr(exc, 'ui_message', str(exc))
        st.session_state.error_msg = _msg
        on_log(f"❌ {_msg}")
        on_progress(0.0, "Failed")
    except Exception as exc:
        _msg = f"Unexpected error: {type(exc).__name__}: {exc}"
        st.session_state.error_msg = _msg
        on_log(f"❌ {_msg}")
        on_progress(0.0, "Failed")
        logger.exception("Unexpected error during media queuing")
    finally:
        st.session_state.processing = False
        # Mark session clear in pipeline_state (local file is in-memory, already gone)
        _ps.update(local_deleted=True, session_cleared=True)


# ---------------------------------------------------------------------------
# Queue result polling — runs on every Streamlit rerun
# ---------------------------------------------------------------------------
_active_job_id: str | None = st.session_state.get("queue_job_id")
if _active_job_id is not None:
    _still_waiting = render_queue_status()
    if not _still_waiting:
        # Job finished — collect result or error
        _job = get_queue_manager().get_job(_active_job_id)
        st.session_state.queue_job_id = None    # clear so we don't re-poll
        if _job is not None and _job.status.value == "done" and _job.result is not None:
            st.session_state.result = _job.result
            st.session_state.current_remote_file = _job.result.remote_file_name
        elif _job is not None and _job.error:
            st.session_state.error_msg = _job.error
            # Clear stale result so the error panel isn't obscured by old output
            st.session_state.result = None
        st.rerun()


# ---------------------------------------------------------------------------
# Error & Result & Chat display
# ---------------------------------------------------------------------------
from ui.error_handling import render_error_state
from ui.chat import render_result_and_chat

render_error_state()
render_result_and_chat(GeminiVideoClient())


# ---------------------------------------------------------------------------
# Privacy Trust Bar & Keyboard Shortcuts (Axis 5)
# ---------------------------------------------------------------------------
st.markdown(
    """
<div style="
    text-align: center;
    padding: 1.5rem 0 1rem 0;
    margin-top: 3rem;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    font-size: 11px;
    color: rgba(255, 255, 255, 0.35);
    letter-spacing: 0.02em;
">
    🔒 Zero footprint · Files deleted after processing · No data stored permanently · TLS 1.3 encrypted
</div>

<script>
document.addEventListener('keydown', function(e) {
    if (e.ctrlKey && e.key.toLowerCase() === 'u') {
        e.preventDefault();
        const uploader = document.querySelector('input[type="file"]');
        if (uploader) uploader.click();
    } else if (e.ctrlKey && e.key === 'Enter') {
        e.preventDefault();
        const btn = document.querySelector('.stButton button');
        if (btn && !btn.disabled) btn.click();
    }
});
</script>
""",
    unsafe_allow_html=True,
)
