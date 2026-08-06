# 🎬 Multilingual AI Video Summarizer

> Upload any video. Get a rich, structured educational summary — in any of 20 languages.  
> Powered by **Google Gemini 2.0 Flash** · Built with **Streamlit** · Zero data stored permanently.

---

## ✨ Features

| Feature | Detail |
|---|---|
| 🤖 AI-Powered Analysis | Full video understanding via Gemini multimodal API |
| 🌐 20+ Languages | English, Spanish, French, Japanese, Arabic, Hindi & more |
| 📚 Rich Output Schema | Metadata · TOC · Key concepts · Section breakdown · Takeaways |
| 🔄 Auto-Cleanup | Local & remote temp files deleted after every run |
| 🔁 Retry Logic | Exponential back-off for network / rate-limit errors |
| 💻 Dual Interface | Streamlit Web UI **+** CLI for scripting / automation |
| ⬇️ Export | Download summary as `.md` or `.txt` |

---

## 📁 Project Structure

```
video-summarizer/
├── app.py               # Streamlit web application
├── cli.py               # Command-line interface
├── gemini_client.py     # Gemini API client (upload, poll, generate, cleanup)
├── file_handler.py      # Local file validation, saving, cleanup
├── prompts.py           # System prompt + dynamic prompt builder
├── config.py            # Centralised config (reads from .env)
├── test_pipeline.py     # pytest test suite (fully mocked)
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
├── .gitignore
└── .streamlit/
    └── config.toml      # Streamlit theme configuration
```

---

## 🚀 Quick Start

### 1. Clone & enter the project
```bash
git clone <your-repo-url>
cd video-summarizer
```

> [!IMPORTANT]
> **OS-Level FFmpeg Required!**
> If you are running this locally without Docker, you *must* have `ffmpeg` installed on your host OS and available in your system PATH. The `ffmpeg-python` package is only a wrapper. Without OS-level FFmpeg, the app will crash if you upload an HEVC/H.265 video.

### Running with Docker (Recommended for Production)

The easiest and most reliable way to run the app is via Docker, which guarantees that `ffmpeg` and all dependencies are perfectly configured.

```bash
docker build -t media-summarizer .
docker run -p 8501:8501 media-summarizer
```

### Running Locally (Python)

```bash
# 1. Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the Streamlit app
streamlit run app.py
```

### 4. Configure your API key
```bash
# Copy the template
cp .env.example .env

# Edit .env and set your key:
# GEMINI_API_KEY=AIza...
```
Get a free API key at **https://aistudio.google.com/app/apikey**

---

## 🖥️ Running the Web UI

```bash
streamlit run app.py
```

Open **http://localhost:8501** in your browser.

**Workflow:**
1. Enter your Gemini API key in the sidebar (or set it in `.env`)
2. Choose your model and output language
3. Upload a video file (MP4, MOV, AVI, WebM, …)
4. Click **🚀 Analyse Video**
5. Watch the live progress log
6. Read the rendered summary or download it

---

## 🔧 Using the CLI

```bash
# Basic usage (English output)
python cli.py lecture.mp4

# Specify output language
python cli.py lecture.mp4 --language French

# Save to a file
python cli.py lecture.mp4 --language Japanese --output summary.md

# Custom instructions
python cli.py interview.mp4 --language Spanish \
    --instructions "Focus heavily on the Q&A section"

# Use a higher-quality model
python cli.py tutorial.mp4 --model gemini-1.5-pro --language German

# Print raw markdown
python cli.py video.mp4 --raw

# Override API key for this run only
python cli.py video.mp4 --api-key AIza...

# See all options
python cli.py --help
```

---

## 🧪 Running Tests

```powershell
# Windows (isolates the venv from system-level google.* namespace packages)
$env:PYTHONNOUSERSITE=1; $env:PYTHONPATH=''
.venv\Scripts\python -m pytest test_pipeline.py -v
```

```bash
# macOS / Linux
PYTHONNOUSERSITE=1 PYTHONPATH='' .venv/bin/python -m pytest test_pipeline.py -v
```

> **Why the env flags?** The Gemini IDE and other Google tools may install a
> global `google` namespace package that shadows `google-genai` in the venv.
> `PYTHONNOUSERSITE=1` prevents user site-packages from bleeding in.
> A `conftest.py` handles this automatically when pytest is invoked via the
> venv's Python.

All tests are mocked — no API key required to run the test suite.

---

## ⚙️ Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | *(required)* | Your Gemini API key |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Model to use |
| `MAX_VIDEO_SIZE_MB` | `500` | Maximum upload size |
| `TEMP_DIR` | `./tmp_uploads` | Temp file directory |
| `DEFAULT_LANGUAGE` | `English` | Default summary language |
| `DEBUG` | `false` | Enable verbose logging |

---

## 🏗️ Architecture

```
User  ──►  Streamlit UI / CLI
              │
              ▼
         file_handler.py
         • Validate extension & size
         • Save to tmp_uploads/ (chunked)
              │
              ▼
         gemini_client.py
         • Upload via Files API
         • Poll until ACTIVE (max 5 min)
         • Generate content with system prompt
         • Delete remote file (finally block)
              │
              ▼
         prompts.py
         • SYSTEM_PROMPT (Educational Synthesizer)
         • build_user_prompt(language, extras)
              │
              ▼
         SummaryResult
         • summary_markdown
         • Download / display
```

---

## 📊 Supported Languages

English · Spanish · French · German · Portuguese · Italian · Dutch · Russian ·
Japanese · Korean · Chinese (Simplified) · Chinese (Traditional) · Arabic ·
Hindi · Turkish · Polish · Swedish · Danish · Norwegian · Finnish

---

## 📝 Output Schema

The AI always returns a structured Markdown report containing:

- **📋 Metadata** — language, duration, domain, difficulty, content type  
- **🗂️ Table of Contents** — timestamped sections  
- **📝 Executive Summary** — 3–5 sentence overview  
- **🔑 Key Concepts & Definitions** — with context and examples  
- **📚 Section-by-Section Breakdown** — bullet points + quotes  
- **💡 Insights & Takeaways** — actionable ideas  
- **❓ Reflection Questions** — 5 open-ended questions for learners  
- **🌐 Cross-Cultural Notes** — idioms / cultural references (when applicable)  
- **🏷️ Tags & Keywords** — for discoverability  

---

## 🔒 Privacy & Security

- API keys are never logged or stored beyond the session.  
- Uploaded video files are deleted from local disk immediately after the Gemini upload completes.  
- Remote files on the Gemini Files API are deleted after generation (they expire automatically within 48 h anyway).  
- No database or persistent storage is used anywhere.  

---

## 📄 License

MIT License — see `LICENSE` for details.
