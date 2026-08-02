"""Tests for the semantic response cache gate in `hermes -z` oneshot mode.

Unit-level coverage for `_run_agent_maybe_cached` with the cache layer fully
mocked (no Ollama/SQLite dependency). See
scripts/test_semantic_cache_gateway_integration.py in ~/.hermes for an
end-to-end smoke test against a real embedding model.
"""

import os
import sys

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from unittest.mock import patch

from hermes_cli.oneshot import _run_agent_maybe_cached


class _FakeCache:
    """Minimal stand-in for clawmem_semantic_cache.SemanticCache.get_or_call."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get_or_call(self, prompt, callable_, ttl=3600):
        if prompt in self.store:
            return self.store[prompt]
        response = callable_(prompt)
        self.store[prompt] = response
        return response


class TestRunAgentMaybeCached:
    @patch("hermes_cli.config.load_config", return_value={})
    @patch("hermes_cli.oneshot._run_agent")
    def test_ineligible_turn_falls_through_untouched(self, mock_run_agent, _mock_cfg):
        mock_run_agent.return_value = ("hi", {"completed": True})

        with patch("agent.semantic_response_cache.is_cache_eligible", return_value=False):
            response, result = _run_agent_maybe_cached("hello", toolsets=None, use_config_toolsets=False)

        assert response == "hi"
        assert result == {"completed": True}
        mock_run_agent.assert_called_once()

    @patch("hermes_cli.config.load_config", return_value={})
    @patch("hermes_cli.oneshot._run_agent")
    def test_eligible_hit_skips_second_provider_call(self, mock_run_agent, _mock_cfg):
        mock_run_agent.return_value = ("answer", {"completed": True, "api_calls": 1})
        fake_cache = _FakeCache()

        with (
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=True),
            patch("agent.semantic_response_cache.get_cache", return_value=fake_cache),
            patch("agent.semantic_response_cache.get_cache_ttl", return_value=60),
        ):
            r1, res1 = _run_agent_maybe_cached("q", toolsets=None, use_config_toolsets=False)
            r2, res2 = _run_agent_maybe_cached("q", toolsets=None, use_config_toolsets=False)

        assert mock_run_agent.call_count == 1
        assert r1 == r2 == "answer"
        assert res1.get("api_calls") == 1
        assert res2.get("cache_hit") is True

    @patch("hermes_cli.config.load_config", return_value={})
    @patch("hermes_cli.oneshot._run_agent")
    def test_cache_layer_error_fails_open(self, mock_run_agent, _mock_cfg):
        """A broken cache (e.g. Ollama down) must never block a real response."""
        mock_run_agent.return_value = ("answer", {"completed": True})

        with (
            patch("agent.semantic_response_cache.is_cache_eligible", return_value=True),
            patch("agent.semantic_response_cache.get_cache", side_effect=RuntimeError("ollama down")),
        ):
            response, result = _run_agent_maybe_cached("q", toolsets=None, use_config_toolsets=False)

        assert response == "answer"
        assert result == {"completed": True}
        mock_run_agent.assert_called_once()

    @patch("hermes_cli.config.load_config", return_value={})
    @patch("hermes_cli.oneshot._run_agent")
    def test_eligibility_check_error_fails_open(self, mock_run_agent, _mock_cfg):
        mock_run_agent.return_value = ("answer", {"completed": True})

        with patch(
            "agent.semantic_response_cache.is_cache_eligible", side_effect=RuntimeError("boom")
        ):
            response, result = _run_agent_maybe_cached("q", toolsets=None, use_config_toolsets=False)

        assert response == "answer"
        mock_run_agent.assert_called_once()

    @patch("hermes_cli.tools_config._get_platform_tools", return_value={"shell"})
    @patch("hermes_cli.config.load_config", return_value={})
    @patch("hermes_cli.oneshot._run_agent")
    def test_config_toolsets_resolved_before_eligibility_check(
        self, mock_run_agent, _mock_cfg, mock_platform_tools
    ):
        """When toolsets aren't explicit, the resolved platform default must
        be what eligibility is judged against — not an empty list."""
        mock_run_agent.return_value = ("answer", {"completed": True})

        with patch("agent.semantic_response_cache.is_cache_eligible") as mock_eligible:
            mock_eligible.return_value = False
            _run_agent_maybe_cached("q", toolsets=None, use_config_toolsets=True)

        mock_platform_tools.assert_called_once()
        _, called_toolsets, _ = mock_eligible.call_args[0]
        assert called_toolsets == ["shell"]
