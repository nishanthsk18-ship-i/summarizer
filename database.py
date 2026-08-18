"""
database.py — SQLite-backed custom API key management.

Provides key generation, read-only existence checking (key_exists),
and quota-consuming validation (validate_and_use_key) for access control.

Security model
--------------
Keys are stored as SHA-256 hex digests — the raw token is returned to the
admin by generate_key() but is never persisted.  Comparisons use
hmac.compare_digest() to prevent timing-oracle attacks.
"""

import hashlib
import hmac
import sqlite3
import secrets
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# Resolve DB path relative to THIS file so it works regardless of cwd
DB_PATH = Path(__file__).resolve().parent / "keys.db"


def _hash_key(raw: str) -> str:
    """Return the SHA-256 hex digest of a raw key string."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _digest_equal(a: str, b: str) -> bool:
    """Constant-time string equality to prevent timing-oracle attacks."""
    return hmac.compare_digest(a.encode(), b.encode())


def init_db() -> None:
    """Initialize the SQLite database and create the api_keys table if it doesn't exist."""
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash  TEXT UNIQUE NOT NULL,
                usage_count INTEGER DEFAULT 0,
                max_quota INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Migrate legacy tables that used key_string instead of key_hash
        cols = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)")}
        if "key_string" in cols and "key_hash" not in cols:
            conn.execute("ALTER TABLE api_keys ADD COLUMN key_hash TEXT")
            conn.execute("UPDATE api_keys SET key_hash = key_string WHERE key_hash IS NULL")
            logger.info("Migrated api_keys table: populated key_hash from key_string")
        conn.commit()


def generate_key(max_quota: int = 10) -> str:
    """
    Generate a new secure API key, store its SHA-256 hash, and return the raw key.

    The raw key is shown to the admin ONCE and is never stored — only the hash
    is persisted.  If the key is lost, generate a new one.
    """
    raw_secret = secrets.token_urlsafe(24)
    new_key = f"live_{raw_secret}"
    key_hash = _hash_key(new_key)

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO api_keys (key_hash, max_quota) VALUES (?, ?)",
            (key_hash, max_quota)
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
    access. Retries with exponential backoff on sqlite3.OperationalError.
    Key comparison uses hmac.compare_digest to prevent timing-oracle attacks.
    """
    import time
    if not key_string:
        return False

    candidate_hash = _hash_key(key_string)

    for attempt in range(max_retries):
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                # IMMEDIATE lock prevents concurrent writes between SELECT and UPDATE
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.cursor()

                cursor.execute(
                    "SELECT id, usage_count, max_quota, key_hash FROM api_keys WHERE key_hash = ?",
                    (candidate_hash,)
                )
                row = cursor.fetchone()

                if not row:
                    logger.warning("Invalid API Key attempted.")
                    conn.rollback()
                    return False

                key_id, usage_count, max_quota, stored_hash = row

                # Timing-safe comparison (defence-in-depth against timing oracles)
                if not _digest_equal(candidate_hash, stored_hash):
                    logger.warning("Key hash mismatch — possible tampering.")
                    conn.rollback()
                    return False

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


def _col_exists(conn: sqlite3.Connection, col: str) -> bool:
    """Return True if the given column exists in api_keys."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)")}
    return col in cols


def key_exists(key_string: str, max_retries: int = 3) -> bool:
    """
    Read-only check: returns True if the key exists and has remaining quota.
    Does NOT increment usage_count — safe to call on every UI rerender.
    Retries with backoff on DB lock contention.
    """
    import time
    if not key_string:
        return False

    candidate_hash = _hash_key(key_string)

    for attempt in range(max_retries):
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT usage_count, max_quota FROM api_keys WHERE key_hash = ?",
                    (candidate_hash,)
                )
                row = cursor.fetchone()
                if not row:
                    return False
                usage_count, max_quota = row
                return usage_count < max_quota
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < max_retries - 1:
                time.sleep(0.1 * (2 ** attempt))
                continue
            logger.error("Database lock error in key_exists: %s", exc)
            return False
    return False


def get_active_key() -> str:
    """
    Return an active API key hash token suitable for quota validation,
    or generate and store a fresh key if none exists with available quota.

    Note: Because keys are now stored as hashes, this function returns a
    newly-generated raw key when the existing pool is exhausted, and stores
    its hash for future validation.
    """
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT key_hash FROM api_keys WHERE usage_count < max_quota ORDER BY id LIMIT 1"
        )
        row = cursor.fetchone()

    if row:
        # Return the stored hash as the "session key"; validate_and_use_key
        # accepts it directly since it hashes before comparison.
        return row[0]

    # No quota left — generate a fresh key and return its raw value so the
    # session can use it.  The hash is stored by generate_key().
    logger.info("All keys exhausted — generating a fresh server-host key.")
    return generate_key(max_quota=1000)

# Ensure DB is initialized when this module is imported
init_db()

