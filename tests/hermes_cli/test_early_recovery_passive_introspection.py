"""Passive CLI introspection must not resolve external secret sources.

``hermes --help`` prints static text and exits, yet the import-time dotenv load used to fetch
every enabled 1Password/Bitwarden source and inject those values into ``os.environ``. These
tests pin the argv shapes that are pure introspection (skip) and the ones that are not.
"""

from __future__ import annotations

import sys

import pytest

from hermes_cli import _early_recovery


@pytest.fixture(autouse=True)
def _reset_recovered_flag(monkeypatch):
    """Keep the updater-retry latch out of the picture for every case here."""
    monkeypatch.setattr(_early_recovery, "_UPDATE_RETRY_RECOVERED", False)


@pytest.mark.parametrize(
    "argv",
    [
        ["hermes", "--help"],
        ["hermes", "-h"],
        ["hermes", "secrets", "--help"],
        ["hermes", "update"],
    ],
)
def test_skips_external_secret_sources(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    assert _early_recovery._should_skip_external_secret_sources() is True


@pytest.mark.parametrize(
    "argv",
    [
        ["hermes"],
        ["hermes", "chat"],
        ["hermes", "-z", "show me the --help output"],
        ["hermes", "gateway", "run"],
    ],
)
def test_resolves_external_secret_sources(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)
    assert _early_recovery._should_skip_external_secret_sources() is False


def test_is_passive_introspection_detects_help_token(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "-h"])
    assert _early_recovery._is_passive_introspection() is True

    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
    assert _early_recovery._is_passive_introspection() is False


def test_recovered_update_retry_still_skips(monkeypatch):
    """The existing updater latch keeps working through the new predicate."""
    monkeypatch.setattr(sys, "argv", ["hermes", "chat"])
    monkeypatch.setattr(_early_recovery, "_UPDATE_RETRY_RECOVERED", True)
    assert _early_recovery._should_skip_external_secret_sources() is True
