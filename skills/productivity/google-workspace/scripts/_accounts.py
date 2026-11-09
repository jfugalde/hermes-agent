"""Multi-account alias layer for the Google Workspace skill scripts.

Layout (accounts dir = ``$GOOGLE_ACCOUNTS_DIR`` else ``$HERMES_HOME/google_accounts``)::

    <accounts_dir>/accounts.json          # registry: {alias: {email, label, scopes, added}}
    <accounts_dir>/<alias>/token.json     # OAuth token for that alias

The registry is *metadata only* (labels, e-mail, scopes).  Access is enforced
purely by filesystem presence: a profile may only use alias ``A`` if
``<accounts_dir>/A/`` exists for that profile.  Profiles share one account by
symlinking the alias directory into the shared store, e.g.::

    ~/.hermes/profiles/apollo/google_accounts/work -> ~/.hermes/google_accounts/work

Backward compatibility is mandatory: when the accounts dir does not exist at
all, every caller keeps using the legacy ``$HERMES_HOME/google_token.json`` and
behaves exactly as before.

This module is stdlib-only so it can be imported by system ``python3`` as well
as by the Hermes virtualenv.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

try:  # pragma: no cover - sibling module, stdlib-only
    from _hermes_home import get_hermes_home
except ImportError:  # pragma: no cover - defensive fallback

    def get_hermes_home() -> Path:
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hermes"


#: Alias used when nothing else is specified.
DEFAULT_ALIAS = "work"

#: File name of the per-alias OAuth token.
TOKEN_FILENAME = "token.json"

#: File name of the alias registry.
REGISTRY_FILENAME = "accounts.json"


class AccountError(Exception):
    """Raised when a requested alias is not usable by the current profile."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def accounts_dir() -> Path:
    """Return the accounts directory for the current profile."""
    override = os.environ.get("GOOGLE_ACCOUNTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return get_hermes_home() / "google_accounts"


def accounts_store_exists() -> bool:
    """True when the accounts dir exists (i.e. multi-account mode is active)."""
    return accounts_dir().is_dir()


def legacy_token_path() -> Path:
    """Return the legacy single-token path (``$HERMES_HOME/google_token.json``)."""
    return get_hermes_home() / "google_token.json"


def alias_dir(alias: str) -> Path:
    return accounts_dir() / alias


def token_path(alias: str) -> Path:
    return alias_dir(alias) / TOKEN_FILENAME


def available_aliases() -> list[str]:
    """Aliases this profile can reach (directories present in the accounts dir)."""
    root = accounts_dir()
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if child.name == REGISTRY_FILENAME or child.name.startswith("."):
            continue
        if child.is_dir():
            out.append(child.name)
    return out


def registry_path() -> Path:
    """Return the path of the alias registry.

    Normally ``<accounts_dir>/accounts.json``.  When the accounts dir only
    holds symlinks into a shared store, the shared store's registry is used so
    that every profile sees the same labels.
    """
    local = accounts_dir() / REGISTRY_FILENAME
    if local.exists():
        return local
    for alias in available_aliases():
        try:
            real_parent = alias_dir(alias).resolve().parent
        except OSError:
            continue
        shared = real_parent / REGISTRY_FILENAME
        if shared.exists():
            return shared
    return local


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def load_registry() -> dict:
    """Load the alias registry (empty dict when missing/unreadable)."""
    path = registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_registry(registry: dict) -> Path:
    """Persist the alias registry atomically.  Returns the path written."""
    path = registry_path()
    ensure_dir(path.parent)
    atomic_write_json(path, registry)
    return path


def upsert_account(alias: str, *, email: str, label: str = "", scopes=None) -> Path:
    """Insert or update one alias entry in the registry."""
    registry = load_registry()
    entry = dict(registry.get(alias) or {})
    entry["email"] = email
    entry["label"] = label or entry.get("label") or alias
    entry["scopes"] = sorted(scopes or entry.get("scopes") or [])
    entry.setdefault("added", datetime.now(UTC).isoformat())
    registry[alias] = entry
    return save_registry(registry)


# ---------------------------------------------------------------------------
# Alias resolution / enforcement
# ---------------------------------------------------------------------------


def default_alias() -> str:
    """Resolve the default alias for this profile.

    ``work`` when it is available (or when nothing is available at all);
    the single available alias when there is exactly one; otherwise the
    caller must pass ``--account`` explicitly.
    """
    aliases = available_aliases()
    if not aliases:
        return DEFAULT_ALIAS
    if DEFAULT_ALIAS in aliases:
        return DEFAULT_ALIAS
    if len(aliases) == 1:
        return aliases[0]
    raise AccountError(
        "Multiple Google accounts are available for this profile; "
        "pass --account <alias> to choose one of: " + ", ".join(aliases)
    )


def require_account(alias: str) -> Path:
    """Return the token path for ``alias`` or raise :class:`AccountError`.

    The alias must exist as a directory inside *this profile's* accounts dir.
    """
    if not alias or not alias.strip():
        raise AccountError("An account alias is required.")
    alias = alias.strip()
    if not accounts_store_exists():
        raise AccountError(
            f"Google account alias '{alias}' is not provisioned for this profile. "
            f"No accounts store exists at {accounts_dir()}. "
            "This profile has no Google accounts available."
        )
    directory = alias_dir(alias)
    if not directory.is_dir():
        have = available_aliases()
        if have:
            message = (
                f"Google account alias '{alias}' is not provisioned for this profile. "
                f"Available aliases: {', '.join(have)}"
            )
        else:
            message = (
                f"Google account alias '{alias}' is not provisioned for this profile. "
                "This profile has no Google accounts available."
            )
        raise AccountError(message)
    return directory / TOKEN_FILENAME


def resolve_token_path(alias: str | None = None) -> Path:
    """Resolve the OAuth token file to use.

    * accounts dir missing  -> legacy ``$HERMES_HOME/google_token.json``
    * alias given           -> that alias' token (must be provisioned)
    * no alias              -> the default alias' token
    """
    if alias:
        return require_account(alias)
    if not accounts_store_exists():
        return legacy_token_path()
    return require_account(default_alias())


def resolve_alias(argv) -> str | None:
    """Pre-scan ``argv`` for ``--account ALIAS`` / ``--account=ALIAS``.

    Returns the alias or ``None``.  The flag is *not* removed from ``argv``;
    callers that need it removed do that themselves.
    """
    argv = list(argv or [])
    found = None
    for index, token in enumerate(argv):
        if token == "--account":
            if index + 1 < len(argv):
                value = argv[index + 1]
                if value and not value.startswith("-"):
                    found = value
        elif token.startswith("--account="):
            value = token.split("=", 1)[1]
            if value:
                found = value
    return found


# ---------------------------------------------------------------------------
# Safe IO helpers
# ---------------------------------------------------------------------------


def ensure_dir(path: Path, mode: int = 0o700) -> Path:
    """Create ``path`` (and parents) with restrictive permissions."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def atomic_write_json(path: Path, data, mode: int = 0o600) -> Path:
    """Write ``data`` as JSON to ``path`` atomically (temp file + os.replace)."""
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    return path


@contextlib.contextmanager
def file_lock(path: Path):
    """Exclusive advisory lock on ``path`` (created if missing)."""
    path = Path(path)
    ensure_dir(path.parent)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def lock_path_for(token_file: Path) -> Path:
    """Lock file used to serialise refreshes of ``token_file``."""
    return Path(str(token_file) + ".lock")
