"""
app.py â€” Streamlit UI for the Multilingual AI Media Summarizer.

Run with:
    streamlit run app.py

UI layout:
  Sidebar  â€” API key, model selection, language, custom instructions.
  Main     â€” Upload zone, media preview, 4-stage visual progress bar,
             live log, tabbed summary output, download buttons.
  Footer   â€” Attribution.
"""

from __future__ import annotations

# â”€â”€ Streamlit Cloud secret injection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

from config import ACCEPTED_AUDIO_EXTENSIONS, SUPPORTED_LANGUAGES, config
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
    format="%(asctime)s [%(levelname)s] %(name)s â€” %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Removed _persist_env_key, now in ui/sidebar.py


# ---------------------------------------------------------------------------
# Page config  â† must be the FIRST Streamlit call
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

# Startup disk hygiene â€” cleanup orphaned files older than 1h from prior crashed runs
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
            "âš ï¸ **FFmpeg / ffprobe binary not found on system PATH.** "
            "Media inspection and transcoding features will be limited. "
            "Please install FFmpeg: `winget install FFmpeg` (Windows) or `brew install ffmpeg` (macOS)."
        )

    # 4. Check GEMINI_API_KEY
    if not config.gemini_api_key:
        st.error(
            "âŒ **API Key is not configured.**\n\n"
            "- **Streamlit Cloud**: Go to your app â†’ âš™ï¸ Settings â†’ **Secrets** and add:\n"
            "  ```\n  GEMINI_API_KEY = \"AIzaSy...\"\n  ```\n"
            "- **Local**: Add `GEMINI_API_KEY=AIzaSy...` to your `.env` file."
        )

_run_startup_health_checks()

# ---------------------------------------------------------------------------
# CSS â€” premium dark-mode
# ---------------------------------------------------------------------------
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Fira+Code:wght@400;500&display=swap');

/* â”€â”€ Global Design Tokens â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
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

/* â”€â”€ Scrollbars & Selection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(99, 179, 237, 0.3); border-radius: 100px; }
::-webkit-scrollbar-thumb:hover { background: rgba(99, 179, 237, 0.6); }
::selection { background: rgba(99, 179, 237, 0.3); color: white; }

/* â”€â”€ Background Drifting Orbs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
@keyframes orb-float-1 {
    0%, 100% { transform: translate(0px, 0px); }
    50%       { transform: translate(120px, 80px); }
}
@keyframes orb-float-2 {
    0%, 100% { transform: translate(0px, 0px); }
    50%       { transform: translate(-120px, -80px); }
}

/* â”€â”€ Sidebar â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
section[data-testid="stSidebar"] {
    background: rgba(10, 10, 20, 0.95) !important;
    border-right: 1px solid var(--glass-border) !important;
    backdrop-filter: blur(20px);
}
section[data-testid="stSidebar"] .block-container { padding: 1.5rem 1rem; }

/* â”€â”€ Hero â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
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

/* â”€â”€ Glass Cards â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
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

/* â”€â”€ Drag & Drop File Uploader Zone â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
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

/* â”€â”€ Fix Streamlit native progress bar & iframe â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
.stProgress, div[data-testid="stProgressBar"] { display: none !important; }
iframe { border: none !important; }

/* â”€â”€ Button Styling (Analyse & Extract) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
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

/* â”€â”€ Inputs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
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

/* â”€â”€ Reduced motion compliance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
@media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
}

/* â”€â”€ Export / Download Buttons â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ */
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

/* PDF button â€” coral/red accent */
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

/* DOCX button â€” blue accent */
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

/* Markdown button â€” green accent */
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
    ("ðŸ“¤", "Upload"),
    ("âš™ï¸", "Process"),
    ("ðŸ¤–", "Generate"),
    ("âœ…", "Done"),
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
            icon_render = "âœ“"
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
            items.append('<span class="stage-arrow">â€º</span>')

    return '<div class="stage-bar">' + "".join(items) + "</div>"


def _progress_fraction_to_stage(fraction: float) -> int:
    """Map a 0â€“1 fraction to a stage index (0â€“3)."""
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
    # â”€â”€ Recorder session state (populated by ui/recorder.py) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    "recorder_clips":         [],     # list[str] MM:SS timestamps of clipping events
    "recorder_quality_score": 0,      # int 0-100 composite quality score
    "recorder_segments":      [],     # list[dict] timeline segments
    # â”€â”€ Upload info (for Stage 1 card) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
    <div class="hero-title">ðŸŽ¬ Multimodal AI Media Summarizer</div>
    <div class="hero-subtitle">
        Upload a lecture, meeting, or recording â€” get a complete AI-powered summary in seconds.
    </div>
    <div style="display: flex; justify-content: center; gap: 12px; margin-top: 16px; flex-wrap: wrap;">
        <span style="padding: 4px 14px; border-radius: 100px; background: rgba(99, 179, 237, 0.1); border: 1px solid rgba(99, 179, 237, 0.25); color: #63B3ED; font-size: 11px; font-weight: 600;">ðŸŽ“ Lecture Mode</span>
        <span style="padding: 4px 14px; border-radius: 100px; background: rgba(104, 211, 145, 0.1); border: 1px solid rgba(104, 211, 145, 0.25); color: #68D391; font-size: 11px; font-weight: 600;">ðŸ”’ Zero Footprint</span>
        <span style="padding: 4px 14px; border-radius: 100px; background: rgba(159, 122, 234, 0.1); border: 1px solid rgba(159, 122, 234, 0.25); color: #9F7AEA; font-size: 11px; font-weight: 600;">âš¡ Advanced AI Engine</span>
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
    st.markdown('<div class="card-header">ðŸ“ Media Input</div>', unsafe_allow_html=True)
    
    tab_upload, tab_record = st.tabs(["ðŸ“ File Upload", "ðŸŽ¤ Record Audio"])
    
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
            f"ðŸ“„ <strong>{uploaded_file.name}</strong> Â· "
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
            # Audio files do not require HEVC/VFR video inspection â€” skip main thread ffprobe
            ext = Path(uploaded_file.name).suffix.lower()
            needs_conv = (
                ext in {".mpeg", ".mpg", ".wma", ".aiff", ".mp2", ".mp1"}
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
                "reasons": ["MPEG or non-standard audio format â€” auto-converting to AAC/MP3 for AI compatibility"] if needs_conv else [],
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
                # Always delete the temp file â€” prevents unbounded disk usage
                try:
                    _tmp_in.unlink(missing_ok=True)
                except OSError:
                    pass

        if inspection:
            if inspection.get("is_iphone"):
                st.info("ðŸ“± iPhone video detected (HEVC format). Auto-converting to H.264 for AI processing â€” takes ~10 seconds.")
            elif inspection.get("is_android"):
                st.info("ðŸ¤– Android video detected. Auto-converting to a compatible format â€” takes ~10 seconds.")
            elif inspection.get("is_vfr"):
                st.info("ðŸŽ¥ Variable frame rate detected. Normalizing to 30fps for AI compatibility.")
            elif inspection.get("needs_transcode"):
                st.info("âš ï¸ Incompatible format detected. Auto-converting to a compatible format â€” takes ~10 seconds.")
            else:
                st.success("âœ… File format is compatible â€” processing immediately.")


with col_opts:
    st.markdown('<div class="card-header">âš™ï¸ Processing Options</div>', unsafe_allow_html=True)

    instructions_preview = (
        "<em>(none)</em>"
        if not extra_instructions.strip()
        else extra_instructions[:80] + ("â€¦" if len(extra_instructions) > 80 else "")
    )
    st.markdown(
        f"""
<div class="card">
    <div style="margin-bottom:0.75rem">
        <span style="color:#94a3b8;font-size:0.82rem">ðŸ¤– Model</span><br>
        <span style="font-weight:600;color:#c7d2fe">{MODEL_DISPLAY_NAMES.get(selected_model, selected_model)}</span>
    </div>
    <div style="margin-bottom:0.75rem">
        <span style="color:#94a3b8;font-size:0.82rem">ðŸŒ Output Language</span><br>
        <span style="font-weight:600;color:#c7d2fe">{target_language}</span>
    </div>
    <div>
        <span style="color:#94a3b8;font-size:0.82rem">ðŸ“ Custom Instructions</span><br>
        <span style="font-weight:600;color:#c7d2fe">{instructions_preview}</span>
    </div>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.expander("â„¹ï¸ How it works", expanded=False):
        st.markdown(
            """
**Step 1 â€” Save & Upload**  
Your file is saved locally then streamed to the secure Cloud AI Files API.

**Step 2 â€” Server Processing**  
The AI engine indexes every frame and audio track.

**Step 3 â€” AI Generation**  
The model synthesises a structured multilingual summary.

**Step 4 â€” Interactive Chat**  
The cloud file is kept alive temporarily so you can chat with it. It is deleted when you upload a new file.
"""
        )

    with st.expander("ðŸ“‹ Supported formats â€” click to expand", expanded=False):
        st.markdown(
            """
âœ… **Fully Supported** (no conversion needed)  
`MP4 (H.264)` &nbsp; `MP3` &nbsp; `WAV` &nbsp; `M4A` &nbsp; `AAC` &nbsp; `OGG` &nbsp; `WEBM (VP8)` &nbsp; `FLAC`

ðŸ”„ **Auto-Converted** (processed automatically)  
`MP4 (HEVC/H.265)` &nbsp; `MOV` &nbsp; `MKV` &nbsp; `AVI` &nbsp; `FLV`  
`WEBM (VP9/AV1)` &nbsp; `iPhone recordings` &nbsp; `Android recordings` &nbsp; `Variable frame rate`

âŒ **Not Supported** (manual conversion required)  
Encrypted/DRM files &nbsp;Â·&nbsp; Corrupted files &nbsp;Â·&nbsp; Files over {max_mb} MB
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
            "ðŸš€ Analyse Media",
            disabled=uploaded_file is None or st.session_state.processing,
            key="analyse_btn",
            use_container_width=True,
        )
    with col2:
        mp3_clicked = st.button(
            "ðŸŽµ Extract Audio & Summarize",
            disabled=uploaded_file is None or st.session_state.processing,
            key="extract_mp3_btn",
            use_container_width=True,
        )
else:
    btn_col, _ = st.columns([2, 3])
    with btn_col:
        analyse_clicked = st.button(
            "ðŸš€ Analyse Media",
            disabled=uploaded_file is None or st.session_state.processing,
            key="analyse_btn",
            use_container_width=True,
        )
    mp3_clicked = False


# ---------------------------------------------------------------------------
# Processing pipeline â€” submit to background queue
# ---------------------------------------------------------------------------
if (analyse_clicked or mp3_clicked) and uploaded_file is not None:
    from ui.analysis_runner import submit_analysis_job

    if mp3_clicked:
        st.session_state.conversion_mode = "mp3"
    else:
        st.session_state.conversion_mode = None

    # Cleanup previous session's remote file
    if st.session_state.current_remote_file:
        try:
            old_client = GeminiVideoClient()
            old_client.delete_remote_file(st.session_state.current_remote_file)
        except Exception as e:
            logger.warning("Failed to clean up old remote file: %s", e)

    # Reset all session state for fresh run
    st.session_state.processing           = True
    st.session_state.result               = None
    st.session_state.error_msg            = None
    st.session_state.log_entries          = []
    st.session_state.chat_history         = []
    st.session_state.current_remote_file  = None
    st.session_state.progress_frac        = 0.0
    st.session_state.progress_lbl         = "Startingâ€¦"
    st.session_state["_queue_poll_start"] = 0.0

    # Pre-flight file size / type validation
    uploaded_file.seek(0, 2)
    _file_size = uploaded_file.tell()
    uploaded_file.seek(0)
    try:
        validate_media_file(uploaded_file.name, _file_size)
    except (InvalidFileTypeError, FileTooLargeError) as _exc:
        st.session_state.error_msg  = _exc.ui_message
        st.session_state.processing = False
        st.rerun()

    # Store upload info for Stage 1 progress card
    _fmt = Path(uploaded_file.name).suffix.lstrip(".").upper() or "MEDIA"
    st.session_state.upload_file_name = uploaded_file.name
    st.session_state.upload_file_size = _file_size
    st.session_state.upload_file_fmt  = _fmt

    # Reset pipeline_state for this run
    _ps.reset()
    _ps.update(
        stage=1,
        stage_label="File received",
        sub_message=uploaded_file.name,
        bytes_total=_file_size,
        bytes_sent=_file_size,
        pct=100.0,
    )

    try:
        from database import validate_and_use_key
        custom_key = st.session_state.get("custom_api_key", "")
        is_direct_gemini_key = bool(
            custom_key and (custom_key.startswith("AIzaSy") or len(custom_key) > 20)
        )
        if not is_direct_gemini_key and not validate_and_use_key(custom_key):
            st.error("Invalid or expired Custom Application Key. Please check your key or request a new one.")
            st.session_state.processing = False
            st.stop()

        job_id = submit_analysis_job(
            uploaded_file=uploaded_file,
            target_language=target_language,
            source_language=source_language,
            extra_instructions=extra_instructions,
            conversion_mode=st.session_state.get("conversion_mode"),
            classroom_mode=st.session_state.get("classroom_mode_toggle", True),
        )
        st.session_state.queue_job_id = job_id
        logger.info("Analysis queued - job %s for file '%s'", job_id[:8], uploaded_file.name)

    except (APIKeyError, VideoProcessingError, SummaryGenerationError,
            TranscodeError, InspectionError) as exc:
        st.session_state.error_msg  = getattr(exc, "ui_message", str(exc))
        st.session_state.processing = False
    except Exception as exc:
        st.session_state.error_msg  = f"Unexpected error: {type(exc).__name__}: {exc}"
        st.session_state.processing = False
        logger.exception("Unexpected error during analysis submission")
    finally:
        _ps.update(local_deleted=True, session_cleared=True)

    st.rerun()


# ---------------------------------------------------------------------------
# Queue result polling — runs on every Streamlit rerun
# ---------------------------------------------------------------------------
from ui.queue_status import render_queue_status

_active_job_id: str | None = st.session_state.get("queue_job_id")
if _active_job_id is not None:
    render_queue_status()


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
    ðŸ”’ Zero footprint Â· Files deleted after processing Â· No data stored permanently Â· TLS 1.3 encrypted
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
