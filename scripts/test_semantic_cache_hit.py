#!/usr/bin/env python3
"""Manual/automated verification that SemanticCache serves cache hits.

Demonstrates the exact flow the gateway integration relies on:
    1. A prompt is answered once via the "provider" callable and stored.
    2. An exact repeat of that prompt is served from cache (no provider call).
    3. A semantically-equivalent rephrasing is also served from cache.
    4. An unrelated prompt is *not* served from cache (provider is called).

Runs fully offline: the Ollama embedding call is replaced with a
deterministic bag-of-words hash vector so this doesn't require a live
Ollama server. Uses a temporary SQLite file, never touches ~/.hermes/state.db.

Usage:
    python3 scripts/test_semantic_cache_hit.py
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import clawmem_semantic_cache as cache_mod  # noqa: E402

# Function words carry no topical meaning; real embedding models mostly
# collapse them away too, which is why "what is the X" and "tell me X"
# land close together in real embedding space.
_STOPWORDS = {"what", "is", "the", "tell", "me", "in", "of", "a", "an"}
_VOCAB = {
    "default": 0, "hermes": 1, "model": 2,
    "weather": 3, "tokyo": 4,
}


def _fake_embed(text: str) -> list[float]:
    """Deterministic embedding stand-in: one axis per known content word.

    Prompts sharing their content words land close together in cosine
    space (score above the 0.94 threshold), regardless of stopword/phrasing
    differences; unrelated prompts don't. Good enough to exercise
    cache-hit/miss logic without a live Ollama server.
    """
    words = {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS}
    vec = [0.0] * (max(_VOCAB.values()) + 1)
    for w in words:
        idx = _VOCAB.get(w)
        if idx is not None:
            vec[idx] = 1.0
    return vec


class SemanticCacheHitTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_embed = cache_mod.ollama_embed
        cache_mod.ollama_embed = _fake_embed  # type: ignore[assignment]

        # Don't actually shell out to the `clawmem` CLI mirroring step;
        # we only care about the cache-hit behavior here.
        self._orig_popen = cache_mod.subprocess.Popen
        cache_mod.subprocess.Popen = lambda *a, **k: None  # type: ignore[assignment]

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test-state.db"

        self._orig_clawmem_dir = cache_mod.CLAWMEM_COLLECTION_DIR
        cache_mod.CLAWMEM_COLLECTION_DIR = Path(self._tmpdir.name) / "clawmem-mirror"

        self.cache = cache_mod.SemanticCache(db_path=db_path)
        self.calls: list[str] = []

    def tearDown(self) -> None:
        cache_mod.ollama_embed = self._orig_embed
        cache_mod.subprocess.Popen = self._orig_popen
        cache_mod.CLAWMEM_COLLECTION_DIR = self._orig_clawmem_dir
        self._tmpdir.cleanup()

    def _provider(self, prompt: str) -> str:
        """Stand-in for the LLM provider call; records every invocation."""
        self.calls.append(prompt)
        return f"Answer for: {prompt}"

    def test_exact_repeat_is_a_cache_hit(self) -> None:
        prompt = "What is the default Hermes model?"

        first = self.cache.get_or_call(prompt, self._provider, ttl=300)
        self.assertEqual(len(self.calls), 1, "first call must hit the provider")

        second = self.cache.get_or_call(prompt, self._provider, ttl=300)
        self.assertEqual(second, first)
        self.assertEqual(len(self.calls), 1, "repeat prompt must be a cache hit")

    def test_semantic_rephrasing_is_a_cache_hit(self) -> None:
        prompt = "What is the default Hermes model?"
        rephrased = "tell me the default Hermes model"

        original_answer = self.cache.get_or_call(prompt, self._provider, ttl=300)
        self.assertEqual(len(self.calls), 1)

        rephrased_answer = self.cache.get_or_call(rephrased, self._provider, ttl=300)
        self.assertEqual(rephrased_answer, original_answer)
        self.assertEqual(len(self.calls), 1, "similar rephrasing must be a cache hit")

        looked_up = self.cache.get(rephrased)
        self.assertEqual(looked_up, original_answer)

    def test_unrelated_prompt_is_a_cache_miss(self) -> None:
        self.cache.get_or_call(
            "What is the default Hermes model?", self._provider, ttl=300
        )
        self.assertIsNone(self.cache.get("What is the weather in Tokyo?"))

        self.cache.get_or_call(
            "What is the weather in Tokyo?", self._provider, ttl=300
        )
        self.assertEqual(len(self.calls), 2, "unrelated prompt must call the provider")


def _demo() -> None:
    """Human-readable walkthrough, mirrors the assertions above."""
    test = SemanticCacheHitTest()
    test.setUp()
    try:
        prompt = "What is the default Hermes model?"
        rephrased = "tell me the default Hermes model"
        unrelated = "What is the weather in Tokyo?"

        print(f"1st call {prompt!r} -> provider hit:", test.cache.get_or_call(prompt, test._provider))
        print(f"   provider calls so far: {len(test.calls)} (expected 1)")

        print(f"2nd call (exact repeat) {prompt!r} -> {test.cache.get_or_call(prompt, test._provider)!r}")
        print(f"   provider calls so far: {len(test.calls)} (expected 1 -> CACHE HIT)")

        print(f"3rd call (rephrased) {rephrased!r} -> {test.cache.get_or_call(rephrased, test._provider)!r}")
        print(f"   provider calls so far: {len(test.calls)} (expected 1 -> SEMANTIC CACHE HIT)")

        print(f"4th call (unrelated) {unrelated!r} -> {test.cache.get_or_call(unrelated, test._provider)!r}")
        print(f"   provider calls so far: {len(test.calls)} (expected 2 -> CACHE MISS, as expected)")
    finally:
        test.tearDown()


if __name__ == "__main__":
    if "--demo" in sys.argv:
        _demo()
    else:
        unittest.main(argv=[sys.argv[0]])
