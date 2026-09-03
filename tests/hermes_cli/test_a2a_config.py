"""Tests for A2A routing config schema validation."""

from hermes_cli.a2a_config import (
    DEFAULT_A2A_CONFIG,
    A2AConfig,
    collect_known_provider_ids,
    normalize_a2a_config,
    parse_a2a_config,
    validate_a2a_config,
)
from hermes_cli.config import DEFAULT_CONFIG, validate_config_structure


def test_default_config_includes_a2a_section():
    assert "a2a" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["a2a"]["enabled"] is False
    assert DEFAULT_CONFIG["a2a"]["routing"]["fallback"] == "heuristic"


def test_normalize_a2a_config_applies_defaults():
    cfg = normalize_a2a_config({"enabled": True, "routing": {"provider": "openrouter", "model": "deepseek-chat"}})

    assert cfg["enabled"] is True
    assert cfg["routing"]["provider"] == "openrouter"
    assert cfg["routing"]["model"] == "deepseek-chat"
    assert cfg["routing"]["temperature"] == 0
    assert cfg["routing"]["timeout_ms"] == 3000
    assert cfg["routing"]["fallback"] == "heuristic"
    assert cfg["kanban"]["bridge_url"] == DEFAULT_A2A_CONFIG["kanban"]["bridge_url"]
    assert cfg["explicit_hints"]["enabled"] is True
    assert cfg["logging"]["log_decisions"] is True


def test_parse_a2a_config_builds_dataclass():
    parsed = parse_a2a_config(
        {
            "a2a": {
                "enabled": True,
                "profiles": {
                    "researcher": {
                        "capabilities": ["research"],
                        "keywords": ["investigate"],
                    }
                },
                "heuristics": [
                    {"pattern": "research", "target": "researcher", "mode": "direct"},
                ],
            }
        }
    )

    assert isinstance(parsed, A2AConfig)
    assert parsed.enabled is True
    assert parsed.profiles["researcher"].capabilities == ["research"]
    assert parsed.heuristics[0].target == "researcher"


def test_validate_a2a_config_requires_provider_when_enabled():
    problems = validate_a2a_config(
        {"enabled": True, "routing": {"model": "deepseek-chat"}},
        {"providers": {"openrouter": {"enabled": True, "base_url": "https://openrouter.ai/api/v1"}}},
    )

    assert any("a2a.routing.provider is required" in p for p in problems)


def test_validate_a2a_config_requires_model_when_enabled():
    problems = validate_a2a_config(
        {"enabled": True, "routing": {"provider": "openrouter"}},
        {"providers": {"openrouter": {"enabled": True, "base_url": "https://openrouter.ai/api/v1"}}},
    )

    assert any("a2a.routing.model is required" in p for p in problems)


def test_validate_a2a_config_checks_provider_exists():
    problems = validate_a2a_config(
        {
            "enabled": True,
            "routing": {"provider": "missing-provider", "model": "deepseek-chat"},
        },
        {"providers": {"openrouter": {"enabled": True, "base_url": "https://openrouter.ai/api/v1"}}},
    )

    assert any("not configured in providers" in p for p in problems)


def test_validate_a2a_config_accepts_configured_provider():
    problems = validate_a2a_config(
        {
            "enabled": True,
            "routing": {"provider": "openrouter", "model": "deepseek/deepseek-chat"},
        },
        {"providers": {"openrouter": {"enabled": True, "base_url": "https://openrouter.ai/api/v1"}}},
    )

    assert problems == []


def test_validate_a2a_config_flags_invalid_heuristic_regex():
    problems = validate_a2a_config(
        {
            "heuristics": [{"pattern": "(unclosed", "target": "researcher", "mode": "direct"}],
            "profiles": {"researcher": {"capabilities": [], "keywords": []}},
        },
        {},
    )

    assert any("not a valid regex" in p for p in problems)


def test_validate_a2a_config_flags_heuristic_target_not_in_profiles():
    problems = validate_a2a_config(
        {
            "heuristics": [{"pattern": "deploy", "target": "deployer", "mode": "direct"}],
            "profiles": {"researcher": {"capabilities": [], "keywords": []}},
        },
        {},
    )

    assert any("not listed in a2a.profiles" in p for p in problems)


def test_collect_known_provider_ids_includes_providers_section():
    known = collect_known_provider_ids(
        {"providers": {"ollama-cloud": {"enabled": True, "base_url": "https://example.com/v1"}}}
    )

    assert "ollama-cloud" in known


def test_validate_config_structure_includes_a2a_issues():
    issues = validate_config_structure(
        {
            "model": {"provider": "openrouter"},
            "providers": {"openrouter": {"enabled": True, "base_url": "https://openrouter.ai/api/v1"}},
            "a2a": {
                "enabled": True,
                "routing": {"provider": "unknown-provider", "model": "deepseek-chat"},
            },
        }
    )

    assert any("a2a.routing.provider" in i.message for i in issues)
