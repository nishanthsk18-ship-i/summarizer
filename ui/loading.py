"""
ui/loading.py — Self-contained HTML/CSS component renderer for the AI Media Summarizer loading experience.

This module replaces all Streamlit native progress bars (st.progress) and markdown fragments
with single self-contained HTML documents rendered via st.components.v1.html().
This guarantees zero style leaks, zero layout collapse, and pixel-perfect design system enforcement.
"""

from __future__ import annotations

import html
import streamlit as st
import streamlit.components.v1 as components

# ---------------------------------------------------------------------------
# Base CSS embedded inside the HTML component
# ---------------------------------------------------------------------------

_COMPONENT_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap');

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  background: transparent;
  color: rgba(255, 255, 255, 0.92);
  overflow: hidden;
}

/* ── Container ──────────────────────────────────────────────────────────── */
.loading-wrapper {
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
  padding: 12px 16px;
  box-sizing: border-box;
}

/* ── Stepper ────────────────────────────────────────────────────────────── */
.stepper-wrapper {
  width: 100%;
  padding: 16px 0 24px 0;
  display: flex;
  justify-content: center;
}
.stepper {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  gap: 0;
  width: 100%;
  max-width: 640px;
  position: relative;
}
.step {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  position: relative;
}
.step-circle {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  font-family: "Inter", system-ui, sans-serif;
  z-index: 2;
  position: relative;
  transition: all 0.3s ease;
}
.step.completed .step-circle {
  background: #68D391;
  color: #0F0F19;
  box-shadow: 0 0 16px rgba(104,211,145,0.4);
}
.step.active .step-circle {
  background: #63B3ED;
  color: #0F0F19;
  box-shadow: 0 0 0 4px rgba(99,179,237,0.25);
  animation: pulse-ring 1.5s ease infinite;
}
.step.upcoming .step-circle {
  background: rgba(255,255,255,0.08);
  color: rgba(255,255,255,0.3);
  border: 1px solid rgba(255,255,255,0.1);
}
.step.skipped .step-circle {
  background: rgba(104,211,145,0.2);
  color: #68D391;
  border: 1px solid rgba(104,211,145,0.4);
}
@keyframes pulse-ring {
  0%   { box-shadow: 0 0 0 0 rgba(99,179,237,0.4); }
  70%  { box-shadow: 0 0 0 8px rgba(99,179,237,0); }
  100% { box-shadow: 0 0 0 0 rgba(99,179,237,0); }
}
.step-line {
  position: absolute;
  top: 18px;
  left: 50%;
  width: 100%;
  height: 2px;
  background: rgba(255,255,255,0.08);
  z-index: 1;
}
.step-line.completed { background: #68D391; }
.step:last-child .step-line { display: none; }
.step-label {
  margin-top: 8px;
  font-size: 10px;
  font-family: "Inter", system-ui, sans-serif;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: rgba(255,255,255,0.3);
}
.step.completed .step-label { color: #68D391; }
.step.active .step-label   { color: #63B3ED; }

/* ── Glass Cards ────────────────────────────────────────────────────────── */
.glass-card {
  background: rgba(15,15,25,0.92);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border: 1px solid rgba(255,255,255,0.08);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.05);
  border-radius: 20px;
  padding: 20px 24px;
  width: 100%;
  margin-bottom: 14px;
}

/* ── File Info Card ─────────────────────────────────────────────────────── */
.file-info-card {
  display: flex;
  align-items: center;
  gap: 16px;
}
.file-icon { font-size: 28px; line-height: 1; }
.file-details { flex: 1; min-width: 0; }
.file-name {
  font-size: 14px;
  font-weight: 600;
  color: #FFFFFF;
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.file-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: rgba(255,255,255,0.4);
}
.meta-dot { color: rgba(255,255,255,0.2); }
.format-badge {
  padding: 2px 8px;
  border-radius: 20px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.format-badge.compatible {
  background: rgba(99,179,237,0.15);
  color: #63B3ED;
  border: 1px solid rgba(99,179,237,0.3);
}
.format-badge.incompatible {
  background: rgba(246,173,85,0.15);
  color: #F6AD55;
  border: 1px solid rgba(246,173,85,0.3);
}
.status-badge { font-size: 11px; font-weight: 500; }
.status-badge.uploading { color: #63B3ED; }
.status-badge.converting { color: #F6AD55; }
.status-badge.done { color: #68D391; }

/* ── Progress Card ──────────────────────────────────────────────────────── */
.progress-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}
.stage-label {
  font-size: 10px;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: rgba(255,255,255,0.4);
}
.progress-pct {
  font-size: 13px;
  font-family: "Fira Code", monospace;
  color: rgba(255,255,255,0.6);
}
.progress-track {
  width: 100%;
  height: 6px;
  background: rgba(255,255,255,0.06);
  border-radius: 100px;
  overflow: hidden;
  margin-bottom: 10px;
}
.progress-fill {
  height: 100%;
  border-radius: 100px;
  background: linear-gradient(90deg, #63B3ED, #9F7AEA);
  transition: width 0.4s ease;
}
.progress-fill.s1 { background: linear-gradient(90deg, #63B3ED, #9F7AEA); }
.progress-fill.s1_5 { background: linear-gradient(90deg, #9F7AEA, #B794F4); }
.progress-fill.s2 { background: linear-gradient(90deg, #F6AD55, #ED8936); }
.progress-fill.s3 { background: linear-gradient(90deg, #9F7AEA, #805AD5); }
.progress-fill.s4 {
  width: 40% !important;
  background: linear-gradient(90deg, #63B3ED, #9F7AEA);
  animation: sweep 2s ease-in-out infinite;
}
.progress-fill.s5 { background: linear-gradient(90deg, #68D391, #48BB78); }

@keyframes sweep {
  0%   { margin-left: -40%; }
  100% { margin-left: 100%; }
}
.progress-message {
  font-size: 13px;
  color: rgba(255,255,255,0.5);
}

/* ── Queue Card ─────────────────────────────────────────────────────────── */
.queue-card {
  border-color: rgba(246,173,85,0.25);
  background: rgba(20, 15, 10, 0.92);
}
.queue-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.queue-icon-svg {
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
}
.spin-icon {
  animation: spin 3s linear infinite;
}
@keyframes spin { 100% { transform: rotate(360deg); } }
.queue-label {
  font-size: 10px;
  letter-spacing: 2px;
  color: rgba(246,173,85,0.8);
  font-weight: 600;
}
.queue-number {
  font-size: 18px;
  font-weight: 700;
  color: #F6AD55;
}
.queue-message {
  font-size: 13px;
  color: rgba(255,255,255,0.6);
  margin-bottom: 8px;
}
.queue-countdown {
  font-size: 11px;
  font-family: "Fira Code", monospace;
  color: rgba(246,173,85,0.6);
}
"""

# ---------------------------------------------------------------------------
# Master UI Renderer
# ---------------------------------------------------------------------------

def show_loading_ui(
    stage: float | int = 1,
    progress: float = 0.0,
    stage_message: str = "",
    file_name: str = "",
    file_size: str = "",
    file_format: str = "MP4",
    is_compatible: bool = True,
    queue_position: int = 0,
    retry_attempt: int = 0,
    retry_wait: int = 0,
    conversion_mode: str = "",
    skipped_stages: list[int] | None = None,
    ffmpeg_speed: str = "",
    ffmpeg_eta: str = "",
) -> None:
    """
    Render the entire loading UI as a single self-contained HTML component.
    """
    skipped = set(skipped_stages or [])
    
    if conversion_mode == "mp3":
        labels = ["Upload", "Extract", "Convert", "Cloud", "AI", "Done"]
    else:
        labels = ["Upload", "Convert", "Cloud", "AI", "Done"]
        
    total_steps = len(labels)
    
    # ── Stepper HTML ──
    stepper_html = []
    for idx, label in enumerate(labels):
        step_num = idx + 1
        is_skip = step_num in skipped
        is_done = step_num < stage
        is_active = step_num == stage or (isinstance(stage, float) and abs(stage - step_num) < 0.1)
        
        if is_skip:
            css_class = "skipped"
            symbol = "✓"
        elif is_done:
            css_class = "completed"
            symbol = "✓"
        elif is_active:
            css_class = "active"
            symbol = str(idx + 1)
        else:
            css_class = "upcoming"
            symbol = str(idx + 1)

        line_css = "completed" if is_done else ""

        stepper_html.append(
            f'<div class="step {css_class}">'
            f'  <div class="step-circle">{symbol}</div>'
            f'  <div class="step-line {line_css}"></div>'
            f'  <div class="step-label">{label}</div>'
            f'</div>'
        )

    # ── File Info Card HTML ──
    file_card_html = ""
    if file_name:
        badge_cls = "compatible" if is_compatible else "incompatible"
        status_txt = "Uploading" if stage <= 1 else ("Converting" if stage == 2 else "Processing")
        status_cls = "uploading" if stage <= 1 else ("converting" if stage == 2 else "done")
        file_card_html = f"""
        <div class="glass-card file-info-card">
          <div class="file-icon">🎬</div>
          <div class="file-details">
            <div class="file-name">{html.escape(file_name)}</div>
            <div class="file-meta">
              <span>{html.escape(file_size or "Media")}</span>
              <span class="meta-dot">·</span>
              <span class="format-badge {badge_cls}">{html.escape(file_format)}</span>
              <span class="meta-dot">·</span>
              <span class="status-badge {status_cls}">⬤ {status_txt}</span>
            </div>
          </div>
        </div>
        """

    # ── Progress Card HTML ──
    pct_val = min(100.0, max(0.0, progress))
    pct_str = f"{int(pct_val)}%"
    
    stage_key = f"s{str(stage).replace('.', '_')}"
    fill_cls = f"progress-fill {stage_key}"
    if stage == 4:
        pct_str = "ANALYSING"

    stage_names = {
        1: "STAGE 1 — UPLOAD",
        1.5: "STAGE 1.5 — EXTRACT AUDIO",
        2: "STAGE 2 — FFMPEG TRANSCODE",
        3: "STAGE 3 — GEMINI CLOUD UPLOAD",
        4: "STAGE 4 — AI SUMMARY GENERATION",
        5: "STAGE 5 — TEARDOWN & CLEANUP",
    }
    lbl = stage_names.get(stage, f"STAGE {stage}")

    extra_meta = ""
    if ffmpeg_speed:
        extra_meta = f" · {ffmpeg_speed}"
    if ffmpeg_eta:
        extra_meta += f" ({ffmpeg_eta})"

    progress_card_html = f"""
    <div class="glass-card">
      <div class="progress-header">
        <span class="stage-label">{lbl}</span>
        <span class="progress-pct">{pct_str}{extra_meta}</span>
      </div>
      <div class="progress-track">
        <div class="{fill_cls}" style="width: {pct_val}%"></div>
      </div>
      <div class="progress-message">{html.escape(stage_message or "Processing media...")}</div>
    </div>
    """

    # ── Queue Card HTML ──
    queue_card_html = ""
    if queue_position > 0:
        queue_card_html = f"""
        <div class="glass-card queue-card">
          <div class="queue-header">
            <div class="queue-icon-svg">
              <svg class="spin-icon" viewBox="0 0 24 24" width="24" height="24">
                <circle cx="12" cy="12" r="10" stroke="rgba(246,173,85,0.3)" stroke-width="3" fill="none"/>
                <path d="M12 2 a 10 10 0 0 1 10 10" stroke="#F6AD55" stroke-width="3" fill="none"/>
              </svg>
            </div>
            <div>
              <div class="queue-label">QUEUE POSITION</div>
              <div class="queue-number">#{queue_position}</div>
            </div>
          </div>
          <div class="queue-message">
            The server is busy processing other requests. Your media will be analysed automatically.
          </div>
          <div class="queue-countdown">
            Auto-refreshing in <span id="countdown">3</span>s...
          </div>
          <script>
            let sec = 3;
            setInterval(function() {{
              sec = sec > 1 ? sec - 1 : 3;
              const el = document.getElementById('countdown');
              if (el) el.innerText = sec;
            }}, 1000);
          </script>
        </div>
        """

    # ── Full Assembly ──
    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>{_COMPONENT_CSS}</style>
    </head>
    <body>
      <div class="loading-wrapper">
        <div class="stepper-wrapper">
          <div class="stepper">
            {"".join(stepper_html)}
          </div>
        </div>
        {file_card_html}
        {progress_card_html}
        {queue_card_html}
      </div>
    </body>
    </html>
    """

    # Calculate precise iframe height to eliminate scrollbars and black gaps
    calculated_height = 200
    if file_name:
        calculated_height += 85
    if queue_position > 0:
        calculated_height += 150
    if retry_attempt > 0:
        calculated_height += 120

    components.html(full_html, height=calculated_height, scrolling=False)


# ---------------------------------------------------------------------------
# Backward Compatibility Wrappers (Delegate to show_loading_ui)
# ---------------------------------------------------------------------------

def show_master_stepper(current_stage: float | int, skipped_stages: list[int] | None = None, conversion_mode: str = "") -> None:
    pass  # Integrated into show_loading_ui

def show_upload_progress(file_name: str, file_size_bytes: int, file_format: str, is_compatible: bool = True, pct: float = 100.0, eta_str: str = "") -> None:
    from file_handler import human_readable_size
    show_loading_ui(
        stage=1,
        progress=pct,
        stage_message=f"Uploaded '{file_name}' ({human_readable_size(file_size_bytes)})",
        file_name=file_name,
        file_size=human_readable_size(file_size_bytes),
        file_format=file_format,
        is_compatible=is_compatible,
    )

def show_transcode_progress(pct: float, speed: str = "", eta: str = "", input_format: str = "HEVC", is_iphone: bool = False, is_android: bool = False, tip_index: int = 0, done: bool = False, in_size_mb: float = 0, out_size_mb: float = 0) -> None:
    show_loading_ui(
        stage=2,
        progress=pct,
        stage_message="Converting mobile video (HEVC → H.264) for Gemini compatibility..." if not done else "✓ Transcode complete",
        file_format=input_format,
        is_compatible=False,
        ffmpeg_speed=speed,
        ffmpeg_eta=eta,
    )

def show_cloud_upload_progress(bytes_sent: int = 0, bytes_total: int = 0, upload_speed: str = "", retry_state: dict | None = None, done: bool = False) -> None:
    pct = (bytes_sent / bytes_total * 100.0) if bytes_total > 0 else 0.0
    show_loading_ui(
        stage=3,
        progress=100.0 if done else pct,
        stage_message="Streaming media to Google Gemini Files API..." if not done else "✓ Cloud upload complete",
    )

def show_ai_processing(word_count: int = 0, sections_done: int = 0, msg_index: int = 0) -> None:
    show_loading_ui(
        stage=4,
        progress=50.0,
        stage_message=f"Gemini is generating summary... ({word_count:,} words generated)",
    )

def show_cleanup_sequence(local_done: bool = True, cloud_done: bool = True, session_done: bool = True) -> None:
    show_loading_ui(
        stage=5,
        progress=100.0,
        stage_message="✓ Temporary files and cloud records deleted (Zero Footprint)",
    )

def show_extraction_progress(pct: float = 0.0, done: bool = False, msg_index: int = 0) -> None:
    show_loading_ui(
        stage=1.5,
        progress=100.0 if done else 50.0,
        stage_message="Extracting audio track from video (MP4 → MP3)..." if not done else "✓ Audio extracted successfully",
        conversion_mode="mp3",
    )
