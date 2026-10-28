"""Ollama Cloud 429 uses an hourly drain-first bench; quota cache honesty.

Two behaviours are pinned here:

1. A 429 on ``ollama-cloud`` benches the key for an hour, not a minute, and it
   does so BEFORE the billing branch — Ollama Cloud meters a weekly window whose
   429 the classifier labels as billing. The explicit policy is drain-first: the
   weekly-refilling key is priority-0 and is drained first, so a 429 means
   "window spent", and an hourly re-probe returns the pool to that key
   automatically once the window refills. The old one-minute bench re-hit a
   spent key on every turn (~5,000 log lines over three days) and cost the
   profile one failed call per turn instead of one per hour. The bench is
   tunable at runtime via ``HERMES_OLLAMA_429_TTL_SECONDS``. Other providers
   (and the provider-less default) keep the historical TTL, and a
   provider-supplied ``last_error_reset_at`` hint still wins over the TTL.

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


EXPECTED_OLLAMA_429_BENCH_SECONDS = 3600


def _ollama_entry(*, last_status_at: float, reset_at=None) -> "cp.PooledCredential":
    payload = {
        "id": "cred-ollama",
        "label": "OLLAMA_API_KEY",
        "auth_type": "api_key",
        "priority": 0,
        "source": "env:OLLAMA_API_KEY",
        "access_token": "sk-ollama-test-token",
        "base_url": "https://ollama.com/v1",
        "last_status": "exhausted",
        "last_status_at": last_status_at,
        "last_error_code": 429,
    }
    if reset_at is not None:
        payload["last_error_reset_at"] = reset_at
    return cp.PooledCredential.from_dict("ollama-cloud", payload)


def test_ollama_cloud_429_benches_for_an_hour(monkeypatch):
    """A plain 429 on ollama-cloud benches the key for one hour."""
    monkeypatch.delenv(cp.OLLAMA_CLOUD_429_TTL_ENV, raising=False)
    assert cp.EXHAUSTED_TTL_OLLAMA_CLOUD_429_SECONDS == EXPECTED_OLLAMA_429_BENCH_SECONDS
    assert (
        cp._exhausted_ttl(429, provider="ollama-cloud")
        == EXPECTED_OLLAMA_429_BENCH_SECONDS
    )


def test_ollama_cloud_billing_429_benches_for_an_hour(monkeypatch):
    """The REAL case: Ollama Cloud's weekly-limit 429 is classified as billing.

    The ollama-cloud branch must sit BEFORE the billing logic and still yield the
    drain-first hour — billing is exactly what a spent weekly window looks like.
    """
    monkeypatch.delenv(cp.OLLAMA_CLOUD_429_TTL_ENV, raising=False)
    assert (
        cp._exhausted_ttl(429, provider="ollama-cloud", failure_reason="billing")
        == EXPECTED_OLLAMA_429_BENCH_SECONDS
    )


def test_ollama_cloud_429_ttl_env_override_is_honoured(monkeypatch):
    """HERMES_OLLAMA_429_TTL_SECONDS retunes the bench on a running install."""
    monkeypatch.setenv(cp.OLLAMA_CLOUD_429_TTL_ENV, "900")
    assert cp.OLLAMA_CLOUD_429_TTL_ENV == "HERMES_OLLAMA_429_TTL_SECONDS"
    assert cp._ollama_cloud_429_ttl() == pytest.approx(900.0)
    assert cp._exhausted_ttl(429, provider="ollama-cloud") == pytest.approx(900.0)
    assert cp._exhausted_ttl(
        429, provider="ollama-cloud", failure_reason="billing"
    ) == pytest.approx(900.0)


@pytest.mark.parametrize("bad_value", ["", "   ", "not-a-number", "0", "-1", "-3600"])
def test_ollama_cloud_429_ttl_env_garbage_falls_back(monkeypatch, bad_value):
    """Garbage, zero and negative values fall back to the one-hour default."""
    monkeypatch.setenv(cp.OLLAMA_CLOUD_429_TTL_ENV, bad_value)
    assert cp._ollama_cloud_429_ttl() == float(EXPECTED_OLLAMA_429_BENCH_SECONDS)
    assert (
        cp._exhausted_ttl(429, provider="ollama-cloud")
        == EXPECTED_OLLAMA_429_BENCH_SECONDS
    )


def test_other_provider_429_keeps_default_ttl(monkeypatch):
    """Regression: a non-Ollama 429 is untouched by the ollama-cloud branch."""
    monkeypatch.delenv(cp.OLLAMA_CLOUD_429_TTL_ENV, raising=False)
    assert cp._exhausted_ttl(429, provider="anthropic") == cp.EXHAUSTED_TTL_429_SECONDS


def test_providerless_429_keeps_default_ttl(monkeypatch):
    """Regression: the default (no provider) keeps the historical TTL."""
    monkeypatch.delenv(cp.OLLAMA_CLOUD_429_TTL_ENV, raising=False)
    assert cp._exhausted_ttl(429) == cp.EXHAUSTED_TTL_429_SECONDS


def test_exhausted_until_honours_ollama_cloud_hourly_bench(monkeypatch):
    """End-to-end through ``_exhausted_until``: a 429-benched Ollama key reopens
    in ~an hour."""
    monkeypatch.delenv(cp.OLLAMA_CLOUD_429_TTL_ENV, raising=False)
    now = time.time()
    entry = _ollama_entry(last_status_at=now)
    until = cp._exhausted_until(entry)
    assert until is not None
    assert until == pytest.approx(now + EXPECTED_OLLAMA_429_BENCH_SECONDS, abs=2.0), (
        f"_exhausted_until returned now+{until - now:.0f}s — expected the "
        "one-hour drain-first bench for an Ollama Cloud 429"
    )


def test_exhausted_until_prefers_provider_reset_hint_over_bench(monkeypatch):
    """A provider-supplied reset hint still wins over the hourly TTL."""
    monkeypatch.delenv(cp.OLLAMA_CLOUD_429_TTL_ENV, raising=False)
    now = time.time()
    hint = now + 120
    entry = _ollama_entry(last_status_at=now, reset_at=hint)
    until = cp._exhausted_until(entry)
    assert until is not None
    assert until == pytest.approx(hint, abs=1e-3)
    assert abs(until - (now + EXPECTED_OLLAMA_429_BENCH_SECONDS)) > 1.0


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
