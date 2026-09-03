#!/usr/bin/env python3
"""Ollama Cloud quota-pacing watchdog (super-lightweight, no_agent cron).

Ollama Cloud enforces a WEEKLY request quota, exposed as a 0..1 fraction at
GET https://ollama.com/api/usage -> limits.weekly.usage (e.g. 0.97 = 97% used).
The API gives NO reset timestamp, but the quota resets weekly (Mon 00:00 UTC,
matching the observed activity-period boundary). We self-calibrate by detecting
the usage drop at reset and snapping the cycle start to that day's UTC midnight.

PACING LOGIC
  ideal linear pace by day N of 7 = N/7 (the "1/7th split" per day).
  rate           = usage / elapsed_days            (quota-fraction burned/day)
  days_to_exhaust= (1 - usage) / rate              (when we hit 100%)
  remaining_days = 7 - elapsed_days                (until reset)
  -> If we'll hit 100% BEFORE reset, we're overspending: ALERT to slow down /
     switch to cheaper models so the subscription lasts the full week.

OUTPUT (no_agent watchdog contract)
  - Prints a short Telegram alert ONLY when over-pace or exhausted.
  - Prints NOTHING (empty stdout => silent, no notification) when on/under pace.
  - Non-zero exit only on hard failure (so a broken watchdog surfaces).

Stdlib-only, including the response_cache helper (cron no_agent scripts stay
dependency-free). The Ollama Cloud usage endpoint is cached for 15 minutes so
manual re-runs and tightly spaced alerts don't re-pay the same HTTP call.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from response_cache import ttl_cached

USAGE_URL = "https://ollama.com/api/usage"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_PATH = HERMES_HOME / "cache" / "ollama-quota-watchdog.json"
TIER_MAP_PATH = HERMES_HOME / "cache" / "ollama-tier-map.json"

# Alert when projected to exhaust with at least this much of the week still
# remaining (headroom so we warn early, not at the last hour).
EXHAUST_HEADROOM_DAYS = 0.25
# Soft warn band: usage running this far ahead of the ideal N/7 pace.
PACE_MARGIN = 0.10
# Reset detected when usage drops by at least this much between runs.
RESET_DROP = 0.15


def load_key() -> str:
    key = (os.environ.get("OLLAMA_API_KEY") or "").strip()
    if key:
        return key
    env = HERMES_HOME / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("OLLAMA_API_KEY") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


@ttl_cached(ttl_seconds=900)
def fetch_usage(key: str) -> dict:
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def last_monday_utc(now: datetime) -> datetime:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=now.weekday())


def read_current_default() -> str | None:
    """Read the active default model via the hermes config CLI (authoritative).

    The top-level `model:` key in config.yaml is a section of provider sub-blocks,
    not a scalar, so we ask hermes itself rather than parsing YAML by hand.
    """
    import subprocess

    candidates = [
        HERMES_HOME / "hermes-agent" / "venv" / "bin" / "hermes",
        Path.home() / ".local" / "bin" / "hermes",
    ]
    hermes_bin = next((str(p) for p in candidates if p.exists()), "hermes")
    try:
        out = subprocess.run(
            [hermes_bin, "config", "get", "model"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # Output looks like "default: deepseek-v4-flash" (may have 1Password preamble).
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("default:"):
            return s.split(":", 1)[1].strip().strip('"').strip("'") or None
        if s.startswith("model:"):
            return s.split(":", 1)[1].strip().strip('"').strip("'") or None
    # Fallback: last non-empty token line.
    vals = [ln.strip() for ln in out.splitlines() if ln.strip()
            and ":" not in ln and "1Password" not in ln]
    return vals[-1] if vals else None


def recommend_substitute(current: str | None) -> str | None:
    """Pick the cheapest tool-capable substitute below the current model's tier.

    Grounded in the tier-map the daily report emits (usage_level 1..4 = GPU-cost
    proxy). We keep tools capability (needed for agentic/coding work) and, if the
    current model has vision/thinking, prefer to preserve those; otherwise we go
    for the lowest usage_level + smallest params. Returns None if we can't beat
    the current model.
    """
    try:
        tmap = json.loads(TIER_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    models = tmap.get("models") or {}
    if not models:
        return None

    cur = models.get(current or "", {})
    cur_level = cur.get("usage_level", 99)
    need_vision = bool(cur.get("vision"))

    def score(item):
        _mid, meta = item
        return (
            meta.get("usage_level", 99),
            meta.get("params") or 10**18,
            _mid,
        )

    candidates = []
    for mid, meta in models.items():
        if mid == current:
            continue
        if not meta.get("tools"):
            continue  # coding default must support tool-calling
        if need_vision and not meta.get("vision"):
            continue  # don't drop a capability we currently rely on
        # Only suggest something strictly cheaper than the current tier.
        if meta.get("usage_level", 99) >= cur_level:
            continue
        candidates.append((mid, meta))

    if not candidates:
        # Nothing strictly cheaper that preserves needed capabilities: the
        # current model is already at/near the cheapest viable tier. Stay quiet
        # rather than suggest a lateral move or drop a capability.
        return None

    return min(candidates, key=score)[0]


def main() -> int:
    key = load_key()
    if not key:
        print("⚠️ Ollama quota watchdog: no OLLAMA_API_KEY", file=sys.stderr)
        return 1

    try:
        data = fetch_usage(key)
    except Exception as e:  # network / auth / parse
        print(f"⚠️ Ollama quota watchdog: fetch failed: {e}", file=sys.stderr)
        return 1

    weekly = (data.get("limits") or {}).get("weekly") or {}
    usage = weekly.get("usage")
    if usage is None:
        print("⚠️ Ollama quota watchdog: no limits.weekly.usage in response",
              file=sys.stderr)
        return 1
    usage = float(usage)

    now = datetime.now(timezone.utc)
    state = load_state()
    prev_usage = state.get("last_usage")
    cycle_start_ts = state.get("cycle_start_ts")

    # Self-calibrate the weekly reset boundary.
    if prev_usage is not None and usage < float(prev_usage) - RESET_DROP:
        # Reset happened since last run: snap cycle start to today's UTC midnight.
        cycle_start_ts = now.replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
    elif cycle_start_ts is None:
        # Cold start: best-guess anchor = most recent Monday 00:00 UTC.
        cycle_start_ts = last_monday_utc(now).timestamp()

    save_state({"last_usage": usage, "cycle_start_ts": cycle_start_ts,
                "updated": now.isoformat()})

    elapsed_days = max(0.05, min(7.0, (now.timestamp() - cycle_start_ts) / 86400))
    remaining_days = max(0.0, 7.0 - elapsed_days)
    ideal_pace = elapsed_days / 7.0
    rate = usage / elapsed_days  # quota-fraction per day
    days_to_exhaust = (1.0 - usage) / rate if rate > 0 else float("inf")

    pct = usage * 100.0
    ideal_pct = ideal_pace * 100.0

    # ── Decide alert level (silent watchdog: only speak when overspending) ──
    lines: list[str] = []
    if usage >= 1.0:
        lines = [
            f"🔴 Ollama weekly quota EXHAUSTED — {pct:.0f}% used, "
            f"~{remaining_days:.1f}d until reset.",
            "Requests will be throttled/blocked. Switch to local models or wait.",
        ]
    elif days_to_exhaust < remaining_days - EXHAUST_HEADROOM_DAYS:
        lines = [
            f"🔴 Ollama quota overpacing — {pct:.0f}% used "
            f"(day {elapsed_days:.1f}/7, ideal ≈{ideal_pct:.0f}%).",
            f"At current burn you hit 100% in ~{days_to_exhaust:.1f}d but reset "
            f"is ~{remaining_days:.1f}d out.",
            "Slow down / route to cheaper models to make it last the week.",
        ]
    elif usage > ideal_pace + PACE_MARGIN:
        lines = [
            f"🟡 Ollama quota ahead of pace — {pct:.0f}% used vs "
            f"~{ideal_pct:.0f}% ideal (day {elapsed_days:.1f}/7).",
            f"~{remaining_days:.1f}d until reset. Consider cheaper routing.",
        ]
    # else: on/under pace -> stay silent (empty stdout = no Telegram message).

    if lines:
        # When overspending, suggest a concrete cheaper model to switch to.
        if usage >= 1.0 or days_to_exhaust < remaining_days - EXHAUST_HEADROOM_DAYS \
                or usage > ideal_pace + PACE_MARGIN:
            current = read_current_default()
            sub = recommend_substitute(current)
            if sub:
                cur_txt = f"`{current}`" if current else "current default"
                lines.append(
                    f"💡 Switch default {cur_txt} → `{sub}` (cheaper tier). "
                    f"Set: hermes config set model {sub}"
                )
        # Append the top request-count models this week for actionable context.
        wk_models = weekly.get("models") or []
        top = sorted(wk_models, key=lambda m: -(m.get("request_count") or 0))[:3]
        if top:
            frag = ", ".join(
                f"{m.get('name')} ({m.get('request_count')})" for m in top)
            lines.append(f"Top consumers: {frag}")
        print("\n".join(lines))

    return 0


if __name__ == "__main__":
    sys.exit(main())
