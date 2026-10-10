"""Ollama Cloud web search — POST https://ollama.com/api/web_search (search only)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from plugins.web._common import (
    SEARCH_LIMIT_CAP,
    BaseWebSearchProvider,
    provider_env,
    run_search,
    search_fail,
    setup_schema,
    title_hit,
)

logger = logging.getLogger(__name__)

_API_URL = "https://ollama.com/api/web_search"
_KEY_ENVS = ("OLLAMA_API_KEY", "OLLAMA_API_KEY_FALLBACK")


class OllamaCloudWebSearchProvider(BaseWebSearchProvider):
    NAME = "ollama-cloud-websearch"
    DISPLAY_NAME = "Ollama Cloud Web Search"
    KEY_ENV = "OLLAMA_API_KEY"

    def is_available(self) -> bool:
        return any(bool(provider_env(n)) for n in _KEY_ENVS)

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        capped = max(1, min(int(limit), SEARCH_LIMIT_CAP))

        def body() -> Dict[str, Any]:
            last = ""
            for name in _KEY_ENVS:
                key = provider_env(name)
                if not key:
                    continue
                try:
                    resp = httpx.post(
                        _API_URL,
                        json={"query": query, "max_results": capped},
                        headers={"Authorization": f"Bearer {key}"},
                        timeout=30.0,
                    )
                except Exception as exc:
                    last = f"{name}: {exc}"
                    continue
                if 200 <= resp.status_code < 300:
                    try:
                        raw = resp.json().get("results") or []
                    except Exception:
                        return search_fail("Ollama Cloud web search returned invalid JSON")
                    hits: List[Dict[str, Any]] = [
                        title_hit(str(r.get("title", "")), str(r.get("url", "")), str(r.get("content", "")), i + 1)
                        for i, r in enumerate(raw)
                    ]
                    logger.info("Ollama Cloud web search %r: %d results", query, len(hits))
                    return {"success": True, "data": {"web": hits}}
                last = f"{name}: HTTP {resp.status_code} {(resp.text or '').strip()[:200]}"
            return search_fail(f"Ollama Cloud web search failed ({last or 'no Ollama key configured'})")

        return run_search("Ollama Cloud", logger, body)

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "Ollama Cloud Web Search",
            "free · Ollama Cloud key · search only",
            "Search via https://ollama.com/api/web_search",
            key_env=self.KEY_ENV,
            prompt="Ollama Cloud API key",
            url="https://ollama.com/account/api",
        )
