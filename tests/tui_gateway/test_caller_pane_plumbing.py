"""Option-B plumbing: pane identity from the submitting CLI client into gateway hook kwargs.

The CLI client sends ``prompt.submit`` ``caller_pane`` (only when it runs inside a Herdr pane),
the gateway stashes it on the session per submit, binds it on the run agent per turn, and every
turn/approval/session-finalize hook receives it as the ``pane_id`` kwarg. Gateway-side plugins
(Herdr lifecycle reporting) read that kwarg instead of the HERDR_* env the gateway process lacks.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

PANE = "wT:p9"


@pytest.fixture(autouse=True)
def _clean_pane_context():
    """Approval pane context is a ContextVar: pytest runs tests in one thread, so always start
    cleared to keep the fixture order irrelevant."""
    from tools.approval_context import bind_pane_context

    bind_pane_context("")
    yield
    bind_pane_context("")


# ---------------------------------------------------------------------------
# Wire contract + client helper
# ---------------------------------------------------------------------------


class TestCallerPaneContract:
    def test_optional_field_default_none(self):
        from tui_gateway.contracts.prompt_voice import PromptSubmitParams

        assert PromptSubmitParams(session_id="sW:s1", text="hi").caller_pane is None
        assert PromptSubmitParams(session_id="sW:s1", text="hi", caller_pane=PANE).caller_pane == PANE

    def test_max_length_64(self):
        from tui_gateway.contracts.prompt_voice import PromptSubmitParams

        with pytest.raises(ValidationError):
            PromptSubmitParams(session_id="sW:s1", text="hi", caller_pane="x" * 65)
        assert PromptSubmitParams(session_id="sW:s1", text="hi", caller_pane="x" * 64).caller_pane == "x" * 64

    def test_unknown_keys_still_forbidden(self):
        from tui_gateway.contracts.prompt_voice import PromptSubmitParams

        with pytest.raises(ValidationError):
            # ``pane_id`` is the HOOK kwarg name, not a wire param — the wire param is ``caller_pane``.
            PromptSubmitParams.model_validate({"session_id": "sW:s1", "text": "hi", "pane_id": PANE})


class TestCallerPaneCanonicalSubmit:
    """The CLI's ``-z`` oneshot submits through the CANONICAL ``prompt.submit`` contract, not the
    standalone ``prompt_voice`` one: ``Params`` forbids unknown keys, so a field added only to the
    standalone model makes every in-pane turn fail with ``invalid_params``. Guard the canonical model
    itself and the live registration, so the two can never drift again."""

    def test_canonical_submit_accepts_caller_pane(self):
        from tui_gateway.contracts.canonical_sessions import CanonicalSubmitParams

        assert CanonicalSubmitParams(session_id="sW:s1", text="hi").caller_pane is None
        assert CanonicalSubmitParams(
            session_id="sW:s1", text="hi", caller_pane=PANE).caller_pane == PANE
        with pytest.raises(ValidationError):
            CanonicalSubmitParams.model_validate(
                {"session_id": "sW:s1", "text": "hi", "pane_id": PANE})

    def test_registered_prompt_submit_model_carries_caller_pane(self):
        from tui_gateway.contracts import canonical_sessions
        from tui_gateway.contracts.registry import CANONICAL_METHODS

        assert canonical_sessions.CanonicalSubmitParams is CANONICAL_METHODS["prompt.submit"].params

        params_model = CANONICAL_METHODS["prompt.submit"].params
        assert "caller_pane" in params_model.model_fields
        # And the real wire payload the CLI sends validates against the registered model.
        validated = params_model.model_validate(
            {"session_id": "sW:s1", "text": "hi", "caller_pane": PANE})
        assert validated.model_dump()["caller_pane"] == PANE


class TestCallerPaneParam:
    def test_empty_outside_herdr(self, monkeypatch):
        from hermes_cli.gateway_client import caller_pane_param

        monkeypatch.delenv("HERDR_ENV", raising=False)
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
        assert caller_pane_param() == {}  # sends nothing outside a Herdr pane

    def test_pane_under_herdr_env(self, monkeypatch):
        from hermes_cli.gateway_client import caller_pane_param

        monkeypatch.setenv("HERDR_ENV", "1")
        monkeypatch.setenv("HERDR_PANE_ID", f"  {PANE} ")
        assert caller_pane_param() == {"caller_pane": PANE}

    def test_blank_pane_id_sends_nothing(self, monkeypatch):
        from hermes_cli.gateway_client import caller_pane_param

        monkeypatch.setenv("HERDR_ENV", "1")
        monkeypatch.setenv("HERDR_PANE_ID", "   ")
        assert caller_pane_param() == {}


# ---------------------------------------------------------------------------
# Submit-side stash + per-turn bind
# ---------------------------------------------------------------------------


class TestStashAndBind:
    def test_stash_caller_pane_per_submit(self):
        from tui_gateway.methods_prompt import _stash_caller_pane

        session: dict = {}
        _stash_caller_pane(session, {"caller_pane": f"  {PANE} "})
        assert session["caller_pane"] == PANE
        _stash_caller_pane(session, {})  # pane-less submit clears it (stale pane must not stick)
        assert session["caller_pane"] == ""
        _stash_caller_pane(session, {"caller_pane": "x" * 100})
        assert session["caller_pane"] == "x" * 64

    def test_bind_caller_pane_to_agent_and_approval_context(self, monkeypatch):
        from tui_gateway import prompt_turn
        from tools.approval_context import current_pane_id

        agent = SimpleNamespace()
        prompt_turn._bind_caller_pane(agent, {"caller_pane": PANE})
        assert agent._caller_pane == PANE
        assert current_pane_id() == PANE

    def test_bind_caller_pane_clears_when_absent(self):
        from tui_gateway import prompt_turn
        from tools.approval_context import current_pane_id

        agent = SimpleNamespace(_caller_pane=PANE)
        prompt_turn._bind_caller_pane(agent, {})
        assert agent._caller_pane == ""
        assert current_pane_id() == ""

    def test_bind_survives_frozen_agent(self):
        from tui_gateway import prompt_turn
        from tools.approval_context import current_pane_id

        class Frozen:
            __slots__ = ()

        agent = Frozen()
        prompt_turn._bind_caller_pane(agent, {"caller_pane": PANE})  # must not raise
        assert current_pane_id() == PANE  # approval context bound even when the agent rejected it


# ---------------------------------------------------------------------------
# Turn hooks receive ``pane_id``
# ---------------------------------------------------------------------------


def _turn_agent():
    return SimpleNamespace(
        session_id="s1", model="m", platform="cli",
        _parent_session_id="", _user_id="", _caller_pane=PANE, _persist_disabled=False)


class TestTurnHookPaneKwarg:
    def test_pre_llm_call_receives_pane_id(self, monkeypatch):
        from agent import turn_context

        captured = {}

        def capture(hook_name, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", capture)
        turn_context._collect_pre_llm_call_context(
            _turn_agent(), effective_task_id="t1", turn_id="tn1",
            original_user_message="hi", messages=[{"role": "user", "content": "hi"}],
            conversation_history=None)
        assert captured["pane_id"] == PANE
        assert captured["session_id"] == "s1" and captured["platform"] == "cli"

    def test_pre_llm_call_pane_id_empty_without_pane(self, monkeypatch):
        from agent import turn_context

        captured = {}
        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kw: captured.update(kw) or [])
        agent = _turn_agent()
        agent._caller_pane = ""  # pane-less session (desktop/web) — must not leak a stale pane
        turn_context._collect_pre_llm_call_context(
            agent, effective_task_id="t1", turn_id="tn1", original_user_message="hi",
            messages=[], conversation_history=None)
        assert captured["pane_id"] == ""

    def test_post_llm_call_receives_pane_id(self, monkeypatch):
        from agent import turn_finalizer

        captured = {}

        def capture(name, logger_, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(turn_finalizer, "_invoke_hook_safely", capture)
        monkeypatch.setattr(
            turn_finalizer, "apply_llm_output_transform",
            lambda agent, response, **kw: (response, False, None))
        turn_finalizer._apply_output_hooks(
            _turn_agent(), "ok", logging.getLogger("test"), platform="cli",
            effective_task_id="t1", turn_id="tn1", original_user_message="hi", messages=[])
        assert captured["pane_id"] == PANE


# ---------------------------------------------------------------------------
# Approval hooks receive ``pane_id``
# ---------------------------------------------------------------------------


class TestApprovalHookPaneKwarg:
    def test_approval_hook_carries_bound_pane(self, monkeypatch):
        from tools.approval_context import _fire_approval_hook, bind_pane_context

        bind_pane_context(PANE)
        captured = {}
        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook",
                            lambda name, **kw: captured.update(kw) or [])
        _fire_approval_hook("pre_approval_request", command="git push")
        assert captured["pane_id"] == PANE

    def test_approval_hook_pane_defaults_empty(self, monkeypatch):
        from tools.approval_context import _fire_approval_hook

        captured = {}
        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook",
                            lambda name, **kw: captured.update(kw) or [])
        _fire_approval_hook("pre_approval_request", command="git push")
        assert captured["pane_id"] == ""

    def test_kwarg_explicitly_passed_wins(self, monkeypatch):
        from tools.approval_context import _fire_approval_hook, bind_pane_context

        bind_pane_context(PANE)
        captured = {}
        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kw: captured.update(kw) or [])
        _fire_approval_hook("pre_approval_request", command="x", pane_id="wT:other")
        assert captured["pane_id"] == "wT:other"


# ---------------------------------------------------------------------------
# Session finalize carries ``pane_id``
# ---------------------------------------------------------------------------


class TestSessionBoundaryPaneKwarg:
    def test_finalize_receives_pane_id(self, monkeypatch):
        from tui_gateway import session_lifecycle

        # _resolve_agent_platform is server.py's global (bodies are rebound at install time);
        # standalone, inject it the same way.
        monkeypatch.setattr(session_lifecycle, "_resolve_agent_platform", lambda p: p or "tui",
                            raising=False)
        captured = {}
        monkeypatch.setattr("hermes_cli.lifecycle.finalize_session",
                            lambda **kw: captured.update(kw) or [])
        session = {"caller_pane": f"  {PANE} "}
        session_lifecycle._notify_session_boundary(
            "on_session_finalize", "s1", "cli", caller_pane=str(session.get("caller_pane") or ""))
        assert captured["pane_id"] == PANE
        assert captured["platform"] == "cli"

    def test_finalize_pane_id_empty_default(self, monkeypatch):
        from tui_gateway import session_lifecycle

        monkeypatch.setattr(session_lifecycle, "_resolve_agent_platform", lambda p: p or "tui",
                            raising=False)
        captured = {}
        monkeypatch.setattr("hermes_cli.lifecycle.finalize_session",
                            lambda **kw: captured.update(kw) or [])
        session_lifecycle._notify_session_boundary("on_session_finalize", "s1", "cli")
        assert captured["pane_id"] == ""


class TestCallerPaneFallback:
    """A newer client must never be blocked by an older owner: ``caller_pane`` is advisory, and its
    validation refusal happens before any admission, so the submit is replayed without it."""

    def _err(self, reason, code=None):
        from hermes_cli.gateway_client import GatewayRPCError

        return GatewayRPCError(reason, code)

    def test_invalid_params_is_a_pane_refusal(self):
        from hermes_cli.gateway_client import caller_pane_rejected

        assert caller_pane_rejected(self._err("invalid_params")) is True
        assert caller_pane_rejected(self._err("request_failed", -32602)) is True

    def test_real_verdicts_are_not_pane_refusals(self):
        from hermes_cli.gateway_client import caller_pane_rejected

        assert caller_pane_rejected(self._err("session_busy")) is False
        assert caller_pane_rejected(self._err("draining")) is False
        assert caller_pane_rejected(RuntimeError("invalid_params")) is False

    def test_without_caller_pane_keeps_everything_else(self):
        from hermes_cli.gateway_client import without_caller_pane

        params = {"session_id": "sW:s1", "text": "hi", "caller_pane": PANE, "queued": True}
        assert without_caller_pane(params) == {"session_id": "sW:s1", "text": "hi", "queued": True}
        assert params["caller_pane"] == PANE  # not mutated
