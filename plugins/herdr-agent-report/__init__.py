"""Hermes plugin: report this pane's Hermes turn lifecycle to a Herdr server.

Herdr cannot classify Hermes panes by screen scraping: idle and working Hermes turn footers are
near-identical, so the ``hermes`` detection manifest matches nothing and every pane reports
``state: idle`` forever (``fallback_reason: default_known_agent_idle_fallback``). This plugin
bypasses screen detection entirely and pushes lifecycle events through Herdr's documented
custom-hook API (https://herdr.dev/tui/docs/add-herdr-support/):

    herdr pane report-agent <PANE_ID> --source hermes-agent-report --agent hermes-agent --state ...
    herdr pane release-agent <PANE_ID> --source hermes-agent-report --agent hermes-agent

SOURCE / AGENT LABEL CHOICE (verified live against herdr 0.9.5)
===============================================================
The docs require ``--source`` that does not start with ``herdr:`` (built-in integrations own
that prefix) and an ``--agent`` that is "your own agent's name, not the name of an agent Herdr
already supports". Both matter on 0.9.5: reports sent with ``--source herdr:hermes`` or with
``--agent hermes`` (a built-in kind) come back ``{"type":"ok"}`` but are silently never applied —
``pane get`` and ``agent list`` stay unchanged because the state is attributed to Herdr's own
hermes integration. With a distinct source + non-built-in agent label, the same call takes
effect immediately. Hence this plugin reports as source ``hermes-agent-report`` / agent
``hermes-agent``; the pane shows a "hermes-agent" lifecycle instead of a dead "hermes" one.

STATE MAPPING — HOOK DISPATCH SEMANTICS VERIFIED IN CODE (not guessed)
=====================================================================

Both turn hooks fire exactly once per turn, so the mapping below yields exactly one ``working``
per turn and one ``idle`` when the turn truly settles — no working/idle flapping mid-turn even
in multi-step tool-loop turns:

* ``pre_llm_call`` -> ``working``. Fired once per turn from
  ``agent.turn_context._collect_pre_llm_call_context`` (turn setup, before the first LLM call).
* ``post_llm_call`` -> ``idle``. Fired once per turn from
  ``agent.turn_finalizer._apply_output_hooks`` ("fire ``post_llm_call`` once per turn after the
  tool loop" — see its docstring). It is NOT fired per LLM call inside the tool loop.
* ``pre_approval_request`` -> ``blocked``. Fired just before an approval transport opens
  (``tools/approval_prompt.py``, ``tools/approval_gateway_wait.py``); the kwargs carry
  ``command`` / ``session_key`` / ``surface`` and no ``platform``.
* ``post_approval_response`` -> ``working``. Fired when the approval is settled; the turn
  proceeds, and ``post_llm_call`` still idles the pane at turn end. Dedupe makes the
  ``blocked -> working`` resend cheap and skips redundant re-sends.
* ``on_session_finalize`` -> ``release-agent``. The true end-of-session seam
  (``hermes_cli/cli_shutdown.py`` ``_notify_session_finalize``, ``hermes_cli/oneshot.py``
  ``_close_agent``, ``tui_gateway/session_lifecycle.py``); it fires once when the CLI/TUI/one-shot
  session tears down.
* ``on_session_end`` is deliberately NOT used for release: ``agent/turn_finalizer.py`` fires it
  once PER TURN (its comment notes "CLI/gateway own session-end cleanup"), so hooking it would
  release (and thus un-identify) the Herdr pane agent after every turn in interactive sessions.

PLATFORM GATE
=============
This plugin only matters inside a Herdr pane, so it is hard-gated on ``HERDR_ENV=1`` and a
non-empty ``HERDR_PANE_ID`` (env Herdr injects into managed panes). Additionally, when a hook's
kwargs carry a ``platform`` value (the turn hooks always do), it must be one of the interactive
surfaces {"cli", "tui", "desktop", "acp"}; hooks that fire without a platform kwarg (the approval
hooks) are not platform-gated — the HERDR_ENV gate is their boundary.

SAFETY RULES
============
* Monotonic ``--seq`` per process (``time.time_ns()`` bumped past the previous value under a
  lock) so late/out-of-order herdr CLI calls cannot overwrite a newer state.
* Consecutive identical states are deduped, so redundant hook firings never spam herdr.
* A failed (non-zero / raising) report reverts the dedupe memory so the next hook retry sends.
* Every hook call is wrapped: subprocess timeout 1s, stdout/stderr discarded, all exceptions
  swallowed — a broken ``herdr`` binary can never break the agent loop.
* Binaries resolve to ``HERDR_BIN_PATH`` when set (herdr-proof/test isolation), else ``herdr``.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

__all__ = ["register"]

_AGENT = "hermes-agent"
_SOURCE = "hermes-agent-report"
_INTERACTIVE_PLATFORMS = {"cli", "tui", "desktop", "acp"}

_MESSAGES = {
    "working": "hermes turn in progress",
    "blocked": "waiting for approval",
    "idle": "hermes turn finished",
}

_STATE_LOCK = threading.Lock()
_LAST_REPORTED: str | None = None  # last state successfully pushed to herdr, for dedupe
_LAST_SEQ = 0


def _pane_id() -> str | None:
    """Return the Herdr pane id, or None when not running inside a Herdr-managed pane."""
    if os.environ.get("HERDR_ENV") != "1":
        return None
    return os.environ.get("HERDR_PANE_ID", "").strip() or None


def _binary() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def _next_seq() -> int:
    """Strictly monotonic sequence number for this process (herdr orders by --seq)."""
    global _LAST_SEQ
    with _STATE_LOCK:
        _LAST_SEQ = max(_LAST_SEQ + 1, time.time_ns())
        return _LAST_SEQ


def _run_herdr(args: list[str]) -> bool:
    """Run one fire-and-forget herdr CLI call. Never raises; True only on exit code 0."""
    command = [_binary(), *args]
    kwargs: dict = {"timeout": 1.0, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(command, check=False, **kwargs)
    except Exception:
        return False
    return getattr(result, "returncode", 1) == 0


def _claim_state(state: str) -> tuple[bool, str | None]:
    """Atomically switch the dedupe memory to *state* when it differs from the last
    successfully reported state. Returns ``(claimed, previous)``: claimed is False when the
    identical state was already reported (nothing to send)."""
    global _LAST_REPORTED
    with _STATE_LOCK:
        previous = _LAST_REPORTED
        if state == previous:
            return False, previous
        _LAST_REPORTED = state
        return True, previous


def _rollback_state(state: str, previous: str | None) -> None:
    """Undo a claim whose herdr call failed, so the next hook retries sending."""
    global _LAST_REPORTED
    with _STATE_LOCK:
        if _LAST_REPORTED == state:
            _LAST_REPORTED = previous


def _clear_state() -> None:
    global _LAST_REPORTED
    with _STATE_LOCK:
        _LAST_REPORTED = None


def _platform_allowed(kwargs: dict) -> bool:
    """Gate to interactive platforms only when the hook carries a platform value.

    Hook kwargs without ``platform`` (pre/post_approval_request) are never platform-gated here;
    the HERDR_ENV gate is their boundary."""
    platform = kwargs.get("platform")
    if platform in (None, ""):
        return True
    return platform in _INTERACTIVE_PLATFORMS


def _report_state(state: str, **kwargs: object) -> None:
    """Push one lifecycle state to the Herdr server for this pane. Never raises."""
    try:
        if not _platform_allowed(kwargs):
            return
        claimed, previous = _claim_state(state)
        if not claimed:
            return
        pane_id = _pane_id()
        if pane_id is None:
            return
        ok = _run_herdr([
            "pane", "report-agent", pane_id,
            "--source", _SOURCE,
            "--agent", _AGENT,
            "--state", state,
            "--seq", str(_next_seq()),
            "--message", _MESSAGES.get(state, state),
        ])
        if not ok:
            _rollback_state(state, previous)
    except Exception:
        return


def _release_agent(**kwargs: object) -> None:
    """Give the pane's agent identity back to Herdr at true session end. Never raises."""
    try:
        if not _platform_allowed(kwargs):
            return
        pane_id = _pane_id()
        if pane_id is None:
            return
        if _run_herdr([
            "pane", "release-agent", pane_id,
            "--source", _SOURCE,
            "--agent", _AGENT,
            "--seq", str(_next_seq()),
        ]):
            _clear_state()  # a fresh session in this pane must report working again
    except Exception:
        return


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", lambda **kw: _report_state("working", **kw))
    ctx.register_hook("post_llm_call", lambda **kw: _report_state("idle", **kw))
    ctx.register_hook("pre_approval_request", lambda **kw: _report_state("blocked", **kw))
    ctx.register_hook("post_approval_response", lambda **kw: _report_state("working", **kw))
    ctx.register_hook("on_session_finalize", _release_agent)
