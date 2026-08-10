"""
database.py — SQLite-backed custom API key management.

Provides key generation, read-only existence checking (key_exists),
and quota-consuming validation (validate_and_use_key) for access control.
"""

import sqlite3
import secrets
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# Resolve DB path relative to THIS file so it works regardless of cwd
DB_PATH = Path(__file__).resolve().parent / "keys.db"

def init_db() -> None:
    """Initialize the SQLite database and create the api_keys table if it doesn't exist."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_string TEXT UNIQUE NOT NULL,
                usage_count INTEGER DEFAULT 0,
                max_quota INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()

def generate_key(max_quota: int = 10) -> str:
    """Generate a new secure custom API key and insert it into the database."""
    # Create a 32-byte secure random string, prefixed for our app
    raw_secret = secrets.token_urlsafe(24)
    new_key = f"live_{raw_secret}"
    
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO api_keys (key_string, max_quota) VALUES (?, ?)",
            (new_key, max_quota)
        )
        conn.commit()
    
    logger.info("Generated new API key with quota: %d", max_quota)
    return new_key

def validate_and_use_key(key_string: str, max_retries: int = 3) -> bool:
    """
    Validate if a key exists and has remaining quota.
    If valid, atomically increments usage_count and returns True.
    If invalid or quota exceeded, returns False.

    Uses BEGIN IMMEDIATE to prevent TOCTOU race conditions under concurrent
    access. Retries with exponential backoff on sqlite3.OperationalError (DB lock contention).
    """
    import time
    if not key_string:
        return False

    for attempt in range(max_retries):
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                # IMMEDIATE lock prevents concurrent writes between SELECT and UPDATE
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.cursor()

                cursor.execute(
                    "SELECT id, usage_count, max_quota FROM api_keys WHERE key_string = ?",
                    (key_string,)
                )
                row = cursor.fetchone()

                if not row:
                    logger.warning("Invalid API Key attempted.")
                    conn.rollback()
                    return False

                key_id, usage_count, max_quota = row

                if usage_count >= max_quota:
                    logger.warning("API Key quota exceeded (ID: %s)", key_id)
                    conn.rollback()
                    return False

                # Valid: Increment usage atomically within the same transaction
                cursor.execute(
                    "UPDATE api_keys SET usage_count = usage_count + 1 WHERE id = ?",
                    (key_id,)
                )
                conn.commit()
                return True
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < max_retries - 1:
                time.sleep(0.1 * (2 ** attempt))
                continue
            logger.error("Database lock error in validate_and_use_key: %s", exc)
            return False
    return False

def key_exists(key_string: str, max_retries: int = 3) -> bool:
    """
    Read-only check: returns True if the key exists and has remaining quota.
    Does NOT increment usage_count — safe to call on every UI rerender.
    Retries with backoff on DB lock contention.
    """
    import time
    if not key_string:
        return False
    for attempt in range(max_retries):
        try:
            with sqlite3.connect(DB_PATH, timeout=5) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM api_keys WHERE key_string = ? AND usage_count < max_quota",
                    (key_string,)
                )
                return cursor.fetchone() is not None
        except sqlite3.OperationalError:
            if attempt < max_retries - 1:
                time.sleep(0.05 * (2 ** attempt))
                continue
            return False
    return False


def get_active_key() -> str:
    """Return an active API key with available quota, or create a fresh one if none exists."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT key_string FROM api_keys WHERE usage_count < max_quota ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row:
            return str(row[0])
            
    # If no valid key exists, generate a new default key with 100 uses
    return generate_key(max_quota=100)

# Ensure DB is initialized when this module is imported
init_db()
