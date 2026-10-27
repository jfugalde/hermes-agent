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
import logging
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

USAGE_URL = "https://ollama.com/api/usage"
# Switch away from a key once its quota reaches this fraction consumed.
# 0.95 = skip a key that has burned 95% of its metered quota window.
DEFAULT_QUOTA_THRESHOLD = 0.95
CACHE_FILENAME = "ollama-quota-daily.json"

_ENV_KEYS = ("OLLAMA_API_KEY", "OLLAMA_API_KEY_FALLBACK", "OLLAMA_API_KEY_2")

# Ollama removed the 0..1 ``limits`` meter from ``/api/usage`` (Oct 2026): the
# response now carries money/requests over a range (``totals`` + ``buckets``)
# and no quota fraction at all. The only constrained quantity still observable
# is spend, so a key whose plan caps a 7-DAY window is scored empirically:
# ``fraction = trailing_7d_spend_usd / cap_usd``. The cap is per key (plans
# differ, and an unmetered key must NOT be given someone else's cap), supplied
# as ``<ENV_NAME>_CAP_USD`` next to the key itself — e.g.
# ``OLLAMA_API_KEY_CAP_USD=110`` in ``$HERMES_HOME/.env``.
#
# ``usage_usd`` from the default ``range=7d`` response IS trailing-7d spend, so
# no window arithmetic is needed. A key without a configured cap reports UNKNOWN
# (None, never skip proactively) and is covered by the reactive 429 rotation.
CAP_USD_SUFFIX = "_CAP_USD"


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


def _load_caps() -> dict[str, float]:
    """Return {env_var: cap_usd} for every OLLAMA_*_KEY that declares a cap.

    A cap is a property of the ACCOUNT's plan, not of the profile, so it is read
    from the same ``$HERMES_HOME/.env`` that supplies the key (with the process
    environment as an override, mirroring ``_load_env_keys``). Absence is
    meaningful: no cap means "we cannot compute a fraction", never "unlimited".
    """
    caps: dict[str, float] = {}

    def _record(name: str, raw: str) -> None:
        if not name.endswith(CAP_USD_SUFFIX):
            return
        key_name = name[: -len(CAP_USD_SUFFIX)]
        if key_name not in _ENV_KEYS:
            return
        try:
            value = float(str(raw).strip().strip('"').strip("'"))
        except (TypeError, ValueError):
            return
        if value > 0:
            caps[key_name] = value

    env_file = _hermes_home() / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, val = line.partition("=")
            _record(name.strip(), val)

    for key_name in _ENV_KEYS:
        raw = (os.environ.get(key_name + CAP_USD_SUFFIX) or "").strip()
        if raw:
            _record(key_name + CAP_USD_SUFFIX, raw)

    return caps


def _trailing_spend_usd(data: dict) -> Optional[float]:
    """Dollars spent in the response's window (``totals``, else summed buckets).

    The endpoint reports spend over a range (7d by default). ``totals.usage_usd``
    is authoritative when present; summing ``buckets[].usage_usd`` is the fallback
    for a response that carries only buckets. Accounts on plans that do not meter
    cost return neither — that is UNKNOWN, not zero.
    """
    totals = data.get("totals")
    if isinstance(totals, dict):
        spend = totals.get("usage_usd")
        if spend is not None:
            try:
                return float(spend)
            except (TypeError, ValueError):
                pass
    buckets = data.get("buckets")
    if isinstance(buckets, list):
        values: list[float] = []
        for bucket in buckets:
            if not isinstance(bucket, dict) or bucket.get("usage_usd") is None:
                continue
            try:
                values.append(float(bucket["usage_usd"]))
            except (TypeError, ValueError):
                continue
        if values:
            return sum(values)
    return None


def _fetch_usage(key: str, env_name: Optional[str] = None) -> Optional[float]:
    """Live query of quota fraction (0..1) for one key, or None if unknown.

    Ollama Cloud keys are not all on the same plan: some accounts meter a
    ``weekly`` window, others only a ``monthly`` one, and some expose both.
    Reading a single bucket silently reports 0.0 for every key that does not
    use it — an account already out of monthly quota then looks pristine and
    the pool keeps selecting it.

    So take the highest consumption across the buckets the response actually
    carries: the binding constraint is whichever window is closest to its cap.

    When the response exposes NO recognised meter at all (an absent/empty
    ``limits`` object, or one that carries neither a ``weekly`` nor a
    ``monthly`` bucket), the answer is ``None`` — genuinely UNKNOWN, not
    healthy. Reporting ``0.0`` there would claim a health we cannot observe;
    ``None`` lets the caller stay conservative (do not proactively skip) while
    the reactive 429 rotation backstops.
    """
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    limits = data.get("limits") or {}
    found_bucket = False
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
        found_bucket = True
        usage = entry.get("usage")
        if usage is None:
            continue
        try:
            highest = max(highest, float(usage))
        except (TypeError, ValueError):
            continue
    if not found_bucket:
        # Neither a ``weekly`` nor a ``monthly`` bucket is present. Two very
        # different situations produce that shape:
        #   * a pre-Oct-2026 response with no long-horizon window -> UNKNOWN;
        #   * the CURRENT response (``range/scope/totals/buckets``), where the
        #     fraction meter is gone entirely and spend is the only signal.
        # In the second case score the key empirically against its own declared
        # cap; without a cap we cannot know how full it is, so stay UNKNOWN
        # (None) rather than fabricate health.
        cap = _load_caps().get(env_name) if env_name else None
        if cap:
            spend = _trailing_spend_usd(data)
            if spend is not None:
                return spend / cap
        return None
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


def _snapshot_from_usage(usage_by_env: dict[str, str], usage: dict[str, Optional[float]]) -> dict:
    def _recorded(name: str) -> Optional[float]:
        value = usage.get(name)
        # An unknown usage is persisted as JSON null rather than a fabricated
        # 0.0. The read side treats null as "do not skip"; ``fp`` is unchanged.
        return None if value is None else round(value, 4)

    keys = {
        name: {"usage": _recorded(name), "fp": _fingerprint(secret)}
        for name, secret in usage_by_env.items()
    }
    return {"date": _today_utc(), "updated_ts": datetime.now(timezone.utc).timestamp(), "keys": keys}


def refresh_daily_cache() -> dict:
    """Fill/refresh the daily cache with a live query of every known key.

    Call this on the first request of the day (or when the cache is stale).
    Network failure is tolerated: a failing key is recorded as UNKNOWN
    (``None``, JSON null) so it stays available and the reactive 429 rotation
    backstops, and a key that cannot be reached is not dropped from the pool.
    """
    keys = _load_env_keys()
    caps = _load_caps()
    usage_by_env: dict[str, Optional[float]] = {}
    for name, secret in keys.items():
        try:
            usage_by_env[name] = _fetch_usage(secret, env_name=name)
        except Exception:
            # Unreachable / unparseable: unknown, NOT healthy. Do not fabricate
            # a 0.0 that would fake a health we never observed.
            usage_by_env[name] = None
    unknown = sorted(name for name, value in usage_by_env.items() if value is None)
    if unknown:
        # Say WHICH unknown this is. "/api/usage changed shape" and "this key's
        # plan does not meter dollars" have the same effect on the pool but very
        # different fixes, and a vague warning sent readers hunting the wrong one.
        no_cap = [name for name in unknown if name not in caps]
        no_spend = [name for name in unknown if name in caps]
        details = []
        if no_cap:
            details.append(
                "no cap configured (set %s in $HERMES_HOME/.env)"
                % ", ".join(f"{name}{CAP_USD_SUFFIX}=<usd>" for name in no_cap)
            )
        if no_spend:
            details.append(
                "cap configured but the account reports no usage_usd for %s"
                % ", ".join(no_spend)
            )
        logger.warning(
            "Ollama quota cache: %s has/have UNKNOWN quota state — %s, so "
            "proactive skip is disabled for these key(s); the reactive 429 "
            "rotation still covers them.",
            ", ".join(unknown),
            "; ".join(details) or "no readable quota signal",
        )
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
            cached = cached_keys.get(name)
            usage = cached.get("usage") if isinstance(cached, dict) else None
            status[name] = _status_from_usage(usage, threshold)
        return status
    except Exception:
        return {name: STATUS_OK for name in _load_env_keys()}


def _status_from_usage(usage: Optional[float], threshold: float) -> str:
    if usage is None:
        # UNKNOWN is NOT unhealthy: we cannot observe a meter, so never skip.
        # Selection behaviour is identical to the old fabricated 0.0.
        return STATUS_OK
    if usage >= 1.0:
        return STATUS_EXHAUSTED
    if usage >= threshold:
        return STATUS_AT_RISK
    return STATUS_OK
