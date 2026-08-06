"""
conftest.py — pytest configuration for the Multilingual AI Video Summarizer.

Fixes the Google namespace package shadowing issue on systems where a global
`google` package (e.g., from Gemini CLI tools) is on PYTHONPATH and shadows
the venv's `google-genai`.

This file is auto-loaded by pytest before any test collection.
"""

import os
import sys

# Ensure user site-packages and any stale PYTHONPATH entries don't
# shadow packages installed in the active virtual environment.
os.environ.setdefault("PYTHONNOUSERSITE", "1")

# Remove any PYTHONPATH entries that could introduce a conflicting
# `google` namespace package (e.g., from globally-installed Gemini CLI).
# We keep the venv's site-packages (already on sys.path by the venv).
venv_site = None
for p in sys.path:
    if ".venv" in p and "site-packages" in p:
        venv_site = p
        break

# Purge any google namespace conflicts before collection starts
if "google" in sys.modules:
    # Force re-import from the correct venv location
    google_mod = sys.modules["google"]
    if venv_site and not any(venv_site in str(pp) for pp in getattr(google_mod, "__path__", [])):
        # The loaded google package is NOT from our venv — purge it
        to_remove = [k for k in sys.modules if k == "google" or k.startswith("google.")]
        for key in to_remove:
            del sys.modules[key]
