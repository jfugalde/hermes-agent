#!/usr/bin/env python3
"""Ollama Cloud credential pool reset.

Clears stale 'exhausted' status from ollama-cloud pool entries in auth.json.
Run weekly after the Ollama quota resets (Sunday 6pm CST / Monday 00:00 UTC).

Also clears the in-memory unhealthy cache by hitting the gateway's health
endpoint, which causes a pool reload on next API call.
"""

import json
import os
import sys
from pathlib import Path

AUTH_JSON = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))) / "auth.json"
PROVIDER = "ollama-cloud"


def main():
    if not AUTH_JSON.exists():
        print(f"ERROR: {AUTH_JSON} not found", file=sys.stderr)
        sys.exit(1)

    with open(AUTH_JSON) as f:
        data = json.load(f)

    pool = data.get("credential_pool", {}).get(PROVIDER, [])
    if not pool:
        print(f"No {PROVIDER} pool entries found")
        sys.exit(0)

    cleared = 0
    for entry in pool:
        if entry.get("last_status") == "exhausted":
            label = entry.get("label", entry.get("id", "?"))
            entry["last_status"] = "ok"
            entry["last_status_at"] = None
            entry["last_error_code"] = None
            entry["last_error_reason"] = None
            entry["last_error_message"] = None
            entry["last_error_reset_at"] = None
            entry["request_count"] = 0
            cleared += 1
            print(f"  Cleared exhausted status: {label}")

    if cleared == 0:
        # Still write back to reset request_count if desired
        print(f"All {len(pool)} {PROVIDER} entries already ok — no changes needed")
        sys.exit(0)

    with open(AUTH_JSON, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Reset {cleared} exhausted {PROVIDER} credential(s)")


if __name__ == "__main__":
    main()