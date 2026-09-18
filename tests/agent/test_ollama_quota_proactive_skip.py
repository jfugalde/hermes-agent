"""Proactive Ollama Cloud quota skip in the credential pool.

Covers the failure mode where two Ollama Cloud keys sit on DIFFERENT quota
plans (one metered weekly, one monthly).  The reactive-only pool kept handing
out a key whose weekly window was already 100% spent, paying a 429 per attempt
and benching the healthy key behind it, so the whole fleet went dark while a
usable credential sat in the same pool.

Two behaviours are pinned here:
  1. ``_fetch_usage`` reads the highest bucket the account exposes, so a
     monthly-metered key is not reported as pristine (0.0).
  2. ``_available_entries`` skips an exhausted key outright, and skips an
     at-risk key only when a healthier one exists (so a lone nearly-spent key
     still serves).
"""

from __future__ import annotations

import json
import time

import pytest


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _env_entry(cred_id: str, env_name: str, *, priority: int) -> dict:
    return {
        "id": cred_id,
        "label": env_name,
        "auth_type": "api_key",
        "priority": priority,
        "source": f"env:{env_name}",
        "access_token": f"sk-test-{env_name}",
        "base_url": "https://ollama.com/v1",
        "last_status": None,
        "last_status_at": None,
        "last_error_code": None,
    }


def _load_pool(tmp_path, monkeypatch, entries: list[dict]):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {"version": 1, "credential_pool": {"ollama-cloud": entries}},
    )
    from agent.credential_pool import load_pool

    return load_pool("ollama-cloud")


# --- _fetch_usage: bucket-aware -------------------------------------------------


def test_fetch_usage_reads_monthly_bucket(monkeypatch):
    """A monthly-only account must not read as 0.0 (the pre-fix behaviour)."""
    from agent import ollama_quota_cache as qc

    payload = {"limits": {"monthly": {"usage": 0.437, "models": []}}}

    class _Resp:
        def read(self_inner):
            return json.dumps(payload).encode()

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

    monkeypatch.setattr(qc.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert qc._fetch_usage("sk-anything") == pytest.approx(0.437)


def test_fetch_usage_takes_highest_bucket(monkeypatch):
    """When both windows exist, the binding constraint (highest) wins."""
    from agent import ollama_quota_cache as qc

    payload = {"limits": {"weekly": {"usage": 0.10}, "monthly": {"usage": 0.90}}}

    class _Resp:
        def read(self_inner):
            return json.dumps(payload).encode()

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

    monkeypatch.setattr(qc.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert qc._fetch_usage("sk-anything") == pytest.approx(0.90)


def test_fetch_usage_absent_limits_is_zero(monkeypatch):
    """No limits object (or malformed usage) degrades to 0.0, not a crash."""
    from agent import ollama_quota_cache as qc

    class _Resp:
        def read(self_inner):
            return json.dumps({"limits": {"weekly": {"usage": None}}}).encode()

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

    monkeypatch.setattr(qc.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert qc._fetch_usage("sk-anything") == 0.0


# --- _available_entries: proactive skip -----------------------------------------


def _patch_status(monkeypatch, mapping: dict[str, str]):
    """Force the quota lookup so the test never touches the network."""
    from agent import credential_pool as cp

    monkeypatch.setattr(cp, "_get_ollama_key_status", lambda threshold=0.95: dict(mapping))


def test_exhausted_key_is_skipped_and_healthy_key_selected(tmp_path, monkeypatch):
    """The spent key is never handed out while a usable one exists."""
    _patch_status(
        monkeypatch,
        {"OLLAMA_API_KEY": "exhausted", "OLLAMA_API_KEY_FALLBACK": "ok"},
    )
    pool = _load_pool(
        tmp_path,
        monkeypatch,
        [
            _env_entry("cred-primary", "OLLAMA_API_KEY", priority=0),
            _env_entry("cred-fallback", "OLLAMA_API_KEY_FALLBACK", priority=1),
        ],
    )
    avail, _pending = pool._available_entries()
    assert [e.label for e in avail] == ["OLLAMA_API_KEY_FALLBACK"]
    assert pool.select().label == "OLLAMA_API_KEY_FALLBACK"


def test_at_risk_key_skipped_only_when_healthier_exists(tmp_path, monkeypatch):
    """At-risk + a healthy sibling -> skip; at-risk alone -> keep serving."""
    _patch_status(
        monkeypatch,
        {"OLLAMA_API_KEY": "at_risk", "OLLAMA_API_KEY_FALLBACK": "ok"},
    )
    pool = _load_pool(
        tmp_path,
        monkeypatch,
        [
            _env_entry("cred-primary", "OLLAMA_API_KEY", priority=0),
            _env_entry("cred-fallback", "OLLAMA_API_KEY_FALLBACK", priority=1),
        ],
    )
    avail, _pending = pool._available_entries()
    assert [e.label for e in avail] == ["OLLAMA_API_KEY_FALLBACK"]

    # Sole at-risk key: keeping it beats returning nothing with quota left.
    _patch_status(monkeypatch, {"OLLAMA_API_KEY": "at_risk"})
    pool2 = _load_pool(
        tmp_path,
        monkeypatch,
        [_env_entry("cred-primary", "OLLAMA_API_KEY", priority=0)],
    )
    avail2, _pending2 = pool2._available_entries()
    assert [e.label for e in avail2] == ["OLLAMA_API_KEY"]


def test_all_keys_ok_when_quota_lookup_fails(tmp_path, monkeypatch):
    """A quota-lookup failure must not remove keys from the pool."""
    _patch_status(monkeypatch, {})  # helper's own everything-OK-on-failure shape
    pool = _load_pool(
        tmp_path,
        monkeypatch,
        [
            _env_entry("cred-primary", "OLLAMA_API_KEY", priority=0),
            _env_entry("cred-fallback", "OLLAMA_API_KEY_FALLBACK", priority=1),
        ],
    )
    avail, _pending = pool._available_entries()
    assert len(avail) == 2


def test_non_ollama_provider_never_consults_quota(tmp_path, monkeypatch):
    """The skip is scoped to ollama-cloud; other pools are untouched."""
    from agent import credential_pool as cp

    calls = {"n": 0}

    def _boom(threshold=0.95):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(cp, "_get_ollama_key_status", _boom)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openrouter": [
                    {
                        "id": "cred-1",
                        "label": "cred-1",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "manual",
                        "access_token": "sk-test-openrouter",
                        "base_url": "https://openrouter.ai/api/v1",
                        "last_status": None,
                    }
                ]
            },
        },
    )
    pool = cp.load_pool("openrouter")
    pool._available_entries()
    assert calls["n"] == 0
