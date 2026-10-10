"""Ollama Cloud web search plugin (bundled, auto-loaded)."""
from __future__ import annotations
from plugins.web.ollama_cloud_websearch.provider import OllamaCloudWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(OllamaCloudWebSearchProvider())
