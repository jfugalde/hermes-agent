#!/usr/bin/env python3
"""Canonical Ollama Cloud / OpenRouter pricing resolver.

Single source of truth for per-token USD prices and Ollama Cloud GPU-tier cost
proxies. Consumers:

  - daily_models_report.py     (full daily catalog + spend analysis)
  - ollama-usage-daily.sh      (24h provider usage report)
  - ollama_quota_watchdog.py   (quota pacing)

Resolution order (most reliable first):
  1. Live Ollama Cloud /api/usage activity.models[].cost  -> REAL dollar cost
     per model over the last 4 weeks (best signal, but coarse: only models that
     actually ran in that window are listed).
  2. Live OpenRouter /v1/models pricing (per-token $/M) — fetched fresh and
     cached to disk. Best per-token resolution for ranking/comparison.
  3. Static fallback table below (manually maintained, corrected 2026-08-05).

A cache refresh is emitted by the daily report (`hermes_pricing.refresh_cache`)
and/or by running `python3 hermes_pricing.py` standalone. All consumers read the
cache file first, then fall back to static, so a stale/offline cache still works.

Cache file:  $HERMES_HOME/cache/ollama-pricing.json
  {
    "updated_at": "<iso8601>",
    "source": "openrouter" | "ollama-usage",
    "per_m": { "<model>": {"in": 0.14, "out": 0.28, "ctx": 1048576}, ... },
    "ollama_activity": { "<model>": {"cost_usd": 2.88, "reqs": 436}, ... }
  }
"""

import json
import os
import time
import urllib.request
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
CACHE_DIR = HERMES_HOME / "cache"
CACHE_FILE = CACHE_DIR / "ollama-pricing.json"

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OLLAMA_USAGE_URL = "https://ollama.com/api/usage"

# Static fallback per-token USD per million tokens (corrected 2026-08-05).
# Only used when live OpenRouter pricing is unavailable.
STATIC_PRICE_PER_M: dict[str, tuple[float, float]] = {
    "gemma4:31b": (0.14, 0.28),
    "gpt-oss:20b": (0.15, 0.30),
    "gpt-oss:120b": (0.30, 0.60),
    "nemotron-3-nano:30b": (0.10, 0.20),
    "nemotron-3-super": (0.20, 0.40),
    "nemotron-3-ultra": (0.40, 0.80),
    "qwen3.5:397b": (0.50, 1.00),
    "mistral-large-3:675b": (0.60, 1.20),
    "minimax-m2.5": (0.20, 0.40),
    "minimax-m2.7": (0.20, 0.40),
    "minimax-m3": (0.30, 0.60),
    "kimi-k2.5": (0.20, 0.40),
    "kimi-k2.6": (0.20, 0.40),
    "kimi-k2.7-code": (0.20, 0.40),
    "kimi-k3": (0.30, 0.60),
    "glm-5.1": (0.20, 0.40),
    "glm-5.2": (0.20, 0.40),
    "deepseek-v4-flash": (0.10, 0.20),
    "deepseek-v4-flash:0731": (0.10, 0.20),
    "deepseek-v4-pro": (0.30, 0.60),
    "claude-haiku-4.5": (0.80, 4.00),
    "claude-sonnet-4.6": (3.00, 15.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4.8": (15.00, 75.00),
    "gpt-5-mini": (0.15, 0.60),
    "gpt-5.4-nano": (0.10, 0.40),
    "gpt-5.6-luna": (0.20, 0.80),
    "gpt-5.3-codex": (0.30, 1.20),
    "gemini-3.6-flash": (0.10, 0.40),
    "grok-4.5": (3.00, 15.00),
}

# Default per-token USD per million for models not in the static table.
PROVIDER_DEFAULT_PER_M: dict[str, tuple[float, float]] = {
    "ollama-cloud": (0.20, 0.40),
}

# Ollama Cloud bills by GPU/compute time but only publishes coarse L1-L4 tiers.
# Maps a model to a per-request USD proxy for rough cost estimation.
OLLAMA_TIER_PROXY: dict[int, float] = {
    1: 0.01,
    2: 0.02,
    3: 0.04,
    4: 0.08,
}


def _http_json(url: str, headers: dict | None = None, timeout: float = 15):
    hdrs = {"Accept": "application/json", "User-Agent": "HermesAgent/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_openrouter_pricing() -> dict:
    """Fetch live per-token USD pricing from OpenRouter /v1/models."""
    try:
        data = _http_json(OPENROUTER_MODELS_URL)
    except Exception:
        return {}
    out: dict = {}
    for m in data.get("data") or []:
        mid = m.get("id")
        pricing = m.get("pricing") or {}
        try:
            out[mid] = {
                "in": float(pricing.get("prompt") or 0),
                "out": float(pricing.get("completion") or 0),
                "ctx": int((m.get("context_length") or 0)),
            }
        except (TypeError, ValueError):
            continue
    return out


def fetch_ollama_activity(key: str) -> dict:
    """Fetch REAL dollar cost per model from Ollama Cloud /api/usage.

    Returns { "<model>": {"cost_usd": float, "reqs": int}, ... } over the last
    4-week activity window. Empty dict on failure or missing key.
    """
    if not key:
        return {}
    try:
        data = _http_json(
            OLLAMA_USAGE_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
    except Exception:
        return {}

    activity = data.get("activity") or {}
    out: dict = {}
    for m in activity.get("models") or []:
        name = m.get("name")
        if not name:
            continue
        try:
            cost = float(m.get("cost") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        out[name] = {
            "cost_usd": cost,
            "reqs": m.get("request_count"),
        }
    return out


def _ollama_api_key() -> str:
    k = os.environ.get("OLLAMA_API_KEY", "").strip()
    if k:
        return k
    # Fall back to dotenv-loaded value.
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv("OLLAMA_API_KEY")
        return os.environ.get("OLLAMA_API_KEY", "")
    except Exception:
        return ""


def read_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def refresh_cache() -> dict:
    """Refresh the on-disk pricing cache and return it."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = read_cache()

    or_pricing = fetch_openrouter_pricing()
    if or_pricing:
        cache["per_m"] = or_pricing
        cache["source"] = "openrouter"

    activity = fetch_ollama_activity(_ollama_api_key())
    if activity:
        cache["ollama_activity"] = activity

    cache["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        CACHE_FILE.write_text(
            json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass
    return cache


def _normalize_model(model_id: str) -> str:
    return model_id.strip().lower()


def _or_lookup(model_id: str, pricing_data: dict) -> dict | None:
    if model_id in pricing_data:
        return pricing_data[model_id]
    return None


def pricing_for(model_id: str, pricing_data: dict | None = None) -> dict | None:
    """Resolve per-token USD pricing for a model.

    Order: live pricing_data (OpenRouter) -> static table -> provider default.
    """
    pricing_data = pricing_data or read_cache().get("per_m", {})
    mid = _normalize_model(model_id)
    p = _or_lookup(mid, pricing_data)
    if p:
        return p
    static = STATIC_PRICE_PER_M.get(mid)
    if static:
        return {"in": static[0], "out": static[1], "ctx": 0}
    default = PROVIDER_DEFAULT_PER_M.get("ollama-cloud")
    if default:
        return {"in": default[0], "out": default[1], "ctx": 0}
    return None


def estimate_cost_usd(
    model_id: str,
    in_tokens: int,
    out_tokens: int,
    pricing_data: dict | None = None,
) -> float:
    """Estimate USD cost for a token run using per-M pricing."""
    p = pricing_for(model_id, pricing_data)
    if not p:
        return 0.0
    return (in_tokens / 1_000_000 * p["in"]) + (out_tokens / 1_000_000 * p["out"])


if __name__ == "__main__":
    cache = refresh_cache()
    print("Ollama pricing cache refreshed ->", CACHE_FILE)
    print("  source:", cache.get("source"))
    print("  updated_at:", cache.get("updated_at"))
    print("  openrouter models:", len(cache.get("per_m", {})))
    print("  ollama activity:", json.dumps(cache.get("ollama_activity", {}), indent=2))
