#!/usr/bin/env python3
"""Ollama Cloud credential pool reset.

Clears stale 'exhausted' status from ollama-cloud pool entries in auth.json.
Runs weekly after the Ollama quota resets (Sunday 6pm CST / Monday 00:00 UTC).

Fixes ALL auth.json files: global + every profile, since each profile
maintains its own credential pool state independently.
"""

import json
import os
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
PROVIDER = "ollama-cloud"


def reset_auth_json(path: Path) -> int:
    """Reset exhausted ollama-cloud entries in a single auth.json. Returns count cleared."""
    if not path.exists():
        return 0

    with open(path) as f:
        data = json.load(f)

    pool = data.get("credential_pool", {}).get(PROVIDER, [])
    if not pool:
        return 0

    cleared = 0
    for entry in pool:
        if entry.get("last_status") in ("exhausted", None) and entry.get("last_error_code"):
            entry["last_status"] = "ok"
            entry["last_status_at"] = None
            entry["last_error_code"] = None
            entry["last_error_reason"] = None
            entry["last_error_message"] = None
            entry["last_error_reset_at"] = None
            entry["request_count"] = 0
            cleared += 1

    if cleared > 0:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    return cleared


def main():
    total = 0

    # 1. Global auth.json
    global_auth = HERMES_HOME / "auth.json"
    cleared = reset_auth_json(global_auth)
    if cleared:
        print(f"  Global: cleared {cleared} key(s)")
    total += cleared

    # 2. Every profile's auth.json
    profiles_dir = HERMES_HOME / "profiles"
    if profiles_dir.exists():
        for profile_dir in sorted(profiles_dir.iterdir()):
            if not profile_dir.is_dir():
                continue
            auth_path = profile_dir / "auth.json"
            cleared = reset_auth_json(auth_path)
            if cleared:
                print(f"  {profile_dir.name}: cleared {cleared} key(s)")
            total += cleared

    if total == 0:
        print("All ollama-cloud entries already ok — no changes needed")
    else:
        print(f"Reset {total} exhausted ollama-cloud credential(s) across all profiles")


if __name__ == "__main__":
    main()