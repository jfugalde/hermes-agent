#!/usr/bin/env python3
"""SQLite-backed TTL response cache for Hermes scripts.

Stores deterministic function responses (API fetches, LLM prompts, rendered
reports) in ~/.hermes/state.db so cron jobs and repeated calls can reuse
recent results instead of paying for identical work.

Usage:
    from response_cache import ttl_cached

    @ttl_cached(ttl_seconds=900)
    def fetch_expensive_data(arg: str) -> dict:
        ...

Or manual:
    from response_cache import get, set

    found, value = get("my-key")
    if not found:
        value = compute()
        set("my-key", value, ttl_seconds=600)
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_DB = HERMES_HOME / "state.db"
DEFAULT_TTL = 300  # 5 minutes


def _ensure_table() -> None:
    """Create the cache table and supporting index if missing; enable WAL."""
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompt_cache (
                key TEXT PRIMARY KEY,
                raw_key TEXT,
                value TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                hit_count INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prompt_cache_expires "
            "ON prompt_cache(expires_at)"
        )
        conn.commit()
    finally:
        conn.close()


def _serialize(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str, ensure_ascii=False)


def _deserialize(text: str) -> Any:
    return json.loads(text)


def _make_key(*args: Any, **kwargs: Any) -> str:
    """Stable SHA-256 hash of positional + keyword arguments."""
    payload = json.dumps(
        (args, kwargs), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get(key: str) -> tuple[bool, Any]:
    """Return (found, value). If found is True, value is cached and fresh."""
    _ensure_table()
    now = datetime.now(timezone.utc).timestamp()
    conn = sqlite3.connect(STATE_DB, timeout=30.0)
    try:
        row = conn.execute(
            "SELECT value, expires_at FROM prompt_cache WHERE key = ?",
            (key,),
        ).fetchone()
        if row:
            value, expires_at = row
            if now < expires_at:
                conn.execute(
                    "UPDATE prompt_cache SET hit_count = hit_count + 1 WHERE key = ?",
                    (key,),
                )
                conn.commit()
                return True, _deserialize(value)
        return False, None
    finally:
        conn.close()


def set(key: str, value: Any, ttl_seconds: int = DEFAULT_TTL) -> None:
    """Store value with TTL."""
    _ensure_table()
    now = datetime.now(timezone.utc).timestamp()
    expires = now + ttl_seconds
    conn = sqlite3.connect(STATE_DB, timeout=30.0)
    try:
        conn.execute(
            """
            INSERT INTO prompt_cache (key, raw_key, value, created_at, expires_at, hit_count)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(key) DO UPDATE SET
                raw_key = excluded.raw_key,
                value = excluded.value,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                hit_count = 0
            """,
            (key, key, _serialize(value), now, expires),
        )
        conn.commit()
    finally:
        conn.close()


def delete(key: str) -> bool:
    """Remove a single entry. Returns True if an entry was deleted."""
    _ensure_table()
    conn = sqlite3.connect(STATE_DB, timeout=30.0)
    try:
        cur = conn.execute("DELETE FROM prompt_cache WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def purge_expired() -> int:
    """Delete expired entries. Returns number deleted."""
    _ensure_table()
    now = datetime.now(timezone.utc).timestamp()
    conn = sqlite3.connect(STATE_DB, timeout=30.0)
    try:
        cur = conn.execute("DELETE FROM prompt_cache WHERE expires_at <= ?", (now,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def ttl_cached(
    ttl_seconds: int = DEFAULT_TTL,
    key_fn: Callable[..., str] | None = None,
) -> Callable[[F], F]:
    """Decorator that caches a function's return value for ttl_seconds."""
    def decorator(func: F) -> F:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            cache_key = (
                key_fn(*args, **kwargs)
                if key_fn
                else _make_key(func.__name__, *args, **kwargs)
            )
            found, cached = get(cache_key)
            if found:
                return cached
            result = func(*args, **kwargs)
            set(cache_key, result, ttl_seconds=ttl_seconds)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
