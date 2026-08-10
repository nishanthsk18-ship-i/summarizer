import streamlit as st
from datetime import datetime
from pathlib import Path
from gemini_client import GeminiVideoClient
from ui.exporter import render_export_buttons


def render_result_and_chat(chat_client: GeminiVideoClient | None = None) -> None:
    """Renders the generated summary and interactive chat with glassmorphism styling."""
    if chat_client is None:
        try:
            chat_client = GeminiVideoClient()
        except Exception:
            chat_client = None  # API key not configured; chat section will be hidden

    if st.session_state.result is not None:
        result = st.session_state.result
        st.markdown("---")

        # Badges row
        res_col1, res_col2, res_col3, res_col4 = st.columns(4)
        with res_col1:
            st.markdown('<div style="padding:6px 12px; background:rgba(104,211,145,0.12); border:1px solid rgba(104,211,145,0.3); border-radius:100px; color:#68D391; font-size:12px; font-weight:600; text-align:center;">✅ Summary Ready</div>', unsafe_allow_html=True)
        with res_col2:
            st.markdown(f'<div style="padding:6px 12px; background:rgba(99,179,237,0.12); border:1px solid rgba(99,179,237,0.3); border-radius:100px; color:#63B3ED; font-size:12px; font-weight:600; text-align:center;">🌐 {result.target_language}</div>', unsafe_allow_html=True)
        with res_col3:
            char_count = len(result.summary_markdown)
            st.markdown(f'<div style="padding:6px 12px; background:rgba(246,173,85,0.12); border:1px solid rgba(246,173,85,0.3); border-radius:100px; color:#F6AD55; font-size:12px; font-weight:600; text-align:center;">📝 {char_count:,} chars</div>', unsafe_allow_html=True)
        with res_col4:
            word_count = len(result.summary_markdown.split())
            st.markdown(f'<div style="padding:6px 12px; background:rgba(159,122,234,0.12); border:1px solid rgba(159,122,234,0.3); border-radius:100px; color:#9F7AEA; font-size:12px; font-weight:600; text-align:center;">📊 {word_count:,} words</div>', unsafe_allow_html=True)

        st.markdown("")

        # Rendered / Raw tabs
        tab_rendered, tab_raw = st.tabs(["📖 Rendered Summary", "📋 Raw Markdown"])
        with tab_rendered:
            st.markdown(result.summary_markdown)
        with tab_raw:
            st.code(result.summary_markdown, language="markdown")

        # ── 1-Click Export (PDF / Word / Markdown) ───────────────────────────
        base_name = Path(result.video_filename).stem
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        render_export_buttons(
            summary_text=result.summary_markdown,
            base_filename=f"{base_name}_summary_{ts}",
        )

    # ---------------------------------------------------------------------------
    # Interactive Chat
    # ---------------------------------------------------------------------------
    if st.session_state.result is not None and st.session_state.current_remote_file and chat_client is not None:
        st.markdown("---")
        st.markdown(
            '<div style="font-size:1.25rem; font-weight:700; color:rgba(255,255,255,0.92); margin-bottom:4px;">💬 Interactive Q&A Assistant</div>'
            '<div style="font-size:13px; color:rgba(255,255,255,0.5); margin-bottom:16px;">Ask follow-up questions about the media. The AI answers using the indexed video/audio context.</div>',
            unsafe_allow_html=True,
        )

        # Display chat messages from history on app rerun
        for message in st.session_state.chat_history:
            role_icon = "user" if message["role"] == "user" else "assistant"
            with st.chat_message(role_icon):
                st.markdown(message["content"])

        # React to user input
        if prompt := st.chat_input("E.g., What did the speaker say about X?"):
            with st.chat_message("user"):
                st.markdown(prompt)
            
            st.session_state.chat_history.append({"role": "user", "content": prompt})

            with st.chat_message("assistant"):
                with st.spinner("Thinking..."):
                    try:
                        answer = chat_client.ask_question(
                            st.session_state.current_remote_file, 
                            st.session_state.chat_history[:-1], 
                            prompt
                        )
                        st.markdown(answer)
                        st.session_state.chat_history.append({"role": "model", "content": answer})
                    except Exception as e:
                        st.error(f"Failed to generate answer: {e}")
                        st.session_state.chat_history.pop()
