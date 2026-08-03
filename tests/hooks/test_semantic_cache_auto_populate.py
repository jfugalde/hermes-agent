"""Tests for hooks/semantic-cache-auto-populate/handler.py.

Covers:
- Safe Q&A → cache entry created
- Unsafe (code response) → no cache entry
- Handler exception → returns ignored, does not re-raise
- Returns decision: ignored with message on skipped events
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REAL_HOME = Path("/home/vivojf")
_SCRIPTS_DIR = _REAL_HOME / ".hermes" / "scripts"
_HANDLER_PATH = _REAL_HOME / ".hermes" / "hooks" / "semantic-cache-auto-populate" / "handler.py"


def _load_handler():
    """Load the handler module by exec'ing its file content.

    We pre-add the real scripts dir to sys.path so the import-time imports
    (semantic_cache_safety, clawmem_semantic_cache) resolve correctly.
    """
    module_name = "semantic_cache_auto_populate_handler"
    sys.modules.pop(module_name, None)

    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))

    import types

    module = types.ModuleType(module_name)
    module.__file__ = str(_HANDLER_PATH)
    module.__package__ = None
    module.__path__ = None
    module.__name__ = module_name

    # Read and exec the source
    source = _HANDLER_PATH.read_text()
    code = compile(source, str(_HANDLER_PATH), "exec")
    exec(code, module.__dict__)

    sys.modules[module_name] = module
    return module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def handler():
    """Load the handler module fresh for each test."""
    return _load_handler()


@pytest.fixture
def mock_config(tmp_path):
    """Write a config.yaml with semantic_cache enabled and auto_populate on."""
    config_dir = tmp_path / ".hermes"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "semantic_cache:\n"
        "  enabled: true\n"
        "  auto_populate: true\n"
        "  ttl_seconds: 300\n"
    )
    with patch.object(Path, "home", return_value=tmp_path):
        yield config_path


@pytest.fixture
def mock_config_disabled(tmp_path):
    """Write a config.yaml with semantic_cache disabled."""
    config_dir = tmp_path / ".hermes"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "semantic_cache:\n"
        "  enabled: false\n"
        "  auto_populate: true\n"
        "  ttl_seconds: 300\n"
    )
    with patch.object(Path, "home", return_value=tmp_path):
        yield config_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSafeQandA:
    """Safe Q&A should create a cache entry."""

    def test_safe_prompt_creates_cache_entry(self, handler, mock_config):
        """A safe factual Q&A should result in a cache.store() call."""
        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            MockCache.return_value = mock_instance

            result = handler.handle("agent:end", {
                "message": "What is the capital of France?",
                "response": "The capital of France is Paris.",
            })

            assert result == {"decision": "ignored"}
            mock_instance.store.assert_called_once_with(
                "What is the capital of France?",
                "The capital of France is Paris.",
                ttl=300,
            )

    def test_safe_prompt_uses_config_ttl(self, handler, mock_config):
        """The TTL from config should be passed to store()."""
        config_path = mock_config
        config_path.write_text(
            "semantic_cache:\n"
            "  enabled: true\n"
            "  auto_populate: true\n"
            "  ttl_seconds: 600\n"
        )

        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            MockCache.return_value = mock_instance

            handler.handle("agent:end", {
                "message": "What is the capital of France?",
                "response": "The capital of France is Paris.",
            })

            mock_instance.store.assert_called_once_with(
                "What is the capital of France?",
                "The capital of France is Paris.",
                ttl=600,
            )


class TestUnsafeResponse:
    """Unsafe responses should NOT create a cache entry."""

    def test_code_fence_response_not_cached(self, handler, mock_config):
        """A response containing code fences should not be cached."""
        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            MockCache.return_value = mock_instance

            result = handler.handle("agent:end", {
                "message": "Write a Python function",
                "response": "Here's a function:\n```python\ndef hello():\n    pass\n```",
            })

            assert result == {"decision": "ignored", "message": "safety: not cacheable"}
            mock_instance.store.assert_not_called()

    def test_error_traceback_response_not_cached(self, handler, mock_config):
        """A response containing an error traceback should not be cached."""
        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            MockCache.return_value = mock_instance

            result = handler.handle("agent:end", {
                "message": "What went wrong?",
                "response": "Traceback (most recent call last):\n  File \"test.py\", line 1\nNameError: name 'x' is not defined",
            })

            assert result == {"decision": "ignored", "message": "safety: not cacheable"}
            mock_instance.store.assert_not_called()

    def test_shell_command_response_not_cached(self, handler, mock_config):
        """A response containing a shell command should not be cached."""
        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            MockCache.return_value = mock_instance

            result = handler.handle("agent:end", {
                "message": "How to list files?",
                "response": "Run: $ curl http://example.com",
            })

            assert result == {"decision": "ignored", "message": "safety: not cacheable"}
            mock_instance.store.assert_not_called()


class TestUnsafePrompt:
    """Unsafe prompts should NOT create a cache entry."""

    def test_code_fence_prompt_not_cached(self, handler, mock_config):
        """A prompt containing code fences should not be cached."""
        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            MockCache.return_value = mock_instance

            result = handler.handle("agent:end", {
                "message": "Explain this code:\n```python\nx = 1\n```",
                "response": "That code assigns 1 to x.",
            })

            assert result == {"decision": "ignored", "message": "safety: not cacheable"}
            mock_instance.store.assert_not_called()

    def test_shell_command_prompt_not_cached(self, handler, mock_config):
        """A prompt containing shell commands should not be cached."""
        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            MockCache.return_value = mock_instance

            result = handler.handle("agent:end", {
                "message": "Run curl http://example.com",
                "response": "I cannot run commands.",
            })

            assert result == {"decision": "ignored", "message": "safety: not cacheable"}
            mock_instance.store.assert_not_called()


class TestConfigGates:
    """Config gates should prevent caching when disabled."""

    def test_disabled_config_returns_ignored(self, handler, mock_config_disabled):
        """When semantic_cache.enabled is false, the handler should skip."""
        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            MockCache.return_value = mock_instance

            result = handler.handle("agent:end", {
                "message": "What is the capital of France?",
                "response": "The capital of France is Paris.",
            })

            assert result == {"decision": "ignored", "message": "config: disabled"}
            mock_instance.store.assert_not_called()

    def test_auto_populate_disabled_returns_ignored(self, handler, tmp_path):
        """When auto_populate is false, the handler should skip."""
        config_dir = tmp_path / ".hermes"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            "semantic_cache:\n"
            "  enabled: true\n"
            "  auto_populate: false\n"
            "  ttl_seconds: 300\n"
        )

        with patch.object(Path, "home", return_value=tmp_path):
            with patch.object(handler, "SemanticCache") as MockCache:
                mock_instance = MagicMock()
                MockCache.return_value = mock_instance

                result = handler.handle("agent:end", {
                    "message": "What is the capital of France?",
                    "response": "The capital of France is Paris.",
                })

                assert result == {"decision": "ignored", "message": "config: disabled"}
                mock_instance.store.assert_not_called()


class TestEdgeCases:
    """Edge cases: empty, wrong event, missing modules."""

    def test_wrong_event_type(self, handler):
        """Non-agent:end events should be ignored immediately."""
        result = handler.handle("agent:start", {
            "message": "test",
            "response": "test",
        })
        assert result == {"decision": "ignored"}

    def test_empty_prompt(self, handler, mock_config):
        """Empty prompt should be ignored."""
        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            MockCache.return_value = mock_instance

            result = handler.handle("agent:end", {
                "message": "",
                "response": "Some response",
            })

            assert result == {"decision": "ignored", "message": "empty prompt or response"}
            mock_instance.store.assert_not_called()

    def test_empty_response(self, handler, mock_config):
        """Empty response should be ignored."""
        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            MockCache.return_value = mock_instance

            result = handler.handle("agent:end", {
                "message": "What is the capital?",
                "response": "",
            })

            assert result == {"decision": "ignored", "message": "empty prompt or response"}
            mock_instance.store.assert_not_called()

    def test_missing_safety_module(self, handler, mock_config):
        """When is_safe_to_cache is None, the handler should skip gracefully."""
        with patch.object(handler, "is_safe_to_cache", None):
            with patch.object(handler, "SemanticCache") as MockCache:
                mock_instance = MagicMock()
                MockCache.return_value = mock_instance

                result = handler.handle("agent:end", {
                    "message": "What is the capital?",
                    "response": "Paris.",
                })

                assert result == {"decision": "ignored", "message": "safety module unavailable"}
                mock_instance.store.assert_not_called()

    def test_missing_cache_module(self, handler, mock_config):
        """When SemanticCache is None, the handler should skip gracefully."""
        with patch.object(handler, "SemanticCache", None):
            result = handler.handle("agent:end", {
                "message": "What is the capital?",
                "response": "Paris.",
            })
            assert result == {"decision": "ignored", "message": "cache module unavailable"}

    def test_store_exception_does_not_propagate(self, handler, mock_config):
        """An exception in cache.store() should be swallowed."""
        with patch.object(handler, "SemanticCache") as MockCache:
            mock_instance = MagicMock()
            mock_instance.store.side_effect = RuntimeError("boom")
            MockCache.return_value = mock_instance

            # Should not raise
            result = handler.handle("agent:end", {
                "message": "What is the capital of France?",
                "response": "The capital of France is Paris.",
            })

            assert result == {"decision": "ignored"}

    def test_missing_config_file(self, handler, tmp_path):
        """When config.yaml doesn't exist, the handler should skip gracefully."""
        with patch.object(Path, "home", return_value=tmp_path):
            result = handler.handle("agent:end", {
                "message": "What is the capital?",
                "response": "Paris.",
            })
            assert result == {"decision": "ignored", "message": "config: disabled"}

    def test_corrupt_config_file(self, handler, tmp_path):
        """A corrupt config.yaml should not crash the handler."""
        config_dir = tmp_path / ".hermes"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(": invalid yaml [\n")

        with patch.object(Path, "home", return_value=tmp_path):
            result = handler.handle("agent:end", {
                "message": "What is the capital?",
                "response": "Paris.",
            })
            assert result == {"decision": "ignored", "message": "config: disabled"}
