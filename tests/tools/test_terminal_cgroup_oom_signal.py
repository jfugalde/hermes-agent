"""R3 — the parent must learn that a command died from a *cgroup* OOM kill.

Before this, a SIGKILL reached the model as the generic "often the kernel OOM killer
on memory exhaustion, or an explicit kill -9" note, and a model that reads a maybe
re-runs the same command — which is killed again, in the gateway's cgroup or the
worker's scope. The verdict is only upgraded when evidence exists (systemd's ``Result``
for a scoped run, or the cgroup ``oom_kill`` counter delta for an unscoped one); with
no evidence the original note is kept verbatim.
"""

import json

import pytest

from tools import cgroup_oom
from tools.terminal_tool_result import (
    _confirmed_oom_note,
    _looks_like_oom_exit,
    finalize_foreground_result,
)


class _Env:
    """Minimal stand-in for a backend env: only ``_cgroup_oom_probe`` is read."""

    def __init__(self, probe=None):
        self._cgroup_oom_probe = probe


def _finalize(result, env, command="pytest -q"):
    return json.loads(finalize_foreground_result(
        command=command, result=result, env=env, env_type="local",
        effective_task_id="t", task_id="t", session_id="s", session_key="k",
        workdir=None, command_cwd=None, approval_note=None))


class TestLooksLikeOomExit:
    @pytest.mark.parametrize("code", [-9, 137])
    def test_sigkill_codes_are_candidates(self, code):
        assert _looks_like_oom_exit(code, "") is True

    def test_collapsed_exit_one_with_empty_output_is_a_candidate(self):
        # A wrapper (`sh -c`, `timeout`, a pipeline) can report 1 with nothing captured.
        assert _looks_like_oom_exit(1, "") is True
        assert _looks_like_oom_exit(1, "   \n") is True

    @pytest.mark.parametrize("output", [
        "Killed",
        "Terminated",
        "/bin/bash: line 3: 12345 Killed  ./heavy",
        "bash: line 1:  9412 Killed                  node --max-old-space-size=100 build.js",
    ])
    def test_collapsed_shell_notices_are_candidates(self, output):
        assert _looks_like_oom_exit(1, output) is True

    @pytest.mark.parametrize("output", [
        "1 failed, 2 passed",
        "error: cannot find module 'x'",
        "Killed process 123 while writing the report to disk",  # not the shell's notice
    ])
    def test_ordinary_failures_are_not_candidates(self, output):
        assert _looks_like_oom_exit(1, output) is False

    def test_real_error_codes_are_not_candidates(self):
        assert _looks_like_oom_exit(0, "") is False
        assert _looks_like_oom_exit(2, "usage: ...") is False
        assert _looks_like_oom_exit(124, "") is False


class TestConfirmCgroupOom:
    def test_systemd_result_oom_kill_confirms(self, monkeypatch):
        monkeypatch.setattr(cgroup_oom, "systemd_unit_oom_killed", lambda unit: True)
        confirmed, evidence = cgroup_oom.confirm_cgroup_oom(
            {"systemd_unit": "hermes-worker-fg-1-abc.scope", "oom_kill_baseline": None})
        assert confirmed is True
        assert "oom-kill" in evidence
        assert "hermes-worker-fg-1-abc.scope" in evidence

    def test_systemd_normal_result_falls_through_to_counter(self, monkeypatch):
        """A scope that systemd says finished normally can still have had its payload
        SIGKILLed inside the shared cgroup — the counter delta decides then."""
        monkeypatch.setattr(cgroup_oom, "systemd_unit_oom_killed", lambda unit: False)
        monkeypatch.setattr(cgroup_oom, "read_oom_kill_count", lambda: 7)
        confirmed, evidence = cgroup_oom.confirm_cgroup_oom(
            {"systemd_unit": "hermes-worker-fg-1-abc.scope", "oom_kill_baseline": 6})
        assert confirmed is True
        assert "6→7" in evidence

    def test_flat_counter_is_not_an_oom(self, monkeypatch):
        monkeypatch.setattr(cgroup_oom, "read_oom_kill_count", lambda: 6)
        confirmed, evidence = cgroup_oom.confirm_cgroup_oom({"oom_kill_baseline": 6})
        assert confirmed is False
        assert evidence == ""

    def test_unknown_probe_is_not_an_oom(self, monkeypatch):
        # "Cannot tell" must never read as "confirmed": that would relabel every
        # explicit kill -9 in a container or on a cgroup-v1 host.
        monkeypatch.setattr(cgroup_oom, "read_oom_kill_count", lambda: None)
        assert cgroup_oom.confirm_cgroup_oom(None) == (False, "")
        assert cgroup_oom.confirm_cgroup_oom({}) == (False, "")
        assert cgroup_oom.confirm_cgroup_oom({"oom_kill_baseline": None}) == (False, "")

    def test_missing_systemctl_answer_does_not_confirm(self, monkeypatch):
        monkeypatch.setattr(cgroup_oom, "systemd_unit_oom_killed", lambda unit: None)
        monkeypatch.setattr(cgroup_oom, "read_oom_kill_count", lambda: None)
        assert cgroup_oom.confirm_cgroup_oom({"systemd_unit": "u.scope"}) == (False, "")


class TestConfirmedNoteInToolResult:
    def test_confirmed_scope_oom_sets_flag_and_guidance(self, monkeypatch):
        monkeypatch.setattr(cgroup_oom, "systemd_unit_oom_killed", lambda unit: True)
        result = _finalize({"output": "", "returncode": -9},
                           _Env({"systemd_unit": "hermes-worker-fg-9-x.scope",
                                 "oom_kill_baseline": None}))
        assert result["oom_killed"] is True
        note = result["exit_code_meaning"]
        assert "cgroup OOM" in note
        assert "do NOT re-run" in note
        assert "terminal(background=true)" in note
        # The generic hedged SIGKILL note must be replaced, not appended to.
        assert "explicit kill -9" not in note

    def test_unconfirmed_sigkill_keeps_the_generic_note(self, monkeypatch):
        monkeypatch.setattr(cgroup_oom, "read_oom_kill_count", lambda: 3)
        result = _finalize({"output": "", "returncode": 137},
                           _Env({"systemd_unit": None, "oom_kill_baseline": 3}))
        assert "oom_killed" not in result
        assert "SIGKILL" in result["exit_code_meaning"]

    def test_env_without_probe_keeps_behaviour(self):
        result = _finalize({"output": "", "returncode": 137}, _Env())
        assert "oom_killed" not in result
        assert result["exit_code_meaning"].startswith("Exit code 137")

    def test_collapsed_exit_one_output_is_confirmed(self, monkeypatch):
        monkeypatch.setattr(cgroup_oom, "read_oom_kill_count", lambda: 4)
        result = _finalize({"output": "/bin/bash: line 3: 12345 Killed\n", "returncode": 1},
                           _Env({"systemd_unit": None, "oom_kill_baseline": 3}))
        assert result["oom_killed"] is True
        assert "cgroup OOM" in result["exit_code_meaning"]

    def test_normal_failure_never_reads_the_cgroup(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cgroup_oom, "confirm_cgroup_oom",
                            lambda probe: calls.append(probe) or (True, "boom"))
        result = _finalize({"output": "1 failed", "returncode": 1}, _Env({}))
        assert calls == []
        assert "oom_killed" not in result
        assert "exit_code_meaning" not in result


class TestConfirmedOomNoteHelper:
    def test_returns_none_for_non_candidates(self):
        assert _confirmed_oom_note(0, "", {}, _Env()) is None

    def test_reads_probe_from_result_before_env(self, monkeypatch):
        monkeypatch.setattr(cgroup_oom, "systemd_unit_oom_killed", lambda unit: unit == "from-result.scope")
        note = _confirmed_oom_note(
            137, "", {"cgroup_oom_probe": {"systemd_unit": "from-result.scope"}},
            _Env({"systemd_unit": "from-env.scope"}))
        assert note is not None and "from-result.scope" in note
