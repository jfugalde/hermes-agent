#!/usr/bin/env python3
"""
Semantic response cache POC backed by local Ollama embeddings + SQLite.
Mirrors entries to a ClawMem collection for cross-agent visibility.

Usage:
    from clawmem_semantic_cache import SemanticCache
    cache = SemanticCache()
    response = cache.get_or_call(
        "What is the default Hermes model?",
        lambda prompt: call_your_llm(prompt),
        ttl=3600
    )
"""

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OLLAMA_URL = os.getenv("CLAWMEM_EMBED_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("CLAWMEM_EMBED_MODEL", "qwen3-embedding:0.6b")
CLAWMEM_COLLECTION_DIR = Path.home() / ".clawmem" / "semantic-prompt-cache"
CLAWMEM_API = "http://localhost:3030"
SIMILARITY_THRESHOLD = 0.94
DEFAULT_TTL = 3600
STATE_DB = Path.home() / ".hermes" / "state.db"

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
_REPLACEMENTS = [
    (r'\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b', '<UUID>'),
    (r'\b[a-f0-9]{24,}\b', '<HASH>'),
    (r'\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?\b', '<ISO_TS>'),
    (r'\b\d{4}-\d{2}-\d{2}\b', '<DATE>'),
    (r'\b\d{2}:\d{2}:\d{2}\b', '<TIME>'),
    (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '<IP>'),
    (r'\b0x[a-f0-9]+\b', '<HEX>'),
    (r'\bt_[a-z0-9_]+\b', '<TASK_ID>'),
    (r'\bsession_[a-z0-9]+\b', '<SESSION_ID>'),
    (r'\b[a-z]+_[a-f0-9]{6,}\b', '<ID>'),
]


def normalize_prompt(prompt: str) -> str:
    """Strip transient tokens so semantically-equivalent prompts collide."""
    text = prompt
    for pattern, replacement in _REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Vector math
# ---------------------------------------------------------------------------
def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Ollama embedding
# ---------------------------------------------------------------------------
def ollama_embed(text: str) -> list[float]:
    payload = json.dumps({"model": OLLAMA_MODEL, "input": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    embeddings = data.get("embeddings") or data.get("embedding")
    if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
        return embeddings[0]
    return embeddings


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------
def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_prompt_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_hash TEXT UNIQUE NOT NULL,
            normalized_prompt TEXT NOT NULL,
            response_text TEXT NOT NULL,
            embedding_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            ttl_seconds INTEGER NOT NULL,
            hit_count INTEGER DEFAULT 0,
            last_hit_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_semantic_prompt_cache_hash ON semantic_prompt_cache(prompt_hash)"
    )


class SemanticCache:
    def __init__(self, db_path: Path = STATE_DB, threshold: float = SIMILARITY_THRESHOLD):
        self.db_path = db_path
        self.threshold = threshold
        with sqlite3.connect(str(db_path)) as conn:
            _init_db(conn)
        CLAWMEM_COLLECTION_DIR.mkdir(parents=True, exist_ok=True)

    def _search(self, normalized: str, embedding: list[float]) -> Optional[dict[str, Any]]:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, prompt_hash, normalized_prompt, response_text,
                       embedding_json, created_at, ttl_seconds, hit_count, last_hit_at
                FROM semantic_prompt_cache
                """
            )
            best: Optional[dict[str, Any]] = None
            best_score = 0.0
            now = datetime.now(timezone.utc).isoformat()
            for row in cur.fetchall():
                stored = json.loads(row["embedding_json"])
                score = cosine_similarity(embedding, stored)
                if score < self.threshold:
                    continue
                created = row["created_at"]
                ttl = row["ttl_seconds"]
                if (datetime.fromisoformat(now) - datetime.fromisoformat(created)).total_seconds() > ttl:
                    continue
                if score > best_score:
                    best_score = score
                    best = dict(row)
                    best["score"] = score
            if best:
                conn.execute(
                    "UPDATE semantic_prompt_cache SET hit_count = hit_count + 1, last_hit_at = ? WHERE id = ?",
                    (now, best["id"]),
                )
                conn.commit()
        return best

    def _store(
        self,
        prompt_hash: str,
        normalized: str,
        response: str,
        embedding: list[float],
        ttl: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO semantic_prompt_cache
                (prompt_hash, normalized_prompt, response_text, embedding_json, created_at, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (prompt_hash, normalized, response, json.dumps(embedding), now, ttl),
            )
            conn.commit()
        self._mirror_to_clawmem(prompt_hash, normalized, response, ttl, now)

    def _mirror_to_clawmem(
        self, prompt_hash: str, normalized: str, response: str, ttl: int, created_at: str
    ) -> None:
        """Write a markdown artifact so ClawMem can index it as a memory."""
        safe_title = normalized[:80].replace("\n", " ").replace("\"", "'")
        path = CLAWMEM_COLLECTION_DIR / f"{prompt_hash}.md"
        frontmatter = {
            "title": f"semantic cache: {safe_title}",
            "collection": "semantic-prompt-cache",
            "cache_key": prompt_hash,
            "created_at": created_at,
            "ttl_seconds": ttl,
            "response_b64": base64.b64encode(response.encode()).decode(),
        }
        lines = ["---", json.dumps(frontmatter, indent=2), "---", "", normalized]
        path.write_text("\n".join(lines))
        # Background update so ClawMem picks up the new file without blocking.
        try:
            subprocess.Popen(
                ["clawmem", "update"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def get(self, prompt: str) -> Optional[str]:
        """Return a cached response for a semantically similar prompt, or None.

        Read-only lookup: it does not call an LLM or store anything on miss.
        Useful for gateway hooks that only want to query the cache without
        side effects. Errors (e.g. Ollama unreachable) return None so the
        caller can fall back to normal processing.
        """
        try:
            normalized = normalize_prompt(prompt)
            embedding = ollama_embed(normalized)
            hit = self._search(normalized, embedding)
            return hit["response_text"] if hit else None
        except Exception:
            return None

    def store(
        self,
        prompt: str,
        response: str,
        ttl: int = DEFAULT_TTL,
    ) -> None:
        """Store a response for a prompt, normalizing and embedding the prompt."""
        normalized = normalize_prompt(prompt)
        prompt_hash = hashlib.sha256(normalized.encode()).hexdigest()[:32]
        embedding = ollama_embed(normalized)
        self._store(prompt_hash, normalized, response, embedding, ttl)

    def get_or_call(
        self,
        prompt: str,
        callable: Callable[[str], str],
        ttl: int = DEFAULT_TTL,
    ) -> str:
        normalized = normalize_prompt(prompt)
        prompt_hash = hashlib.sha256(normalized.encode()).hexdigest()[:32]

        embedding = ollama_embed(normalized)
        hit = self._search(normalized, embedding)
        if hit:
            return hit["response_text"]

        response = callable(prompt)
        self._store(prompt_hash, normalized, response, embedding, ttl)
        return response


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------
def _demo() -> None:
    def fake_llm(prompt: str) -> str:
        time.sleep(0.2)  # simulate provider latency
        return f"Answer for: {prompt[:50]}"

    cache = SemanticCache()
    prompts = [
        "What is the default Hermes model?",
        "What is the default Hermes model?",  # exact duplicate
        "tell me the default Hermes model",  # semantic duplicate
        "What is the weather in Tokyo?",      # unrelated
    ]

    for p in prompts:
        start = time.time()
        answer = cache.get_or_call(p, fake_llm, ttl=300)
        elapsed = time.time() - start
        print(f"[{elapsed:.2f}s] {p!r} -> {answer!r}")


if __name__ == "__main__":
    _demo()
