#!/usr/bin/env python3
"""Shared safety classifier for the semantic response cache.

Exports ``is_safe_to_cache`` and ``skip_reason`` so both the auto-populate
script and the pre-dispatch gateway hook can reuse the same conservative
heuristic: only short, tool-free, deterministic Q&A prompts are cacheable.

Conservative / false-positive-free design
------------------------------------------
Returning ``False`` for a cacheable prompt is cheap (the real provider call
still happens).  Returning ``True`` for an unsafe prompt could serve a stale
or destructive cached response.  Therefore the classifier errs on the side of
rejection — every heuristic is a narrow allowlist, not a broad blocklist.

Usage::

    from semantic_cache_safety import is_safe_to_cache, skip_reason

    if not is_safe_to_cache(prompt, response=response):
        logger.info("skip: %s", skip_reason(prompt, response=response))
        return  # don't cache
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_PROMPT_CHARS = 2000

# Patterns that make a prompt unsafe to cache.  Each is a compiled regex;
# any match disqualifies the prompt.
_UNSAFE_PROMPT_PATTERNS: list[re.Pattern] = [
    # Code fences
    re.compile(r"```"),
    # Shell / file / git / network commands
    re.compile(
        r"\b(?:"
        r"rm\s+-rf"
        r"|curl\b"
        r"|ssh\b"
        r"|git\s+(?:push|commit|merge|rebase|checkout|pull|clone|reset)"
        r"|docker\s+(?:run|exec|compose|build|push)"
        r"|kubectl\b"
        r"|npm\s+(?:install|publish|run)"
        r"|pip\s+install"
        r"|chmod\b"
        r"|sudo\b"
        r"|wget\b"
        r"|scp\b"
        r"|rsync\b"
        r"|systemctl\b"
        r"|journalctl\b"
        r")\b"
    ),
    # Tool-call mentions
    re.compile(r"\btool[_ ]call\b", re.IGNORECASE),
    # Multi-step agentic request patterns
    re.compile(
        r"\b(?:"
        r"search\s+(?:for|the|on)"
        r"|look\s+up"
        r"|find\s+(?:out|the|me)"
        r"|go\s+(?:to|and)"
        r"|navigate\s+to"
        r"|fetch\s+(?:the|data|url)"
        r"|scrape\b"
        r"|crawl\b"
        r")\b",
        re.IGNORECASE,
    ),
    # File-path-like patterns (absolute paths, home dirs)
    re.compile(r"(?:/[\w.\-]+){2,}"),
    re.compile(r"~[/.][\w.\-]+"),
]

# Patterns that make a *response* unsafe to cache.
_UNSAFE_RESPONSE_PATTERNS: list[re.Pattern] = [
    re.compile(r"```"),
    re.compile(
        r"\b(?:"
        r"Traceback\s*\(most\s+recent\s+call\s+last\)"
        r"|Error:"
        r"|Exception:"
        r"|SyntaxError"
        r"|NameError"
        r"|TypeError"
        r"|ValueError"
        r"|KeyError"
        r"|IndexError"
        r"|AttributeError"
        r"|ImportError"
        r"|ModuleNotFoundError"
        r"|FileNotFoundError"
        r"|PermissionError"
        r"|OSError"
        r"|RuntimeError"
        r"|ZeroDivisionError"
        r"|stack\s+trace"
        r")"
    ),
    # Shell commands in response (someone pasted a command)
    re.compile(r"\$\s+(?:curl|ssh|git|docker|kubectl|npm|pip|sudo|rm)\b"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_safe_to_cache(
    prompt: str,
    response: Optional[str] = None,
    context: Optional[dict] = None,
) -> bool:
    """Return ``True`` if the prompt (and optional response) are safe to cache.

    Parameters
    ----------
    prompt:
        The user's input text.
    response:
        Optional response text to also check.  When provided, the response
        is checked for error markers, code fences, and shell commands.
    context:
        Optional dict with additional metadata (currently unused; reserved
        for future heuristics such as toolset flags or conversation length).

    Returns
    -------
    bool
        ``True`` only when the prompt (and response, if given) pass every
        safety check.
    """
    # --- Prompt-level checks ---

    if not prompt or not prompt.strip():
        return False

    if len(prompt) > _MAX_PROMPT_CHARS:
        return False

    for pattern in _UNSAFE_PROMPT_PATTERNS:
        if pattern.search(prompt):
            return False

    # --- Response-level checks (optional) ---

    if response is not None:
        if not response.strip():
            return False

        for pattern in _UNSAFE_RESPONSE_PATTERNS:
            if pattern.search(response):
                return False

    return True


def skip_reason(
    prompt: str,
    response: Optional[str] = None,
    context: Optional[dict] = None,
) -> str:
    """Return a human-readable explanation of why this prompt is not cacheable.

    Returns an empty string when the prompt IS safe to cache (all checks
    passed).  This is a companion to ``is_safe_to_cache`` — call it when
    ``is_safe_to_cache`` returned ``False`` to get a log-friendly reason.

    Parameters
    ----------
    prompt:
        The user's input text.
    response:
        Optional response text to also check.
    context:
        Optional dict with additional metadata (currently unused).

    Returns
    -------
    str
        A short reason string (e.g. ``"prompt: empty"``, ``"prompt: code
        fences"``, ``"response: error traceback"``) or ``""`` if safe.
    """
    # --- Prompt-level checks ---

    if not prompt or not prompt.strip():
        return "prompt: empty or whitespace-only"

    if len(prompt) > _MAX_PROMPT_CHARS:
        return f"prompt: too long ({len(prompt)} > {_MAX_PROMPT_CHARS} chars)"

    for pattern in _UNSAFE_PROMPT_PATTERNS:
        if pattern.search(prompt):
            return f"prompt: matches unsafe pattern {pattern.pattern!r}"

    # --- Response-level checks (optional) ---

    if response is not None:
        if not response.strip():
            return "response: empty or whitespace-only"

        for pattern in _UNSAFE_RESPONSE_PATTERNS:
            if pattern.search(response):
                return f"response: matches unsafe pattern {pattern.pattern!r}"

    return ""
