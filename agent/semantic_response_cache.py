"""Semantic response cache integration shared by Hermes's oneshot
(``hermes -z``) path and the live gateway's plain-chat message handler.

Wraps the ClawMem-backed POC (``~/.hermes/scripts/clawmem_semantic_cache.py``)
behind a narrow eligibility gate so caching only ever applies to short,
tool-free, deterministic Q&A prompts. Everything else — tool calls, code
generation, and any turn with tool access — is left completely untouched.
Both callers (``hermes_cli/oneshot.py`` and ``gateway/run.py``) share this one
eligibility gate, one feature flag, and one ``SemanticCache`` instance so the
"is this prompt safe to cache?" definition never drifts between the two.

Safety model:
    - Off by default. Must be explicitly enabled via the
      ``HERMES_SEMANTIC_CACHE_ENABLED`` env var or the ``semantic_cache.enabled``
      config.yaml key.
    - Only eligible when the turn has zero toolsets enabled (no tool access
      at all, so there is nothing stateful or destructive it could have done
      anyway).
    - Rejects prompts that look like code generation, shell/file operations,
      or multi-step agentic requests via a conservative keyword heuristic.
    - Rejects long prompts (likely non-trivial requests).
    - Fails open: any error talking to Ollama/SQLite/ClawMem falls back to
      the real provider call rather than blocking or corrupting the response.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

_ENV_FLAG = "HERMES_SEMANTIC_CACHE_ENABLED"
_DEFAULT_TTL_SECONDS = 3600
_MAX_PROMPT_CHARS = 2000

# Conservative "this is not a safe cache candidate" heuristic. Any match
# disqualifies the prompt — false negatives (skipping a cacheable prompt)
# are cheap, false positives (caching something unsafe) are not.
_UNSAFE_PROMPT_RE = re.compile(
    r"```"
    r"|\b(write|generate|create|refactor|fix)\b[^.\n]{0,40}\b(script|function|code|program|class|file|bug)\b"
    r"|\b(run|execute|install|deploy|delete|rm\s+-rf|curl|ssh|git\s+push|git\s+commit)\b"
    r"|\b(edit|open|read|write)\b[^.\n]{0,20}\bfile\b"
    r"|\btool[_ ]call\b",
    re.IGNORECASE,
)


def _scripts_dir() -> Path:
    return Path.home() / ".hermes" / "scripts"


def _load_poc_module():
    """Lazily import the ClawMem semantic cache POC from ``~/.hermes/scripts``.

    Kept as a plain module import (rather than a packaged dependency) so the
    gateway integration and the standalone POC/demo script share one
    implementation without requiring the POC to be installed as a package.
    """
    scripts_dir = str(_scripts_dir())
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import clawmem_semantic_cache  # noqa: PLC0415 - deliberate lazy import

    return clawmem_semantic_cache


_cache_instance: Optional[Any] = None


def get_cache() -> Any:
    """Return the process-wide SemanticCache instance, creating it on first use."""
    global _cache_instance
    if _cache_instance is None:
        module = _load_poc_module()
        _cache_instance = module.SemanticCache()
    return _cache_instance


def _cache_enabled(cfg: Optional[dict]) -> bool:
    env_value = os.getenv(_ENV_FLAG)
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(cfg, dict):
        semantic_cfg = cfg.get("semantic_cache")
        if isinstance(semantic_cfg, dict):
            return bool(semantic_cfg.get("enabled", False))
    return False


def is_cache_enabled(cfg: Optional[dict]) -> bool:
    """Public wrapper around the feature-flag check.

    Exposed so callers (e.g. ``gateway/run.py``) can cheaply bail out before
    doing any per-turn work (resolving toolsets, etc.) when the cache is off
    — which is the default, and therefore the hot path for every message.
    """
    return _cache_enabled(cfg)


def get_cache_ttl(cfg: Optional[dict]) -> int:
    if isinstance(cfg, dict):
        semantic_cfg = cfg.get("semantic_cache")
        if isinstance(semantic_cfg, dict):
            try:
                return int(semantic_cfg.get("ttl_seconds", _DEFAULT_TTL_SECONDS))
            except (TypeError, ValueError):
                pass
    return _DEFAULT_TTL_SECONDS


def is_cache_eligible(
    prompt: str,
    toolsets: Optional[list[str]],
    cfg: Optional[dict],
) -> bool:
    """Narrow allowlist gate: only short, tool-free, non-code Q&A prompts.

    ``toolsets`` must be the *fully resolved* list of toolsets that would be
    enabled for this turn (explicit ``--toolsets``, or the platform default
    when none was given) — a non-empty list means the agent has tool access
    for this turn and is disqualified, regardless of what the prompt text
    looks like.
    """
    if not _cache_enabled(cfg):
        return False
    if toolsets:
        return False
    if not prompt or not prompt.strip():
        return False
    if len(prompt) > _MAX_PROMPT_CHARS:
        return False
    if _UNSAFE_PROMPT_RE.search(prompt):
        return False
    return True
