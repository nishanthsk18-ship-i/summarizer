#!/usr/bin/env bash
# ==============================================================================
# scripts/deploy.sh — Production deployment script for Multimodal AI Media Summarizer
#
# USAGE: Run from the project ROOT directory, not from scripts/:
#   bash scripts/deploy.sh
#
# This script references all paths relative to the repo root (CWD).
# ==============================================================================
set -euo pipefail

# Guard: abort if not run from the project root (where app.py lives).
if [ ! -f "app.py" ]; then
    echo "❌ Error: This script must be run from the project root directory."
    echo "   Usage: bash scripts/deploy.sh"
    exit 1
fi

echo "======================================================================"
echo "🚀 Production Deployment Script for Multimodal AI Media Summarizer"
echo "======================================================================"

# Check for environment file
if [ ! -f ".env" ]; then
    echo "⚠️ .env file not found! Copying from .env.example..."
    cp .env.example .env
    echo "❗ Please configure your GEMINI_API_KEY in .env before proceeding."
    exit 1
fi

# Run test suite before deployment
echo "🧪 Running Pytest suite..."
if command -v pytest &> /dev/null; then
    pytest -v --tb=short
elif [ -f ".venv/bin/pytest" ]; then
    .venv/bin/pytest -v --tb=short
else
    echo "ℹ️ Pytest not found in PATH; skipping pre-build test run."
fi

# Build & launch docker container (Dockerfile now lives in docker/)
echo "📦 Building and starting Docker container..."
docker compose -f docker/docker-compose.yml down || true
docker compose -f docker/docker-compose.yml build --no-cache
docker compose -f docker/docker-compose.yml up -d

echo "======================================================================"
echo "✅ Deployment successful! App is live at http://localhost:8501"
echo "======================================================================"
