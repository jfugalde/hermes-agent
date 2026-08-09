#!/usr/bin/env python3
"""Ollama Cloud credential pool reset.

Clears stale 'exhausted' status from ollama-cloud pool entries in auth.json.
Run weekly after the Ollama quota resets (Sunday 6pm CST / Monday 00:05 UTC)
or on-demand when keys are genuinely exhausted but quota has reopened.

Usage:
  python3 ~/.hermes/scripts/ollama-pool-reset.py          # Reset all pools
  python3 ~/.hermes/scripts/ollama-pool-reset.py ollama-cloud  # Reset specific provider
  python3 ~/.hermes/scripts/ollama-pool-reset.py --status     # Show status only
"""

import json
import os
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
AUTH_JSON = HERMES_HOME / "auth.json"

STATUS_FIELDS = (
    "last_status",
    "last_status_at",
    "last_error_code",
    "last_error_reason",
    "last_error_message",
    "last_error_reset_at",
)


def load_auth():
    if not AUTH_JSON.exists():
        print(f"Error: {AUTH_JSON} not found")
        sys.exit(1)
    with open(AUTH_JSON) as f:
        return json.load(f)


def save_auth(data):
    with open(AUTH_JSON, "w") as f:
        json.dump(data, f, indent=2)


def show_status(data):
    pool = data.get("credential_pool", {})
    if not pool:
        print("No credential pools found")
        return

    print(f"{'Provider':<35} {'Label':<30} {'Status':<12} {'Error':<8} {'Age'}")
    print("-" * 100)
    for provider, entries in sorted(pool.items()):
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            label = e.get("label", "?")[:29]
            status = (e.get("last_status") or "ok")[:11]
            error = str(e.get("last_error_code", ""))[:7]
            ts = e.get("last_status_at")
            if ts:
                age = f"{time.time() - ts:.0f}s ago"
            else:
                age = "-"
            print(f"{provider:<35} {label:<30} {status:<12} {error:<8} {age}")


def reset_provider(data, provider_name):
    pool = data.get("credential_pool", {})
    if provider_name and provider_name != "all":
        providers = {provider_name: pool.get(provider_name, [])}
    else:
        providers = pool

    total_cleared = 0
    for prov, entries in providers.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            if e.get("last_status") == "exhausted":
                for field in STATUS_FIELDS:
                    e[field] = None
                e["request_count"] = 0
                e["last_status"] = "ok"
                total_cleared += 1
                print(f"  Cleared: {prov}/{e.get('label', '?')}")

    return total_cleared


def main():
    args = sys.argv[1:]
    show_only = "--status" in args

    data = load_auth()

    if show_only:
        show_status(data)
        return

    provider = None
    for arg in args:
        if arg != "--status":
            provider = arg
            break

    print("Before reset:")
    show_status(data)
    print()

    cleared = reset_provider(data, provider)

    if cleared:
        save_auth(data)
        print(f"\nCleared {cleared} exhausted entries")
        # Re-load and show
        data = load_auth()
        print("\nAfter reset:")
        show_status(data)
    else:
        print("\nNo exhausted entries to clear")


if __name__ == "__main__":
    main()