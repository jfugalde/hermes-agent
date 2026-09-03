"""A2A (Agent-to-Agent) routing configuration schema and validation."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

VALID_FALLBACK_MODES = frozenset({"heuristic", "current", "error"})
VALID_ROUTE_MODES = frozenset({"direct", "kanban", "current"})

DEFAULT_A2A_CONFIG: dict[str, Any] = {
    "enabled": False,
    "routing": {
        "provider": "",
        "model": "",
        "temperature": 0,
        "timeout_ms": 3000,
        "fallback": "heuristic",
    },
    "profiles": {},
    "kanban": {
        "enabled": True,
        "bridge_url": "http://127.0.0.1:3046",
        "route_keywords": [],
    },
    "heuristics": [],
    "explicit_hints": {
        "enabled": True,
    },
    "logging": {
        "enabled": True,
        "log_decisions": True,
        "include_confidence": True,
    },
}


@dataclass
class A2ARoutingConfig:
    provider: str = ""
    model: str = ""
    temperature: float = 0.0
    timeout_ms: int = 3000
    fallback: str = "heuristic"


@dataclass
class A2AProfileEntry:
    capabilities: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class A2AKanbanConfig:
    enabled: bool = True
    bridge_url: str = "http://127.0.0.1:3046"
    route_keywords: list[str] = field(default_factory=list)


@dataclass
class A2AHeuristicRule:
    pattern: str
    target: str
    mode: str


@dataclass
class A2AExplicitHintsConfig:
    enabled: bool = True


@dataclass
class A2ALoggingConfig:
    enabled: bool = True
    log_decisions: bool = True
    include_confidence: bool = True


@dataclass
class A2AConfig:
    enabled: bool = False
    routing: A2ARoutingConfig = field(default_factory=A2ARoutingConfig)
    profiles: dict[str, A2AProfileEntry] = field(default_factory=dict)
    kanban: A2AKanbanConfig = field(default_factory=A2AKanbanConfig)
    heuristics: list[A2AHeuristicRule] = field(default_factory=list)
    explicit_hints: A2AExplicitHintsConfig = field(default_factory=A2AExplicitHintsConfig)
    logging: A2ALoggingConfig = field(default_factory=A2ALoggingConfig)


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"0", "false", "no", "off"}:
            return False
        if text in {"1", "true", "yes", "on"}:
            return True
        return default
    return bool(value)


def _coerce_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _coerce_fallback(value: Any) -> str:
    mode = str(value or "heuristic").strip().lower()
    return mode if mode in VALID_FALLBACK_MODES else "heuristic"


def _coerce_route_mode(value: Any) -> str:
    mode = str(value or "direct").strip().lower()
    return mode if mode in VALID_ROUTE_MODES else "direct"


def _normalize_profile_entry(raw: Any) -> A2AProfileEntry:
    if not isinstance(raw, dict):
        return A2AProfileEntry()
    return A2AProfileEntry(
        capabilities=_coerce_str_list(raw.get("capabilities")),
        keywords=_coerce_str_list(raw.get("keywords")),
    )


def _normalize_heuristic_rule(raw: Any) -> A2AHeuristicRule | None:
    if not isinstance(raw, dict):
        return None
    pattern = str(raw.get("pattern") or "").strip()
    target = str(raw.get("target") or "").strip()
    if not pattern or not target:
        return None
    return A2AHeuristicRule(
        pattern=pattern,
        target=target,
        mode=_coerce_route_mode(raw.get("mode")),
    )


def _normalize_routing(raw: Any) -> A2ARoutingConfig:
    defaults = DEFAULT_A2A_CONFIG["routing"]
    if not isinstance(raw, dict):
        raw = {}
    return A2ARoutingConfig(
        provider=str(raw.get("provider") or defaults["provider"]).strip(),
        model=str(raw.get("model") or defaults["model"]).strip(),
        temperature=_coerce_float(raw.get("temperature"), defaults["temperature"]),
        timeout_ms=max(0, _coerce_int(raw.get("timeout_ms"), defaults["timeout_ms"])),
        fallback=_coerce_fallback(raw.get("fallback", defaults["fallback"])),
    )


def _normalize_kanban(raw: Any) -> A2AKanbanConfig:
    defaults = DEFAULT_A2A_CONFIG["kanban"]
    if not isinstance(raw, dict):
        raw = {}
    return A2AKanbanConfig(
        enabled=_coerce_bool(raw.get("enabled"), defaults["enabled"]),
        bridge_url=str(raw.get("bridge_url") or defaults["bridge_url"]).strip(),
        route_keywords=_coerce_str_list(raw.get("route_keywords")),
    )


def _normalize_explicit_hints(raw: Any) -> A2AExplicitHintsConfig:
    defaults = DEFAULT_A2A_CONFIG["explicit_hints"]
    if not isinstance(raw, dict):
        raw = {}
    return A2AExplicitHintsConfig(
        enabled=_coerce_bool(raw.get("enabled"), defaults["enabled"]),
    )


def _normalize_logging(raw: Any) -> A2ALoggingConfig:
    defaults = DEFAULT_A2A_CONFIG["logging"]
    if not isinstance(raw, dict):
        raw = {}
    return A2ALoggingConfig(
        enabled=_coerce_bool(raw.get("enabled"), defaults["enabled"]),
        log_decisions=_coerce_bool(raw.get("log_decisions"), defaults["log_decisions"]),
        include_confidence=_coerce_bool(
            raw.get("include_confidence"), defaults["include_confidence"]
        ),
    )


def normalize_a2a_config(raw: Any) -> dict[str, Any]:
    """Return a normalized ``a2a`` section with defaults applied."""
    parsed = parse_a2a_config({"a2a": raw})
    return a2a_config_to_dict(parsed)


def parse_a2a_config(config: dict[str, Any] | None) -> A2AConfig:
    """Parse the ``a2a`` section from a full Hermes config dict."""
    config = config or {}
    raw = config.get("a2a")
    if raw is None:
        return A2AConfig(**{
            "enabled": DEFAULT_A2A_CONFIG["enabled"],
            "routing": A2ARoutingConfig(**deepcopy(DEFAULT_A2A_CONFIG["routing"])),
            "profiles": {},
            "kanban": A2AKanbanConfig(**deepcopy(DEFAULT_A2A_CONFIG["kanban"])),
            "heuristics": [],
            "explicit_hints": A2AExplicitHintsConfig(**deepcopy(DEFAULT_A2A_CONFIG["explicit_hints"])),
            "logging": A2ALoggingConfig(**deepcopy(DEFAULT_A2A_CONFIG["logging"])),
        })

    if not isinstance(raw, dict):
        raw = {}

    profiles: dict[str, A2AProfileEntry] = {}
    raw_profiles = raw.get("profiles")
    if isinstance(raw_profiles, dict):
        for name, entry in raw_profiles.items():
            profile_name = str(name or "").strip()
            if profile_name:
                profiles[profile_name] = _normalize_profile_entry(entry)

    heuristics: list[A2AHeuristicRule] = []
    raw_heuristics = raw.get("heuristics")
    if isinstance(raw_heuristics, list):
        for item in raw_heuristics:
            rule = _normalize_heuristic_rule(item)
            if rule is not None:
                heuristics.append(rule)

    return A2AConfig(
        enabled=_coerce_bool(raw.get("enabled"), DEFAULT_A2A_CONFIG["enabled"]),
        routing=_normalize_routing(raw.get("routing")),
        profiles=profiles,
        kanban=_normalize_kanban(raw.get("kanban")),
        heuristics=heuristics,
        explicit_hints=_normalize_explicit_hints(raw.get("explicit_hints")),
        logging=_normalize_logging(raw.get("logging")),
    )


def a2a_config_to_dict(cfg: A2AConfig) -> dict[str, Any]:
    """Serialize ``A2AConfig`` back to a YAML-friendly dict."""
    return {
        "enabled": cfg.enabled,
        "routing": {
            "provider": cfg.routing.provider,
            "model": cfg.routing.model,
            "temperature": cfg.routing.temperature,
            "timeout_ms": cfg.routing.timeout_ms,
            "fallback": cfg.routing.fallback,
        },
        "profiles": {
            name: {
                "capabilities": list(entry.capabilities),
                "keywords": list(entry.keywords),
            }
            for name, entry in cfg.profiles.items()
        },
        "kanban": {
            "enabled": cfg.kanban.enabled,
            "bridge_url": cfg.kanban.bridge_url,
            "route_keywords": list(cfg.kanban.route_keywords),
        },
        "heuristics": [
            {"pattern": rule.pattern, "target": rule.target, "mode": rule.mode}
            for rule in cfg.heuristics
        ],
        "explicit_hints": {"enabled": cfg.explicit_hints.enabled},
        "logging": {
            "enabled": cfg.logging.enabled,
            "log_decisions": cfg.logging.log_decisions,
            "include_confidence": cfg.logging.include_confidence,
        },
    }


def collect_known_provider_ids(config: dict[str, Any] | None) -> set[str]:
    """Return provider ids that exist in the user's Hermes config."""
    config = config or {}
    known: set[str] = {"openrouter", "custom", "auto", "moa"}

    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        known.update(str(name).strip().lower() for name in PROVIDER_REGISTRY.keys())
    except Exception:
        pass

    user_providers = config.get("providers")
    if isinstance(user_providers, dict):
        from hermes_cli.config import is_provider_enabled

        for name, prov_cfg in user_providers.items():
            provider_name = str(name or "").strip().lower()
            if provider_name and is_provider_enabled(prov_cfg):
                known.add(provider_name)

    try:
        from hermes_cli.config import get_compatible_custom_providers

        for entry in get_compatible_custom_providers(config):
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip().lower()
            if name:
                known.add(name)
                known.add("custom:" + name.replace(" ", "-"))
    except Exception:
        pass

    return known


def _provider_is_known(provider: str, known_providers: set[str], full_config: dict[str, Any]) -> bool:
    provider_id = str(provider or "").strip().lower()
    if not provider_id:
        return False
    if provider_id in known_providers:
        return True
    try:
        from hermes_cli.auth import resolve_provider as _resolve_auth_provider

        resolved = str(_resolve_auth_provider(provider_id) or "").strip().lower()
        if resolved and resolved in known_providers:
            return True
    except Exception:
        pass
    try:
        from hermes_cli.config import get_compatible_custom_providers
        from hermes_cli.providers import resolve_provider_full as _resolve_provider_full

        provider_def = _resolve_provider_full(
            provider_id,
            full_config.get("providers"),
            get_compatible_custom_providers(full_config),
        )
        if provider_def is not None:
            return True
    except Exception:
        pass
    return False


def validate_a2a_config(raw: Any, full_config: dict[str, Any] | None = None) -> list[str]:
    """Return human-readable validation problems for the ``a2a`` section."""
    full_config = full_config or {}
    problems: list[str] = []

    if raw is None:
        return problems

    if not isinstance(raw, dict):
        return ["a2a must be an object"]

    enabled = _coerce_bool(raw.get("enabled"), DEFAULT_A2A_CONFIG["enabled"])

    routing_raw = raw.get("routing")
    if routing_raw is not None and not isinstance(routing_raw, dict):
        problems.append("a2a.routing must be an object")

    routing = _normalize_routing(routing_raw if isinstance(routing_raw, dict) else {})

    if routing.fallback not in VALID_FALLBACK_MODES:
        problems.append(
            f"a2a.routing.fallback must be one of {sorted(VALID_FALLBACK_MODES)} "
            f"(got {routing.fallback!r})"
        )

    if routing.timeout_ms < 0:
        problems.append("a2a.routing.timeout_ms must be >= 0")

    profiles_raw = raw.get("profiles")
    if profiles_raw is not None and not isinstance(profiles_raw, dict):
        problems.append("a2a.profiles must be an object mapping profile names to capability blocks")
    elif isinstance(profiles_raw, dict):
        for name, entry in profiles_raw.items():
            label = str(name or "").strip() or "(unnamed)"
            if not isinstance(entry, dict):
                problems.append(f"a2a.profiles.{label} must be an object")
                continue
            for field_name in ("capabilities", "keywords"):
                value = entry.get(field_name)
                if value is not None and not isinstance(value, (list, str)):
                    problems.append(
                        f"a2a.profiles.{label}.{field_name} must be a list of strings"
                    )

    kanban_raw = raw.get("kanban")
    if kanban_raw is not None and not isinstance(kanban_raw, dict):
        problems.append("a2a.kanban must be an object")
    elif isinstance(kanban_raw, dict):
        bridge_url = str(kanban_raw.get("bridge_url") or "").strip()
        if bridge_url:
            parsed = urlparse(bridge_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                problems.append(
                    f"a2a.kanban.bridge_url must be an http(s) URL (got {bridge_url!r})"
                )
        route_keywords = kanban_raw.get("route_keywords")
        if route_keywords is not None and not isinstance(route_keywords, (list, str)):
            problems.append("a2a.kanban.route_keywords must be a list of strings")

    heuristics_raw = raw.get("heuristics")
    if heuristics_raw is not None and not isinstance(heuristics_raw, list):
        problems.append("a2a.heuristics must be a list")
    elif isinstance(heuristics_raw, list):
        for index, item in enumerate(heuristics_raw):
            if not isinstance(item, dict):
                problems.append(f"a2a.heuristics[{index}] must be an object")
                continue
            pattern = str(item.get("pattern") or "").strip()
            target = str(item.get("target") or "").strip()
            mode = str(item.get("mode") or "direct").strip().lower()
            if not pattern:
                problems.append(f"a2a.heuristics[{index}].pattern is required")
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    problems.append(
                        f"a2a.heuristics[{index}].pattern is not a valid regex: {exc}"
                    )
            if not target:
                problems.append(f"a2a.heuristics[{index}].target is required")
            if mode not in VALID_ROUTE_MODES:
                problems.append(
                    f"a2a.heuristics[{index}].mode must be one of "
                    f"{sorted(VALID_ROUTE_MODES)} (got {mode!r})"
                )

    explicit_hints_raw = raw.get("explicit_hints")
    if explicit_hints_raw is not None and not isinstance(explicit_hints_raw, dict):
        problems.append("a2a.explicit_hints must be an object")

    logging_raw = raw.get("logging")
    if logging_raw is not None and not isinstance(logging_raw, dict):
        problems.append("a2a.logging must be an object")

    known_providers = collect_known_provider_ids(full_config)
    provider = routing.provider
    model = routing.model

    if enabled:
        if not provider:
            problems.append("a2a.routing.provider is required when a2a.enabled is true")
        elif not _provider_is_known(provider, known_providers, full_config):
            known_list = ", ".join(sorted(known_providers)) or "(none configured)"
            problems.append(
                f"a2a.routing.provider {provider!r} is not configured in providers: "
                f"(known: {known_list})"
            )
        if not model:
            problems.append("a2a.routing.model is required when a2a.enabled is true")
    elif provider and not _provider_is_known(provider, known_providers, full_config):
        known_list = ", ".join(sorted(known_providers)) or "(none configured)"
        problems.append(
            f"a2a.routing.provider {provider!r} is not configured in providers: "
            f"(known: {known_list})"
        )

    configured_profile_names = set()
    if isinstance(profiles_raw, dict):
        configured_profile_names = {
            str(name).strip()
            for name in profiles_raw.keys()
            if str(name).strip()
        }

    if isinstance(heuristics_raw, list):
        for index, item in enumerate(heuristics_raw):
            if not isinstance(item, dict):
                continue
            target = str(item.get("target") or "").strip()
            if configured_profile_names and target and target not in configured_profile_names:
                problems.append(
                    f"a2a.heuristics[{index}].target {target!r} is not listed in a2a.profiles"
                )

    return problems
