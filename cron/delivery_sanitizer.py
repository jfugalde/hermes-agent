"""Sanitize an agent's final response before it is delivered to a human.

WHY THIS EXISTS
On a successful run ``_compose_run_delivery`` delivered ``final_response`` verbatim. Two observed
failure modes therefore reached the user as if they *were* the lesson:

1. **Error-shaped bodies.** The whole message was an exception line::

       FileNotFound: /home/…/cache/audio/tts_20261019_140157_831824.mp3

   In one case the "missing" file existed, was executable, and ran correctly when tried by hand —
   the model invented the error. Another run returned ``FileNotFound: No such file or directory:
   '…/scripts/full-reading.py'`` about a 175-line script that had not been touched in four days.
   Nothing checked the body, so a fabrication was delivered as the lesson.

2. **Documentation dumps.** The prompt injects a skill in full. The model then *continues* the
   skill document instead of executing it: it starts at ``## Response Evaluation``, reproduces the
   procedure section by section (one run repeated a template 25 times) and only reaches the real
   lesson ~250 lines and ~39 KB later. The prompt already said "do not dump the skill in the chat";
   a prompt cannot enforce that.

This module is pure and standard-library-only, so the scheduler and the tests share it and neither
needs the network, the clock, or the agent.

DESIGN RULE
Prefer *cutting* over *rewriting*. We never paraphrase the model's words — we remove noise in front
of them, stop a runaway repeat, cap the size, and refuse to ship a body that is only an error. When
nothing usable survives we say so explicitly instead of delivering rubbish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Chat messages in this product begin with one of these headers (the session templates define
# them). A response that carries one has real content FROM that point on; everything before it is
# prelude — usually the skill being echoed back.
DEFAULT_CONTENT_MARKERS: tuple[str, ...] = (
    "📐", "🎯", "🎧", "🗣️", "📍", "🔄", "✍️", "📝", "📖", "🌙", "☀️", "👋", "🔊", "🌸", "📚", "⭐",
)

# First-line signatures of an exception rendered as text.
_ERROR_START_RES = (
    re.compile(r"^FileNotFound(Error)?\s*:"),
    re.compile(r"^\[Errno\s+-?\d+\]"),
    re.compile(r"^Traceback\s+\(most\s+recent\s+call\s+last\)"),
    re.compile(r"^[A-Za-z_]*(Error|Exception)\s*:"),
    re.compile(r"^ERROR\s*:"),
)

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_RULE_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")

DEFAULT_MAX_CHARS = 6000
DEFAULT_MAX_REPEATS = 3


@dataclass(frozen=True)
class SanitizedMessage:
    """Result of :func:`sanitize_agent_response`.

    ``text`` is what should be delivered (may be empty when nothing usable survived).
    ``degraded_reason`` is ``None`` for a clean message, otherwise a short machine-readable tag
    (``"error_shaped"``, ``"documentation_dump"``, ``"repetition"``, ``"too_long"``,
    ``"empty"``) describing what was removed — joined with ``+`` when several applied.
    """

    text: str
    degraded_reason: str | None = None
    removed_chars: int = 0

    @property
    def is_degraded(self) -> bool:
        return self.degraded_reason is not None


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def is_error_shaped(text: str) -> bool:
    """True when the message is nothing but a rendered exception.

    Deliberately narrow: the FIRST non-empty line must look like an exception *and* the body must
    be short enough to be one. A real lesson that happens to mention an error deep inside is not
    touched.
    """
    first = _first_non_empty_line(text)
    if not first:
        return False
    if not any(rx.match(first) for rx in _ERROR_START_RES):
        return False
    # An exception render is short. A 40 KB document that happens to open with "Error:" is a
    # different problem (handled by size/repetition), not this one.
    return len(text) <= 800


def truncate_repetition(text: str, *, max_repeats: int = DEFAULT_MAX_REPEATS) -> tuple[str, bool]:
    """Cut the text at the point a line starts repeating beyond ``max_repeats``.

    Catches the observed degeneration (a template reproduced 25 times) without touching a normal
    message, where repeated lines are short (``---``, blank, a list bullet).
    """
    if max_repeats <= 0:
        return text, False
    lines = text.splitlines(keepends=True)
    seen: dict[str, int] = {}
    for idx, line in enumerate(lines):
        key = line.strip()
        if len(key) < 12:  # punctuation, separators, short bullets: legitimate repetition
            continue
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > max_repeats:
            kept = "".join(lines[:idx]).rstrip()
            return kept, True
    return text, False


def strip_prelude(text: str, content_markers: tuple[str, ...] = DEFAULT_CONTENT_MARKERS) -> tuple[str, bool]:
    """Remove anything before the first real content marker.

    Prefers an explicit template header (see ``DEFAULT_CONTENT_MARKERS``). Falls back to dropping
    leading markdown-heading sections, because a chat message to a human does not open with ``##``
    the way a document does.
    """
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(content_markers):
            head = "\n".join(lines[:idx]).strip()
            return "\n".join(lines[idx:]).strip(), bool(head)

    # Fallback: drop leading heading SECTIONS — the heading line AND its body, up to the next
    # heading or the next horizontal rule. Dropping only the `## ` line would leave the section's
    # prose stranded at the top of the message, which is the same noise with fewer clues.
    lines = text.splitlines()
    idx = 0
    dropped = False
    while idx < len(lines):
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx >= len(lines) or not _HEADING_RE.match(lines[idx]):
            break
        dropped = True
        idx += 1  # consume the heading itself
        while idx < len(lines):
            if _HEADING_RE.match(lines[idx]):
                break
            if _RULE_RE.match(lines[idx]):
                idx += 1  # consume the rule that closes the section
                break
            idx += 1
    if dropped:
        return "\n".join(lines[idx:]).strip(), True
    return text.strip(), False


def cap_length(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, bool]:
    """Truncate on a paragraph boundary and mark the cut."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    boundary = cut.rfind("\n\n")
    if boundary > max_chars // 2:
        cut = cut[:boundary]
    return cut.rstrip() + "\n\n… (mensaje recortado)", True


def sanitize_agent_response(
    text: str | None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_repeats: int = DEFAULT_MAX_REPEATS,
    content_markers: tuple[str, ...] = DEFAULT_CONTENT_MARKERS,
) -> SanitizedMessage:
    """Return the deliverable text plus why it was degraded (``None`` when untouched)."""
    original = text or ""
    if not original.strip():
        return SanitizedMessage("", "empty", 0)

    if is_error_shaped(original):
        # Never ship a bare exception as a lesson. Callers substitute a human notice.
        return SanitizedMessage("", "error_shaped", len(original))

    reasons: list[str] = []
    working = original

    working, prelude_dropped = strip_prelude(working, content_markers)
    if prelude_dropped:
        reasons.append("documentation_dump")
    if not working.strip():
        return SanitizedMessage("", "+".join(reasons + ["empty"]), len(original))

    working, repetition_cut = truncate_repetition(working, max_repeats=max_repeats)
    if repetition_cut:
        reasons.append("repetition")

    working, length_cut = cap_length(working, max_chars=max_chars)
    if length_cut:
        reasons.append("too_long")

    reason = "+".join(reasons) if reasons else None
    return SanitizedMessage(working, reason, max(0, len(original) - len(working)))


def degraded_run_notice(job_name: str, reason: str | None) -> str:
    """Human-facing notice when a run produced nothing deliverable.

    Mirrors the failure-notice style already used by the scheduler: name the job, say what
    happened, and make the operator's next step obvious.
    """
    detail = {
        "error_shaped": "la respuesta fue solo un mensaje de error, no una lección",
        "empty": "la respuesta llegó vacía",
    }.get(reason or "", "la respuesta no contenía contenido entregable")

    return (
        f"⚠️ *{_escape(job_name)}* no pudo entregar la sesión: {detail}.\n\n"
        f"El run terminó, pero no se envió nada para no entregarte basura. "
        f"Motivo técnico: `{_escape(reason or 'unknown')}`.\n"
        f"Revisa el run con `hermes cron runs`."
    )


def _escape(value: str) -> str:
    return str(value).replace("`", "'")
