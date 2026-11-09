#!/usr/bin/env python3
"""Google Workspace OAuth2 setup for Hermes Agent.

Fully non-interactive — designed to be driven by the agent via terminal commands.
The agent mediates between this script and the user (works on CLI, Telegram, Discord, etc.)

Commands:
  setup.py --check                          # Is auth valid? Exit 0 = yes, 1 = no
  setup.py --list                          # List Google accounts for this profile
  setup.py --client-secret /path/to.json    # Store OAuth client credentials
  setup.py --auth-url                       # Print the OAuth URL for user to visit
  setup.py --auth-code CODE                 # Exchange auth code for token
  setup.py --revoke                         # Revoke and delete stored token
  setup.py --install-deps                   # Install Python dependencies only

Multiple accounts:
  setup.py --check --account work
  setup.py --auth-code CODE --account work
  The accounts dir is $GOOGLE_ACCOUNTS_DIR, else $HERMES_HOME/google_accounts.
  An alias is usable by this profile only when <accounts_dir>/<alias>/ exists.
  When the accounts dir does not exist, the legacy single token at
  $HERMES_HOME/google_token.json is used (backward compatible).

Agent workflow:
  1. Run --check. If exit 0, auth is good — skip setup.
  2. Ask user for client_secret.json path. Run --client-secret PATH.
  3. Run --auth-url. Send the printed URL to the user.
  4. User opens URL, authorizes, gets redirected to a page with a code.
  5. User pastes the code. Agent runs --auth-code CODE.
  6. Run --check to verify. Done.
"""

from __future__ import annotations  # allow PEP 604 `X | None` on Python 3.9+

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import pm
except ImportError:
    # A copied skill must not install into an unrelated Python environment.
    pm = None

# Ensure sibling modules (_hermes_home, _accounts) are importable when run standalone.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _accounts import (
    DEFAULT_ALIAS,
    AccountError,
    accounts_dir,
    accounts_store_exists,
    atomic_write_json,
    available_aliases,
    default_alias,
    ensure_dir,
    file_lock,
    legacy_token_path,
    load_registry,
    lock_path_for,
    registry_path,
    require_account,
    resolve_token_path,
    upsert_account,
)
from _hermes_home import display_hermes_home, get_hermes_home

HERMES_HOME = get_hermes_home()
CLIENT_SECRET_PATH = HERMES_HOME / "google_client_secret.json"
PENDING_AUTH_PATH = HERMES_HOME / "google_oauth_pending.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]

# OAuth redirect for "out of band" manual code copy flow.
# Google deprecated OOB, so we use a localhost redirect and tell the user to
# copy the code from the browser's URL bar (or the page body).
REDIRECT_URI = "http://localhost:1"

# Resolved lazily by _resolve_account() once argv is known.
_STATE = {
    "alias": None,       # alias in use ("default" in legacy mode)
    "token_path": None,  # resolved token file
    "legacy": True,      # True when running against the legacy single token
}


def _resolve_account(explicit_alias: str | None):
    """Resolve the alias + token path for this run.

    Legacy mode (no accounts dir) keeps the historical behaviour: the token
    lives at ``$HERMES_HOME/google_token.json``.  In accounts mode the alias
    must be provisioned for this profile.
    """
    if explicit_alias:
        try:
            path = require_account(explicit_alias)
        except AccountError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)
        _STATE["alias"] = explicit_alias
        _STATE["token_path"] = path
        _STATE["legacy"] = False
        return path

    if not accounts_store_exists():
        _STATE["alias"] = "default"
        _STATE["token_path"] = legacy_token_path()
        _STATE["legacy"] = True
        return _STATE["token_path"]

    try:
        alias = default_alias()
        path = require_account(alias)
    except AccountError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    _STATE["alias"] = alias
    _STATE["token_path"] = path
    _STATE["legacy"] = False
    return path


def _token_path() -> Path:
    """Return the token file for the resolved account."""
    if _STATE["token_path"] is None:
        _resolve_account(None)
    return _STATE["token_path"]  # type: ignore[return-value]


def _current_alias() -> str:
    if _STATE["alias"] is None:
        _resolve_account(None)
    return _STATE["alias"]  # type: ignore[return-value]


def _normalize_authorized_user_payload(payload: dict) -> dict:
    normalized = dict(payload)
    if not normalized.get("type"):
        normalized["type"] = "authorized_user"
    return normalized


def _load_token_payload(path: Path | None = None) -> dict:
    path = path or _token_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _missing_scopes_from_payload(payload: dict) -> list[str]:
    raw = payload.get("scopes") or payload.get("scope")
    if not raw:
        return []
    granted = {s.strip() for s in (raw.split() if isinstance(raw, str) else raw) if s.strip()}
    return sorted(scope for scope in SCOPES if scope not in granted)


def _format_missing_scopes(missing_scopes: list[str]) -> str:
    bullets = "\n".join(f"  - {scope}" for scope in missing_scopes)
    return (
        "Token is valid but missing required Google Workspace scopes:\n"
        f"{bullets}\n"
        "Run the Google Workspace setup again from this same Hermes profile to refresh consent."
    )


def _write_token(path: Path, payload: dict) -> None:
    """Persist a token payload atomically, under the refresh lock."""
    ensure_dir(path.parent)
    with file_lock(lock_path_for(path)):
        atomic_write_json(path, payload, mode=0o600)


def _capture_email(creds) -> str:
    """Best-effort identity lookup for a freshly minted credential."""
    try:
        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        email = (profile or {}).get("emailAddress", "")
        if email:
            return email
    except Exception:
        pass
    try:
        from googleapiclient.discovery import build

        service = build("drive", "v3", credentials=creds)
        about = service.about().get(fields="user").execute()
        return ((about or {}).get("user") or {}).get("emailAddress", "")
    except Exception:
        return ""


def install_deps():
    """Sync Hermes' declared Google extra, ready for the next process."""
    if pm is None:
        print("ERROR: Run this script in the Hermes environment; use hermes setup first.")
        return False
    try:
        pm.sync_venv(["google"], explicit=True)
    except Exception as exc:
        print(f"ERROR: Failed to install Google dependencies: {exc}")
        return False
    print("Google dependencies synced. Restart Hermes, then rerun setup to continue OAuth.")
    return True


def _ensure_deps():
    """Let PM check imports and stop if activation needs a new process."""
    if pm is None:
        print("ERROR: Run this script in the Hermes environment; use hermes setup first.")
        sys.exit(1)
    try:
        pm.ensure_import("google")
    except Exception as exc:
        print(f"ERROR: Google dependencies unavailable: {exc}")
        sys.exit(1)


def check_auth_live():
    """Check auth with a real API call to detect disabled_client/account issues."""
    # quiet=True suppresses the "AUTHENTICATED" print from check_auth so the
    # final status line reflects the live-call outcome (OK or FAILED).
    if not check_auth(quiet=True):
        return False
    try:
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(_token_path()))
        service = build("calendar", "v3", credentials=creds)
        service.calendarList().list(maxResults=1).execute()
        print("LIVE_CHECK_OK: Real API call succeeded.")
        return True
    except Exception as e:
        err_str = str(e).lower()
        if "disabled_client" in err_str or "invalid_client" in err_str:
            print(f"LIVE_CHECK_FAILED: OAuth client or account disabled: {e}")
            print("  1. Check Google Cloud Console for disabled OAuth client")
            print("  2. Check myaccount.google.com for account status")
            print("  3. Do NOT retry with a disabled account")
        else:
            print(f"LIVE_CHECK_FAILED: {e}")
        return False


def _report_account(email: str = "") -> None:
    """Print the alias / email of the account that was checked."""
    alias = _current_alias()
    label = f"{alias} (legacy)" if _STATE["legacy"] else alias
    email = email or "unknown"
    print(f"ACCOUNT: email={email} alias={label} token={_token_path()}")


def _lookup_email(creds=None) -> str:
    """Resolve the e-mail of the checked account (registry first, then live)."""
    alias = _current_alias()
    if not _STATE["legacy"]:
        entry = load_registry().get(alias) or {}
        if entry.get("email"):
            return entry["email"]
    if creds is None:
        try:
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(str(_token_path()))
        except Exception:
            return ""
    return _capture_email(creds)


def check_auth(quiet: bool = False):
    """Check if stored credentials are valid. Prints status, exits 0 or 1."""
    token_path = _token_path()
    if not token_path.exists():
        print(f"NOT_AUTHENTICATED: No token at {token_path}")
        return False

    _ensure_deps()
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    try:
        # Don't pass scopes — user may have authorized only a subset.
        # Passing scopes forces google-auth to validate them on refresh,
        # which fails with invalid_scope if the token has fewer scopes
        # than requested.
        creds = Credentials.from_authorized_user_file(str(token_path))
    except Exception as e:
        print(f"TOKEN_CORRUPT: {e}")
        return False

    payload = _load_token_payload(token_path)
    if creds.valid:
        missing_scopes = _missing_scopes_from_payload(payload)
        if missing_scopes:
            print(f"AUTHENTICATED (partial): Token valid but missing {len(missing_scopes)} scopes:")
            for s in missing_scopes:
                print(f"  - {s}")
        if not quiet:
            print(f"AUTHENTICATED: Token valid at {token_path}")
            _report_account(_lookup_email(creds))
        return True

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _write_token(
                token_path,
                _normalize_authorized_user_payload(json.loads(creds.to_json())),
            )
            missing_scopes = _missing_scopes_from_payload(_load_token_payload(token_path))
            if missing_scopes:
                print(f"AUTHENTICATED (partial): Token refreshed but missing {len(missing_scopes)} scopes:")
                for s in missing_scopes:
                    print(f"  - {s}")
            if not quiet:
                print(f"AUTHENTICATED: Token refreshed at {token_path}")
                _report_account(_lookup_email(creds))
            return True
        except Exception as e:
            err_str = str(e).lower()
            if "disabled_client" in err_str or "invalid_client" in err_str:
                print(f"OAUTH_CLIENT_DISABLED: {e}")
                print("  The OAuth client or Google account has been disabled.")
                print("  Steps to resolve:")
                print("    1. Check your Google Cloud Console — verify the OAuth client is not disabled")
                print("    2. Check if your Google account itself has been disabled at myaccount.google.com")
                print("    3. If the account is disabled, you can appeal at accounts.google.com/signin/recovery")
                print("    4. Do NOT retry API calls with a disabled account — this may worsen the situation")
                print("    5. If the OAuth client is disabled, create a new one in Google Cloud Console")
            elif "token_revoked" in err_str or "invalid_grant" in err_str:
                print(f"TOKEN_REVOKED: {e}")
                print("  Re-run setup to re-authenticate.")
            else:
                print(f"REFRESH_FAILED: {e}")
            return False

    print("TOKEN_INVALID: Re-run setup.")
    return False


def list_accounts():
    """Show the Google accounts this profile can use."""
    if not accounts_store_exists():
        print(f"NO_ACCOUNTS_DIR: No Google accounts store at {accounts_dir()}")
        print("This profile uses the legacy single-token setup.")
        legacy = legacy_token_path()
        if not legacy.exists():
            print(f"NO_TOKEN: No token at {legacy}")
            return False
        payload = _load_token_payload(legacy)
        missing = _missing_scopes_from_payload(payload)
        email = ""
        try:
            from google.oauth2.credentials import Credentials

            email = _capture_email(Credentials.from_authorized_user_file(str(legacy)))
        except Exception:
            email = ""
        print("alias=default(legacy)")
        print(f"  email: {email or 'unknown'}")
        print(f"  token: {legacy}")
        print(f"  expiry: {payload.get('expiry', 'unknown')}")
        print(f"  scopes: {len(SCOPES) - len(missing)}/{len(SCOPES)} ok")
        for scope in missing:
            print(f"    missing: {scope}")
        return True

    aliases = available_aliases()
    if not aliases:
        print(f"NO_ACCOUNTS: No Google accounts provisioned for this profile in {accounts_dir()}")
        return False

    registry = load_registry()
    for alias in aliases:
        entry = registry.get(alias) or {}
        path = resolve_token_path(alias)
        payload = _load_token_payload(path)
        missing = _missing_scopes_from_payload(payload)
        email = entry.get("email", "")
        if not email and path.exists():
            try:
                from google.oauth2.credentials import Credentials

                email = _capture_email(Credentials.from_authorized_user_file(str(path)))
            except Exception:
                email = ""
        print(f"alias={alias}")
        print(f"  label: {entry.get('label', '')}")
        print(f"  email: {email or 'unknown'}")
        print(f"  token: {path}")
        print(f"  expiry: {payload.get('expiry', 'unknown')}")
        print(f"  scopes: {len(SCOPES) - len(missing)}/{len(SCOPES)} ok")
        for scope in missing:
            print(f"    missing: {scope}")
    return True


def store_client_secret(path: str):
    """Copy and validate client_secret.json to Hermes home."""
    src = Path(path).expanduser().resolve()
    if not src.exists():
        print(f"ERROR: File not found: {src}")
        sys.exit(1)

    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("ERROR: File is not valid JSON.")
        sys.exit(1)

    if "installed" not in data and "web" not in data:
        print("ERROR: Not a Google OAuth client secret file (missing 'installed' key).")
        print("Download the correct file from: https://console.cloud.google.com/apis/credentials")
        sys.exit(1)

    CLIENT_SECRET_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"OK: Client secret saved to {CLIENT_SECRET_PATH}")


def _save_pending_auth(*, state: str, code_verifier: str):
    """Persist the OAuth session bits needed for a later token exchange."""
    PENDING_AUTH_PATH.write_text(
        json.dumps(
            {
                "state": state,
                "code_verifier": code_verifier,
                "redirect_uri": REDIRECT_URI,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _load_pending_auth() -> dict:
    """Load the pending OAuth session created by get_auth_url()."""
    if not PENDING_AUTH_PATH.exists():
        print("ERROR: No pending OAuth session found. Run --auth-url first.")
        sys.exit(1)

    try:
        data = json.loads(PENDING_AUTH_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: Could not read pending OAuth session: {e}")
        print("Run --auth-url again to start a fresh OAuth session.")
        sys.exit(1)

    if not data.get("state") or not data.get("code_verifier"):
        print("ERROR: Pending OAuth session is missing PKCE data.")
        print("Run --auth-url again to start a fresh OAuth session.")
        sys.exit(1)

    return data


def _extract_code_and_state(code_or_url: str) -> tuple[str, str | None]:
    """Accept either a raw auth code or the full redirect URL pasted by the user."""
    if not code_or_url.startswith("http"):
        return code_or_url, None

    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(code_or_url)
    params = parse_qs(parsed.query)
    if "code" not in params:
        print("ERROR: No 'code' parameter found in URL.")
        sys.exit(1)

    state = params.get("state", [None])[0]
    return params["code"][0], state


def get_auth_url():
    """Print the OAuth authorization URL. User visits this in a browser."""
    if not CLIENT_SECRET_PATH.exists():
        print("ERROR: No client secret stored. Run --client-secret first.")
        sys.exit(1)

    _ensure_deps()
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=True,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    _save_pending_auth(state=state, code_verifier=flow.code_verifier)
    # Print just the URL so the agent can extract it cleanly
    print(auth_url)


def exchange_auth_code(code: str):
    """Exchange the authorization code for a token and save it."""
    if not CLIENT_SECRET_PATH.exists():
        print("ERROR: No client secret stored. Run --client-secret first.")
        sys.exit(1)

    pending_auth = _load_pending_auth()
    raw_callback = code
    code, returned_state = _extract_code_and_state(code)
    if returned_state and returned_state != pending_auth["state"]:
        print("ERROR: OAuth state mismatch. Run --auth-url again to start a fresh session.")
        sys.exit(1)

    _ensure_deps()
    from google_auth_oauthlib.flow import Flow
    from urllib.parse import parse_qs, urlparse

    # Extract granted scopes from the callback URL if the user pasted the full redirect URL.
    granted_scopes = list(SCOPES)
    if isinstance(raw_callback, str) and raw_callback.startswith("http"):
        params = parse_qs(urlparse(raw_callback).query)
        scope_val = (params.get("scope") or [""])[0].strip()
        if scope_val:
            granted_scopes = scope_val.split()

    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=granted_scopes,
        redirect_uri=pending_auth.get("redirect_uri", REDIRECT_URI),
        state=pending_auth["state"],
        code_verifier=pending_auth["code_verifier"],
    )

    try:
        # Accept partial scopes — user may deselect some permissions in the consent screen
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        flow.fetch_token(code=code)
    except Exception as e:
        print(f"ERROR: Token exchange failed: {e}")
        print("The code may have expired. Run --auth-url to get a fresh URL.")
        sys.exit(1)

    creds = flow.credentials
    token_payload = _normalize_authorized_user_payload(json.loads(creds.to_json()))

    # Store only the scopes actually granted by the user, not what was requested.
    # creds.to_json() writes the requested scopes, which causes refresh to fail
    # with invalid_scope if the user only authorized a subset.
    actually_granted = list(creds.granted_scopes or []) if hasattr(creds, "granted_scopes") and creds.granted_scopes else []
    if actually_granted:
        token_payload["scopes"] = actually_granted
    elif granted_scopes != SCOPES:
        # granted_scopes was extracted from the callback URL
        token_payload["scopes"] = granted_scopes

    missing_scopes = _missing_scopes_from_payload(token_payload)
    if missing_scopes:
        print(f"WARNING: Token missing some Google Workspace scopes: {', '.join(missing_scopes)}")
        print("Some services may not be available.")

    token_path = _token_path()
    ensure_dir(token_path.parent)
    _write_token(token_path, token_payload)
    PENDING_AUTH_PATH.unlink(missing_ok=True)
    print(f"OK: Authenticated. Token saved to {token_path}")
    print(f"Profile-scoped token location: {display_hermes_home()}/google_token.json")

    # Capture the account identity and record it in the alias registry.
    alias = _current_alias()
    email = _capture_email(creds)
    try:
        upsert_account(
            alias,
            email=email,
            label=email or alias,
            scopes=list(token_payload.get("scopes") or []),
        )
        print(f"ACCOUNT: email={email or 'unknown'} alias={alias}")
    except Exception as exc:
        print(f"WARNING: Could not update the accounts registry at {registry_path()}: {exc}")


def revoke():
    """Revoke stored token and delete it."""
    token_path = _token_path()
    if not token_path.exists():
        print("No token to revoke.")
        return

    _ensure_deps()
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        import urllib.request
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://oauth2.googleapis.com/revoke?token={creds.token}",
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            timeout=15,
        )
        print("Token revoked with Google.")
    except Exception as e:
        print(f"Remote revocation failed (token may already be invalid): {e}")

    token_path.unlink(missing_ok=True)
    PENDING_AUTH_PATH.unlink(missing_ok=True)
    print(f"Deleted {token_path}")


def main():
    parser = argparse.ArgumentParser(description="Google Workspace OAuth setup for Hermes")
    parser.add_argument(
        "--account",
        metavar="ALIAS",
        default=None,
        help=f"Google account alias to use (default: {DEFAULT_ALIAS})",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Check if auth is valid (exit 0=yes, 1=no)")
    group.add_argument("--check-live", action="store_true", help="Check auth with a real API call (detects disabled_client)")
    group.add_argument("--list", action="store_true", help="List the Google accounts available to this profile")
    group.add_argument("--client-secret", metavar="PATH", help="Store OAuth client_secret.json")
    group.add_argument("--auth-url", action="store_true", help="Print OAuth URL for user to visit")
    group.add_argument("--auth-code", metavar="CODE", help="Exchange auth code for token")
    group.add_argument("--revoke", action="store_true", help="Revoke and delete stored token")
    group.add_argument("--install-deps", action="store_true", help="Install Python dependencies")
    args = parser.parse_args()

    if args.list:
        # Listing must work even when the requested alias is unusable.
        if args.account:
            _STATE["alias"] = args.account
        else:
            _resolve_account(None)
        sys.exit(0 if list_accounts() else 1)
    elif args.account:
        _resolve_account(args.account)
    else:
        _resolve_account(None)

    if args.check:
        sys.exit(0 if check_auth() else 1)
    if getattr(args, "check_live", False):
        sys.exit(0 if check_auth_live() else 1)
    elif args.client_secret:
        store_client_secret(args.client_secret)
    elif args.auth_url:
        get_auth_url()
    elif args.auth_code:
        exchange_auth_code(args.auth_code)
    elif args.revoke:
        revoke()
    elif args.install_deps:
        sys.exit(0 if install_deps() else 1)


if __name__ == "__main__":
    main()
