"""Ollama Cloud daily quota cache — fast availability lookup for the credential pool.

Why this exists
---------------
The credential pool rotates Ollama Cloud keys reactively: when a request to the
selected key returns 429 (quota exhausted), Hermes marks it and rotates to the
next key. That works but pays the price of one failed request per exhausted key.

This module makes the pool PROACTIVE. It maintains a small daily cache file
mapping each OLLAMA_*_KEY env var -> its weekly quota usage (0..1) + healthy
flag. The pool reads this file on the hot path (fast, local, no network). The
file is refreshed once per UTC day — the "first request of the day" becomes the
cache filler — so availability is decided from a local read for the rest of the
day instead of a live API call per request.

File location: $HERMES_HOME/cache/ollama-quota-daily.json
  HERMES_HOME defaults to ~/.hermes (same convention as the quota watchdog).

Schema:
    {
      "date": "2026-08-06",            # UTC date the snapshot was taken
      "updated_ts": 1722941234.0,
      "keys": {
        "OLLAMA_API_KEY":          {"usage": 0.969, "healthy": false},
        "OLLAMA_API_KEY_FALLBACK": {"usage": 0.0,   "healthy": true}
      }
    }

A key is "healthy" = its weekly usage fraction is below OLLAMA_QUOTA_THRESHOLD.
Stdlib + urllib only; no hermes deps, so importing this module cannot create an
import cycle with the credential pool.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://ollama.com/api/usage"
# Switch away from a key once its weekly quota reaches this fraction consumed.
# 0.95 = skip a key that has burned 95% of its weekly quota. Tune via env.
DEFAULT_QUOTA_THRESHOLD = 0.95
CACHE_FILENAME = "ollama-quota-daily.json"

_ENV_KEYS = ("OLLAMA_API_KEY", "OLLAMA_API_KEY_FALLBACK", "OLLAMA_API_KEY_2")


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _cache_path() -> Path:
    return _hermes_home() / "cache" / CACHE_FILENAME


def _threshold() -> float:
    try:
        return float(os.environ.get("OLLAMA_QUOTA_THRESHOLD", DEFAULT_QUOTA_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_QUOTA_THRESHOLD


def _load_env_keys() -> dict[str, str]:
    """Return {env_var: secret} for every OLLAMA_*_KEY we can resolve.

    Mirrors the watchdog's key loading: prefer the process environment, then
    fall back to $HERMES_HOME/.env (the authoritative file `hermes setup`
    writes). Returns only non-empty secrets.
    """
    secrets: dict[str, str] = {}

    def _grab(env_file: Path | None) -> None:
        if env_file is not None and env_file.is_file():
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if "=" not in line:
                    continue
                name, _, val = line.partition("=")
                name = name.strip()
                if name not in _ENV_KEYS:
                    continue
                val = val.strip().strip('"').strip("'")
                if val:
                    secrets.setdefault(name, val)

    # Prefer explicit env (gateway sets the active profile's keys in env).
    for name in _ENV_KEYS:
        v = (os.environ.get(name) or "").strip()
        if v:
            secrets.setdefault(name, v)
    # Fall back to the .env file for any OLLAMA_* key not already in env.
    _grab(_hermes_home() / ".env")
    return secrets


def _fetch_usage(key: str) -> float:
    """Live query of weekly quota fraction (0..1) for one key."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    weekly = (data.get("limits") or {}).get("weekly") or {}
    usage = weekly.get("usage")
    return float(usage) if usage is not None else 0.0


def _read_cache() -> dict | None:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(snapshot: dict) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    except OSError:
        pass  # cache is best-effort; a failed write degrades to a live fetch


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _cache_stale(cache: dict | None) -> bool:
    """The cache is stale when missing, malformed, or from a previous UTC day.

    This is the "re-read on first [request of the day]" trigger: one network
    call to refill the file, then local reads for the rest of the day.
    """
    if not isinstance(cache, dict):
        return True
    if cache.get("date") != _today_utc():
        return True
    return not isinstance(cache.get("keys"), dict)


def _snapshot_from_usage(usage_by_env: dict[str, float]) -> dict:
    threshold = _threshold()
    keys = {
        name: {
            "usage": round(usage, 4),
            "healthy": usage < threshold,
        }
        for name, usage in usage_by_env.items()
    }
    return {"date": _today_utc(), "updated_ts": datetime.now(timezone.utc).timestamp(), "keys": keys}


def refresh_daily_cache() -> dict:
    """Fill/refresh the daily cache with a live query of every known key.

    Call this on the first request of the day (or when the cache is stale).
    Network failure is tolerated: we return a cache whose keys are all marked
    healthy (so the pool keeps working and the reactive 429 rotation backstops).
    """
    keys = _load_env_keys()
    usage_by_env: dict[str, float] = {}
    for name, secret in keys.items():
        try:
            usage_by_env[name] = _fetch_usage(secret)
        except Exception:
            # A single key failing to report shouldn't sink the snapshot.
            usage_by_env[name] = 0.0  # assume healthy; reactive 429 catches it
    snapshot = _snapshot_from_usage(usage_by_env)
    _write_cache(snapshot)
    return snapshot


def get_exhausted_env_vars() -> set[str]:
    """Return the set of OLLAMA_*_KEY env var names that are over quota.

    Fast path: reads the local daily cache. If the cache is missing or from a
    previous day, performs ONE network refresh (the day's cache filler). Never
    raises — returns an empty set on any failure so the pool keeps functioning.
    """
    try:
        cache = _read_cache()
        if _cache_stale(cache):
            cache = refresh_daily_cache()
        keys = (cache or {}).get("keys") or {}
        return {
            name
            for name, meta in keys.items()
            if isinstance(meta, dict) and not meta.get("healthy", True)
        }
    except Exception:
        return set()
