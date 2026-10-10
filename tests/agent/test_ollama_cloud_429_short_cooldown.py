"""Ollama Cloud 429 uses a short cooldown; the quota cache stops faking health.

Two behaviours are pinned here:

1. A 429 on ``ollama-cloud`` benches the key for a minute, not an hour, and it
   does so BEFORE the billing branch — Ollama Cloud meters a weekly window whose
   429 the classifier labels as billing, which would otherwise force the full
   one-hour bench. Ollama Cloud bills per key, so a 429 attributed to the wrong
   (healthy) key must self-heal within a minute instead of starving the profile.
   Other providers (and the provider-less default) keep the historical TTL.

2. ``_fetch_usage`` answers ``None`` (UNKNOWN) — not ``0.0`` — when the endpoint
   exposes no recognisable weekly/monthly meter, so the cache stops claiming a
   health it never observed. Selection behaviour is unchanged: unknown reads as
   "do not skip", exactly like the old fabricated ``0.0``.
"""

from __future__ import annotations

import json
import time

import pytest

import agent.credential_pool as cp
from agent import ollama_quota_cache as qc


# --- _exhausted_ttl: Ollama Cloud 429 --------------------------------------------


def test_ollama_cloud_429_uses_short_ttl():
    """A plain 429 on ollama-cloud drops to the short Ollama-specific TTL."""
    assert (
        cp._exhausted_ttl(429, provider="ollama-cloud")
        == cp.EXHAUSTED_TTL_OLLAMA_CLOUD_429_SECONDS
    )


def test_ollama_cloud_billing_429_still_uses_short_ttl():
    """The REAL case: Ollama Cloud's weekly-limit 429 is classified as billing.

    The new branch must sit BEFORE the billing logic, otherwise the billing
    verdict forces the full one-hour bench.
    """
    assert (
        cp._exhausted_ttl(429, provider="ollama-cloud", failure_reason="billing")
        == cp.EXHAUSTED_TTL_OLLAMA_CLOUD_429_SECONDS
    )


def test_other_provider_429_keeps_default_ttl():
    """Regression: a non-Ollama 429 is untouched by the new branch."""
    assert cp._exhausted_ttl(429, provider="anthropic") == cp.EXHAUSTED_TTL_429_SECONDS


def test_providerless_429_keeps_default_ttl():
    """Regression: the default (no provider) keeps the historical TTL."""
    assert cp._exhausted_ttl(429) == cp.EXHAUSTED_TTL_429_SECONDS


def test_exhausted_until_honours_ollama_cloud_short_cooldown():
    """End-to-end through _exhausted_until: a 429-benched Ollama key reopens in
    ~a minute, not an hour."""
    now = time.time()
    entry = cp.PooledCredential.from_dict(
        "ollama-cloud",
        {
            "id": "cred-ollama",
            "label": "OLLAMA_API_KEY",
            "auth_type": "api_key",
            "priority": 0,
            "source": "env:OLLAMA_API_KEY",
            "access_token": "sk-test-ollama",
            "base_url": "https://ollama.com/v1",
            "last_status": "exhausted",
            "last_status_at": now,
            "last_error_code": 429,
        },
    )
    until = cp._exhausted_until(entry)
    assert until is not None
    assert until <= now + 120, (
        f"_exhausted_until returned now+{until - now:.0f}s — expected the short "
        "Ollama Cloud 429 cooldown, not the one-hour bench"
    )


# --- _fetch_usage: honest unknown ------------------------------------------------


def _mock_urlopen(monkeypatch, payload: dict) -> None:
    class _Resp:
        def read(self_inner):
            return json.dumps(payload).encode()

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

    monkeypatch.setattr(qc.urllib.request, "urlopen", lambda *a, **k: _Resp())


def test_fetch_usage_new_schema_without_limits_is_unknown(monkeypatch):
    """The NEW schema (range/totals/buckets, no ``limits`` key) exposes no
    recognised meter -> UNKNOWN (None), not a fabricated 0.0."""
    _mock_urlopen(
        monkeypatch,
        {
            "range": {"from": "2026-01-01", "to": "2026-01-31"},
            "totals": {"request_count": 1234},
            "buckets": [],
        },
    )
    assert qc._fetch_usage("sk-anything") is None


def test_fetch_usage_old_schema_still_reads_weekly(monkeypatch):
    """The OLD schema (limits.weekly.usage) keeps returning the fraction."""
    _mock_urlopen(monkeypatch, {"limits": {"weekly": {"usage": 1.0}}})
    assert qc._fetch_usage("sk-anything") == pytest.approx(1.0)


# --- cache honesty: unknown never skips -----------------------------------------


def test_status_from_usage_unknown_is_ok():
    """UNKNOWN must preserve selection exactly: treated as available."""
    assert qc._status_from_usage(None, qc.DEFAULT_QUOTA_THRESHOLD) == qc.STATUS_OK


def test_snapshot_records_null_for_unknown_usage(tmp_path, monkeypatch):
    """An unknown usage is persisted as JSON null (fp preserved), never 0.0."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    snapshot = qc._snapshot_from_usage(
        {"OLLAMA_API_KEY": "sk-test"},
        {"OLLAMA_API_KEY": None},
    )
    record = snapshot["keys"]["OLLAMA_API_KEY"]
    assert record["usage"] is None
    assert "fp" in record
    # Round-trips as JSON null, not a number.
    assert json.loads(json.dumps(record))["usage"] is None
