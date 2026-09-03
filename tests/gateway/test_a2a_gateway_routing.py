"""Gateway integration tests for core A2A routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli.a2a_router import A2AConfig, A2ARoute


def _make_runner(platform: Platform = Platform.WHATSAPP):
    from gateway.run import GatewayRunner

    config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True)},
    )
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {platform: SimpleNamespace(send=AsyncMock())}
    return runner


def _make_source(platform: Platform = Platform.WHATSAPP) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id="15551234567@s.whatsapp.net",
        chat_id="15551234567@s.whatsapp.net",
        user_name="tester",
        chat_type="dm",
    )


@pytest.mark.asyncio
async def test_apply_a2a_disabled_is_noop():
    runner = _make_runner()
    source = _make_source()
    disabled = A2AConfig(enabled=False)

    with patch("hermes_cli.a2a_router.load_a2a_config", return_value=disabled):
        msg, out_source, early = await runner._apply_a2a_gateway_routing("hello", source)

    assert msg == "hello"
    assert early is None
    assert out_source is source


@pytest.mark.asyncio
async def test_apply_a2a_switches_profile_on_direct_route():
    runner = _make_runner()
    source = _make_source()
    enabled = A2AConfig(enabled=True)
    route = A2ARoute(
        mode="direct",
        target_profile="researcher",
        reason="research intent",
        rewritten_prompt="research LangChain routers",
        metadata={"method": "heuristic"},
    )

    with patch("hermes_cli.a2a_router.load_a2a_config", return_value=enabled), patch(
        "hermes_cli.a2a_router.route_prompt",
        return_value=route,
    ), patch(
        "hermes_cli.profiles.list_profiles",
        return_value=[SimpleNamespace(name="athena"), SimpleNamespace(name="researcher")],
    ), patch("hermes_cli.profiles.profile_exists", return_value=True), patch.object(
        runner,
        "_active_profile_name",
        return_value="athena",
    ):
        msg, out_source, early = await runner._apply_a2a_gateway_routing(
            "research LangChain routers",
            source,
        )

    assert early is None
    assert msg == "research LangChain routers"
    assert out_source.profile == "researcher"


@pytest.mark.asyncio
async def test_apply_a2a_kanban_returns_early_reply():
    runner = _make_runner()
    source = _make_source()
    enabled = A2AConfig(enabled=True)
    route = A2ARoute(
        mode="kanban",
        target_profile="orchestrator",
        reason="board dispatch",
        rewritten_prompt="dispatch work package",
        metadata={"method": "heuristic"},
    )

    with patch("hermes_cli.a2a_router.load_a2a_config", return_value=enabled), patch(
        "hermes_cli.a2a_router.route_prompt",
        return_value=route,
    ), patch(
        "hermes_cli.profiles.list_profiles",
        return_value=[SimpleNamespace(name="athena"), SimpleNamespace(name="orchestrator")],
    ), patch(
        "hermes_cli.a2a_router.dispatch_to_kanban",
        return_value=(True, "Kanban task t_abc dispatched via profile orchestrator."),
    ), patch.object(runner, "_active_profile_name", return_value="athena"):
        msg, out_source, early = await runner._apply_a2a_gateway_routing(
            "dispatch work package",
            source,
        )

    assert early == "Kanban task t_abc dispatched via profile orchestrator."
    assert msg == "dispatch work package"


def test_profile_scope_for_source_when_a2a_routes_away():
    runner = _make_runner()
    source = _make_source()
    source = source.__class__(
        platform=source.platform,
        user_id=source.user_id,
        chat_id=source.chat_id,
        user_name=source.user_name,
        chat_type=source.chat_type,
        profile="researcher",
    )

    with patch.object(runner, "_active_profile_name", return_value="athena"):
        assert runner._profile_scope_for_source(source) is True

    with patch.object(runner, "_active_profile_name", return_value="researcher"):
        assert runner._profile_scope_for_source(source) is False


def test_profile_scope_for_source_multiplex_always_true():
    runner = _make_runner()
    runner.config.multiplex_profiles = True
    source = _make_source()

    assert runner._profile_scope_for_source(source) is True
