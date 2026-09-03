"""Semantic-cache slash-command hook.

Usage in chat:
    /cacheask What is the default Hermes model?

The handler queries the local semantic cache (Ollama embeddings + SQLite) and
returns the cached response if a sufficiently similar prompt exists.
It never calls the LLM, so it is safe for low-stakes, repetitive Q&A.
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from hermes_cli.config import get_hermes_home
except Exception:

    def get_hermes_home() -> Path:
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hermes"


_SCRIPTS_DIR = get_hermes_home() / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from clawmem_semantic_cache import SemanticCache
except Exception as _import_err:
    SemanticCache = None  # type: ignore[misc,assignment]
    _CACHE_IMPORT_ERROR = _import_err


_cache_instance: Optional[SemanticCache] = None


def _get_cache() -> Optional[SemanticCache]:
    global _cache_instance
    if SemanticCache is None:
        return None
    if _cache_instance is None:
        _cache_instance = SemanticCache()
    return _cache_instance


def handle(event_type: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle /cacheask <question>."""
    if event_type != "command:cacheask":
        return None

    args = str(context.get("args") or context.get("raw_args") or "").strip()
    if not args:
        return {
            "decision": "handled",
            "message": (
                "Usage: `/cacheask <question>`\n\n"
                "Looks up a cached answer for low-stakes, repetitive Q&A."
            ),
        }

    if SemanticCache is None:
        return {
            "decision": "handled",
            "message": (
                "Semantic cache is unavailable.\n\n"
                f"Import error: {_CACHE_IMPORT_ERROR}"
            ),
        }

    cache = _get_cache()
    if cache is None:
        return {
            "decision": "handled",
            "message": "Semantic cache could not be initialized.",
        }

    try:
        cached_response = cache.get(args)
    except Exception as exc:
        return {
            "decision": "handled",
            "message": f"Cache lookup failed: {exc}",
        }

    if cached_response:
        return {
            "decision": "handled",
            "message": f"*[cached]*\n\n{cached_response}",
        }

    return {
        "decision": "handled",
        "message": (
            "No cached answer found for this question.\n\n"
            "Ask normally and the response will be cached for future similar questions."
        ),
    }
