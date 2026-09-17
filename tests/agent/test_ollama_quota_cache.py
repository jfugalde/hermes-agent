"""Tests for the Ollama Cloud quota cache (per-account windows + status)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from agent import ollama_quota_cache as qc


def _write_cache(tmp_path, payload: dict) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / qc.CACHE_FILENAME).write_text(json.dumps(payload))


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """Isolate the module-level in-flight flag between tests.

    maybe_refresh_in_background() guards a process-global, and a daemon thread
    spawned by one test can still be in flight when the next begins (it may even
    hold _refresh_lock during a real network call if a monkeypatch was unwound
    mid-flight). Without this reset, tests leak state into each other.
    """
    qc._refresh_in_flight = False
    yield
    # Give any in-flight worker a bounded chance to release, then force it.
    for _ in range(50):
        if not qc._refresh_in_flight:
            break
        time.sleep(0.1)
    qc._refresh_in_flight = False


def _snapshot(keys: dict) -> dict:
    return {"updated_ts": datetime.now(timezone.utc).timestamp(), "keys": keys}


def test_status_from_usage_pro_weekly_at_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Pro account: weekly >= 0.80 -> at_risk
    assert qc._status_from_usage({"weekly": 0.85, "session": 0.1}, qc._ACCOUNT_CONFIG["OLLAMA_API_KEY"]) == qc.STATUS_AT_RISK


def test_status_from_usage_pro_session_at_risk(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Pro account: session >= 0.90 -> at_risk even if weekly is low
    assert qc._status_from_usage({"weekly": 0.1, "session": 0.95}, qc._ACCOUNT_CONFIG["OLLAMA_API_KEY"]) == qc.STATUS_AT_RISK


def test_status_from_usage_pro_exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert qc._status_from_usage({"weekly": 1.0, "session": 0.1}, qc._ACCOUNT_CONFIG["OLLAMA_API_KEY"]) == qc.STATUS_EXHAUSTED


def test_status_from_usage_max_monthly(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Max account: monthly >= 0.80 -> at_risk
    assert qc._status_from_usage({"monthly": 0.9}, qc._ACCOUNT_CONFIG["OLLAMA_API_KEY_FALLBACK"]) == qc.STATUS_AT_RISK


def test_status_from_usage_max_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert qc._status_from_usage({"monthly": 0.24}, qc._ACCOUNT_CONFIG["OLLAMA_API_KEY_FALLBACK"]) == qc.STATUS_OK


def test_get_status_reads_cache_not_live(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Seed a fresh cache with a Pro key at weekly 0.9 (at_risk) and Max at monthly 0.5 (ok)
    _write_cache(tmp_path, _snapshot({
        "OLLAMA_API_KEY": {"weekly": 0.9, "session": 0.1, "fp": "a1b2c3d4e5f6"},
        "OLLAMA_API_KEY_FALLBACK": {"monthly": 0.5, "fp": "f7e8d9c0b1a2"},
    }))
    # Monkeypatch _load_env_keys to return matching fingerprints
    monkeypatch.setattr(qc, "_load_env_keys", lambda: {
        "OLLAMA_API_KEY": "x" * 57,
        "OLLAMA_API_KEY_FALLBACK": "y" * 57,
    })
    monkeypatch.setattr(qc, "_fingerprint", lambda k: "a1b2c3d4e5f6" if k.startswith("x") else "f7e8d9c0b1a2")
    # Ensure no live fetch happens
    monkeypatch.setattr(qc, "refresh_cache", lambda: pytest.fail("should not refresh fresh cache"))
    status = qc.get_ollama_key_status()
    assert status["OLLAMA_API_KEY"] == qc.STATUS_AT_RISK
    assert status["OLLAMA_API_KEY_FALLBACK"] == qc.STATUS_OK


def test_cached_reader_never_refreshes(tmp_path, monkeypatch):
    """The hot-path cached reader must never do network I/O.

    Even with a stale/missing cache it returns STATUS_OK for every key and
    never calls refresh_cache — the pool calls this under its lock and must
    not block on the network.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # No cache file at all (stale/missing)
    monkeypatch.setattr(qc, "_load_env_keys", lambda: {
        "OLLAMA_API_KEY": "x" * 57,
        "OLLAMA_API_KEY_FALLBACK": "y" * 57,
    })
    monkeypatch.setattr(qc, "refresh_cache", lambda: pytest.fail("cached reader must not refresh"))
    status = qc.get_ollama_key_status_cached()
    assert status == {
        "OLLAMA_API_KEY": qc.STATUS_OK,
        "OLLAMA_API_KEY_FALLBACK": qc.STATUS_OK,
    }


def test_maybe_refresh_background_spawns_thread_when_stale(tmp_path, monkeypatch):
    """maybe_refresh_in_background spawns a daemon thread only when stale."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # No cache -> stale -> should spawn a thread
    monkeypatch.setattr(qc, "refresh_cache", lambda: {"refreshed": True})
    started = []
    monkeypatch.setattr(
        qc.threading.Thread,
        "start",
        lambda self: started.append(self),
    )
    qc.maybe_refresh_in_background()
    assert len(started) == 1, "expected a background refresh thread when cache is stale"


def test_maybe_refresh_background_noop_when_fresh(tmp_path, monkeypatch):
    """maybe_refresh_in_background does nothing when the cache is fresh."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_cache(tmp_path, _snapshot({
        "OLLAMA_API_KEY": {"weekly": 0.1, "session": 0.05, "fp": "a1b2c3d4e5f6"},
    }))
    monkeypatch.setattr(qc, "refresh_cache", lambda: pytest.fail("should not refresh fresh cache"))
    started = []
    monkeypatch.setattr(
        qc.threading.Thread,
        "start",
        lambda self: started.append(self),
    )
    qc.maybe_refresh_in_background()
    assert started == [], "expected no refresh thread when cache is fresh"


def test_get_status_refreshes_stale_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Stale cache (old timestamp) -> triggers refresh
    _write_cache(tmp_path, {
        "updated_ts": time.time() - qc.CACHE_TTL_SECONDS - 10,
        "keys": {"OLLAMA_API_KEY": {"weekly": 0.1, "fp": "a1b2c3d4e5f6"}},
    })
    monkeypatch.setattr(qc, "_load_env_keys", lambda: {"OLLAMA_API_KEY": "x" * 57})
    monkeypatch.setattr(qc, "_fingerprint", lambda k: "a1b2c3d4e5f6")
    monkeypatch.setattr(qc, "_fetch_usage", lambda k: {"weekly": 0.2, "session": 0.05})
    status = qc.get_ollama_key_status()
    assert status["OLLAMA_API_KEY"] == qc.STATUS_OK


def test_fetch_usage_parses_windows(monkeypatch):
    """_fetch_usage returns only the windows the account exposes."""
    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({
                "limits": {
                    "weekly": {"usage": 0.073},
                    "session": {"usage": 0.178},
                }
            }).encode()
    class FakeUrlopen:
        def __call__(self, *a, **kw):
            return FakeResp()
    monkeypatch.setattr(qc.urllib.request, "urlopen", FakeUrlopen())
    usage = qc._fetch_usage("fake-key")
    assert usage == {"weekly": 0.073, "session": 0.178}


def test_concurrent_refresh_calls_are_coalesced(tmp_path, monkeypatch):
    """A burst of concurrent stale-cache checks must fire exactly ONE refresh.

    The pool calls maybe_refresh_in_background() from its per-request hot path.
    _refresh_lock alone only SERIALIZES refreshes, so before the in-flight guard
    every concurrent caller queued on the lock and fired its own network probe —
    a thundering herd. Regression guard for that bug.
    """
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_cache(tmp_path, {                      # genuinely stale
        "updated_ts": time.time() - qc.CACHE_TTL_SECONDS - 10,
        "keys": {},
    })

    probes = []
    lock = threading.Lock()

    def slow_fetch(_secret):                      # simulate network latency
        with lock:
            probes.append(1)
        time.sleep(0.25)
        return {"weekly": 0.01, "session": 0.01}

    monkeypatch.setattr(qc, "_load_env_keys", lambda: {"OLLAMA_API_KEY": "k" * 57})
    monkeypatch.setattr(qc, "_fetch_usage", slow_fetch)
    # _write_cache is left real so the refresh actually makes the cache fresh.

    threads = [threading.Thread(target=qc.maybe_refresh_in_background) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for _ in range(60):                           # bounded wait for the worker
        if not qc._refresh_in_flight:
            break
        time.sleep(0.1)

    assert len(probes) == 1, f"expected 1 coalesced refresh, got {len(probes)}"
    assert not qc._cache_stale(qc._read_cache()), "refresh should have left a fresh cache"
    assert qc._refresh_in_flight is False, "in-flight flag must be released"


def test_refresh_flag_released_after_failure(tmp_path, monkeypatch):
    """A failing refresh must not wedge the in-flight flag permanently."""
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_cache(tmp_path, {
        "updated_ts": time.time() - qc.CACHE_TTL_SECONDS - 10,
        "keys": {},
    })

    def boom(_secret):
        raise RuntimeError("network down")

    monkeypatch.setattr(qc, "_load_env_keys", lambda: {"OLLAMA_API_KEY": "k" * 57})
    monkeypatch.setattr(qc, "_fetch_usage", boom)
    # refresh_cache tolerates a failing key; force the whole refresh to raise.
    monkeypatch.setattr(qc, "_write_cache", lambda snap: (_ for _ in ()).throw(RuntimeError("disk")))

    threading.Thread(target=qc.maybe_refresh_in_background).start()
    for _ in range(60):                           # bounded wait for the worker
        if not qc._refresh_in_flight:
            break
        time.sleep(0.1)

    assert qc._refresh_in_flight is False, "flag must be cleared even when the refresh raises"
