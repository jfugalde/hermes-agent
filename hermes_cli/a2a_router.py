"""Core A2A (Agent-to-Agent) routing for Hermes Agent profiles.

Analyzes incoming prompts and routes them to the best-suited profile or kanban
board. Uses LangChain structured output when configured; falls back to
rule-based heuristics when the LLM path is unavailable.

See ``docs/superpowers/specs/2026-08-05-hermes-a2a-routing-design.md`` in the
infrastructure repo for the full design.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

logger = logging.getLogger(__name__)

ROUTER_SYSTEM_PROMPT = """You are an A2A router for Hermes Agent profiles.

Available profiles and their capabilities:
{profile_capabilities}

Current active profile: {current_profile}

Routing modes:
- direct: route to a specific profile for direct execution
- kanban: dispatch to kanban board for orchestrator handling
- current: stay on current profile (no routing needed)

Choose the best profile based on the user prompt. Consider:
1. Profile capabilities match
2. Keyword signals
3. Complexity (kanban for multi-step)
4. Current profile suitability

Return routing decision with confidence score."""

ROUTER_USER_PROMPT = """User prompt: {user_prompt}

Route this prompt to the best profile."""

_EXPLICIT_PROFILE_RE = re.compile(r"(?:@profile:|profile=)([A-Za-z0-9_.-]+)")


@dataclass
class A2AHeuristicRule:
    pattern: str
    target: str
    mode: str = "direct"


@dataclass
class A2AProfileMeta:
    capabilities: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class A2ARoutingConfig:
    provider: str = ""
    model: str = ""
    temperature: float = 0.0
    timeout_ms: int = 3000
    fallback: str = "heuristic"  # heuristic | current | error
    base_url: str = ""
    api_key: str = ""


@dataclass
class A2AKanbanConfig:
    enabled: bool = True
    bridge_url: str = "http://127.0.0.1:3046"
    route_keywords: list[str] = field(default_factory=list)


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
    profiles: dict[str, A2AProfileMeta] = field(default_factory=dict)
    kanban: A2AKanbanConfig = field(default_factory=A2AKanbanConfig)
    heuristics: list[A2AHeuristicRule] = field(default_factory=list)
    explicit_hints: A2AExplicitHintsConfig = field(default_factory=A2AExplicitHintsConfig)
    logging: A2ALoggingConfig = field(default_factory=A2ALoggingConfig)


@dataclass
class A2ARoute:
    mode: str  # direct | kanban | current
    target_profile: str
    reason: str
    rewritten_prompt: str | None
    metadata: dict[str, Any]


_DEFAULT_HEURISTICS: tuple[A2AHeuristicRule, ...] = (
    A2AHeuristicRule(
        pattern=r"research|investigate|review|evidence",
        target="researcher",
        mode="direct",
    ),
    A2AHeuristicRule(
        pattern=r"board|kanban|orchestrate|work package|work-package",
        target="orchestrator",
        mode="kanban",
    ),
    A2AHeuristicRule(
        pattern=r"deploy|restart|docker|compose|infra|service",
        target="deployer",
        mode="direct",
    ),
)


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
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
    if not value:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                out.append(text)
        return out
    return []


def parse_a2a_config(raw: dict[str, Any] | None) -> A2AConfig:
    """Parse the ``a2a:`` section from config.yaml into ``A2AConfig``."""
    raw = raw or {}
    if not isinstance(raw, dict):
        return A2AConfig()

    routing_raw = raw.get("routing") if isinstance(raw.get("routing"), dict) else {}
    kanban_raw = raw.get("kanban") if isinstance(raw.get("kanban"), dict) else {}
    hints_raw = raw.get("explicit_hints") if isinstance(raw.get("explicit_hints"), dict) else {}
    logging_raw = raw.get("logging") if isinstance(raw.get("logging"), dict) else {}

    profiles: dict[str, A2AProfileMeta] = {}
    profiles_raw = raw.get("profiles")
    if isinstance(profiles_raw, dict):
        for name, meta in profiles_raw.items():
            slug = str(name or "").strip()
            if not slug or not isinstance(meta, dict):
                continue
            profiles[slug] = A2AProfileMeta(
                capabilities=_coerce_str_list(meta.get("capabilities")),
                keywords=_coerce_str_list(meta.get("keywords")),
            )

    heuristics: list[A2AHeuristicRule] = []
    heuristics_raw = raw.get("heuristics")
    if isinstance(heuristics_raw, list):
        for rule in heuristics_raw:
            if not isinstance(rule, dict):
                continue
            pattern = str(rule.get("pattern") or "").strip()
            target = str(rule.get("target") or "").strip()
            if not pattern or not target:
                continue
            mode = str(rule.get("mode") or "direct").strip().lower() or "direct"
            heuristics.append(A2AHeuristicRule(pattern=pattern, target=target, mode=mode))

    return A2AConfig(
        enabled=_coerce_bool(raw.get("enabled"), False),
        routing=A2ARoutingConfig(
            provider=str(routing_raw.get("provider") or "").strip(),
            model=str(routing_raw.get("model") or "").strip(),
            temperature=_coerce_float(routing_raw.get("temperature"), 0.0),
            timeout_ms=_coerce_int(routing_raw.get("timeout_ms"), 3000),
            fallback=str(routing_raw.get("fallback") or "heuristic").strip().lower()
            or "heuristic",
            base_url=str(routing_raw.get("base_url") or "").strip().rstrip("/"),
            api_key=str(routing_raw.get("api_key") or "").strip(),
        ),
        profiles=profiles,
        kanban=A2AKanbanConfig(
            enabled=_coerce_bool(kanban_raw.get("enabled"), True),
            bridge_url=str(kanban_raw.get("bridge_url") or "http://127.0.0.1:3046").strip()
            or "http://127.0.0.1:3046",
            route_keywords=_coerce_str_list(kanban_raw.get("route_keywords")),
        ),
        heuristics=heuristics,
        explicit_hints=A2AExplicitHintsConfig(
            enabled=_coerce_bool(hints_raw.get("enabled"), True),
        ),
        logging=A2ALoggingConfig(
            enabled=_coerce_bool(logging_raw.get("enabled"), True),
            log_decisions=_coerce_bool(logging_raw.get("log_decisions"), True),
            include_confidence=_coerce_bool(logging_raw.get("include_confidence"), True),
        ),
    )


def load_a2a_config(full_config: dict[str, Any] | None) -> A2AConfig:
    """Load ``A2AConfig`` from a full Hermes ``config.yaml`` dict."""
    if not isinstance(full_config, dict):
        return A2AConfig()
    return parse_a2a_config(full_config.get("a2a"))


def _normalize_profiles(profiles: Iterable[str], current_profile: str) -> list[str]:
    out: list[str] = []
    for profile in profiles:
        clean = (profile or "").strip()
        if clean and clean not in out:
            out.append(clean)
    current = (current_profile or "").strip()
    if current and current not in out:
        out.insert(0, current)
    elif not out and current:
        out.append(current)
    return out


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:16]


def _effective_heuristics(config: A2AConfig) -> tuple[A2AHeuristicRule, ...]:
    return tuple(config.heuristics) if config.heuristics else _DEFAULT_HEURISTICS


def _sanitize_route_with_config(
    route: A2ARoute,
    *,
    current_profile: str,
    profiles: list[str],
    config: A2AConfig,
) -> A2ARoute:
    mode = (route.mode or "current").strip().lower()
    if mode not in {"direct", "kanban", "current"}:
        mode = "direct"

    target = (route.target_profile or current_profile).strip()
    if target not in profiles:
        logger.debug(
            "A2A route target %r not in available profiles %s; staying on %r",
            target,
            profiles,
            current_profile,
        )
        target = current_profile
        mode = "current"

    if mode == "kanban" and not config.kanban.enabled:
        logger.debug("A2A kanban routing requested but kanban disabled; using direct")
        mode = "direct"

    if mode == "current":
        target = current_profile

    return A2ARoute(
        mode=mode,
        target_profile=target,
        reason=(route.reason or "").strip() or "routing decision",
        rewritten_prompt=route.rewritten_prompt,
        metadata=dict(route.metadata or {}),
    )


def _build_profile_capabilities_text(config: A2AConfig, profiles: list[str]) -> str:
    lines: list[str] = []
    for profile in profiles:
        meta = config.profiles.get(profile)
        if meta and meta.capabilities:
            caps = ", ".join(meta.capabilities)
        elif meta and meta.keywords:
            caps = f"keywords: {', '.join(meta.keywords)}"
        else:
            caps = "(no configured capabilities)"
        lines.append(f"- {profile}: {caps}")
    return "\n".join(lines)


def _resolve_routing_credentials(config: A2AConfig) -> tuple[str, str, str] | None:
    """Return ``(model, base_url, api_key)`` for the routing LLM, or None."""
    routing = config.routing
    model = routing.model.strip()
    if not model:
        return None

    inline_key = routing.api_key.strip()
    inline_url = routing.base_url.strip()
    if inline_key and inline_url:
        return model, inline_url.rstrip("/"), inline_key

    provider = routing.provider.strip()
    if not provider:
        if inline_key and inline_url:
            return model, inline_url.rstrip("/"), inline_key
        return None

    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        creds = resolve_runtime_provider(
            requested=provider,
            explicit_api_key=inline_key or None,
            explicit_base_url=inline_url or None,
            target_model=model,
        )
    except Exception as exc:
        logger.warning("A2A failed to resolve routing provider %r: %s", provider, exc)
        return None

    base_url = (inline_url or str(creds.get("base_url") or "")).strip().rstrip("/")
    api_key = (inline_key or str(creds.get("api_key") or "")).strip()
    if not api_key:
        logger.warning("A2A routing provider %r has no API key", provider)
        return None
    if not base_url:
        logger.warning("A2A routing provider %r has no base URL", provider)
        return None
    return model, base_url, api_key


def _log_route_decision(
    *,
    config: A2AConfig,
    prompt: str,
    current_profile: str,
    route: A2ARoute,
) -> None:
    if not config.logging.enabled or not config.logging.log_decisions:
        return

    payload: dict[str, Any] = {
        "event": "a2a_route",
        "prompt_hash": _prompt_hash(prompt),
        "from_profile": current_profile,
        "to_profile": route.target_profile,
        "mode": route.mode,
        "reason": route.reason,
        "method": route.metadata.get("method", "unknown"),
    }
    if config.logging.include_confidence and "confidence" in route.metadata:
        payload["confidence"] = route.metadata["confidence"]
    if "model" in route.metadata:
        payload["model"] = route.metadata["model"]
    if "latency_ms" in route.metadata:
        payload["latency_ms"] = route.metadata["latency_ms"]
    if "pattern" in route.metadata:
        payload["pattern"] = route.metadata["pattern"]
    if "fallback_reason" in route.metadata:
        payload["fallback_reason"] = route.metadata["fallback_reason"]

    logger.info("A2A route decision: %s", payload)


def _extract_explicit_profile(prompt: str, profiles: list[str]) -> str | None:
    """Parse ``@profile:name`` or ``profile=name`` hints from the prompt."""
    match = _EXPLICIT_PROFILE_RE.search(prompt or "")
    if not match:
        return None
    wanted = match.group(1).strip()
    if wanted in profiles:
        return wanted
    logger.debug("A2A explicit profile hint %r not in available profiles", wanted)
    return None


def _heuristic_route(
    prompt: str,
    current_profile: str,
    profiles: list[str],
    config: A2AConfig,
) -> A2ARoute:
    """Rule-based routing fallback."""
    text = (prompt or "").lower()

    if config.explicit_hints.enabled:
        explicit = _extract_explicit_profile(prompt, profiles)
        if explicit:
            return A2ARoute(
                mode="direct",
                target_profile=explicit,
                reason=f"explicit profile hint: {explicit}",
                rewritten_prompt=prompt,
                metadata={"method": "explicit_hint"},
            )

    for rule in _effective_heuristics(config):
        try:
            matched = re.search(rule.pattern, text, re.IGNORECASE)
        except re.error as exc:
            logger.warning("A2A invalid heuristic pattern %r: %s", rule.pattern, exc)
            continue
        if not matched:
            continue
        if rule.target not in profiles:
            logger.debug(
                "A2A heuristic target %r not available; skipping pattern %r",
                rule.target,
                rule.pattern,
            )
            continue
        mode = rule.mode if rule.mode in {"direct", "kanban", "current"} else "direct"
        if mode == "kanban" and not config.kanban.enabled:
            mode = "direct"
        return A2ARoute(
            mode=mode,
            target_profile=rule.target,
            reason=f"heuristic match: {rule.pattern}",
            rewritten_prompt=prompt,
            metadata={"method": "heuristic", "pattern": rule.pattern},
        )

    kanban_keywords = config.kanban.route_keywords or [
        "board",
        "kanban",
        "orchestrator",
        "work package",
        "work-package",
    ]
    if config.kanban.enabled and any(keyword.lower() in text for keyword in kanban_keywords):
        orchestrator = "orchestrator" if "orchestrator" in profiles else current_profile
        if orchestrator in profiles:
            return A2ARoute(
                mode="kanban",
                target_profile=orchestrator,
                reason="heuristic kanban keyword match",
                rewritten_prompt=prompt,
                metadata={"method": "heuristic", "pattern": "kanban_keywords"},
            )

    return A2ARoute(
        mode="current",
        target_profile=current_profile,
        reason="no routing signal detected",
        rewritten_prompt=prompt,
        metadata={"method": "default"},
    )


def _langchain_route(
    prompt: str,
    current_profile: str,
    profiles: list[str],
    config: A2AConfig,
) -> A2ARoute | None:
    """LangChain-based routing with structured output."""
    try:
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI
        from pydantic import BaseModel, Field
    except ImportError:
        logger.debug("A2A LangChain dependencies unavailable")
        return None

    credentials = _resolve_routing_credentials(config)
    if not credentials:
        return None

    model_name, base_url, api_key = credentials

    class RouteDecision(BaseModel):
        mode: Literal["direct", "kanban", "current"] = Field(
            description="Routing mode: direct, kanban, or current",
        )
        target_profile: str = Field(description="Target Hermes profile slug")
        reason: str = Field(description="Short reason for the routing decision")
        confidence: float = Field(
            description="Routing confidence between 0.0 and 1.0",
            ge=0.0,
            le=1.0,
        )
        rewritten_prompt: str | None = Field(
            default=None,
            description="Optional rewritten prompt for the target profile",
        )
        route_metadata: dict[str, Any] = Field(default_factory=dict)

    llm = ChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        temperature=config.routing.temperature,
        timeout=max(config.routing.timeout_ms, 1) / 1000,
    )
    structured_llm = llm.with_structured_output(RouteDecision)

    prompt_template = ChatPromptTemplate.from_messages(
        [
            ("system", ROUTER_SYSTEM_PROMPT),
            ("human", ROUTER_USER_PROMPT),
        ]
    )
    chain = prompt_template | structured_llm

    try:
        result = chain.invoke(
            {
                "profile_capabilities": _build_profile_capabilities_text(config, profiles),
                "current_profile": current_profile,
                "user_prompt": prompt,
            }
        )
    except Exception as exc:
        logger.warning("A2A LangChain invoke failed: %s", exc)
        raise

    mode = (getattr(result, "mode", "current") or "current").strip().lower()
    target = (getattr(result, "target_profile", current_profile) or current_profile).strip()
    reason = (getattr(result, "reason", "") or "").strip() or "langchain router decision"
    rewritten = getattr(result, "rewritten_prompt", None)
    if rewritten is not None:
        rewritten = str(rewritten).strip() or None
    confidence = float(getattr(result, "confidence", 0.0) or 0.0)
    route_metadata = getattr(result, "route_metadata", {}) or {}

    route = A2ARoute(
        mode=mode,
        target_profile=target,
        reason=reason,
        rewritten_prompt=rewritten if rewritten is not None else prompt,
        metadata={
            "confidence": confidence,
            "method": "langchain",
            "model": model_name,
            **(route_metadata if isinstance(route_metadata, dict) else {}),
        },
    )
    return _sanitize_route_with_config(
        route,
        current_profile=current_profile,
        profiles=profiles,
        config=config,
    )


def _role_for_profile(profile: str) -> str:
    """Map Hermes profile slugs to kanban bridge role ids."""
    mapping = {
        "orchestrator": "pm",
        "researcher": "spike",
        "deployer": "infra",
        "reviewer": "quality",
        "specifier": "developer",
        "apollo": "fullstack",
        "athena": "design",
        "default": "developer",
    }
    return mapping.get((profile or "").strip(), "developer")


def dispatch_to_kanban(
    prompt: str,
    profile: str,
    config: A2AConfig,
) -> tuple[bool, str]:
    """POST an A2A-routed prompt to the Hermes kanban bridge ``/assist`` endpoint."""
    import json
    import urllib.error
    import urllib.request

    bridge = (config.kanban.bridge_url or "http://127.0.0.1:3046").rstrip("/")
    url = f"{bridge}/assist"
    body = {
        "text": prompt,
        "role": _role_for_profile(profile),
        "playbook": {
            "slug": "hermes-a2a-gateway",
            "hermes_profile": profile,
            "hermes_skills": ["kanban-orchestrator"],
        },
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        return False, f"kanban dispatch HTTP {exc.code}: {detail[:300]}"
    except Exception as exc:
        return False, f"kanban dispatch failed: {exc}"

    if payload.get("dispatched"):
        task_id = payload.get("kanban_task_id", "unknown")
        return True, f"Kanban task {task_id} dispatched via profile {profile}."

    reason = payload.get("reason", "dispatch rejected")
    return False, f"Kanban dispatch not accepted: {reason}"


def route_prompt(
    prompt: str,
    current_profile: str,
    available_profiles: list[str],
    config: A2AConfig,
) -> A2ARoute:
    """Main routing entry point."""
    profiles = _normalize_profiles(available_profiles, current_profile)
    current = (current_profile or "").strip() or (profiles[0] if profiles else "default")
    started = time.monotonic()

    if not config.enabled:
        route = A2ARoute(
            mode="current",
            target_profile=current,
            reason="a2a disabled",
            rewritten_prompt=prompt,
            metadata={"method": "disabled"},
        )
        route.metadata["latency_ms"] = 0
        return route

    if config.explicit_hints.enabled:
        explicit = _extract_explicit_profile(prompt, profiles)
        if explicit:
            route = A2ARoute(
                mode="direct",
                target_profile=explicit,
                reason=f"explicit profile hint: {explicit}",
                rewritten_prompt=prompt,
                metadata={"method": "explicit_hint"},
            )
            route.metadata["latency_ms"] = int((time.monotonic() - started) * 1000)
            _log_route_decision(
                config=config,
                prompt=prompt,
                current_profile=current,
                route=route,
            )
            return route

    route: A2ARoute | None = None
    llm_error: Exception | None = None

    if config.routing.provider or config.routing.model:
        try:
            route = _langchain_route(prompt, current, profiles, config)
        except Exception as exc:
            llm_error = exc
            logger.warning("A2A LangChain routing failed; using fallback: %s", exc)

    if route is None:
        fallback = config.routing.fallback
        if fallback == "error":
            raise RuntimeError(f"A2A routing failed: {llm_error}")
        if fallback == "current":
            route = A2ARoute(
                mode="current",
                target_profile=current,
                reason="routing fallback: current profile",
                rewritten_prompt=prompt,
                metadata={"method": "fallback_current"},
            )
            if llm_error is not None:
                route.metadata["fallback_reason"] = str(llm_error)
        else:
            route = _heuristic_route(prompt, current, profiles, config)
            if llm_error is not None:
                route.metadata["fallback_reason"] = str(llm_error)

    route = _sanitize_route_with_config(
        route,
        current_profile=current,
        profiles=profiles,
        config=config,
    )
    route.metadata.setdefault("latency_ms", int((time.monotonic() - started) * 1000))
    _log_route_decision(
        config=config,
        prompt=prompt,
        current_profile=current,
        route=route,
    )
    return route


_PROFILE_ROLE_MAP = {
    "orchestrator": "pm",
    "researcher": "spike",
    "deployer": "infra",
    "reviewer": "quality",
    "specifier": "developer",
    "apollo": "fullstack",
    "athena": "design",
    "default": "developer",
}


def format_a2a_console_line(
    route: A2ARoute,
    *,
    from_profile: str,
    include_confidence: bool = True,
) -> str:
    """Format a human-readable routing decision for CLI stderr."""
    method = str(route.metadata.get("method") or "unknown")
    detail = route.reason.strip() or method
    if include_confidence and "confidence" in route.metadata:
        detail = f"{detail}, confidence={route.metadata['confidence']:.2f}"
    latency_ms = route.metadata.get("latency_ms")
    latency_suffix = f", {latency_ms}ms" if latency_ms is not None else ""
    if route.mode == "current" or route.target_profile == from_profile:
        return (
            f"[A2A] stay on {from_profile} ({method}: {detail}{latency_suffix})"
        )
    return (
        f"[A2A] {from_profile} → {route.target_profile} "
        f"({method}: {detail}{latency_suffix})"
    )


def inject_a2a_execution_context(
    route: A2ARoute,
    *,
    from_profile: str,
) -> None:
    """Expose routing metadata to downstream agent execution via env vars."""
    payload = {
        "mode": route.mode,
        "from_profile": from_profile,
        "to_profile": route.target_profile,
        "reason": route.reason,
        "metadata": dict(route.metadata or {}),
    }
    os.environ["HERMES_A2A_ROUTE_MODE"] = route.mode
    os.environ["HERMES_A2A_FROM_PROFILE"] = from_profile
    os.environ["HERMES_A2A_TO_PROFILE"] = route.target_profile
    os.environ["HERMES_A2A_REASON"] = route.reason
    os.environ["HERMES_A2A_METHOD"] = str(route.metadata.get("method") or "")
    os.environ["HERMES_A2A_METADATA"] = json.dumps(payload, separators=(",", ":"))


def _role_for_profile(profile: str) -> str:
    return _PROFILE_ROLE_MAP.get(profile, "developer")


def dispatch_to_kanban(
    prompt: str,
    profile: str,
    config: A2AConfig,
) -> tuple[bool, str]:
    """POST the prompt to the Hermes kanban bridge ``/assist`` endpoint."""
    bridge = (
        os.environ.get("HERMES_BRIDGE_URL", "").strip().rstrip("/")
        or config.kanban.bridge_url.strip().rstrip("/")
        or "http://127.0.0.1:3046"
    )
    url = f"{bridge}/assist"
    body = {
        "text": prompt,
        "role": _role_for_profile(profile),
        "playbook": {
            "slug": "hermes-a2a-cli",
            "hermes_profile": profile,
            "hermes_skills": ["kanban-orchestrator"],
        },
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return False, f"kanban dispatch HTTP {exc.code}: {detail[:300]}"
    except Exception as exc:
        return False, f"kanban dispatch failed: {exc}"

    if payload.get("dispatched"):
        task_id = payload.get("kanban_task_id", "unknown")
        return True, f"kanban task {task_id} dispatched via profile {profile}"
    reason = payload.get("reason", "dispatch rejected")
    return False, str(reason)
