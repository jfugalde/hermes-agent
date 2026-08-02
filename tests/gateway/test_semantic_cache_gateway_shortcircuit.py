"""Tests for the semantic-cache short-circuit in the gateway's plain-chat
message handler (``gateway.run._maybe_get_cached_agent_result``).

Unit-level coverage with the cache layer fully mocked (no Ollama/SQLite
dependency, no live Gateway/session/adapter setup needed) — mirrors
``tests/hermes_cli/test_oneshot_semantic_cache.py``'s pattern so both call
sites of ``agent/semantic_response_cache.py`` are covered the same way. See
``scripts/test_semantic_cache_gateway_integration.py`` in ``~/.hermes`` for an
end-to-end smoke test against a real embedding model.
"""

import asyncio

import pytest
from unittest.mock import patch

from gateway.run import (
    _maybe_get_cached_agent_result,
    _semantic_cache_lookup_sync,
    _semantic_cache_store_sync,
)


class _FakeCache:
    """Minimal stand-in for clawmem_semantic_cache.SemanticCache."""

    def __init__(self, response=None):
        self._response = response
        self.stored: list[tuple[str, str, int]] = []

    def get(self, prompt):
        return self._response

    def store(self, prompt, response, ttl):
        self.stored.append((prompt, response, ttl))


def _run(coro):
    return asyncio.run(coro)


class TestSemanticCacheLookupSync:
    def test_disabled_returns_none_without_touching_cache(self):
        with (
            patch("agent.semantic_response_cache.is_cache_enabled", return_value=False),
            patch("agent.semantic_response_cache.get_cache") as mock_get_cache,
        ):
            result = _semantic_cache_lookup_sync("hello", [], {})

        assert result is None
        mock_get_cache.assert_not_called()

    def test_ineligible_turn_returns_none(self):
        with (
            patch("agent.semantic_response_cache.is_cache_enabled", return_value=True),
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=False),
            patch("agent.semantic_response_cache.get_cache") as mock_get_cache,
        ):
            result = _semantic_cache_lookup_sync("Write a python function", [], {})

        assert result is None
        mock_get_cache.assert_not_called()

    def test_eligible_hit_returns_cached_text(self):
        fake_cache = _FakeCache(response="Paris")
        with (
            patch("agent.semantic_response_cache.is_cache_enabled", return_value=True),
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=True),
            patch("agent.semantic_response_cache.get_cache", return_value=fake_cache),
        ):
            result = _semantic_cache_lookup_sync("What is the capital of France?", [], {})

        assert result == "Paris"

    def test_any_toolset_disqualifies_even_when_enabled(self):
        with (
            patch("agent.semantic_response_cache.is_cache_enabled", return_value=True),
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=False) as mock_eligible,
        ):
            _semantic_cache_lookup_sync("What is 2+2?", ["shell"], {})

        # Toolsets are forwarded to the eligibility gate so tool-enabled
        # turns are never cached regardless of the prompt text.
        _, called_toolsets, _ = mock_eligible.call_args[0]
        assert called_toolsets == ["shell"]


class TestMaybeGetCachedAgentResult:
    def test_disabled_by_default_returns_none(self):
        with patch("agent.semantic_response_cache.is_cache_enabled", return_value=False):
            result = _run(_maybe_get_cached_agent_result("hello", [], {}))

        assert result is None

    def test_cache_hit_returns_run_agent_compatible_dict(self):
        fake_cache = _FakeCache(response="The default Hermes model is gemma4:31b.")
        with (
            patch("agent.semantic_response_cache.is_cache_enabled", return_value=True),
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=True),
            patch("agent.semantic_response_cache.get_cache", return_value=fake_cache),
        ):
            result = _run(
                _maybe_get_cached_agent_result(
                    "What is the default Hermes model?", [], {}
                )
            )

        assert result is not None
        assert result["final_response"] == "The default Hermes model is gemma4:31b."
        assert result["completed"] is True
        assert result["failed"] is False
        assert result["api_calls"] == 0
        assert result["cache_hit"] is True
        # Keys deliberately omitted (messages, history_offset, session_id,
        # etc.) must be safe to `.get()` with defaults downstream in
        # _handle_message_with_agent — this only asserts the contract this
        # function documents, not the caller's behavior.
        assert "messages" not in result

    def test_cache_miss_returns_none(self):
        fake_cache = _FakeCache(response=None)
        with (
            patch("agent.semantic_response_cache.is_cache_enabled", return_value=True),
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=True),
            patch("agent.semantic_response_cache.get_cache", return_value=fake_cache),
        ):
            result = _run(_maybe_get_cached_agent_result("something obscure", [], {}))

        assert result is None

    def test_cache_layer_error_fails_open(self):
        """A broken cache (e.g. Ollama down) must never block the real agent."""
        with (
            patch("agent.semantic_response_cache.is_cache_enabled", return_value=True),
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=True),
            patch(
                "agent.semantic_response_cache.get_cache",
                side_effect=RuntimeError("ollama down"),
            ),
        ):
            result = _run(_maybe_get_cached_agent_result("hello", [], {}))

        assert result is None

    def test_toolset_enabled_turn_never_short_circuits(self):
        """Any toolset enabled disqualifies the turn even if the text looks
        like a harmless question — tool calls and state-changing commands
        must never be answered from cache."""
        with (
            patch("agent.semantic_response_cache.is_cache_enabled", return_value=True),
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=False),
            patch("agent.semantic_response_cache.get_cache") as mock_get_cache,
        ):
            result = _run(
                _maybe_get_cached_agent_result("What is 2+2?", ["shell"], {})
            )

        assert result is None
        mock_get_cache.assert_not_called()


class TestSemanticCacheStoreSync:
    """Coverage for the write-back path: freshly-generated eligible gateway
    responses are stored so a later similar question can be served from
    cache without a provider call."""

    def test_ineligible_turn_never_stored(self):
        fake_cache = _FakeCache()
        with (
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=False),
            patch("agent.semantic_response_cache.get_cache", return_value=fake_cache),
        ):
            _semantic_cache_store_sync("Write a python function", "def f(): ...", [], {})

        assert fake_cache.stored == []

    def test_eligible_turn_is_stored(self):
        fake_cache = _FakeCache()
        with (
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=True),
            patch("agent.semantic_response_cache.get_cache", return_value=fake_cache),
            patch("agent.semantic_response_cache.get_cache_ttl", return_value=60),
        ):
            _semantic_cache_store_sync(
                "What is the default Hermes model?", "gemma4:31b", [], {}
            )

        assert fake_cache.stored == [
            ("What is the default Hermes model?", "gemma4:31b", 60)
        ]

    def test_toolset_enabled_turn_never_stored(self):
        fake_cache = _FakeCache()
        with (
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=False) as mock_eligible,
            patch("agent.semantic_response_cache.get_cache", return_value=fake_cache),
        ):
            _semantic_cache_store_sync("What is 2+2?", "4", ["shell"], {})

        _, called_toolsets, _ = mock_eligible.call_args[0]
        assert called_toolsets == ["shell"]
        assert fake_cache.stored == []

    def test_store_error_is_not_swallowed_here(self):
        """The sync helper itself doesn't catch errors — that's the caller's
        (gateway/run.py's async wrapper call site) job, matching the lookup
        path's contract."""
        with (
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=True),
            patch(
                "agent.semantic_response_cache.get_cache",
                side_effect=RuntimeError("ollama down"),
            ),
        ):
            with pytest.raises(RuntimeError):
                _semantic_cache_store_sync("hello", "hi", [], {})
