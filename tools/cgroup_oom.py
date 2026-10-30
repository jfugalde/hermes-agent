"""Confirm that a terminal command died from a *cgroup* OOM kill, not a bare ``kill -9``.

A SIGKILL reaching the tool result (``exit_code`` ``-9`` / ``137``, or a shell that
collapsed the report to ``1`` with nothing but ``Killed``) is ambiguous: an explicit
``kill -9``, the kernel OOM killer in the host namespace, or the memory limit of the
command's own cgroup. Only the last case makes the model's next move different — do
not re-run the same command unbounded — so the claim is confirmed before it is made:

* a scoped command (``systemd-run --user --scope``) is judged by
  ``systemctl --user show <unit> -p Result``: systemd records ``oom-kill`` for a
  transient scope whose payload hit ``MemoryMax``/``MemorySwapMax``;
* an unscoped command is judged by the ``oom_kill`` counter of the cgroup it ran in,
  read as a delta against the snapshot taken immediately before the spawn.

Both are read-only. Nothing here can fail a command: every function returns ``None``
when the host cannot answer (no cgroup v2, no user bus, no systemd, timeout).
"""

import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("tools.cgroup_oom")

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_SYSTEMCTL_TIMEOUT_S = 2.0
_OOM_RESULT_PREFIX = "oom-kill"


def _own_cgroup_memory_events_path() -> Optional[Path]:
    """``memory.events`` of this process's cgroup (cgroup v2 only), or None."""
    try:
        # The unified-hierarchy line is the only one that carries a usable path; a v1
        # host lists subsystem lines instead and simply has no cgroup-v2 memory.events.
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            if line.startswith("0::"):
                relative = line.partition("::")[2].lstrip("/")
                return _CGROUP_ROOT / relative / "memory.events"
    except OSError:
        pass
    return None


def read_oom_kill_count() -> Optional[int]:
    """Value of ``oom_kill`` in this process's cgroup ``memory.events``, or None.

    The counter is cumulative per cgroup and resets when the cgroup is reaped, which
    is exactly what a delta taken across one command needs. ``None`` means "cannot
    tell", never ``0``.
    """
    path = _own_cgroup_memory_events_path()
    if path is None:
        return None
    try:
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                stripped = value.strip()
                return int(stripped) if stripped.isdigit() else None
    except (OSError, ValueError):
        return None
    return None


def systemd_unit_oom_killed(unit: str) -> Optional[bool]:
    """``True`` when systemd attributes the unit's exit to a cgroup OOM kill.

    ``False`` when systemd answered and said the unit finished normally, ``None``
    when the host could not answer (no user bus, unit already collected, timeout).
    """
    if not unit:
        return None
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=Result", "--value"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=_SYSTEMCTL_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("systemctl --user show %s failed: %s", unit, exc)
        return None
    value = (completed.stdout or "").strip()
    if completed.returncode != 0 or not value:
        return None
    if value == "success":
        return False
    return value.startswith(_OOM_RESULT_PREFIX)


def confirm_cgroup_oom(probe: Any) -> tuple[bool, str]:
    """``(confirmed, evidence)`` for a cgroup OOM kill, from a spawn-time probe.

    *probe* is ``{"systemd_unit": str|None, "oom_kill_baseline": int|None}`` recorded
    before the command started. ``systemd``'s own ``Result`` wins when it is
    available; the counter delta covers the unscoped path, where the command shared
    the gateway's cgroup and systemd has no per-command verdict to give.
    """
    if not isinstance(probe, dict):
        return False, ""
    unit = probe.get("systemd_unit")
    if unit:
        verdict = systemd_unit_oom_killed(str(unit))
        if verdict:
            return True, f"systemd recorded Result=oom-kill for {unit}"
    baseline = probe.get("oom_kill_baseline")
    if isinstance(baseline, int):
        current = read_oom_kill_count()
        if current is not None and current > baseline:
            return True, f"cgroup oom_kill rose {baseline}→{current}"
    return False, ""
