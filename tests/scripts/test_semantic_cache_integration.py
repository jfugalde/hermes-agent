"""Integration test for the 3-layer semantic cache system.

Exercises all three layers end-to-end with mocked Ollama embeddings:

1. **Safety classifier** — ``semantic_cache_safety.is_safe_to_cache``
2. **Cache store + retrieve** — ``clawmem_semantic_cache.SemanticCache``
3. **Hook handlers** — pre-dispatch, auto-populate, and /cacheask

No Ollama dependency — all embeddings are mocked with deterministic vectors
where cosine similarity > 0.94 for near-duplicates and < 0.94 for unrelated
prompts.

Usage::

    pytest hermes-agent/tests/scripts/test_semantic_cache_integration.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts dir so we can import the shared modules
_SCRIPTS_DIR = Path("/home/vivojf/.hermes/scripts")
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import semantic_cache_safety as safety
from clawmem_semantic_cache import SemanticCache, cosine_similarity, normalize_prompt

# Real paths for hook loading
_REAL_HOME = Path("/home/vivojf")
_HERMES_HOME = _REAL_HOME / ".hermes"
_HOOKS_DIR = _HERMES_HOME / "hooks"

# ---------------------------------------------------------------------------
# Deterministic mock vectors
# ---------------------------------------------------------------------------
# Vectors designed so that:
#   cosine_similarity(V_CAPITAL, V_CAPITAL_SIMILAR) ≈ 0.9998 > 0.94
#   cosine_similarity(V_CAPITAL, V_WEATHER) = 0.0 < 0.94
#   cosine_similarity(V_CAPITAL, V_UNRELATED) = 0.0 < 0.94

V_CAPITAL = [1.0, 0.0, 0.0, 0.0]
V_CAPITAL_SIMILAR = [0.98, 0.02, 0.0, 0.0]  # cos_sim with V_CAPITAL ≈ 0.9998
V_WEATHER = [0.0, 1.0, 0.0, 0.0]  # cos_sim with V_CAPITAL = 0.0
V_UNRELATED = [0.0, 0.0, 1.0, 0.0]


def _mock_ollama_embed(text: str) -> list[float]:
    """Deterministic mock: returns fixed vectors based on prompt content.

    Uses normalized prompt text to classify the prompt into a concept,
    then returns the corresponding vector.
    """
    normalized = normalize_prompt(text).lower()

    # Capital of France concept
    if "capital" in normalized and "france" in normalized:
        # Distinguish exact vs similar by checking for "what is"
        if normalized.startswith("what is"):
            return V_CAPITAL
        return V_CAPITAL_SIMILAR

    # Weather in Tokyo concept
    if "weather" in normalized and "tokyo" in normalized:
        return V_WEATHER

    # Default model concept
    if "default" in normalized and "model" in normalized:
        return V_CAPITAL  # same concept as capital for cross-test

    return V_UNRELATED


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for cache isolation."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    try:
        db_path.unlink()
    except OSError:
        pass


@pytest.fixture
def cache(temp_db):
    """Create a SemanticCache with a temp DB and mocked embeddings."""
    with patch("clawmem_semantic_cache.ollama_embed", _mock_ollama_embed):
        yield SemanticCache(db_path=temp_db, threshold=0.94)


# ---------------------------------------------------------------------------
# Helper: load hook handlers with patched dependencies
# ---------------------------------------------------------------------------


def _write_config(hermes_home: Path, **overrides):
    """Write a semantic_cache config.yaml to the given hermes home dir.

    The conftest redirects HERMES_HOME to a per-test tempdir, so handlers
    that read config.yaml at call time find an empty dir.  This helper
    writes the minimal config they need.
    """
    config_dir = hermes_home
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config = {
        "semantic_cache": {
            "enabled": True,
            "auto_populate": True,
            "pre_dispatch": True,
            "ttl_seconds": 300,
        },
    }
    # Apply any overrides (e.g. enabled=False)
    for key, value in overrides.items():
        parts = key.split(".")
        target = config
        for p in parts[:-1]:
            target = target.setdefault(p, {})
        target[parts[-1]] = value
    import yaml
    with open(config_path, "w") as f:
        yaml.dump(config, f)
    return config_path


def _load_pre_dispatch_handler():
    """Load the pre-dispatch hook handler via importlib."""
    mod_name = "test_int_pre_dispatch_handler"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(
        mod_name,
        _HOOKS_DIR / "semantic-cache-pre-dispatch" / "handler.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_auto_populate_handler():
    """Load the auto-populate hook handler via importlib."""
    mod_name = "test_int_auto_populate_handler"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(
        mod_name,
        _HOOKS_DIR / "semantic-cache-auto-populate" / "handler.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_cacheask_handler():
    """Load the /cacheask hook handler via importlib."""
    mod_name = "test_int_cacheask_handler"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(
        mod_name,
        _HOOKS_DIR / "semantic-cache" / "handler.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _make_cache_init_patch(temp_db_path: Path):
    """Return a patched __init__ that forces SemanticCache to use temp_db_path.

    This is needed because the hook handlers call ``SemanticCache()`` with no
    arguments, which would use the real ``STATE_DB``.  We patch the class's
    ``__init__`` so all instances created inside hooks use the temp DB.
    """
    original_init = SemanticCache.__init__

    def patched_init(self, db_path=None, threshold=None):
        original_init(self, db_path=temp_db_path, threshold=threshold or 0.94)

    return patch("clawmem_semantic_cache.SemanticCache.__init__", patched_init)


@pytest.fixture
def hermes_home():
    """Return the per-test HERMES_HOME path set by conftest."""
    return Path(os.environ["HERMES_HOME"])


# =========================================================================
# Layer 1: Safety classifier
# =========================================================================


class TestSafetyClassifier:
    """Verify the safety classifier with real-world inputs (no mocking needed)."""

    def test_safe_qa_is_cacheable(self):
        assert safety.is_safe_to_cache("What is the capital of France?", response="Paris") is True

    def test_code_fence_is_not_cacheable(self):
        assert safety.is_safe_to_cache("Write code ```python\ndef f(): pass\n```") is False

    def test_shell_command_is_not_cacheable(self):
        assert safety.is_safe_to_cache("run curl http://example.com") is False

    def test_error_response_is_not_cacheable(self):
        assert safety.is_safe_to_cache("What happened?", response="Error: connection refused") is False

    def test_tool_call_is_not_cacheable(self):
        assert safety.is_safe_to_cache("use a tool_call to fetch data") is False

    def test_multi_step_request_is_not_cacheable(self):
        assert safety.is_safe_to_cache("search for the latest news") is False

    def test_file_path_is_not_cacheable(self):
        assert safety.is_safe_to_cache("check /etc/passwd") is False

    def test_safe_without_response(self):
        assert safety.is_safe_to_cache("What is the capital of France?") is True


# =========================================================================
# Layer 2: Cache store + retrieve (with mocked embeddings)
# =========================================================================


class TestCacheStoreAndRetrieve:
    """Test SemanticCache store() and get() with mocked deterministic embeddings."""

    def test_store_and_get_exact_match(self, cache):
        """Store a response, then retrieve it with the exact same prompt."""
        cache.store("What is the capital of France?", "Paris", ttl=300)
        cached = cache.get("What is the capital of France?")
        assert cached == "Paris"

    def test_store_and_get_semantic_match(self, cache):
        """A semantically similar prompt should return the cached response."""
        cache.store("What is the capital of France?", "Paris", ttl=300)
        cached = cache.get("Tell me the capital of France")
        assert cached == "Paris"

    def test_miss_on_unrelated_prompt(self, cache):
        """An unrelated prompt should NOT return a cached response."""
        cache.store("What is the capital of France?", "Paris", ttl=300)
        cached = cache.get("What is the weather in Tokyo?")
        assert cached is None

    def test_ttl_expiration(self, cache):
        """A cached entry with TTL=0 should not be returned."""
        cache.store("What is 2+2?", "4", ttl=0)
        cached = cache.get("What is 2+2?")
        assert cached is None

    def test_store_overwrites_existing(self, cache):
        """Storing the same prompt again should overwrite the old response."""
        cache.store("What is 2+2?", "4", ttl=300)
        cache.store("What is 2+2?", "four", ttl=300)
        cached = cache.get("What is 2+2?")
        assert cached == "four"

    def test_get_returns_none_on_empty_cache(self, cache):
        """An empty cache should return None for any prompt."""
        cached = cache.get("What is the capital of France?")
        assert cached is None

    def test_get_handles_ollama_error_gracefully(self, temp_db):
        """When ollama_embed raises, get() should return None (fail-open)."""
        with patch("clawmem_semantic_cache.ollama_embed", side_effect=RuntimeError("Ollama down")):
            cache = SemanticCache(db_path=temp_db)
            cached = cache.get("What is the capital of France?")
            assert cached is None

    def test_store_handles_ollama_error_gracefully(self, temp_db):
        """When ollama_embed raises, store() should not crash."""
        with patch("clawmem_semantic_cache.ollama_embed", side_effect=RuntimeError("Ollama down")):
            cache = SemanticCache(db_path=temp_db)
            result = cache.store("What is the capital of France?", "Paris", ttl=300)
            assert result == ""  # empty hash on failure


# =========================================================================
# Layer 3: Full turn simulation (pre-dispatch + auto-populate)
# =========================================================================


class TestFullTurnSimulation:
    """Simulate a complete agent turn: miss -> LLM -> store -> hit."""

    def test_full_turn_flow(self, temp_db, hermes_home):
        """Simulate: miss -> LLM produces response -> auto-populate stores -> hit."""
        _write_config(hermes_home)
        with (
            patch("clawmem_semantic_cache.ollama_embed", _mock_ollama_embed),
            _make_cache_init_patch(temp_db),
        ):
            pre_handler = _load_pre_dispatch_handler()
            auto_handler = _load_auto_populate_handler()

            # Step 1: Pre-dispatch hook — cache miss (cache is empty)
            result = pre_handler.handle("agent:start", {
                "platform": "telegram",
                "user_id": "user123",
                "session_id": "session_abc",
                "message": "What is the capital of France?",
                "chat_id": "-100123",
                "thread_id": "",
                "chat_type": "dm",
            })
            assert result["decision"] == "ignored", "Expected cache miss on first call"

            # Step 2: LLM produces a response (simulated)
            llm_response = "The capital of France is Paris."

            # Step 3: Auto-populate hook — stores the response
            result = auto_handler.handle("agent:end", {
                "message": "What is the capital of France?",
                "response": llm_response,
            })
            assert result["decision"] == "ignored"

            # Step 4: Pre-dispatch hook — cache hit on semantically similar prompt
            result = pre_handler.handle("agent:start", {
                "platform": "telegram",
                "user_id": "user123",
                "session_id": "session_abc",
                "message": "Tell me the capital of France",
                "chat_id": "-100123",
                "thread_id": "",
                "chat_type": "dm",
            })
            assert result["decision"] == "handled", "Expected cache hit on similar prompt"
            assert "Paris" in result["message"], "Cached response should contain 'Paris'"

    def test_unsafe_prompt_never_cached_nor_short_circuited(self, temp_db, hermes_home):
        """Unsafe prompts (code, tools) should never be cached or short-circuited."""
        _write_config(hermes_home)
        with (
            patch("clawmem_semantic_cache.ollama_embed", _mock_ollama_embed),
            _make_cache_init_patch(temp_db),
        ):
            pre_handler = _load_pre_dispatch_handler()
            auto_handler = _load_auto_populate_handler()

            # Step 1: Pre-dispatch should ignore unsafe prompt
            result = pre_handler.handle("agent:start", {
                "platform": "telegram",
                "user_id": "user123",
                "session_id": "session_abc",
                "message": "Write a Python function to sort a list:\n\ndef sort_list(items):\n    return sorted(items)",
                "chat_id": "-100123",
                "thread_id": "",
                "chat_type": "dm",
            })
            assert result["decision"] == "ignored", "Unsafe prompt should be ignored"

            # Step 2: Auto-populate should skip unsafe response
            result = auto_handler.handle("agent:end", {
                "message": "Write a Python function",
                "response": "Here's a function:\n```python\ndef hello():\n    pass\n```",
            })
            assert result["decision"] == "ignored"
            assert "safety" in result.get("message", ""), "Should indicate safety skip"

    def test_toolsets_never_short_circuit(self, temp_db):
        """Non-empty toolsets should always skip the cache."""
        with (
            patch("clawmem_semantic_cache.ollama_embed", _mock_ollama_embed),
            _make_cache_init_patch(temp_db),
        ):
            pre_handler = _load_pre_dispatch_handler()

            result = pre_handler.handle("agent:start", {
                "platform": "telegram",
                "user_id": "user123",
                "session_id": "session_abc",
                "message": "What is the capital of France?",
                "chat_id": "-100123",
                "thread_id": "",
                "chat_type": "dm",
                "toolsets": ["terminal", "web"],
            })
            assert result["decision"] == "ignored", "Toolsets should skip cache"

    def test_empty_message_returns_ignored(self, temp_db):
        """Empty messages should be ignored without touching the cache."""
        with (
            patch("clawmem_semantic_cache.ollama_embed", _mock_ollama_embed),
            _make_cache_init_patch(temp_db),
        ):
            pre_handler = _load_pre_dispatch_handler()
            result = pre_handler.handle("agent:start", {
                "message": "",
            })
            assert result["decision"] == "ignored"

    def test_cache_miss_after_storing_unrelated(self, temp_db):
        """Storing one concept should not cause a hit on an unrelated concept."""
        with (
            patch("clawmem_semantic_cache.ollama_embed", _mock_ollama_embed),
            _make_cache_init_patch(temp_db),
        ):
            pre_handler = _load_pre_dispatch_handler()
            auto_handler = _load_auto_populate_handler()

            # Store capital of France
            auto_handler.handle("agent:end", {
                "message": "What is the capital of France?",
                "response": "Paris",
            })

            # Ask about weather in Tokyo — should miss
            result = pre_handler.handle("agent:start", {
                "platform": "telegram",
                "user_id": "user123",
                "session_id": "session_abc",
                "message": "What is the weather in Tokyo?",
                "chat_id": "-100123",
                "thread_id": "",
                "chat_type": "dm",
            })
            assert result["decision"] == "ignored", "Unrelated prompt should miss"


# =========================================================================
# Layer 4: /cacheask command hook
# =========================================================================


class TestCacheAskCommand:
    """Verify the /cacheask command hook returns cached answers."""

    def test_cacheask_returns_cached_answer(self, temp_db):
        """After storing a response, /cacheask should return it."""
        with (
            patch("clawmem_semantic_cache.ollama_embed", _mock_ollama_embed),
            _make_cache_init_patch(temp_db),
        ):
            # First, store a response via the cache directly
            cache = SemanticCache(db_path=temp_db)
            cache.store("What is the default Hermes model?", "deepseek-v4-flash", ttl=300)

            # Then, /cacheask should find it
            cacheask = _load_cacheask_handler()
            result = cacheask.handle("command:cacheask", {
                "args": "What is the default Hermes model?",
            })
            assert result["decision"] == "handled"
            assert "deepseek-v4-flash" in result["message"]

    def test_cacheask_returns_cached_semantic_match(self, temp_db):
        """/cacheask should return cached answer for a semantically similar prompt."""
        with (
            patch("clawmem_semantic_cache.ollama_embed", _mock_ollama_embed),
            _make_cache_init_patch(temp_db),
        ):
            cache = SemanticCache(db_path=temp_db)
            cache.store("What is the capital of France?", "Paris", ttl=300)

            cacheask = _load_cacheask_handler()
            result = cacheask.handle("command:cacheask", {
                "args": "Tell me the capital of France",
            })
            assert result["decision"] == "handled"
            assert "Paris" in result["message"]

    def test_cacheask_miss_returns_no_answer(self, temp_db):
        """/cacheask for an uncached question should return a miss message."""
        with (
            patch("clawmem_semantic_cache.ollama_embed", _mock_ollama_embed),
            _make_cache_init_patch(temp_db),
        ):
            cacheask = _load_cacheask_handler()
            result = cacheask.handle("command:cacheask", {
                "args": "What is the weather in Tokyo?",
            })
            assert result["decision"] == "handled"
            assert "No cached answer" in result["message"]

    def test_cacheask_empty_args_shows_usage(self):
        """/cacheask with no args should show usage."""
        cacheask = _load_cacheask_handler()
        result = cacheask.handle("command:cacheask", {"args": ""})
        assert result["decision"] == "handled"
        assert "Usage" in result["message"]

    def test_cacheask_unavailable_module_shows_error(self):
        """When SemanticCache is None, /cacheask should show import error."""
        cacheask = _load_cacheask_handler()
        with patch.object(cacheask, "SemanticCache", None):
            # _CACHE_IMPORT_ERROR only exists when the import fails at module
            # level; since the import succeeds in our test env, we set it
            # explicitly so the handler can reference it.
            setattr(cacheask, "_CACHE_IMPORT_ERROR", "Mock import error")
            result = cacheask.handle("command:cacheask", {
                "args": "What is the default model?",
            })
            assert result["decision"] == "handled"
            assert "unavailable" in result["message"].lower()


# =========================================================================
# Normalization and vector math
# =========================================================================


class TestNormalization:
    """Verify prompt normalization removes transient tokens."""

    def test_normalize_removes_uuids(self):
        raw = "Check task t_abc123 and session_xyz789 at 2026-08-03T12:00:00Z from 10.0.0.1"
        normalized = normalize_prompt(raw)
        assert "t_abc123" not in normalized
        assert "session_xyz789" not in normalized
        assert "2026-08-03T12:00:00Z" not in normalized
        assert "10.0.0.1" not in normalized

    def test_normalize_removes_hex_and_ip(self):
        raw = "Error at 0xdeadbeef from 192.168.1.1"
        normalized = normalize_prompt(raw)
        assert "0xdeadbeef" not in normalized
        assert "192.168.1.1" not in normalized

    def test_normalize_collapses_whitespace(self):
        raw = "hello    world\n\n\nfoo"
        normalized = normalize_prompt(raw)
        assert "    " not in normalized
        assert "\n\n\n" not in normalized


class TestCosineSimilarity:
    """Verify cosine similarity calculations."""

    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-10

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert abs(cosine_similarity(a, b)) < 1e-10

    def test_zero_vector(self):
        assert abs(cosine_similarity([0.0, 0.0], [1.0, 0.0])) < 1e-10
        assert abs(cosine_similarity([0.0, 0.0], [0.0, 0.0])) < 1e-10

    def test_similar_vectors_above_threshold(self):
        """V_CAPITAL and V_CAPITAL_SIMILAR should have similarity > 0.94."""
        sim = cosine_similarity(V_CAPITAL, V_CAPITAL_SIMILAR)
        assert sim > 0.94, f"Expected > 0.94, got {sim}"

    def test_different_vectors_below_threshold(self):
        """V_CAPITAL and V_WEATHER should have similarity < 0.94."""
        sim = cosine_similarity(V_CAPITAL, V_WEATHER)
        assert sim < 0.94, f"Expected < 0.94, got {sim}"
