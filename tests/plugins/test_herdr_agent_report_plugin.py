"""Tests for the herdr-agent-report plugin.

Covers the bundled plugin at ``plugins/herdr-agent-report/``:

  * Hook dispatch semantics: ``pre_llm_call`` -> working, ``post_llm_call`` -> idle,
    ``pre_approval_request`` -> blocked, ``post_approval_response`` -> working,
    ``on_session_finalize`` -> release-agent. Each turn hook fires once per turn in the agent
    core (agent/turn_context.py, agent/turn_finalizer.py), so the plugin reports exactly one
    ``working`` per turn and one ``idle`` when the turn settles.
  * Gating: silent without ``HERDR_ENV``/``HERDR_PANE_ID``; platform-gated when the hook kwargs
    carry a ``platform`` value, ungated when they don't (approval hooks).
  * ``--seq`` monotonic per process; consecutive identical states deduped; failed herdr calls
    roll the dedupe memory back; missing/slow binary never raises.
  * Bundled-plugin discovery via ``PluginManager.discover_and_load`` (not enabled by default,
    loads and registers hooks when enabled).

The herdr binary is faked with a script that appends ``sys.argv`` to a JSONL file; the plugin
resolves it through ``HERDR_BIN_PATH``.
"""

import importlib.util
import itertools
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake executable contract")

_MODULE_IDS = itertools.count(1)

FAKE_HERDR = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["HERDR_AGENT_REPORT_FAKE_LOG"]
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(0)
"""

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "herdr-agent-report"

INTERACTIVE_PLATFORM = "cli"
NON_INTERACTIVE_PLATFORM = "telegram"


class _Ctx:
    """Records ctx.register_hook() calls like the real plugin context wires them."""

    def __init__(self):
        self.hooks: dict[str, list] = {}

    def register_hook(self, hook_name, callback):
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_tool(self, *args, **kwargs):
        pass

    def register_cli_command(self, *args, **kwargs):
        pass


def _load_plugin():
    """Fresh module instance per load: dedupe/seq live in module globals, so each test gets
    its own clean state instead of cross-test ordering effects."""
    spec = importlib.util.spec_from_file_location(
        f"herdr_agent_report_under_test_{next(_MODULE_IDS)}", PLUGIN_DIR / "__init__.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _register(mod) -> _Ctx:
    ctx = _Ctx()
    mod.register(ctx)
    return ctx


@pytest.fixture(autouse=True)
def herdr_env(tmp_path, monkeypatch):
    """Fresh HERMES_HOME + a fake herdr binary that records every invocation."""
    log = tmp_path / "herdr-calls.jsonl"
    log.touch()
    fake = tmp_path / "fake-herdr"
    fake.write_text(FAKE_HERDR)
    fake.chmod(fake.stat().st_mode | 0o111)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("HERMES_BUNDLED_PLUGINS", raising=False)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "wT:p1")
    monkeypatch.setenv("HERDR_BIN_PATH", str(fake))
    monkeypatch.setenv("HERDR_AGENT_REPORT_FAKE_LOG", str(log))
    yield SimpleNamespace(log=log, fake=fake)


def _records(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _states(log: Path) -> list[str | None]:
    """Reported --state per call; None for release-agent calls."""
    out = []
    for argv in _records(log):
        argv = argv[1:]  # strip "pane"
        if argv and argv[0] == "release-agent":
            out.append(None)
        else:
            assert argv[0] == "report-agent", argv
            out.append(argv[argv.index("--state") + 1])
    return out


def _seqs(log: Path) -> list[int]:
    return [int(argv[argv.index("--seq") + 1]) for argv in _records(log)]


def _fire(ctx: _Ctx, hook: str, **kwargs) -> None:
    for cb in ctx.hooks[hook]:
        cb(**kwargs)


TURN_KW = {"session_id": "s1", "platform": INTERACTIVE_PLATFORM}
APPROVAL_KW = {"session_key": "sk", "surface": "transport:human", "command": "git push"}
FINALIZE_KW = {"session_id": "s1", "platform": INTERACTIVE_PLATFORM, "reason": "shutdown"}


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TestGating:
    def test_silent_without_herdr_env(self, herdr_env, monkeypatch):
        monkeypatch.delenv("HERDR_ENV", raising=False)
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **TURN_KW)
        assert _records(herdr_env.log) == [], "no HERDR_ENV -> no herdr calls, ever"

    def test_silent_without_pane_id(self, herdr_env, monkeypatch):
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **TURN_KW)
        assert _records(herdr_env.log) == []

    def test_blank_pane_id_is_ignored(self, herdr_env, monkeypatch):
        monkeypatch.setenv("HERDR_PANE_ID", "   ")
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **TURN_KW)
        assert _records(herdr_env.log) == []

    def test_non_interactive_platform_is_skipped(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", session_id="s1", platform=NON_INTERACTIVE_PLATFORM)
        assert _records(herdr_env.log) == []

    def test_interactive_platform_reports(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **TURN_KW)
        assert _states(herdr_env.log) == ["working"]

    def test_hooks_without_platform_kwarg_are_not_platform_gated(self, herdr_env):
        """Approval hooks fire with command/session_key/surface kwargs and no platform value —
        they are gated only by HERDR_ENV (mirrors tools/approval_prompt.py)."""
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_approval_request", **APPROVAL_KW)
        _fire(ctx, "post_approval_response", **APPROVAL_KW, choice="allow")
        assert _states(herdr_env.log) == ["blocked", "working"]


# ---------------------------------------------------------------------------
# Turn lifecycle semantics
# ---------------------------------------------------------------------------


class TestTurnLifecycle:
    def test_turn_reports_working_then_idle_exactly_once(self, herdr_env):
        """pre_llm_call and post_llm_call each fire once per turn in the agent core; repeated
        firings (e.g. hook retries) must dedupe instead of spamming herdr."""
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **TURN_KW)
        _fire(ctx, "pre_llm_call", **TURN_KW)
        _fire(ctx, "post_llm_call", **TURN_KW)
        _fire(ctx, "post_llm_call", **TURN_KW)
        assert _states(herdr_env.log) == ["working", "idle"], "identical consecutive states dedupe"

    def test_multi_tool_turn_does_not_flap(self, herdr_env):
        """A multi-step turn runs several LLM calls under the same turn hooks — still exactly
        one working + one idle, because pre/post_llm_call fire once per turn and consecutive
        identical states dedupe."""
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **TURN_KW)
        _fire(ctx, "post_llm_call", **TURN_KW)
        assert _states(herdr_env.log) == ["working", "idle"]
        assert len(_records(herdr_env.log)) == 2

    def test_approval_flow_sequence(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **TURN_KW)
        _fire(ctx, "pre_approval_request", **APPROVAL_KW)
        _fire(ctx, "post_approval_response", **APPROVAL_KW, choice="allow")
        _fire(ctx, "post_llm_call", **TURN_KW)
        assert _states(herdr_env.log) == ["working", "blocked", "working", "idle"]

    def test_report_agent_argv(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **TURN_KW)
        (argv,) = _records(herdr_env.log)
        assert argv[0:6] == ["pane", "report-agent", "wT:p1",
                             "--source", "hermes-agent-report", "--agent"]
        # hermes.toml declares aliases = ["hermes-agent"]: reports naming an agent Herdr
        # already owns come back {"type":"ok"} but are silently never applied, hence
        # the plugin must label itself with a genuinely unclaimed name.
        assert argv[6] == "hermes-report"
        assert argv[6] not in ("hermes", "hermes-agent")

    def test_release_agent_argv(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "on_session_finalize", **FINALIZE_KW)
        (argv,) = _records(herdr_env.log)
        assert argv[0:3] == ["pane", "release-agent", "wT:p1"]
        assert argv[3:8] == ["--source", "hermes-agent-report", "--agent", "hermes-report", "--seq"]

    def test_release_clears_dedupe_memory_for_next_session(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "post_llm_call", **TURN_KW)  # turn ends -> idle
        _fire(ctx, "on_session_finalize", **FINALIZE_KW)  # session ends -> release
        _fire(ctx, "pre_llm_call", session_id="s2", platform=INTERACTIVE_PLATFORM)  # fresh session
        states = _states(herdr_env.log)
        assert states[0] == "idle" and states[1] is None
        assert states[2] == "working", "after release the next session reports working again"

    def test_seq_is_strictly_monotonic(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **TURN_KW)
        _fire(ctx, "pre_approval_request", **APPROVAL_KW)
        _fire(ctx, "post_approval_response", **APPROVAL_KW, choice="allow")
        _fire(ctx, "post_llm_call", **TURN_KW)
        _fire(ctx, "on_session_finalize", **FINALIZE_KW)
        seqs = _seqs(herdr_env.log)
        assert len(seqs) == 5
        assert seqs == sorted(seqs) and len(set(seqs)) == 5, seqs


# ---------------------------------------------------------------------------
# Failure resilience
# ---------------------------------------------------------------------------


class TestFailureResilience:
    def test_missing_binary_never_raises_and_state_retries_when_fixed(self, herdr_env, monkeypatch):
        ctx = _register(_load_plugin())
        monkeypatch.setenv("HERDR_BIN_PATH", str(herdr_env.fake.parent / "no-such-herdr"))
        _fire(ctx, "pre_llm_call", **TURN_KW)  # must not raise, must not dedupe-lock the state
        assert _records(herdr_env.log) == []
        monkeypatch.setenv("HERDR_BIN_PATH", str(herdr_env.fake))
        _fire(ctx, "pre_llm_call", **TURN_KW)  # dedupe memory rolled back -> retried next hook
        assert _states(herdr_env.log) == ["working"]

    def test_nonzero_exit_is_rolled_back(self, herdr_env, tmp_path, monkeypatch):
        failing = tmp_path / "failing-herdr"
        failing.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(2)\n")
        failing.chmod(failing.stat().st_mode | 0o111)
        ctx = _register(_load_plugin())
        monkeypatch.setenv("HERDR_BIN_PATH", str(failing))
        _fire(ctx, "pre_llm_call", **TURN_KW)
        monkeypatch.setenv("HERDR_BIN_PATH", str(herdr_env.fake))
        _fire(ctx, "pre_llm_call", **TURN_KW)
        assert _states(herdr_env.log) == ["working"], "failure must not lock dedupe memory"

    def test_slow_binary_cannot_exceed_one_second(self, herdr_env, tmp_path, monkeypatch):
        import time
        hanging = tmp_path / "hanging-herdr"
        hanging.write_text("#!/usr/bin/env python3\nimport time; time.sleep(30)\n")
        hanging.chmod(hanging.stat().st_mode | 0o111)
        monkeypatch.setenv("HERDR_BIN_PATH", str(hanging))
        ctx = _register(_load_plugin())
        started = time.monotonic()
        _fire(ctx, "pre_llm_call", **TURN_KW)
        assert time.monotonic() - started < 5, "hook must return promptly (1s subprocess timeout)"
        assert _records(herdr_env.log) == []


# ---------------------------------------------------------------------------
# Pane identity via hook kwargs (option B)
# ---------------------------------------------------------------------------


class TestPaneKwargIdentity:
    """Gateway-side turn/approval hooks carry the pane as the ``pane_id`` kwarg (option B: the
    submitting CLI's ``prompt.submit`` ``caller_pane`` param, bound per turn by the gateway).
    The gateway process itself has no HERDR_* env, so the kwarg must work with the env gate unset."""

    KW = {"session_id": "s1", "platform": INTERACTIVE_PLATFORM, "pane_id": "wT:p9"}

    def test_kwarg_pane_reports_without_herdr_env(self, herdr_env, monkeypatch):
        monkeypatch.delenv("HERDR_ENV", raising=False)
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **self.KW)
        (argv,) = _records(herdr_env.log)
        assert argv[2] == "wT:p9"
        assert _states(herdr_env.log) == ["working"]

    def test_kwarg_pane_wins_over_env(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **self.KW)
        _fire(ctx, "pre_llm_call", session_id="s1", platform=INTERACTIVE_PLATFORM)  # env path
        assert _records(herdr_env.log)[0][2] == "wT:p9", "kwarg identity wins over HERDR_PANE_ID"

    def test_blank_kwarg_falls_back_to_env(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", session_id="s1", platform=INTERACTIVE_PLATFORM, pane_id="  ")
        assert _records(herdr_env.log)[0][2] == "wT:p1"

    def test_approval_hooks_carry_kwarg_pane_without_platform(self, herdr_env, monkeypatch):
        monkeypatch.delenv("HERDR_ENV", raising=False)
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_approval_request", session_key="sk", command="git push", pane_id="wT:p9")
        _fire(ctx, "post_approval_response", session_key="sk", command="git push",
              pane_id="wT:p9", choice="allow")
        assert _states(herdr_env.log) == ["blocked", "working"]
        assert all(argv[2] == "wT:p9" for argv in _records(herdr_env.log))

    def test_panes_dedupe_independently(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **self.KW)
        _fire(ctx, "pre_llm_call", session_id="s1", platform=INTERACTIVE_PLATFORM, pane_id="wT:p8")
        _fire(ctx, "pre_llm_call", **self.KW)
        assert _states(herdr_env.log) == ["working", "working"], \
            "a different pane still reports; its own duplicate dedupes"

    def test_release_clears_only_own_pane_dedupe(self, herdr_env):
        ctx = _register(_load_plugin())
        _fire(ctx, "pre_llm_call", **self.KW)
        _fire(ctx, "pre_llm_call", session_id="s1", platform=INTERACTIVE_PLATFORM, pane_id="wT:p8")
        _fire(ctx, "post_llm_call", **self.KW)
        _fire(ctx, "on_session_finalize", session_id="s1", platform=INTERACTIVE_PLATFORM,
              pane_id="wT:p9")
        _fire(ctx, "pre_llm_call", session_id="s2", platform=INTERACTIVE_PLATFORM, pane_id="wT:p9")
        assert _states(herdr_env.log) == ["working", "working", "idle", None, "working"], \
            "only the released pane forgets its dedupe memory"


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registered_hooks_match_manifest_and_valid_hooks(self):
        import hermes_yaml as yaml
        from hermes_cli.plugins import VALID_HOOKS
        mod = _load_plugin()
        ctx = _Ctx()
        mod.register(ctx)
        manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
        assert set(ctx.hooks) == set(manifest["provides_hooks"])
        assert set(ctx.hooks) <= VALID_HOOKS

    def test_on_session_end_is_not_used_for_release(self):
        """on_session_end fires once PER TURN (agent/turn_finalizer.py) — hooking it would
        release the Herdr pane agent after every interactive turn; on_session_finalize is the
        true end-of-session seam (hermes_cli/cli_shutdown.py, hermes_cli/oneshot.py)."""
        ctx = _register(_load_plugin())
        assert "on_session_end" not in ctx.hooks
        assert "on_session_finalize" in ctx.hooks


# ---------------------------------------------------------------------------
# Bundled-plugin discovery
# ---------------------------------------------------------------------------


class TestBundledDiscovery:
    def _write_enabled_config(self, home: Path, names):
        import hermes_yaml as yaml
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": list(names)}}))

    def _home(self) -> Path:
        return Path(os.environ["HERMES_HOME"])

    def test_discovered_but_not_enabled_by_default(self, herdr_env):
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        assert "herdr-agent-report" in mgr._plugins
        loaded = mgr._plugins["herdr-agent-report"]
        assert loaded.manifest.source == "bundled"
        assert not loaded.enabled
        assert loaded.error and "not enabled" in loaded.error

    def test_enabled_plugin_manifest_carries_all_hooks(self, herdr_env):
        self._write_enabled_config(self._home(), ["herdr-agent-report"])
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        loaded = mgr._plugins["herdr-agent-report"]
        assert loaded.enabled
        assert loaded.manifest.provides_hooks == [
            "pre_llm_call", "post_llm_call", "pre_approval_request",
            "post_approval_response", "on_session_finalize",
        ]
