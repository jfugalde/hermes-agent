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


def test_fetch_usage_ignores_session_bucket(monkeypatch):
    """The 6-hour rolling ``session`` window must NOT count as quota.

    Regression: ``session`` is Ollama Cloud's 6-hour rolling window, which
    self-heals on a 6h cycle. Caching it in the DAILY quota cache would skip
    the primary key for up to 24h on a window that already rolled over,
    defeating the "prefer primary, return on reset" failover policy. Only the
    long-horizon ``weekly``/``monthly`` windows may drive proactive skip; the
    6h window is left to the reactive 429 rotation.
    """
    from agent import ollama_quota_cache as qc

    payload = {
        "limits": {
            "weekly": {"usage": 0.10},
            "session": {"usage": 0.95},  # near-cap on the 6h window — must be ignored
        }
    }

    class _Resp:
        def read(self_inner):
            return json.dumps(payload).encode()

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

    monkeypatch.setattr(qc.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert qc._fetch_usage("sk-anything") == pytest.approx(0.10)


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


def test_two_at_risk_keys_still_serve_one(tmp_path, monkeypatch):
    """Two AT-RISK keys must not eliminate each other.

    Regression: ``can_skip_at_risk`` counted AT_RISK keys as "usable" when
    deciding whether an at-risk key may be skipped, while the loop then skipped
    AT_RISK keys.  With both keys at-risk each was skipped on account of the
    other, ``available`` came back empty and ``select()`` returned None — a
    hard outage while BOTH keys still had quota left (usage in
    [threshold, 1.0)).  Only a strictly healthier (STATUS_OK) sibling may
    justify skipping an at-risk key.
    """
    _patch_status(
        monkeypatch,
        {"OLLAMA_API_KEY": "at_risk", "OLLAMA_API_KEY_FALLBACK": "at_risk"},
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
    assert avail, "both keys at-risk must not empty the pool"
    assert pool.select() is not None


def test_at_risk_skipped_only_for_a_healthy_sibling(tmp_path, monkeypatch):
    """An at-risk key yields only to a STATUS_OK key, never to another at-risk.

    Exhausted siblings must not count as healthier either — otherwise a spent
    partner key would hide the at-risk one and empty the pool.
    """
    # at-risk + exhausted -> the at-risk key is the best available, keep it.
    _patch_status(
        monkeypatch,
        {"OLLAMA_API_KEY": "exhausted", "OLLAMA_API_KEY_FALLBACK": "at_risk"},
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

    # at-risk + ok -> yield the at-risk key to the healthy one.
    _patch_status(
        monkeypatch,
        {"OLLAMA_API_KEY": "at_risk", "OLLAMA_API_KEY_FALLBACK": "ok"},
    )
    avail2, _p2 = pool._available_entries()
    assert [e.label for e in avail2] == ["OLLAMA_API_KEY_FALLBACK"]


def test_cache_write_is_atomic_under_concurrent_readers(tmp_path, monkeypatch):
    """A concurrent reader must never observe a partial/empty cache file.

    Pins the invariant, not a counter: while one thread keeps replacing the
    cache, every concurrent reader gets either a parseable snapshot or no file
    at all -- never a truncated one. The pre-fix ``path.write_text()`` truncates
    before writing, so readers hit empty/partial content and raise
    ``json.JSONDecodeError``; this test fails on that implementation and passes
    on the atomic ``os.replace`` one.
    """
    import threading

    from agent import ollama_quota_cache as qc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = qc._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"keys": {"OLLAMA_API_KEY": "ok"}}), encoding="utf-8")

    torn: list[str] = []
    stop = threading.Event()

    def writer() -> None:
        payload = {"keys": {f"K{i}": {"usage": 0.1 * (i % 9)} for i in range(200)}}
        while not stop.is_set():
            qc._write_cache(payload)

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                torn.append(str(exc))
            except FileNotFoundError:
                # os.replace() swaps atomically; a briefly-absent path is a
                # legitimate race outcome, a CORRUPT read is not.
                pass

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(4)
    ]
    for t in threads:
        t.daemon = True
        t.start()
    time.sleep(2)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert torn == [], f"cache readers observed {len(torn)} torn reads: {torn[:3]}"
