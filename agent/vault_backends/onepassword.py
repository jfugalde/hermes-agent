"""1Password Login items as a vault backend (``op`` CLI).

Unlock: ``op signin --raw`` with the master password on stdin (desktop-app
integration or account-level auth) mints an ``OP_SESSION_<account>`` token.
A configured service-account token skips the prompt entirely (headless).
List: ``op item list --categories Login --format json`` → title, urls,
username. Resolve: ``op item get <id> --fields label=password --reveal``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import LoginBackend, UnlockRequired, run_with_stdin_secret
from agent.vault_backends import unlock as _unlock
from agent.vault_store import VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0

# A service account has no default vault, so `op item get <id>` refuses to run without one.
_VAULT_REQUIRED = "a vault query must be provided"
_NOT_IN_VAULT = "isn't an item in the"


def _vault_mismatch(exc: Exception) -> bool:
    """True when the failure is an absent/wrong vault — another vault may still hold the item."""
    message = str(exc)
    return _VAULT_REQUIRED in message or _NOT_IN_VAULT in message


class OnePasswordLoginBackend(LoginBackend):
    name = "onepassword"
    display_name = "1Password"
    prefix = "op:"
    needs_unlock = True

    def __init__(self, cfg: Optional[Dict] = None):
        self.cfg = cfg or {}
        from agent.secret_scope import get_secret
        env_name = str(self.cfg.get("service_account_token_env") or "OP_SERVICE_ACCOUNT_TOKEN")
        self._service_token = get_secret(env_name, "") or ""
        # Vault that accepted the last item read: only service accounts need it (no default vault).
        self._vault_hint: Optional[str] = None

    # ── auth ────────────────────────────────────────────────────────────────

    def _op(self) -> Path:
        op = find_op(str(self.cfg.get("binary_path") or ""))
        if op is None:
            raise RuntimeError("1Password CLI (op) not found — install it or set vault.onepassword.binary_path")
        return op

    def _env(self, session_token: Optional[str]) -> Dict[str, str]:
        from agent.secret_scope import get_secret
        env = {k: os.environ[k] for k in _OP_ENV_ALLOWLIST if k in os.environ and not k.startswith("OP_CONNECT_")}
        # Connect credentials outrank OP_SERVICE_ACCOUNT_TOKEN inside op, so they must come from the
        # profile's own secret scope like the service token does — never from the launch environment.
        for k in ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN"):
            if v := get_secret(k, ""):
                env[k] = v
        env["NO_COLOR"] = "1"
        account = str(self.cfg.get("account") or "")
        if account:
            env["OP_ACCOUNT"] = account
        if self._service_token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = self._service_token
        elif session_token:
            # op signin --raw prints the bare token; the env var name carries the account shorthand,
            # which op also accepts as plain OP_SESSION for the default account.
            env[f"OP_SESSION_{account}" if account else "OP_SESSION"] = session_token
        return env

    def is_unlocked(self) -> bool:
        return bool(self._service_token) or _unlock.is_unlocked(self.name)

    def unlock(self, master_password: str) -> None:
        """Mint a session token from the master password (consumed on stdin, never argv)."""
        generation = _unlock.begin_unlock(self.name)
        cmd = [str(self._op()), "signin", "--raw"]
        if account := str(self.cfg.get("account") or ""):
            cmd += ["--account", account]
        proc = run_with_stdin_secret(cmd, env=self._env(None), secret=master_password, timeout=_TIMEOUT, label="op")
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise RuntimeError(f"1Password unlock failed: {_scrub(proc.stderr or '')[:200] or 'no session token'}")
        if not _unlock.store_session_token(self.name, token, generation):
            raise RuntimeError("1Password was locked while unlocking; try again")

    def _run(self, *args: str) -> str:
        token = None if self._service_token else _unlock.get_session_token(self.name)
        if not self._service_token and not token:
            raise UnlockRequired(self)
        proc = run_cli([str(self._op()), *args], env=self._env(token), timeout=_TIMEOUT, label="op",
                       timeout_message="op timed out", stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            err = _scrub(proc.stderr or "")
            if "session" in err.lower() or "sign in" in err.lower() or "not signed in" in err.lower():
                _unlock.lock(self.name)
                raise UnlockRequired(self)
            raise RuntimeError(f"op failed: {err[:200]}")
        return proc.stdout or ""

    # ── backend contract ───────────────────────────────────────────────────
    def list_items(self) -> List[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        raw = json.loads(self._run("item", "list", "--categories", "Login", "--format", "json") or "[]")
        out: List[VaultItemMeta] = []
        for item in raw if isinstance(raw, list) else []:
            urls = [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]
            origins = _all_origins(urls)
            if not origins:
                continue
            username = str(item.get("additional_information") or "").strip() or None
            out.append(VaultItemMeta(
                id=f"{self.prefix}{item.get('id')}", kind="login", label=str(item.get("title") or origins[0]),
                origin=origins[0], created_at=str(item.get("created_at") or ""),
                identifier_type="username" if username else None, identifier=username,
                allowed_origins=_web_origins(origins)))
        return out

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        return next((m for m in self.list_items() if m.id == handle), None)

    def resolve_password(self, handle: str) -> str:
        return self._item_get(handle[len(self.prefix):], "--fields", "label=password", "--reveal").rstrip("\r\n")

    def resolve_otp(self, handle: str) -> Optional[str]:
        # `--otp` mints the current TOTP from the item's one-time-password field; items without one error out.
        try:
            code = self._item_get(handle[len(self.prefix):], "--otp").strip()
        except Exception:
            return None
        return code if code.isdigit() else None

    # ── item reads ──────────────────────────────────────────────────────────

    def _item_get(self, item_id: str, *args: str) -> str:
        """``op item get`` bound to a vault.

        A service account has no default vault, so ``item get <id>`` fails with "a vault query must
        be provided ..." even for an id its own ``item list`` returned. ``vault.onepassword.vault``
        (or ``vault_name``) pins the vault the way ``account`` pins the session; otherwise the
        vaults this account can read are probed once and the one holding the item is remembered.
        """
        pinned = str(self.cfg.get("vault") or self.cfg.get("vault_name") or "").strip()
        if pinned:
            return self._run("item", "get", item_id, "--vault", pinned, *args)
        if self._vault_hint:
            try:
                return self._run("item", "get", item_id, "--vault", self._vault_hint, *args)
            except RuntimeError as exc:
                if not _vault_mismatch(exc):
                    raise
                self._vault_hint = None
        try:
            return self._run("item", "get", item_id, *args)
        except RuntimeError as exc:
            if not _vault_mismatch(exc):
                raise
        return self._item_get_from_any_vault(item_id, *args)

    def _item_get_from_any_vault(self, item_id: str, *args: str) -> str:
        """First readable vault holding *item_id* wins; a vault without it is skipped, not fatal."""
        error: Optional[Exception] = None
        for vault in self._vault_names():
            try:
                out = self._run("item", "get", item_id, "--vault", vault, *args)
            except RuntimeError as exc:
                if not _vault_mismatch(exc):
                    raise
                error = exc
                continue
            self._vault_hint = vault
            return out
        raise error or RuntimeError("op failed: no vault this account can read holds that item")

    def _vault_names(self) -> List[str]:
        """Vault names this account can read (``--vault`` accepts a name or an id)."""
        raw = json.loads(self._run("vault", "list", "--format", "json") or "[]")
        out: List[str] = []
        for vault in raw if isinstance(raw, list) else []:
            name = str((vault or {}).get("name") or (vault or {}).get("id") or "").strip()
            if name and name not in out:
                out.append(name)
        return out


def _web_origins(origins: List[str]) -> tuple:
    """Fill targets are browser pages, so app URIs (``androidapp://`` etc.) never
    widen the fill set; an item whose only URI is an app URI keeps its single
    (unfillable-from-a-page) origin exactly as before."""
    web = tuple(o for o in origins if o.startswith(("http://", "https://")))
    return web or (origins[0],)


def _all_origins(urls: List[str]) -> List[str]:
    """Every normalized origin saved on the item, deduped, order preserved.

    A 1Password Login item can carry several websites; each of them is a place the
    user told 1Password the credential belongs, so all of them are valid fill targets.
    """
    out: List[str] = []
    for u in urls:
        try:
            origin = normalize_origin(u)
        except Exception:
            continue
        if origin not in out:
            out.append(origin)
    return out
