"""Ollama Cloud quota cache — fast availability lookup for the credential pool.

Why this exists
---------------
The credential pool rotates Ollama Cloud keys reactively: when a request to the
selected key returns 429 (quota exhausted), Hermes marks it and rotates to the
next key. That works but pays the price of one failed request per exhausted key.

This module makes the pool PROACTIVE. It maintains a small cache file mapping
each OLLAMA_*_KEY env var -> its quota usage across the windows that account
actually exposes. The pool reads this file on the hot path (fast, local, no
network). The file is refreshed once per hour — the "first request of the
hour" becomes the cache filler — so availability is decided from a local read
for the rest of the hour instead of a live API call per request.

Account windows (verified live 2026-09-08)
------------------------------------------
Ollama Cloud exposes different usage windows per account tier:

    OLLAMA_API_KEY          (Pro, rolling)  -> weekly + session
    OLLAMA_API_KEY_FALLBACK (Max, $300)     -> monthly

The Pro account refills its weekly/session windows on a rolling basis, so it is
the key to burn first. The Max account is a finite monthly spend, so it is the
reserve. The cache stores whichever windows each key reports and derives status
per account from the correct window(s).

File location: $HERMES_HOME/cache/ollama-quota.json
  HERMES_HOME defaults to ~/.hermes (same convention as the quota watchdog).

Schema:
    {
      "updated_ts": 1722941234.0,
      "keys": {
        "OLLAMA_API_KEY":          {"weekly": 0.014, "session": 0.008, "fp": "a1b2c3d4e5f6"},
        "OLLAMA_API_KEY_FALLBACK": {"monthly": 0.235, "fp": "f7e8d9c0b1a2"}
      }
    }

Each entry stores the RAW usage fractions plus a non-secret fingerprint of the
key, so a key rotation mid-hour invalidates that entry (a fresh key inherits no
stale status). The "healthy" status is derived at read time from the current
threshold — never baked into the cache — so tuning the threshold never requires
a cache refresh.

Refresh is guarded by a module-level threading lock so concurrent callers
(several model calls arriving as the cache expires) do not each fire a network
probe; one refreshes, the rest read the stale-but-valid cache.

Stdlib + urllib only; no hermes deps, so importing this module cannot create an
import cycle with the credential pool.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USAGE_URL = "https://ollama.com/api/usage"
CACHE_FILENAME = "ollama-quota.json"
# Refresh the cache once per hour (Pro weekly/session windows move on a
# sub-hourly basis; monthly moves slowly but the hourly cadence keeps the
# reserve's spend fresh without hammering the API).
CACHE_TTL_SECONDS = 60 * 60

_ENV_KEYS = ("OLLAMA_API_KEY", "OLLAMA_API_KEY_FALLBACK", "OLLAMA_API_KEY_2")

# Per-account window + threshold config. The Pro account (primary) refills its
# rolling windows, so it is the key to burn first; the Max account (fallback) is
# a finite monthly spend, so it is the reserve. Thresholds:
#   Pro  weekly >= 0.80  -> at_risk (skip if a healthier key exists)
#   Pro  session >= 0.90 -> at_risk
#   Max  monthly >= 0.80 -> at_risk
# Exhausted (>= 1.0) on any tracked window always skips the key.
_ACCOUNT_CONFIG = {
    "OLLAMA_API_KEY": {
        "windows": ("weekly", "session"),
        "at_risk": {"weekly": 0.80, "session": 0.90},
    },
    "OLLAMA_API_KEY_FALLBACK": {
        "windows": ("monthly",),
        "at_risk": {"monthly": 0.80},
    },
    "OLLAMA_API_KEY_2": {
        "windows": ("weekly", "session"),
        "at_risk": {"weekly": 0.80, "session": 0.90},
    },
}

# Status strings returned by get_ollama_key_status().
STATUS_OK = "ok"
STATUS_AT_RISK = "at_risk"  # crossed threshold but not exhausted — skip only if a healthier key exists
STATUS_EXHAUSTED = "exhausted"  # usage >= 1.0 — always skip

_refresh_lock = threading.Lock()
# Compare-and-set guard so only ONE refresh is ever in flight per process.
# _refresh_lock alone only SERIALIZES refreshes — it does not collapse them:
# N threads that all observe a stale cache will each queue on the lock and each
# fire a network probe in turn. The pool calls this from its per-request hot
# path, so under concurrency that is a thundering herd of redundant probes.
_refresh_in_flight = False


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


def _fetch_usage(key: str) -> dict[str, float]:
    """Live query of usage fractions (0..1) for one key, per exposed window.

    Returns {window: usage} for every window the account reports (weekly,
    session, monthly, ...). A window the account does not expose is absent.
    """
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    limits = data.get("limits") or {}
    usage: dict[str, float] = {}
    for window, payload in limits.items():
        if not isinstance(payload, dict):
            continue
        val = payload.get("usage")
        if val is not None:
            try:
                usage[window] = float(val)
            except (TypeError, ValueError):
                continue
    return usage


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


def _cache_stale(cache: dict | None) -> bool:
    """The cache is stale when missing, malformed, or older than the TTL."""
    if not isinstance(cache, dict):
        return True
    updated_ts = cache.get("updated_ts")
    if not isinstance(updated_ts, (int, float)):
        return True
    if datetime.now(timezone.utc).timestamp() - float(updated_ts) > CACHE_TTL_SECONDS:
        return True
    return not isinstance(cache.get("keys"), dict)


def _snapshot_from_usage(usage_by_env: dict[str, str], usage: dict[str, dict[str, float]]) -> dict:
    keys: dict[str, dict] = {}
    for name, secret in usage_by_env.items():
        entry: dict = {"fp": _fingerprint(secret)}
        for window, val in usage.get(name, {}).items():
            entry[window] = round(val, 4)
        keys[name] = entry
    return {
        "updated_ts": datetime.now(timezone.utc).timestamp(),
        "keys": keys,
    }


def refresh_cache() -> dict:
    """Fill/refresh the cache with a live query of every known key.

    Guarded by a module-level lock so concurrent callers do not each fire a
    network probe. Network failure is tolerated: a failing key is recorded with
    no usage (so it stays available and the reactive 429 rotation backstops),
    and a key that cannot be reached is not dropped from the pool.
    """
    with _refresh_lock:
        keys = _load_env_keys()
        usage_by_env: dict[str, dict[str, float]] = {}
        for name, secret in keys.items():
            try:
                usage_by_env[name] = _fetch_usage(secret)
            except Exception:
                usage_by_env[name] = {}  # assume healthy; reactive 429 catches it
        snapshot = _snapshot_from_usage(keys, usage_by_env)
        _write_cache(snapshot)
        return snapshot


# Backward-compatible alias for any caller of the old daily API.
refresh_daily_cache = refresh_cache


def _status_from_usage(usage: dict[str, float], account: dict) -> str:
    """Derive status for one key from its per-window usage + account config."""
    for window in account.get("windows", ()):
        val = usage.get(window, 0.0)
        if val >= 1.0:
            return STATUS_EXHAUSTED
    for window, threshold in account.get("at_risk", {}).items():
        if usage.get(window, 0.0) >= threshold:
            return STATUS_AT_RISK
    return STATUS_OK


def _status_from_cache(keys: dict[str, str], cached_keys: dict) -> dict[str, str]:
    """Derive {env_var: status} from a cache snapshot (no network I/O)."""
    status: dict[str, str] = {}
    for name in keys:
        cached = cached_keys.get(name)
        usage = cached if isinstance(cached, dict) else {}
        account = _ACCOUNT_CONFIG.get(name, {"windows": (), "at_risk": {}})
        status[name] = _status_from_usage(usage, account)
    return status


def get_ollama_key_status_cached() -> dict[str, str]:
    """Return {env_var: status} from the local cache WITHOUT any network I/O.

    This is the hot-path reader for the credential pool, which calls it under
    the pool lock. It must never block on the network: if the cache is missing,
    stale, or a key's fingerprint changed, it returns STATUS_OK for every key
    (so the pool keeps functioning and the reactive 429 rotation backstops).
    The cache is refreshed by ``get_ollama_key_status`` / ``refresh_cache``
    outside the lock.
    """
    try:
        keys = _load_env_keys()
        cache = _read_cache()
        cached_keys = (cache or {}).get("keys") or {}
        return _status_from_cache(keys, cached_keys)
    except Exception:
        return {name: STATUS_OK for name in _load_env_keys()}


def maybe_refresh_in_background() -> None:
    """Refresh the cache in a background thread if it is stale.

    Safe to call from the pool hot path: the staleness check is a cheap local
    file read, and the actual network refresh runs on a daemon thread so the
    caller never blocks.

    At most ONE refresh is in flight per process at a time. The in-flight flag
    is set under the same lock that guards the staleness decision, so a burst of
    concurrent callers (the pool selecting a credential on every request) spawns
    exactly one thread; the rest observe the flag and return. Without this,
    ``_refresh_lock`` would only serialize the refreshes — each caller would
    still fire its own redundant network probe, one after another.

    The flag is cleared by a ``finally`` in the worker so a failed refresh
    cannot wedge refreshes permanently. The thread is a daemon, discarded on
    process exit.
    """
    global _refresh_in_flight
    try:
        with _refresh_lock:
            if _refresh_in_flight:
                return
            if not _cache_stale(_read_cache()):
                return
            _refresh_in_flight = True
    except Exception:
        return

    def _run() -> None:
        global _refresh_in_flight
        try:
            refresh_cache()
        except Exception:
            # A background refresh failure must never surface as an unhandled
            # thread exception (it would land in logs as a bare traceback).
            # The reactive 429 rotation backstops a refresh that never landed,
            # and the cache keeps its previous contents.
            pass
        finally:
            _refresh_in_flight = False

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        # Thread spawn failed — release the flag so a later call can retry.
        _refresh_in_flight = False


def get_ollama_key_status() -> dict[str, str]:
    """Return {env_var: status} for each known Ollama key.

    Status is one of STATUS_OK / STATUS_AT_RISK / STATUS_EXHAUSTED and is
    derived at read time from the current per-account thresholds — never from a
    stale cache flag — so tuning the threshold never requires a cache refresh.

    This variant MAY refresh the cache (network I/O) when it is missing, older
    than the TTL, or a key's fingerprint changed. Use it off the pool hot path;
    the pool itself calls ``get_ollama_key_status_cached`` to avoid blocking
    under its lock. Never raises — returns STATUS_OK for every key on any
    failure so the pool keeps functioning (the reactive 429 rotation backstops).
    """
    try:
        keys = _load_env_keys()
        cache = _read_cache()
        cached_keys = (cache or {}).get("keys") or {}
        needs_refresh = _cache_stale(cache)
        if not needs_refresh:
            # A key whose fingerprint changed mid-hour must be re-probed so it
            # doesn't inherit a stale exhausted status. Refresh the whole file
            # once (cheap, one hour's first rotation).
            for name, secret in keys.items():
                cached = cached_keys.get(name)
                if not isinstance(cached, dict) or cached.get("fp") != _fingerprint(secret):
                    needs_refresh = True
                    break
        if needs_refresh:
            cache = refresh_cache()
            cached_keys = (cache or {}).get("keys") or {}
        return _status_from_cache(keys, cached_keys)
    except Exception:
        return {name: STATUS_OK for name in _load_env_keys()}
