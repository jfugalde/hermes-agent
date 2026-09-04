"""Semantic cache auto-populate hook.

Listens on ``agent:end`` and caches the final assistant response when safe.
Uses the ``semantic_cache_safety`` classifier to avoid caching code, errors,
or multi-step agentic responses.  Fail-open: any exception returns
``{decision: "ignored"}`` and does not crash the agent.
"""

import sys
from pathlib import Path
from typing import Any, Dict

# Add scripts directory to path for shared modules — use Path.home() directly
# (get_hermes_home() is not reliably importable from hook module context)
sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

try:
    from semantic_cache_safety import is_safe_to_cache
except Exception:
    is_safe_to_cache = None  # type: ignore[assignment]

try:
    from clawmem_semantic_cache import SemanticCache
except Exception:
    SemanticCache = None  # type: ignore[assignment]


def _load_config() -> Dict[str, Any]:
    """Load the ``semantic_cache`` section from ``config.yaml``.

    Returns an empty dict on any error (file missing, parse failure, etc.)
    so the caller can treat it as "all gates closed".
    """
    config_path = Path.home() / ".hermes" / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) if yaml else {}
        return config.get("semantic_cache", {}) or {}
    except Exception:
        return {}


def handle(event_type: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """Handle ``agent:end`` — cache the response if safe.

    Returns ``{decision: "ignored"}`` in all cases so normal flow continues.
    The return value is discarded by the ``emit()`` path but kept for
    consistency with decision-style hooks and future debugging.
    """
    if event_type != "agent:end":
        return {"decision": "ignored"}

    # 1. Config gate — only proceed when both enabled and auto_populate are on
    sc_config = _load_config()
    if not sc_config.get("enabled") or not sc_config.get("auto_populate"):
        return {"decision": "ignored", "message": "config: disabled"}

    # 2. Extract prompt and response from the event context
    prompt = (context.get("message") or "").strip()
    response = (context.get("response") or "").strip()

    if not prompt or not response:
        return {"decision": "ignored", "message": "empty prompt or response"}

    # 3. Safety check — reject code, errors, shell commands, etc.
    if is_safe_to_cache is None:
        return {"decision": "ignored", "message": "safety module unavailable"}

    if not is_safe_to_cache(prompt, response=response):
        return {"decision": "ignored", "message": "safety: not cacheable"}

    # 4. Store in cache
    if SemanticCache is None:
        return {"decision": "ignored", "message": "cache module unavailable"}

    try:
        ttl = sc_config.get("ttl_seconds", 300)
        cache = SemanticCache()
        cache.store(prompt, response, ttl=ttl)
    except Exception:
        # Fail-open: any exception returns ignored and does not crash the agent
        pass

    return {"decision": "ignored"}
