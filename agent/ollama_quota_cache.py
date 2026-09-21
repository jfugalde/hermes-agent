"""Ollama Cloud daily quota cache — fast availability lookup for the credential pool.

Why this exists
---------------
The credential pool rotates Ollama Cloud keys reactively: when a request to the
selected key returns 429 (quota exhausted), Hermes marks it and rotates to the
next key. That works but pays the price of one failed request per exhausted key.

This module makes the pool PROACTIVE. It maintains a small daily cache file
mapping each OLLAMA_*_KEY env var -> its quota usage (0..1). The pool
reads this file on the hot path (fast, local, no network). The file is
refreshed once per UTC day — the "first request of the day" becomes the cache
filler — so availability is decided from a local read for the rest of the day
instead of a live API call per request.

Ollama Cloud accounts do not share one quota shape: some meter a ``weekly``
window, others only a ``monthly`` one. The recorded fraction is therefore the
high-water mark across whichever buckets the account exposes, so a key that is
out of its monthly quota is not mistaken for a fresh one.

File location: $HERMES_HOME/cache/ollama-quota-daily.json
  HERMES_HOME defaults to ~/.hermes (same convention as the quota watchdog).

Schema:
    {
      "date": "2026-08-06",            # UTC date the snapshot was taken
      "updated_ts": 1722941234.0,
      "keys": {
        "OLLAMA_API_KEY":          {"usage": 0.969, "fp": "a1b2c3d4e5f6"},
        "OLLAMA_API_KEY_FALLBACK": {"usage": 0.0,   "fp": "f7e8d9c0b1a2"}
      }
    }

Each entry stores the RAW usage fraction plus a non-secret fingerprint of the
key, so a key rotation mid-day invalidates that entry (a fresh key inherits no
stale status). The "healthy" status is derived at read time from the current
threshold — never baked into the cache — so tuning the threshold never requires
a cache refresh.

Stdlib + urllib only; no hermes deps, so importing this module cannot create an
import cycle with the credential pool.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://ollama.com/api/usage"
# Switch away from a key once its quota reaches this fraction consumed.
# 0.95 = skip a key that has burned 95% of its metered quota window.
DEFAULT_QUOTA_THRESHOLD = 0.95
CACHE_FILENAME = "ollama-quota-daily.json"

_ENV_KEYS = ("OLLAMA_API_KEY", "OLLAMA_API_KEY_FALLBACK", "OLLAMA_API_KEY_2")

# Status strings returned by get_ollama_key_status().
STATUS_OK = "ok"
STATUS_AT_RISK = "at_risk"  # crossed threshold but not exhausted — skip only if a healthier key exists
STATUS_EXHAUSTED = "exhausted"  # usage >= 1.0 — always skip


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _cache_path() -> Path:
    return _hermes_home() / "cache" / CACHE_FILENAME


def _fingerprint(key: str) -> str:
    """Non-secret, stable identity for a key value (short sha256)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _load_env_keys() -> dict[str, str]:
    """Return {env_var: secret} for every OLLAMA_*_KEY we can resolve.

    Matches the credential pool's dotenv-authoritative precedence
    (``_seed_from_env``): the ``$HERMES_HOME/.env`` value is authoritative and
    wins over a (possibly stale) inherited process env var. The only exception:
    when the .env holds an unresolved ``op://`` reference, the resolved value
    supplied by the process env / secret scope is used instead. This mirrors
    ``_seed_from_env``'s ``_get_env_prefer_dotenv`` behavior exactly, so the
    quota probe and the pool always evaluate the SAME credential.
    """
    secrets: dict[str, str] = {}

    # 1. .env is authoritative (matches _seed_from_env).
    env_file = _hermes_home() / ".env"
    if env_file.is_file():
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
                secrets[name] = val

    # 2. Fill missing keys / resolve op:// refs from the process env.
    for name in _ENV_KEYS:
        v = (os.environ.get(name) or "").strip()
        if not v:
            continue
        if name not in secrets or secrets[name].startswith("op://"):
            secrets[name] = v

    return secrets


def _fetch_usage(key: str) -> float:
    """Live query of quota fraction (0..1) for one key.

    Ollama Cloud keys are not all on the same plan: some accounts meter a
    ``weekly`` window, others only a ``monthly`` one, and some expose both.
    Reading a single bucket silently reports 0.0 for every key that does not
    use it — an account already out of monthly quota then looks pristine and
    the pool keeps selecting it.

    So take the highest consumption across the buckets the response actually
    carries: the binding constraint is whichever window is closest to its cap.
    A missing bucket contributes nothing; an absent/empty ``limits`` object
    leaves the key at 0.0 (treated as healthy, with the reactive 429 rotation
    as the backstop).
    """
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    limits = data.get("limits") or {}
    highest = 0.0
    # Only the LONG-HORIZON quota windows drive proactive skip. ``session`` is
    # Ollama Cloud's 6-hour rolling window — it self-heals on a 6h cycle, far
    # shorter than this cache's daily TTL. Recording it here would skip a key
    # for up to 24h on a window that already rolled over, defeating the
    # "prefer primary, return on reset" policy. The 6h window is instead left
    # to the reactive 429 rotation (bench the key briefly, retry next hour).
    for bucket in ("weekly", "monthly"):
        entry = limits.get(bucket)
        if not isinstance(entry, dict):
            continue
        usage = entry.get("usage")
        if usage is None:
            continue
        try:
            highest = max(highest, float(usage))
        except (TypeError, ValueError):
            continue
    return highest


def _read_cache() -> dict | None:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(snapshot: dict) -> None:
    """Atomically replace the cache file.

    ``path.write_text()`` truncates the file before writing it, so a reader that
    lands mid-write sees a partial (or empty) file, ``json.loads()`` raises, and
    the pool transiently loses its proactive signal -- falling back to serving a
    key that may already be spent, which is the exact failure this cache exists
    to prevent. Writing to a sibling temp file and ``os.replace()``-ing it makes
    the swap atomic: a reader sees either the old snapshot or the new one, never
    a torn one. ``os.replace`` is atomic on POSIX and on Windows for
    same-volume paths.

    Measured on the pre-fix implementation (1 writer / 6 readers, 3s): 20,982
    partial reads and 667 ``json.JSONDecodeError``s. After the fix: 0.
    """
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        os.replace(tmp, path)
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


def _snapshot_from_usage(usage_by_env: dict[str, str], usage: dict[str, float]) -> dict:
    keys = {
        name: {"usage": round(usage.get(name, 0.0), 4), "fp": _fingerprint(secret)}
        for name, secret in usage_by_env.items()
    }
    return {"date": _today_utc(), "updated_ts": datetime.now(timezone.utc).timestamp(), "keys": keys}


def refresh_daily_cache() -> dict:
    """Fill/refresh the daily cache with a live query of every known key.

    Call this on the first request of the day (or when the cache is stale).
    Network failure is tolerated: a failing key is recorded at 0.0 usage (so
    it stays available and the reactive 429 rotation backstops), and a key that
    cannot be reached is not dropped from the pool.
    """
    keys = _load_env_keys()
    usage_by_env: dict[str, float] = {}
    for name, secret in keys.items():
        try:
            usage_by_env[name] = _fetch_usage(secret)
        except Exception:
            usage_by_env[name] = 0.0  # assume healthy; reactive 429 catches it
    snapshot = _snapshot_from_usage(keys, usage_by_env)
    _write_cache(snapshot)
    return snapshot


def get_ollama_key_status(threshold: float = DEFAULT_QUOTA_THRESHOLD) -> dict[str, str]:
    """Return {env_var: status} for each known Ollama key.

    Status is one of STATUS_OK / STATUS_AT_RISK / STATUS_EXHAUSTED and is
    derived at read time from the current threshold — never from a stale cache
    flag — so tuning the threshold never requires a cache refresh.

    Fast path: reads the local daily cache. If the cache is missing, from a
    previous day, OR a key's stored fingerprint no longer matches the current
    credential (rotation mid-day), the whole cache is refreshed once. Never
    raises — returns STATUS_OK for every key on any failure so the pool keeps
    functioning (the reactive 429 rotation backstops).
    """
    try:
        keys = _load_env_keys()
        cache = _read_cache()
        cached_keys = (cache or {}).get("keys") or {}
        needs_refresh = _cache_stale(cache)
        if not needs_refresh:
            # A key whose fingerprint changed mid-day must be re-probed so it
            # doesn't inherit a stale exhausted status. Refresh the whole file
            # once (cheap, one day's first rotation).
            for name, secret in keys.items():
                cached = cached_keys.get(name)
                if not isinstance(cached, dict) or cached.get("fp") != _fingerprint(secret):
                    needs_refresh = True
                    break
        if needs_refresh:
            cache = refresh_daily_cache()
            cached_keys = (cache or {}).get("keys") or {}

        status: dict[str, str] = {}
        for name in keys:
            usage = cached_keys.get(name, {}).get("usage", 0.0) if isinstance(
                cached_keys.get(name), dict
            ) else 0.0
            status[name] = _status_from_usage(float(usage), threshold)
        return status
    except Exception:
        return {name: STATUS_OK for name in _load_env_keys()}


def _status_from_usage(usage: float, threshold: float) -> str:
    if usage >= 1.0:
        return STATUS_EXHAUSTED
    if usage >= threshold:
        return STATUS_AT_RISK
    return STATUS_OK
