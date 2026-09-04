"""Pre-dispatch semantic cache hook.

Listens on ``agent:start`` and short-circuits the LLM call when a safe
cached response exists.  Fail-open: any exception returns ``ignored``
so normal processing continues.

Handler signature
-----------------
    def handle(event_type: str, context: dict) -> dict | None

Return values
-------------
    {"decision": "handled", "message": str}  -- short-circuit with cached response
    {"decision": "ignored"}                   -- let normal processing continue
    None                                       -- same as ignored (legacy)
"""

import os
import sys
from pathlib import Path

import yaml

# Resolve the Hermes home directory the same way the existing
# semantic-cache hook does — try the official import first, fall
# back to env var / Path.home() so it works both inside and
# outside the gateway process.
try:
    from hermes_cli.config import get_hermes_home
except Exception:

    def get_hermes_home() -> Path:
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hermes"


_SCRIPTS_DIR = get_hermes_home() / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from semantic_cache_safety import is_safe_to_cache
from clawmem_semantic_cache import SemanticCache


def handle(event_type: str, context: dict) -> dict | None:
    """Check semantic cache before LLM dispatch.

    Parameters
    ----------
    event_type:
        The event identifier (e.g. ``"agent:start"``).
    context:
        Dict with keys ``platform``, ``user_id``, ``session_id``,
        ``message``, ``chat_id``, ``thread_id``, ``chat_type``.
        May also contain ``toolsets`` (list of active toolset names).

    Returns
    -------
    dict or None
        ``{"decision": "handled", "message": str}`` on cache hit,
        ``{"decision": "ignored"}`` on miss or skip, or ``None``.
    """
    # ------------------------------------------------------------------
    # 1. Config gate
    # ------------------------------------------------------------------
    try:
        config_path = get_hermes_home() / "config.yaml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        sc_config = config.get("semantic_cache", {})
        if not sc_config.get("enabled", False):
            return {"decision": "ignored"}
        if not sc_config.get("pre_dispatch", True):
            return {"decision": "ignored"}
    except Exception:
        # Fail-open: config errors don't block the agent
        return {"decision": "ignored"}

    # ------------------------------------------------------------------
    # 2. Toolsets check — non-empty toolsets always skip
    # ------------------------------------------------------------------
    toolsets = context.get("toolsets")
    if toolsets and isinstance(toolsets, (list, tuple)) and len(toolsets) > 0:
        return {"decision": "ignored"}

    # ------------------------------------------------------------------
    # 3. Extract prompt
    # ------------------------------------------------------------------
    message = context.get("message", "")
    if not message or not message.strip():
        return {"decision": "ignored"}

    # ------------------------------------------------------------------
    # 4. Safety classifier
    # ------------------------------------------------------------------
    if not is_safe_to_cache(message):
        return {"decision": "ignored"}

    # ------------------------------------------------------------------
    # 5. Cache lookup
    # ------------------------------------------------------------------
    try:
        cache = SemanticCache()
        cached_response = cache.get(message)
        if cached_response:
            return {
                "decision": "handled",
                "message": cached_response,
            }
    except Exception:
        # Fail-open: cache errors don't block the agent
        pass

    return {"decision": "ignored"}
