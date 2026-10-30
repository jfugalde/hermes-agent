"""R2 — foreground terminal commands get their own memory-capped cgroup.

Background workers already ran under ``systemd-run --user --scope`` with a MemoryMax, so
an OOM there kills the worker's scope. Foreground commands (``pytest``, ``npm run
build``, ``codex exec``) still ran inside the gateway's cgroup, where the same spike
takes the whole messaging control plane down with it. These tests pin the wrapper, the
``MemorySwapMax`` property on both paths, and — the part that matters most — that every
non-gateway / non-Linux / degraded case keeps the plain Popen argv.
"""

import subprocess
import sys

import pytest

import tools.process_registry as pr
from tools.process_registry import (
    _build_systemd_scope_argv,
    _systemd_scope_argv,
    foreground_scope_argv,
)


@pytest.fixture(autouse=True)
def _reset_scope_flags():
    """Restore the module-level probe verdicts/flags a test mutates."""
    original = (pr._SYSTEMD_SCOPE_AVAILABLE, pr._SYSTEMD_SCOPE_PROBED_AT,
                pr._SYSTEMD_RUN_NO_EXPAND, pr._SYSTEMD_SCOPE_MEMORY_SWAP_MAX)
    yield
    (pr._SYSTEMD_SCOPE_AVAILABLE, pr._SYSTEMD_SCOPE_PROBED_AT,
     pr._SYSTEMD_RUN_NO_EXPAND, pr._SYSTEMD_SCOPE_MEMORY_SWAP_MAX) = original


def _properties(argv):
    return [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--property"]


class TestMemorySwapMaxProperty:
    def test_scope_argv_carries_swap_cap_with_memory_max(self):
        argv = _systemd_scope_argv("/usr/bin/systemd-run", "hermes-worker-x", "/bin/sh", "-c", "true")
        props = _properties(argv)
        assert "MemoryAccounting=yes" in props
        assert "MemorySwapMax=512M" in props
        memory_max = next(p for p in props if p.startswith("MemoryMax="))
        assert int(memory_max.split("=", 1)[1]) > 0
        # The command must still come after the ``--`` separator, byte-identical.
        assert argv[argv.index("--") + 1:] == ["/bin/sh", "-c", "true"]

    def test_swap_cap_is_dropped_when_systemd_rejects_it(self, monkeypatch):
        monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_MEMORY_SWAP_MAX", False)
        argv = _systemd_scope_argv("/usr/bin/systemd-run", "hermes-worker-x", "/bin/sh", "-c", "true")
        assert not any(p.startswith("MemorySwapMax=") for p in _properties(argv))
        assert any(p.startswith("MemoryMax=") for p in _properties(argv))

    def test_probe_retries_without_swap_cap_instead_of_declaring_scopes_dead(self, monkeypatch):
        """A systemd that rejects ``MemorySwapMax`` must degrade the *property*, not
        the isolation: every scoped spawn would otherwise fall back into the gateway
        cgroup on an otherwise working host."""
        monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_AVAILABLE", None)
        monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_MEMORY_SWAP_MAX", True)
        monkeypatch.setattr(pr, "_SYSTEMD_RUN_NO_EXPAND", True)
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(_properties(argv))
            rejected = any(p.startswith("MemorySwapMax=") for p in _properties(argv))
            return subprocess.CompletedProcess(
                argv, 1 if rejected else 0, b"",
                b"Unknown assignment MemorySwapMax." if rejected else b"")

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")
        assert pr._systemd_run_user_scope_available() is True
        assert len(seen) == 2
        assert pr._SYSTEMD_SCOPE_MEMORY_SWAP_MAX is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: systemd scopes")
class TestForegroundScopeArgv:
    def _available(self, monkeypatch, *, gateway=True, scope=True):
        monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: gateway)
        monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: scope)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")

    def test_wraps_under_supervised_gateway(self, monkeypatch):
        if not pr._IS_LINUX:
            pytest.skip("Linux-only path")
        self._available(monkeypatch)
        argv = foreground_scope_argv(["/bin/bash", "-c", "pytest -q"])
        assert argv[0] == "/usr/bin/systemd-run"
        assert "--scope" in argv
        unit = argv[argv.index("--unit") + 1]
        # The exporter classifies scopes by prefix: hermes-worker-* -> worker-scope,
        # which is what lets the warning-level worker-scope OOM alert cover foreground.
        assert unit.startswith("hermes-worker-fg-"), unit
        assert argv[argv.index("--") + 1:] == ["/bin/bash", "-c", "pytest -q"]
        assert "MemorySwapMax=512M" in _properties(argv)

    def test_unit_names_are_unique_per_command(self, monkeypatch):
        if not pr._IS_LINUX:
            pytest.skip("Linux-only path")
        self._available(monkeypatch)
        units = {foreground_scope_argv(["true"])[foreground_scope_argv(["true"]).index("--unit") + 1]
                 for _ in range(3)}
        assert len(units) == 3

    def test_plain_argv_outside_the_gateway(self, monkeypatch):
        """CLI, cron and any non-gateway process keep today's path exactly."""
        self._available(monkeypatch, gateway=False)
        original = ["/bin/bash", "-c", "echo hi"]
        assert foreground_scope_argv(original) == original

    def test_plain_argv_when_scope_unavailable(self, monkeypatch):
        """No user D-Bus (containers, system services without linger) = degraded mode:
        the command still runs, just without its own cgroup."""
        self._available(monkeypatch, scope=False)
        original = ["/bin/bash", "-c", "echo hi"]
        assert foreground_scope_argv(original) == original

    def test_plain_argv_when_systemd_run_missing(self, monkeypatch):
        self._available(monkeypatch)
        monkeypatch.setattr("shutil.which", lambda name: None)
        original = ["/bin/bash", "-c", "echo hi"]
        assert foreground_scope_argv(original) == original

    def test_operator_can_disable_with_env(self, monkeypatch):
        self._available(monkeypatch)
        monkeypatch.setenv("HERMES_TERMINAL_FOREGROUND_SCOPE", "0")
        original = ["/bin/bash", "-c", "echo hi"]
        assert foreground_scope_argv(original) == original

    def test_identity_probe_failure_never_fails_the_command(self, monkeypatch):
        def boom():
            raise RuntimeError("pid file unreadable")

        monkeypatch.setattr(pr, "_is_supervised_gateway_process", boom)
        original = ["/bin/bash", "-c", "echo hi"]
        assert foreground_scope_argv(original) == original

    def test_non_linux_never_touches_systemd(self, monkeypatch):
        monkeypatch.setattr(pr, "_IS_LINUX", False)
        self._available(monkeypatch)
        original = ["/bin/bash", "-c", "echo hi"]
        assert foreground_scope_argv(original) == original


class TestBuildScopeArgvUnitNaming:
    def test_background_unit_stays_hermes_worker(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")
        argv = _build_systemd_scope_argv(["/bin/bash", "-c", "x"], unit_suffix="abc")
        assert argv[argv.index("--unit") + 1] == "hermes-worker-abc"
