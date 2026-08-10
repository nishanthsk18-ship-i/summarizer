# ==============================================================================
# Production Dockerfile — Multi-stage build for Multimodal AI Media Summarizer
#
# Stage 1 (builder): Installs build-essential + compiles any C-extension wheels.
# Stage 2 (runtime): Copies only the installed packages + FFmpeg.
#                    build-essential is NOT present in the final image,
#                    reducing image size by ~200 MB and attack surface.
# ==============================================================================

# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# Install compile-time dependencies (not copied to runtime image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
# Build wheels into a local directory so the runtime stage can install them
# without needing a compiler.
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

# Only runtime system dependencies: FFmpeg + curl (for HEALTHCHECK)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install pre-built wheels from the builder stage (no compiler needed)
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
    && rm -rf /wheels

# Copy application source
COPY . .

# Create required directories and lock down to non-root user
RUN mkdir -p .tmp tmp_uploads \
    && useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8501

# Healthcheck against Streamlit's built-in health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl --fail http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["streamlit", "run", "app.py", \
            "--server.port=8501", \
            "--server.address=0.0.0.0", \
            "--server.headless=true"]
