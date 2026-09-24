"""Ollama session-limit holds the spent key, and restore uses the other one."""

import time
from unittest.mock import MagicMock, patch

from agent.credential_pool import (
    AUTH_TYPE_API_KEY,
    STATUS_EXHAUSTED,
    STATUS_OK,
    CredentialPool,
    PooledCredential,
    _normalize_error_context,
)
from tests.run_agent.test_primary_runtime_restore import _make_agent


def test_session_usage_limit_sets_six_hour_reset(monkeypatch):
    monkeypatch.setattr("agent.credential_pool.time.time", lambda: 1_000_000.0)

    normalized = _normalize_error_context({
        "message": (
            "you (example) have reached your session usage limit, "
            "upgrade for higher limits: https://ollama.com/upgrade"
        ),
    })

    assert normalized["reset_at"] == 1_000_000.0 + 6 * 60 * 60


def test_other_billing_text_does_not_invent_a_reset():
    normalized = _normalize_error_context({
        "message": "insufficient credits",
    })
    assert "reset_at" not in normalized


def test_session_phrase_without_ollama_host_does_not_stamp_six_hours():
    normalized = _normalize_error_context({
        "message": "you have reached your session usage limit",
    })
    assert "reset_at" not in normalized


def _entry(entry_id, token, priority, status, reset_at=None):
    return PooledCredential(
        provider="ollama-cloud",
        id=entry_id,
        label=entry_id,
        auth_type=AUTH_TYPE_API_KEY,
        priority=priority,
        source="env",
        access_token=token,
        last_status=status,
        last_status_at=1.0,
        last_error_code=429 if status == STATUS_EXHAUSTED else None,
        last_error_reset_at=reset_at,
    )


def test_fill_first_skips_primary_until_session_reset():
    primary = _entry("primary", "primary-key", 0, STATUS_EXHAUSTED, time.time() + 1000)
    fallback = _entry("fallback", "fallback-key", 1, STATUS_OK)
    pool = CredentialPool("ollama-cloud", [primary, fallback])

    picked = pool.select()

    assert picked is not None
    assert picked.id == "fallback"
    assert picked.runtime_api_key == "fallback-key"


def test_restore_keeps_available_key_when_snapshot_key_is_exhausted():
    agent = _make_agent(
        provider="ollama-cloud",
        base_url="https://ollama.com/v1",
        fallback_model={"provider": "cursor-go", "model": "gpt-5.4-nano-low"},
    )
    agent._primary_runtime["model"] = "glm-5.2"
    agent._primary_runtime["api_key"] = "primary-key"
    agent.model = "gemma4:31b"
    agent._fallback_activated = True
    agent._rate_limited_until = 0

    class _Entry:
        provider = "ollama-cloud"
        id = "fallback"
        label = "OLLAMA_API_KEY_FALLBACK"
        runtime_api_key = "fallback-key"
        access_token = "fallback-key"
        base_url = "https://ollama.com/v1"

    class _Pool:
        provider = "ollama-cloud"

        def has_available(self):
            return True

        def select(self):
            return _Entry()

    agent._credential_pool = _Pool()
    agent._swap_credential = MagicMock()

    with patch("run_agent.OpenAI", return_value=MagicMock()):
        restored = agent._restore_primary_runtime()

    assert restored is True
    assert agent.model == "glm-5.2"
    agent._swap_credential.assert_called_once()
    assert agent._swap_credential.call_args.args[0].runtime_api_key == "fallback-key"
