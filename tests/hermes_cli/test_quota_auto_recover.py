"""Quota exhaustion recovers in-process: same model, next key, fresh chain."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.chat_completion_helpers import try_activate_fallback
from agent.error_classifier import FailoverReason
from hermes_cli.fallback_config import (
    apply_fallback_chain_to_agent,
    refresh_agent_fallback_chain,
)


def test_refresh_replaces_frozen_chain(monkeypatch):
    agent = SimpleNamespace(
        _fallback_chain=[{"provider": "ollama-cloud", "model": "gemma4:31b"}],
        _fallback_model={"provider": "ollama-cloud", "model": "gemma4:31b"},
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
        _unavailable_fallback_keys={"old"},
    )

    def _chain(_cfg):
        return [
            {"provider": "ollama-cloud", "model": "deepseek-v4.1-flash"},
            {"provider": "cursor-go", "model": "gpt-5.4-nano-low"},
        ]

    monkeypatch.setattr(
        "hermes_cli.config.get_config_path",
        lambda: SimpleNamespace(
            exists=lambda: True,
            stat=lambda: SimpleNamespace(st_mtime_ns=7),
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.config.read_user_config_raw",
        lambda *_a, **_k: {"fallback_providers": []},
    )
    monkeypatch.setattr("hermes_cli.fallback_config.get_fallback_chain", _chain)

    refresh_agent_fallback_chain(agent)

    assert agent._fallback_chain[0]["model"] == "deepseek-v4.1-flash"
    assert agent._fallback_index == 0
    assert agent._unavailable_fallback_keys == set()


def test_refresh_skips_parse_when_mtime_unchanged(monkeypatch):
    calls = {"n": 0}

    def _raw(*_a, **_k):
        calls["n"] += 1
        return {
            "fallback_providers": [
                {"provider": "ollama-cloud", "model": "deepseek-v4.1-flash"},
            ]
        }

    monkeypatch.setattr(
        "hermes_cli.config.get_config_path",
        lambda: SimpleNamespace(
            exists=lambda: True,
            stat=lambda: SimpleNamespace(st_mtime_ns=11),
        ),
    )
    monkeypatch.setattr("hermes_cli.config.read_user_config_raw", _raw)
    agent = SimpleNamespace(
        _fallback_chain=[],
        _fallback_model=None,
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
    )
    refresh_agent_fallback_chain(agent)
    refresh_agent_fallback_chain(agent)
    assert calls["n"] == 1
    assert agent._fallback_chain[0]["model"] == "deepseek-v4.1-flash"


def test_refresh_keeps_chain_while_fallback_cooldown_holds():
    live = [{"provider": "cursor-go", "model": "gpt-5.4-nano-low"}]
    agent = SimpleNamespace(
        _fallback_chain=live,
        _fallback_model=live[0],
        _fallback_index=1,
        _fallback_activated=True,
        _rate_limited_until=10**12,
    )
    apply_fallback_chain_to_agent(
        agent,
        [{"provider": "ollama-cloud", "model": "deepseek-v4.1-flash"}],
    )
    assert agent._fallback_chain == live
    assert agent._fallback_index == 1


def test_ollama_billing_skips_same_provider_and_reaches_next(monkeypatch):
    seen = []

    def _local(_agent, fb):
        seen.append(fb["provider"])
        return "stop-before-client"

    monkeypatch.setattr(
        "agent.chat_completion_helpers._fallback_entry_unavailable_without_network",
        _local,
    )
    agent = SimpleNamespace(
        provider="ollama-cloud",
        model="glm-5.2",
        _fallback_chain=[
            {"provider": "ollama-cloud", "model": "gemma4:31b"},
            {"provider": "cursor-go", "model": "gpt-5.4-nano-low"},
        ],
        _fallback_index=0,
        _fallback_activated=False,
        _primary_runtime={"provider": "ollama-cloud"},
        _rate_limited_until=0,
        _unavailable_fallback_keys=set(),
    )
    agent._try_activate_fallback = lambda reason: try_activate_fallback(agent, reason)

    activated = try_activate_fallback(agent, reason=FailoverReason.billing)

    assert activated is False
    assert agent.model == "glm-5.2"
    assert seen == ["cursor-go"]


def test_ollama_rate_limit_still_considers_same_provider_hop(monkeypatch):
    seen = []

    def _local(_agent, fb):
        seen.append(fb["provider"])
        return "stop-before-client"

    monkeypatch.setattr(
        "agent.chat_completion_helpers._fallback_entry_unavailable_without_network",
        _local,
    )
    agent = SimpleNamespace(
        provider="ollama-cloud",
        model="glm-5.2",
        _fallback_chain=[{"provider": "ollama-cloud", "model": "gemma4:31b"}],
        _fallback_index=0,
        _fallback_activated=False,
        _primary_runtime={"provider": "ollama-cloud"},
        _rate_limited_until=0,
        _unavailable_fallback_keys=set(),
    )
    agent._try_activate_fallback = lambda reason: try_activate_fallback(agent, reason)

    try_activate_fallback(agent, reason=FailoverReason.rate_limit)

    assert seen == ["ollama-cloud"]


def test_missing_pool_is_attached_before_rotation(monkeypatch):
    from agent.agent_runtime_helpers import recover_with_credential_pool

    next_entry = SimpleNamespace(id="fallback-key")
    pool = MagicMock()
    pool.has_credentials.return_value = True
    pool.entries.return_value = [SimpleNamespace(id="primary"), next_entry]
    pool.provider = "ollama-cloud"
    pool.current.return_value = None
    pool.mark_exhausted_and_rotate.return_value = next_entry

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    agent = SimpleNamespace(
        provider="ollama-cloud",
        api_key="primary-key",
        base_url="https://ollama.com/v1",
        _credential_pool=None,
        _credential_pool_entry_id=None,
        _swap_credential=MagicMock(),
    )

    recovered, retried = recover_with_credential_pool(
        agent,
        status_code=429,
        has_retried_429=False,
        classified_reason=FailoverReason.billing,
    )

    assert recovered is True
    assert retried is False
    assert agent._credential_pool is pool
    agent._swap_credential.assert_called_once_with(next_entry)
