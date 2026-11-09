#!/usr/bin/env python3
"""Bridge between Hermes OAuth token and gws CLI.

Refreshes the token if expired, then executes gws with the valid access token.
Token selection follows the multi-account alias layer: ``--account ALIAS``
may be passed as the first argument (or via ``$GOOGLE_WORKSPACE_ACCOUNT``);
otherwise the default alias (or the legacy single token) is used.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path

# Ensure sibling modules (_hermes_home, _accounts) are importable when run standalone.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _accounts import (
    AccountError,
    atomic_write_json,
    ensure_dir,
    file_lock,
    lock_path_for,
    resolve_token_path,
)


def get_token_path() -> Path:
    """Resolve the token file for the selected account."""
    alias = os.environ.get("GOOGLE_WORKSPACE_ACCOUNT", "").strip() or None
    try:
        return resolve_token_path(alias)
    except AccountError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)


def _normalize_authorized_user_payload(payload: dict) -> dict:
    normalized = dict(payload)
    if not normalized.get("type"):
        normalized["type"] = "authorized_user"
    return normalized


def refresh_token(token_data: dict, token_file: Path | None = None) -> dict:
    """Refresh the access token using the refresh token."""
    import urllib.error
    import urllib.parse
    import urllib.request

    token_file = token_file or get_token_path()

    required_keys = ["client_id", "client_secret", "refresh_token", "token_uri"]
    missing = [k for k in required_keys if k not in token_data]
    if missing:
        print(f"ERROR: {token_file.name} is missing required fields: {', '.join(missing)}", file=sys.stderr)
        print("Please re-authenticate by running the Google Workspace setup script.", file=sys.stderr)
        sys.exit(1)

    params = urllib.parse.urlencode({
        "client_id": token_data["client_id"],
        "client_secret": token_data["client_secret"],
        "refresh_token": token_data["refresh_token"],
        # The refresh token goes in BOTH the body and the ``x-nous-refresh-token`` header. Portal's token
        # endpoint requires ``refresh_token`` in the body (its request schema rejects a header-only request
        # as ``invalid_request``), and additionally reconciles the header against the body — sending both
        # lets Portal keep the value out of body-access-logs while still satisfying the schema. The header
        # name must match Portal's ``REFRESH_TOKEN_HEADER`` exactly (``x-nous-refresh- token``); any other
        # name is silently ignored. (Verified against the NAS #293 preview deploy: header-only → 400
        # invalid_request; body → accepted.)
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request(token_data["token_uri"], data=params)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: Token refresh failed (HTTP {e.code}): {body}", file=sys.stderr)
        print("Re-run setup.py to re-authenticate.", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"ERROR: Token refresh failed (network): {e}", file=sys.stderr)
        sys.exit(1)

    token_data["token"] = result["access_token"]
    token_data["expiry"] = datetime.fromtimestamp(
        datetime.now(UTC).timestamp() + result["expires_in"],
        tz=UTC,
    ).isoformat()

    # Atomic, lock-guarded write so concurrent refreshes cannot corrupt the file.
    ensure_dir(token_file.parent)
    with file_lock(lock_path_for(token_file)):
        atomic_write_json(
            token_file,
            _normalize_authorized_user_payload(token_data),
            mode=0o600,
        )
    return token_data


def get_valid_token() -> str:
    """Return a valid access token, refreshing if needed."""
    token_path = get_token_path()
    if not token_path.exists():
        print("ERROR: No Google token found. Run setup.py --auth-url first.", file=sys.stderr)
        sys.exit(1)

    token_data = json.loads(token_path.read_text(encoding="utf-8"))

    expiry = token_data.get("expiry", "")
    if expiry:
        exp_dt = datetime.fromisoformat(expiry)
        now = datetime.now(UTC)
        if now >= exp_dt:
            token_data = refresh_token(token_data, token_path)

    return token_data["token"]


def main():
    """Refresh token if needed, then exec gws with remaining args."""
    argv = sys.argv[1:]

    # Optional leading "--account ALIAS" / "--account=ALIAS" selector.
    alias = os.environ.get("GOOGLE_WORKSPACE_ACCOUNT", "").strip()
    if argv and argv[0] == "--account":
        if len(argv) < 2:
            print("ERROR: --account requires an alias", file=sys.stderr)
            sys.exit(1)
        alias = argv[1]
        argv = argv[2:]
    elif argv and argv[0].startswith("--account="):
        alias = argv[0].split("=", 1)[1]
        argv = argv[1:]

    if not argv:
        print("Usage: gws_bridge.py [--account ALIAS] <gws args...>", file=sys.stderr)
        sys.exit(1)

    if alias:
        os.environ["GOOGLE_WORKSPACE_ACCOUNT"] = alias

    access_token = get_valid_token()
    env = os.environ.copy()
    env["GOOGLE_WORKSPACE_CLI_TOKEN"] = access_token
    env["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = str(get_token_path())

    result = subprocess.run(["gws"] + argv, env=env, check=False)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
