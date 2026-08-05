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

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
CACHE_DIR = HERMES_HOME / "cache"
CACHE_FILE = CACHE_DIR / "ollama-pricing.json"

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OLLAMA_USAGE_URL = "https://ollama.com/api/usage"

# ── Static fallback table ($/M input, $/M output) ────────────────────────────
# Corrected 2026-08-05 against live OpenRouter. Key gotcha: the pinned
# deepseek-v4-flash:0731 is $0.09/$0.18, NOT $0.14/$0.28 (the bare model is
# $0.14/$0.28). Ollama Cloud bills by GPU-time/quota, so these per-token rates
# are ranking/cross-provider proxies only — real spend comes from the quota API.
STATIC_PRICE_PER_M: dict[str, tuple[float, float]] = {
    # Ollama Cloud (cloud-only)
    "gemma4:31b":            (0.03, 0.12),
    "gpt-oss:20b":           (0.03, 0.12),
    "gpt-oss:120b":          (0.20, 0.80),
    "nemotron-3-nano:30b":   (0.03, 0.12),
    "nemotron-3-super":      (0.20, 0.80),
    "nemotron-3-ultra":      (0.80, 3.20),
    "qwen3.5:397b":          (0.20, 0.80),
    "mistral-large-3:675b":  (0.20, 0.80),
    "minimax-m2.5":          (0.20, 0.80),
    "minimax-m2.7":          (0.20, 0.80),
    "minimax-m3":            (0.80, 3.20),
    "kimi-k2.5":             (0.57, 2.85),
    "kimi-k2.6":             (0.60, 3.41),
    "kimi-k2.7-code":        (0.73, 3.50),
    "kimi-k3":               (3.00, 15.00),
    "glm-5.1":               (0.80, 3.20),
    "glm-5.2":               (0.80, 3.20),
    # DeepSeek (OR live 2026-08-05)
    "deepseek-v4-flash":     (0.14, 0.28),
    "deepseek-v4-flash:0731":(0.09, 0.18),   # pinned ≠ bare! ~35% cheaper
    "deepseek-v4-pro":       (0.44, 0.87),
    # OpenRouter / Copilot / Cursor cross-ref
    "claude-haiku-4.5":      (1.00, 5.00),
    "claude-sonnet-4.6":     (3.00, 15.00),
    "claude-sonnet-5":       (2.00, 10.00),
    "claude-opus-4.8":       (10.00, 50.00),
    "gpt-5-mini":            (0.25, 2.00),
    "gpt-5.4-nano":          (0.20, 1.25),
    "gpt-5.6-luna":          (0.20, 1.20),
    "gpt-5.3-codex":         (1.75, 14.00),
    "gemini-3.6-flash":      (1.50, 7.50),
    "grok-4.5":              (2.00, 6.00),
    "hermes-agent":          (0.20, 0.80),
    "auto":                  (3.00, 15.00),
    "default":               (3.00, 15.00),
}

# Provider default per-token rates ($/M) when no per-model match.
PROVIDER_DEFAULT_PER_M: dict[str, tuple[float, float]] = {
    "ollama-cloud": (0.20, 0.80),
    "openai":       (5.00, 15.00),
    "anthropic":    (3.00, 15.00),
    "google":       (1.25, 5.00),
    "xai":          (5.00, 15.00),
    "unknown":      (0.20, 0.80),
}

# Ollama Cloud GPU-tier → proxy $/M input (ranking only, not real billing).
OLLAMA_TIER_PROXY: dict[int, float] = {1: 0.03, 2: 0.20, 3: 0.80, 4: 3.00, 99: 5.00}


def _http_json(url: str, headers: dict | None = None, timeout: int = 15) -> dict:
    hdrs = {"Accept": "application/json", "User-Agent": "HermesAgent/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _load_dotenv(*names: str) -> dict[str, str]:
    out: dict[str, str] = {}
    env_path = HERMES_HOME / ".env"
    if not env_path.is_file():
        return out
    wanted = set(names)
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in wanted:
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def fetch_openrouter_pricing() -> dict[str, dict]:
    """Live per-token $/M from OpenRouter. Returns {id: {"in","out","ctx"}}."""
    try:
        data = _http_json(OPENROUTER_MODELS_URL)
    except Exception:
        return {}
    result: dict[str, dict] = {}
    for item in data.get("data", []):
        mid = item.get("id")
        pricing = item.get("pricing")
        if mid and isinstance(pricing, dict):
            try:
                in_m = float(pricing.get("prompt") or 0) * 1_000_000
                out_m = float(pricing.get("completion") or 0) * 1_000_000
            except (TypeError, ValueError):
                in_m = out_m = 0.0
            result[mid] = {
                "in": round(in_m, 4),
                "out": round(out_m, 4),
                "ctx": item.get("context_length"),
            }
    return result


def fetch_ollama_activity(key: str) -> dict[str, dict]:
    """Real dollarized cost per model from Ollama Cloud quota API (4-week window)."""
    if not key:
        return {}
    try:
        data = _http_json(OLLAMA_USAGE_URL, headers={"Authorization": f"Bearer {key}"})
    except Exception:
        return {}
    activity = data.get("activity") or {}
    out: dict[str, dict] = {}
    for m in activity.get("models") or []:
        name = m.get("name")
        if not name:
            continue
        try:
            cost = float(m.get("cost") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        out[name] = {"cost_usd": cost, "reqs": m.get("request_count")}
    return out


def _ollama_api_key() -> str:
    k = (os.environ.get("OLLAMA_API_KEY") or "").strip()
    if k:
        return k
    return _load_dotenv("OLLAMA_API_KEY").get("OLLAMA_API_KEY", "")


def read_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def refresh_cache() -> dict:
    """Fetch live sources and write the canonical pricing cache."""
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
        CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass
    return cache


def _normalize_model(model: str) -> str:
    model = (model or "").strip()
    # bare "deepseek-v4-flash" should NOT match the pinned variant via prefix,
    # but the pinned variant IS "deepseek-v4-flash" + ":0731".
    return model


def _or_lookup(model: str, per_m: dict) -> tuple[float, float] | None:
    """Look up $/M in/out for an Ollama model id against OpenRouter per_m cache."""
    if not per_m:
        return None
    candidates: list[str] = []
    # Full model id first (may itself be an OR id).
    candidates.append(model)
    # Pinned variant "name:tag" -> prefer the tag-specific OR id over the bare
    # base, so deepseek-v4-flash:0731 matches deepseek-v4-flash-0731 (not the
    # cheaper-to-confuse bare deepseek-v4-flash).
    if ":" in model:
        base, _, tag = model.partition(":")
        candidates += [f"deepseek/{base}-{tag}", f"deepseek/{model.replace(':', '-')}", base]
    candidates.append(f"deepseek/{model}")
    for c in candidates:
        if c in per_m:
            p = per_m[c]
            if p["in"] > 0:
                return (p["in"], p["out"])
    # suffix match: any OR id ending with "/<c>"
    for or_id, p in per_m.items():
        for c in candidates:
            if or_id.endswith("/" + c) and p["in"] > 0:
                return (p["in"], p["out"])
    return None


def pricing_for(provider: str, model: str, per_m: dict | None = None) -> tuple[float, float]:
    """Return ($/M in, $/M out) for a model. Most reliable data wins.

    Order: live OR cache > static table > provider default.
    """
    provider = (provider or "unknown").strip().lower()
    model = _normalize_model(model)

    if per_m:
        hit = _or_lookup(model, per_m)
        if hit and hit[0] > 0:
            return hit

    if model in STATIC_PRICE_PER_M:
        return STATIC_PRICE_PER_M[model]
    # prefix fallback: deepseek-v4-flash:xxxx → deepseek-v4-flash base? NO for
    # the pinned variant (different price). Only fall back for unknown tags.
    for base, rates in STATIC_PRICE_PER_M.items():
        if model.startswith(base + ":") and "0731" not in model:
            return rates

    return PROVIDER_DEFAULT_PER_M.get(provider, PROVIDER_DEFAULT_PER_M["unknown"])


def estimate_cost_usd(provider: str, model: str, in_tok: int, out_tok: int,
                      per_m: dict | None = None) -> float:
    in_r, out_r = pricing_for(provider, model, per_m)
    return (in_tok / 1_000_000.0 * in_r) + (out_tok / 1_000_000.0 * out_r)


if __name__ == "__main__":
    cache = refresh_cache()
    per_m = cache.get("per_m", {})
    print(f"Ollama pricing cache refreshed -> {CACHE_FILE}")
    print(f"  source: {cache.get('source')}  updated_at: {cache.get('updated_at')}")
    print(f"  openrouter models: {len(per_m)}")
    for probe in ("deepseek-v4-flash", "deepseek-v4-flash:0731", "gemma4:31b", "gpt-oss:20b"):
        in_r, out_r = pricing_for("ollama-cloud", probe, per_m)
        print(f"  {probe:24} -> ${in_r:.3f}/${out_r:.3f} per M")
