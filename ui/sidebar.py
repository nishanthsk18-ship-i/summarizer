"""
ui/sidebar.py — Glassmorphism sidebar for model selection, key input, and options.
"""

from pathlib import Path
import os
import stat
import streamlit as st
from config import config, SUPPORTED_LANGUAGES

MODEL_DISPLAY_NAMES: dict[str, str] = {
    "gemini-2.5-flash":                 "NeuralFlash v2.5  🧠 Ultra Fast & Smart",
    "gemini-2.5-pro":                   "NeuralPro v2.5    🔬 Deep Reasoning",
    "gemini-1.5-flash":                 "NeuralFlash v1.5  ⚡ Fast & Reliable",
    "gemini-1.5-pro":                   "NeuralPro v1.5    📊 Research",
    "gemini-2.0-flash":                 "NeuralFlash v2.0  ⚖️ Legacy Flash",
}


def render_sidebar() -> tuple[str, str, str, str]:
    """Renders the dark-glass sidebar and returns (selected_model, target_language, source_language, extra_instructions)."""
    with st.sidebar:
        # App logo area with glow ring & gradient title
        st.markdown(
            """
<div style="text-align: center; padding: 0.5rem 0 1.25rem 0;">
    <div style="
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 56px; height: 56px;
        border-radius: 50%;
        background: rgba(15, 15, 30, 0.9);
        box-shadow: 0 0 40px rgba(99, 179, 237, 0.25);
        border: 1px solid rgba(99, 179, 237, 0.3);
        font-size: 2rem;
        margin-bottom: 0.5rem;
    ">🎬</div>
    <div style="
        font-size: 1.25rem;
        font-weight: 700;
        background: linear-gradient(135deg, #63B3ED, #9F7AEA);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        letter-spacing: -0.02em;
    ">Media Summarizer</div>
    <div style="
        font-size: 11px;
        color: rgba(255, 255, 255, 0.35);
        margin-top: 2px;
    ">Powered by Advanced Multimodal AI</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("---")

        # ── Access Status Header ──
        st.markdown(
            '<div style="font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:2px; color:rgba(255,255,255,0.4); border-left:2px solid #63B3ED; padding-left:8px; margin-bottom:12px;">⚡ ACCESS STATUS</div>',
            unsafe_allow_html=True,
        )

        from database import get_active_key
        default_key = get_active_key()
        st.session_state.custom_api_key = default_key

        st.markdown(
            '<div style="font-size:11px; color:#68D391; background:rgba(104,211,145,0.1); border:1px solid rgba(104,211,145,0.25); padding:8px 12px; border-radius:8px; margin-bottom:12px; font-weight:600; display:flex; align-items:center; gap:8px;">'
            '<span>⚡</span> <span>Server AI Active (Free Unlimited Access)</span></div>',
            unsafe_allow_html=True,
        )



        # ── Model Selection Header ──
        st.markdown(
            '<div style="font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:2px; color:rgba(255,255,255,0.4); border-left:2px solid #63B3ED; padding-left:8px; margin-top:16px; margin-bottom:12px;">🤖 AI MODEL SELECTION</div>',
            unsafe_allow_html=True,
        )

        model_options = list(MODEL_DISPLAY_NAMES.values())
        _model_api_names = {v: k for k, v in MODEL_DISPLAY_NAMES.items()}
        _current_display = MODEL_DISPLAY_NAMES.get(config.gemini_model, model_options[0])
        
        selected_model_display = st.selectbox(
            "AI Model Engine",
            model_options,
            index=model_options.index(_current_display) if _current_display in model_options else 0,
            help="Select the AI model engine for media summarisation.",
            key="model_select",
            label_visibility="collapsed",
        )
        selected_model = _model_api_names[selected_model_display]
        # Store in session_state only — never mutate the global config singleton,
        # which would cause race conditions in multi-user deployments.
        st.session_state["selected_model"] = selected_model

        st.markdown("---")

        # ── Output Settings Header ──
        st.markdown(
            '<div style="font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:2px; color:rgba(255,255,255,0.4); border-left:2px solid #63B3ED; padding-left:8px; margin-bottom:12px;">🌐 OUTPUT SETTINGS</div>',
            unsafe_allow_html=True,
        )

        target_language = st.selectbox(
            "Summary Language",
            SUPPORTED_LANGUAGES,
            index=SUPPORTED_LANGUAGES.index(config.default_language)
            if config.default_language in SUPPORTED_LANGUAGES else 0,
            key="language_select",
        )

        source_options = ["Auto-detect"] + SUPPORTED_LANGUAGES
        source_language = st.selectbox(
            "Source Language (Input)",
            source_options,
            index=source_options.index(config.default_source_language)
            if config.default_source_language in source_options else 0,
            key="source_language_select",
            help="Specify media language if Auto-detect needs assistance.",
        )

        # ── Classroom Settings Header ──
        st.markdown(
            '<div style="font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:2px; color:rgba(255,255,255,0.4); border-left:2px solid #63B3ED; padding-left:8px; margin-top:16px; margin-bottom:12px;">🏫 CLASSROOM NOISE ISOLATION</div>',
            unsafe_allow_html=True,
        )
        classroom_on = st.toggle(
            "Classroom Mode",
            value=True,
            help="Filters out ambient classroom noise and focuses 100% on the professor's voice.",
            key="classroom_mode_toggle",
        )
        if classroom_on:
            st.markdown('<div style="font-size:11px; color:#63B3ED; font-weight:600; margin-top:-4px; margin-bottom:8px;">🎓 Classroom Mode Active</div>', unsafe_allow_html=True)

        st.markdown("---")

        # ── Custom Instructions Header ──
        st.markdown(
            '<div style="font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:2px; color:rgba(255,255,255,0.4); border-left:2px solid #63B3ED; padding-left:8px; margin-bottom:12px;">✏️ CUSTOM INSTRUCTIONS</div>',
            unsafe_allow_html=True,
        )
        extra_instructions = st.text_area(
            "Additional instructions (optional)",
            placeholder='e.g. "Focus on the Q&A" or "Emphasise statistical data"',
            height=100,
            key="extra_instructions",
            label_visibility="collapsed",
        )

        st.markdown("---")

        # Sidebar footer
        st.markdown(
            """
<div style="text-align: center; padding-top: 0.5rem; font-size: 11px; color: rgba(255, 255, 255, 0.25);">
    Powered by Advanced Multimodal AI · Built with Streamlit<br>
    <span style="font-family: monospace; font-size: 10px; color: rgba(99, 179, 237, 0.8); font-weight: 600;">v2.1.0 (Production)</span>
</div>
""",
            unsafe_allow_html=True,
        )


        return selected_model, target_language, source_language, extra_instructions
