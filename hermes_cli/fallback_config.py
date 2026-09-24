"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

import time
from typing import Any


def _normalized_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``key_env`` names the env var
    holding the key; ``api_key_env`` accepted as an alias). Returns None when
    neither yields a non-empty value, letting ``resolve_runtime_provider``
    fall through to the provider's standard credential resolution.

    ``key_env`` is resolved through ``agent.secret_scope.get_secret`` rather
    than a raw ``os.getenv`` — in a multiplexed gateway a bare env read would
    ignore the active profile's scope and can return another profile's
    credential. ``get_secret`` already implements the right fallback: it
    reads ``os.environ`` when there's no active multiplexed scope (matching
    prior single-profile behavior), and fails closed only when multiplexing
    is active with no scope installed.
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        from agent.secret_scope import get_secret

        return (get_secret(key_env) or "").strip() or None
    return None


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

        normalized = dict(entry)
        normalized["provider"] = provider
        normalized["model"] = model

        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url

        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its
    order. Legacy ``fallback_model`` entries are appended afterwards unless
    they target the same provider/model/base_url route as an earlier entry.
    The returned list always contains fresh dict copies.
    """

    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            chain.append(entry)

    return chain


def apply_fallback_chain_to_agent(agent: Any, chain: list | None) -> None:
    """Align a live agent's fallback chain with the chain just read from disk.

    Skips the rewrite while a cooldown is holding an already-activated
    fallback. ``restore_primary_runtime`` owns that turn. When the primary
    is active, replace the chain so a long-lived CLI or gateway session
    picks up ``fallback_providers`` edits without a process restart.
    """
    if agent is None:
        return
    new_chain = list(chain or [])
    rate_limited_until = getattr(agent, "_rate_limited_until", 0) or 0
    if getattr(agent, "_fallback_activated", False) and rate_limited_until > time.monotonic():
        return
    old_chain = list(getattr(agent, "_fallback_chain", []) or [])
    agent._fallback_chain = new_chain
    agent._fallback_model = new_chain[0] if new_chain else None
    if not getattr(agent, "_fallback_activated", False):
        agent._fallback_index = 0
    if new_chain != old_chain:
        unavailable = getattr(agent, "_unavailable_fallback_keys", None)
        if unavailable:
            unavailable.clear()


# path -> ((st_mtime_ns, size), chain). Size is part of the stamp because
# a same-tick rewrite can keep mtime_ns unchanged on a coarse filesystem.
_CHAIN_MTIME_CACHE: dict[str, tuple[tuple[int, int | None], list | None]] = {}


def _config_stamp(cfg_path: Any) -> tuple[int, int | None]:
    st = cfg_path.stat()
    return (st.st_mtime_ns, getattr(st, "st_size", None))


def load_fallback_chain_for_path(cfg_path: Any) -> list | None:
    """Return the fallback chain for one config file.

    A missing file returns None. A parse failure raises so callers can
    keep the last good chain. An unchanged mtime and size returns the
    cached chain without reading the file again.
    """
    if not cfg_path.exists():
        return None
    stamp = _config_stamp(cfg_path)
    key = str(cfg_path)
    cached = _CHAIN_MTIME_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return list(cached[1]) if cached[1] else None

    from hermes_cli.config import _expand_env_vars, read_user_config_raw

    cfg = read_user_config_raw(cfg_path)
    try:
        from hermes_cli import managed_scope

        cfg = managed_scope.apply_managed_overlay(cfg)
    except Exception:
        pass
    try:
        expanded = _expand_env_vars(cfg)
        if isinstance(expanded, dict):
            cfg = expanded
    except Exception:
        pass
    chain = get_fallback_chain(cfg) or None
    _CHAIN_MTIME_CACHE[key] = (stamp, chain)
    return list(chain) if chain else None


def refresh_agent_fallback_chain(agent: Any) -> list | None:
    """Re-read ``fallback_providers`` from disk onto a live agent.

    A torn config write keeps the chain already on the agent. A successful
    read that has no chain clears it. An unchanged mtime skips the parse.
    """
    if agent is None:
        return None
    try:
        from hermes_cli.config import get_config_path

        cfg_path = get_config_path()
        if not cfg_path.exists():
            apply_fallback_chain_to_agent(agent, None)
            agent._fallback_chain_stamp = None
            return None
        stamp = _config_stamp(cfg_path)
        if (
            getattr(agent, "_fallback_chain_stamp", None) == stamp
            and getattr(agent, "_fallback_chain", None) is not None
        ):
            return list(agent._fallback_chain)
        chain = load_fallback_chain_for_path(cfg_path)
    except Exception:
        return list(getattr(agent, "_fallback_chain", []) or []) or None
    held = (
        getattr(agent, "_fallback_activated", False)
        and (getattr(agent, "_rate_limited_until", 0) or 0) > time.monotonic()
    )
    apply_fallback_chain_to_agent(agent, chain)
    # A cooldown skip leaves the in-use chain in place. Do not stamp that
    # read, or the next turn treats the file as unchanged and never applies it.
    if not held:
        agent._fallback_chain_stamp = stamp
    return chain
