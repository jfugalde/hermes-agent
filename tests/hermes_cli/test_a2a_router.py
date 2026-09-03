"""Unit tests for hermes_cli.a2a_router."""

from hermes_cli.a2a_router import A2AConfig, A2AProfileMeta, load_a2a_config, route_prompt


def test_route_prompt_heuristic_research():
    cfg = A2AConfig(
        enabled=True,
        profiles={
            "researcher": A2AProfileMeta(capabilities=["research"]),
        },
    )
    route = route_prompt(
        "please research LangChain routing patterns",
        current_profile="athena",
        available_profiles=["athena", "researcher"],
        config=cfg,
    )
    assert route.target_profile == "researcher"
    assert route.mode == "direct"
    assert route.metadata.get("method") in {"heuristic", "default", "explicit_hint"}


def test_route_prompt_disabled_stays_on_current():
    cfg = load_a2a_config({"a2a": {"enabled": False}})
    route = route_prompt(
        "research something",
        current_profile="athena",
        available_profiles=["athena", "researcher"],
        config=cfg,
    )
    assert route.mode == "current"
    assert route.target_profile == "athena"
    assert route.metadata.get("method") == "disabled"


def test_explicit_profile_hint():
    cfg = A2AConfig(enabled=True)
    route = route_prompt(
        "@profile:researcher summarize this",
        current_profile="athena",
        available_profiles=["athena", "researcher"],
        config=cfg,
    )
    assert route.target_profile == "researcher"
    assert route.metadata.get("method") == "explicit_hint"
