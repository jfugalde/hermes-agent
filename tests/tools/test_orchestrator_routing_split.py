#!/usr/bin/env python3
"""
Smoke tests for the orchestrator fan-out vs board-orchestrator routing split.

Verifies that:
1. The new skill file exists and is parseable.
2. Fan-out children cannot see or mutate Kanban tools (existing isolation).
3. Board-routing tools are hidden from dispatcher-spawned workers.
4. Delegation config is tuned for low-thinking fan-out.

Run with:
    python3 -m pytest tests/test_orchestrator_routing_split.py -v
or:
    python3 tests/test_orchestrator_routing_split.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Ensure repo-under-test is on PYTHONPATH when running standalone.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest


SKILL_PATH = (
    Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    / "profiles/orchestrator/skills/autonomous-ai-agents"
    / "orchestrator-fan-out-vs-board-routing/SKILL.md"
)


def test_skill_file_exists_and_has_expected_sections():
    assert SKILL_PATH.exists(), f"Skill not found at {SKILL_PATH}"
    text = SKILL_PATH.read_text(encoding="utf-8")
    expected = [
        "name: orchestrator-fan-out-vs-board-routing",
        "Orchestrator Routing Split: Fan-Out vs Board Orchestration",
        "Trigger rules",
        "Safeguards",
        "Fan-out never invokes board orchestration",
        "Fan-out children cannot mutate the board",
        "Board orchestrator tools are hidden from workers",
        "Low-thinking model is pinned for fan-out",
    ]
    missing = [s for s in expected if s not in text]
    assert not missing, f"Skill missing sections: {missing}"


def test_skill_yaml_frontmatter_parsable():
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert text.startswith("---")
    import yaml

    parts = text.split("---", 2)
    front = yaml.safe_load(parts[1])
    assert front["name"] == "orchestrator-fan-out-vs-board-routing"
    assert "orchestrator" in front["metadata"]["hermes"]["tags"]


class TestFanOutIsolation:
    """Existing isolation: delegate_task children must not mutate the board."""

    def test_build_child_agent_strips_kanban(self, monkeypatch):
        from tools import delegate_tool
        from agent.delegation_context import delegated_child_context

        captured = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.valid_tool_names = {"terminal"}
                self.session_id = "child"

        import run_agent

        monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
        monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})

        class Parent:
            enabled_toolsets = ["terminal", "kanban"]
            valid_tool_names = {"terminal", "kanban_complete"}
            model = "test-model"
            provider = "test-provider"
            base_url = "http://example.invalid"
            api_mode = "chat_completions"
            platform = "cli"
            session_id = "parent"
            _delegate_depth = 0
            _active_children = []
            _active_children_lock = None
            _print_fn = None
            tool_progress_callback = None
            thinking_callback = None

        with delegated_child_context():
            child = delegate_tool._build_child_agent(
                task_index=0,
                goal="test isolation",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=3,
                task_count=1,
                parent_agent=Parent(),
            )

        assert child.valid_tool_names == {"terminal"}
        assert "kanban" not in captured["enabled_toolsets"]
        assert "kanban" in captured["disabled_toolsets"]

    def test_delegated_child_kanban_mutator_rejected(self, monkeypatch):
        from tools import kanban_tools
        from agent.delegation_context import delegated_child_context

        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
        with delegated_child_context():
            raw = kanban_tools._handle_complete(
                {"task_id": "t_parent", "summary": "child bypass"}
            )
        payload = json.loads(raw)
        assert payload["error"]
        assert "delegate_task child" in payload["error"]


class TestBoardRoutingGatedFromWorkers:
    """Board-routing tools must be hidden from dispatcher-spawned task workers."""

    def test_worker_with_kanban_toolset_still_hides_board_routing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
        monkeypatch.setenv("HERMES_HOME", str(home))

        import tools.kanban_tools  # noqa: F401 - ensure registered
        from tools.registry import invalidate_check_fn_cache, registry
        from toolsets import resolve_toolset

        invalidate_check_fn_cache()
        schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
        names = {s["function"].get("name") for s in schema if "function" in s}
        kanban = {n for n in names if n and n.startswith("kanban_")}
        assert {"kanban_list", "kanban_unblock"}.isdisjoint(kanban), (
            f"Board-routing tools leaked into worker schema: {kanban}"
        )

    def test_orchestrator_profile_sees_board_routing_tools(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
        monkeypatch.setenv("HERMES_HOME", str(home))

        import tools.kanban_tools  # noqa: F401 - ensure registered
        from tools.registry import invalidate_check_fn_cache, registry
        from toolsets import resolve_toolset

        invalidate_check_fn_cache()
        schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
        names = {s["function"].get("name") for s in schema if "function" in s}
        assert "kanban_list" in names
        assert "kanban_unblock" in names


class TestDelegationConfigTunedForFanOut:
    """Delegation config should use low reasoning effort for cheap fan-out."""

    def test_active_config_has_low_reasoning_effort(self):
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        delegation = cfg.get("delegation", {})
        effort = str(delegation.get("reasoning_effort") or "").strip().lower()
        assert effort in {"low", "minimal"}, f"expected low/minimal, got {effort!r}"
        assert delegation.get("max_concurrent_children") is not None
        assert delegation.get("max_spawn_depth") is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
