"""
ui/sidebar.py — Glassmorphism sidebar for model selection, key input, and options.
"""

from pathlib import Path
import os
import stat
import streamlit as st
from config import config, SUPPORTED_LANGUAGES

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
    ">Powered by Google Gemini</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("---")

        # ── Access Configuration Header ──
        st.markdown(
            '<div style="font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:2px; color:rgba(255,255,255,0.4); border-left:2px solid #63B3ED; padding-left:8px; margin-bottom:12px;">🔑 ACCESS CONFIGURATION</div>',
            unsafe_allow_html=True,
        )

        from database import get_active_key, validate_and_use_key
        from database import key_exists  # read-only check (no quota burn)
        default_key = get_active_key()
        if not st.session_state.get("custom_api_key"):
            st.session_state.custom_api_key = default_key

        api_key_input = st.text_input(
            "Custom Application Key",
            value=st.session_state.get("custom_api_key", default_key),
            type="password",
            placeholder="live_...",
            help="Enter the custom API key provided by the host.",
        )
        
        # Key validation badge — read-only check, does NOT consume quota
        is_valid_key = bool(api_key_input and key_exists(api_key_input))
        if is_valid_key:
            st.markdown('<div style="font-size:11px; color:#68D391; margin-top:-8px; margin-bottom:12px; font-weight:600;">✓ Valid Key</div>', unsafe_allow_html=True)
            st.session_state.custom_api_key = api_key_input
        else:
            st.markdown('<div style="font-size:11px; color:#FC8181; margin-top:-8px; margin-bottom:12px; font-weight:600;">✗ Invalid Key</div>', unsafe_allow_html=True)
            st.session_state.custom_api_key = default_key


        # ── Model Selection Header ──
        st.markdown(
            '<div style="font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:2px; color:rgba(255,255,255,0.4); border-left:2px solid #63B3ED; padding-left:8px; margin-top:16px; margin-bottom:12px;">🤖 AI MODEL SELECTION</div>',
            unsafe_allow_html=True,
        )

        model_options = [
            "gemini-3.6-flash  ⚡ Fast",
            "gemini-3.5-flash  🧠 Powerful",
            "gemini-3.5-flash-lite  ⚖️ Balanced",
            "gemini-3.1-pro (preview)  🔬 Research",
            "gemini-3.1-flash-lite (preview)  ⚡ Ultra-Fast",
        ]
        _model_api_names = {
            "gemini-3.6-flash  ⚡ Fast":                 "gemini-3.6-flash",
            "gemini-3.5-flash  🧠 Powerful":             "gemini-3.5-flash",
            "gemini-3.5-flash-lite  ⚖️ Balanced":        "gemini-3.5-flash-lite",
            "gemini-3.1-pro (preview)  🔬 Research":     "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite (preview)  ⚡ Ultra-Fast": "gemini-3.1-flash-lite-preview",
        }
        _current_display = next(
            (k for k, v in _model_api_names.items() if v == config.gemini_model),
            "gemini-3.6-flash  ⚡ Fast",
        )
        selected_model_display = st.selectbox(
            "Gemini Model",
            model_options,
            index=model_options.index(_current_display) if _current_display in model_options else 0,
            help="Select the AI model for media summarisation.",
            key="model_select",
            label_visibility="collapsed",
        )
        selected_model = _model_api_names[selected_model_display]
        config.gemini_model = selected_model

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
    Powered by Google Gemini · Built with Streamlit<br>
    <span style="font-family: monospace; font-size: 10px; color: rgba(99, 179, 237, 0.6);">v2.0.0</span>
</div>
""",
            unsafe_allow_html=True,
        )

        return selected_model, target_language, source_language, extra_instructions
