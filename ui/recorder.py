"""
ui/recorder.py — Second-generation glassmorphism audio recorder component.

This module declares and renders the custom Streamlit audio recorder component
defined in ui/recorder_component/index.html.  All audio capture, DSP filtering,
and quality analysis live in the browser-side JavaScript; Python only decodes
the returned payload and writes session-state keys.

Session state keys managed by this module
------------------------------------------
st.session_state.recorder_clips  (list[str]):
    Timestamps in "MM:SS" format where the input signal exceeded 90% of the
    maximum amplitude (clipping events).  An empty list means a clean
    recording with no clipping detected.

st.session_state.recorder_quality_score  (int, 0–100):
    Composite recording-quality score computed after each recording stops.
    Weighted average of three sub-scores:
        SNR estimate   (40 %) – ratio of speaking RMS to silence RMS
        Clipping score (30 %) – −5 points per detected clip event
        Silence score  (30 %) – deduction when >40 % of recording is silence
    Labels:  80–100 = Excellent | 60–79 = Good | 40–59 = Fair | 0–39 = Poor

st.session_state.recorder_segments  (list[dict]):
    Ordered list of timeline segments.  Each dict contains:
        "type"   (str)   – "speaking", "silence", or "paused"
        "start"  (float) – segment start in seconds from recording start
        "end"    (float) – segment end   in seconds from recording start
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Component declaration  (module-level — idempotent across re-runs)
# ---------------------------------------------------------------------------

_COMPONENT_DIR = Path(__file__).parent / "recorder_component"

_recorder_component = components.declare_component(
    "audio_recorder_v2",
    path=str(_COMPONENT_DIR),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_audio_recorder(
    classroom_mode: bool = False,
    max_duration_seconds: int = 3600,
    noise_gate_default: float = 0.008,
) -> Optional[bytes]:
    """
    Render the second-generation glassmorphism audio recorder.

    The component is rendered as an iframe.  Audio capture, DSP filtering,
    VAD, clip detection, quality scoring, and all UI state live entirely in
    browser JavaScript — no Python audio libraries are required.

    When the user clicks "✓ Use This Recording", the browser base64-encodes
    the Opus/WebM blob and sends it back via Streamlit's component protocol.
    This function decodes the payload, updates session state, and returns
    the raw bytes to the caller.

    Args:
        classroom_mode:
            When True the component activates:
              • 6-node precision filter chain
                (HP80 → CUT300 → BOOST2.5K → COMP → LP7K → GAIN)
              • Spectral noise gate on the 100-300 Hz classroom-chatter band
              • Visual theme diff (cyan card border, violet waveform,
                filter-chain pill diagram)
        max_duration_seconds:
            Hard cap on recording length (seconds).  The ring timer counts
            up to this value; recording auto-stops at the limit.
            Defaults to 3600 (1 hour).
        noise_gate_default:
            Initial broadband noise-gate threshold (0.001 – 0.05).
            The in-UI slider lets the user override this in real time.
            Defaults to 0.008.

    Returns:
        Raw audio bytes (Opus/WebM) when the user has submitted a completed
        recording in this Streamlit run; otherwise ``None``.
    """
    # Initialise session-state slots on first call
    for _key, _default in (
        ("recorder_clips", []),
        ("recorder_quality_score", 0),
        ("recorder_segments", []),
    ):
        if _key not in st.session_state:
            st.session_state[_key] = _default

    result: dict | None = _recorder_component(
        classroom_mode=classroom_mode,
        max_duration_seconds=max_duration_seconds,
        noise_gate_default=noise_gate_default,
        key="audio_recorder_v2",
        default=None,
    )

    if not result:
        st.session_state["recorded_audio_bytes"] = None
        return None

    try:
        audio_b64: str = result.get("audio_b64", "")
        if not audio_b64:
            return None

        audio_bytes = base64.b64decode(audio_b64)

        st.session_state.recorder_clips = result.get("clips", [])
        st.session_state.recorder_quality_score = int(
            result.get("quality_score", 0)
        )
        st.session_state.recorder_segments = result.get("segments", [])

        logger.info(
            "Recorder: %d bytes | quality=%d | clips=%d | segments=%d",
            len(audio_bytes),
            st.session_state.recorder_quality_score,
            len(st.session_state.recorder_clips),
            len(st.session_state.recorder_segments),
        )
        return audio_bytes

    except Exception as exc:  # pragma: no cover
        logger.error("Recorder component decode error: %s", exc)
        return None
