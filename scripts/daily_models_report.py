#!/usr/bin/env python3
"""Daily models report: Copilot + Ollama Cloud + Cursor — Telegram-friendly.

Fetches live data from:
  1. GitHub Copilot /models API (context caps via max_prompt_tokens)
  2. OpenRouter /v1/models API (per-token $/M pricing for Copilot cross-ref)
  3. Ollama Cloud /v1/models (available cloud IDs)
  4. Ollama /api/show (context, capabilities: thinking/tools/vision)
  5. ollama.com/library pages (usage level L1–L4 = GPU-time cost proxy)
  6. ~/.hermes profile configs (what's actually wired)
  7. ~/.hermes/provider_models_cache.json (Cursor model list — SDK bridge not
     available in no_agent cron, so we read from Hermes' own cache file)

Output for Telegram cron (no_agent):
  - stdout = SHORT mobile blurb + MEDIA:/path lines
  - full catalog written to ~/.hermes/cache/daily-models-report/
    as .md (searchable) and .html (opens nicely in Telegram)

Telegram hates long digests — chat gets picks + in-use only;
catalogs ride as document attachments.
"""

from __future__ import annotations

import concurrent.futures
import html as html_mod
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from response_cache import ttl_cached

# ─── Config ───────────────────────────────────────────────────────────────────
COPILOT_MODELS_URL = "https://api.githubcopilot.com/models"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OLLAMA_CLOUD_MODELS_URL = "https://ollama.com/v1/models"
OLLAMA_SHOW_URL = "https://ollama.com/api/show"
OLLAMA_LIBRARY_BASE = "https://ollama.com/library"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
REPORT_CACHE_DIR = HERMES_HOME / "cache" / "daily-models-report"
STATE_DB = HERMES_HOME / "state.db"
PROVIDER_MODELS_CACHE = HERMES_HOME / "provider_models_cache.json"

# Cursor model families for grouping
CURSOR_FAMILY_ORDER = ["claude", "openai", "gemini", "xai", "kimi", "glm", "composer", "other"]
# Machine-readable tier map consumed by the quota watchdog to pick cheaper
# substitute models. Single source of truth: the daily report enriches these
# capability rows, the watchdog just reads the JSON.
TIER_MAP_PATH = HERMES_HOME / "cache" / "ollama-tier-map.json"

USAGE_LABELS = {
    1: "L1 light",
    2: "L2 mid",
    3: "L3 high",
    4: "L4 xheavy",
}

USAGE_EMOJI = {1: "🟢", 2: "🟡", 3: "🟠", 4: "🔴"}


def load_dotenv_keys(*names: str) -> dict[str, str]:
    """Read selected keys from ~/.hermes/.env without printing values."""
    out: dict[str, str] = {}
    env_path = HERMES_HOME / ".env"
    if not env_path.is_file():
        return out
    try:
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    wanted = set(names)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k not in wanted:
            continue
        v = v.strip().strip('"').strip("'")
        if v:
            out[k] = v
    return out


def get_gh_token() -> str:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def get_ollama_api_key() -> str:
    key = (os.environ.get("OLLAMA_API_KEY") or "").strip()
    if key:
        return key
    return load_dotenv_keys("OLLAMA_API_KEY").get("OLLAMA_API_KEY", "")


def http_json(url: str, headers: dict | None = None, data: bytes | None = None, timeout: int = 20):
    hdrs = {"Accept": "application/json", "User-Agent": "HermesAgent/1.0"}
    if headers:
        hdrs.update(headers)
    method = "POST" if data is not None else "GET"
    if data is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def get_copilot_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Editor-Version": "vscode/1.100.0",
        "User-Agent": "HermesAgent/1.0",
        "Openai-Intent": "conversation-edits",
        "x-initiator": "agent",
        "Accept": "application/json",
    }


@ttl_cached(ttl_seconds=1800)
def fetch_copilot_models(token: str) -> list[dict]:
    try:
        data = http_json(COPILOT_MODELS_URL, headers=get_copilot_headers(token), timeout=15)
    except Exception as e:
        print(f"⚠️  Copilot /models fetch failed: {e}", file=sys.stderr)
        return []

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    else:
        return []

    models = []
    seen = set()
    for item in items:
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        model_type = str(item.get("model_type") or item.get("type") or "").lower()
        if model_type in ("embedding", "image", "audio", "moderation"):
            continue
        if "embedding" in mid.lower() or "embed" in mid.lower():
            continue

        caps = item.get("capabilities") or {}
        family = str(item.get("family") or "").lower()
        limits = caps.get("limits") or {}
        max_prompt = limits.get("max_prompt_tokens") or limits.get("max_input_tokens")
        max_output = limits.get("max_output_tokens") or limits.get("max_completion_tokens")

        seen.add(mid)
        models.append({
            "id": mid,
            "name": item.get("name") or mid,
            "family": family,
            "max_prompt_tokens": int(max_prompt) if max_prompt else None,
            "max_output_tokens": int(max_output) if max_output else None,
        })

    return models


@ttl_cached(ttl_seconds=3600)
def fetch_openrouter_pricing() -> dict[str, dict]:
    try:
        data = http_json(OPENROUTER_MODELS_URL, timeout=15)
    except Exception as e:
        print(f"⚠️  OpenRouter pricing fetch failed: {e}", file=sys.stderr)
        return {}

    result = {}
    for item in data.get("data", []):
        mid = item.get("id")
        pricing = item.get("pricing")
        if mid and isinstance(pricing, dict):
            result[mid] = {
                "prompt": pricing.get("prompt", "0"),
                "completion": pricing.get("completion", "0"),
                "context_length": item.get("context_length"),
            }
    return result


@ttl_cached(ttl_seconds=900)
def fetch_ollama_cloud_ids(api_key: str) -> list[str]:
    try:
        data = http_json(
            OLLAMA_CLOUD_MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
    except Exception as e:
        print(f"⚠️  Ollama Cloud /models fetch failed: {e}", file=sys.stderr)
        return []

    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    ids = []
    seen = set()
    for item in items:
        mid = str((item or {}).get("id") or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    return ids


@ttl_cached(ttl_seconds=3600)
def ollama_show(api_key: str, model_id: str) -> dict | None:
    try:
        return http_json(
            OLLAMA_SHOW_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            data=json.dumps({"model": model_id}).encode(),
            timeout=20,
        )
    except Exception as e:
        print(f"⚠️  Ollama show failed for {model_id}: {e}", file=sys.stderr)
        return None


def library_path_candidates(model_id: str) -> list[str]:
    out: list[str] = []
    if ":" in model_id:
        out.append(f"{model_id}-cloud")
        out.append(model_id)
        out.append(model_id.split(":", 1)[0])
    else:
        out.append(model_id)
        out.append(f"{model_id}:cloud")
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


@ttl_cached(ttl_seconds=86400)
def fetch_usage_level(model_id: str) -> int | None:
    for path in library_path_candidates(model_id):
        try:
            req = urllib.request.Request(
                f"{OLLAMA_LIBRARY_BASE}/{path}",
                headers={"User-Agent": "Mozilla/5.0 HermesAgent/1.0", "Accept": "text/html"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                page = resp.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        idx = page.find(">Usage</div>")
        if idx < 0:
            continue
        block = page[idx : idx + 800]
        filled = len(re.findall(r"bg-neutral-900", block))
        if 1 <= filled <= 4:
            return filled
    return None


def enrich_ollama_model(api_key: str, model_id: str) -> dict:
    show = ollama_show(api_key, model_id) or {}
    caps = list(show.get("capabilities") or [])
    info = show.get("model_info") or {}
    details = show.get("details") or {}

    ctx = None
    for k, v in info.items():
        if str(k).endswith("context_length"):
            try:
                ctx = int(v)
            except (TypeError, ValueError):
                ctx = None
            break

    params = details.get("parameter_size") or info.get("general.parameter_count")
    try:
        params_i = int(params) if params not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        params_i = None

    has_thinking = "thinking" in caps
    if not has_thinking:
        think = "no"
    elif params_i and params_i >= 500_000_000_000:
        think = "high"
    elif params_i and params_i >= 100_000_000_000:
        think = "mid"
    elif params_i and params_i > 0:
        think = "low"
    else:
        think = "yes"

    return {
        "id": model_id,
        "context": ctx,
        "params": params_i,
        "thinking": think,
        "has_thinking": has_thinking,
        "tools": "tools" in caps,
        "vision": "vision" in caps,
        "usage_level": fetch_usage_level(model_id),
        "caps": caps,
    }


def price_per_mtok(per_token_str: str) -> str:
    try:
        val = float(per_token_str)
    except (TypeError, ValueError):
        return "?"
    if val == 0:
        return "free"
    per_m = val * 1_000_000
    if per_m >= 1:
        return f"${per_m:.2f}"
    return f"${per_m:.4f}"


def format_tokens(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def format_params(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000_000_000:
        return f"{n / 1_000_000_000_000:.1f}T"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.0f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    return str(n)


def match_pricing(model_id: str, pricing_data: dict) -> dict | None:
    if model_id in pricing_data:
        return pricing_data[model_id]

    prefixes = [
        "anthropic/", "openai/", "google/", "meta-llama/",
        "mistralai/", "cohere/", "deepseek/", "x-ai/",
    ]
    for prefix in prefixes:
        key = prefix + model_id
        if key in pricing_data:
            return pricing_data[key]

    for or_id, p in pricing_data.items():
        if or_id.endswith("/" + model_id):
            return p

    return None


def _simple_yaml_model_block(text: str) -> dict:
    """Tiny parser for model/fallback/delegation/vision keys we care about.

    Avoids PyYAML dependency (cron no_agent scripts should stay stdlib-only).
    """
    out: dict = {
        "main": None,
        "provider": None,
        "fallback": [],
        "subagent": None,
        "vision": None,
    }
    section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        s = line.strip()

        if indent == 0 and s.endswith(":"):
            section = s[:-1]
            continue

        if section == "model":
            if s.startswith("default:"):
                out["main"] = s.split(":", 1)[1].strip().strip("'\"")
            elif s.startswith("provider:"):
                out["provider"] = s.split(":", 1)[1].strip().strip("'\"")
        elif section == "fallback_providers":
            if s.startswith("- provider:") or s.startswith("provider:"):
                out["fallback"].append({"provider": s.split(":", 1)[1].strip().strip("'\""), "model": None})
            elif s.startswith("model:") and out["fallback"]:
                out["fallback"][-1]["model"] = s.split(":", 1)[1].strip().strip("'\"")
            # string-wrapped JSON list: '[{"provider":"cursor","model":"auto"}]'
            elif s.startswith("'[") or s.startswith('"[') or s.startswith("["):
                try:
                    lit = s.strip().strip("'\"")
                    parsed = json.loads(lit)
                    if isinstance(parsed, list):
                        out["fallback"] = [
                            {"provider": (x or {}).get("provider"), "model": (x or {}).get("model")}
                            for x in parsed if isinstance(x, dict)
                        ]
                except Exception:
                    pass
        elif section == "delegation":
            if s.startswith("model:"):
                out["subagent"] = s.split(":", 1)[1].strip().strip("'\"")
        elif section == "auxiliary":
            # nested vision handled loosely: look for model under vision later
            pass

    # vision: scan for auxiliary.vision.model near "vision:" under auxiliary
    in_aux = False
    in_vision = False
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0 and s == "auxiliary:":
            in_aux = True
            in_vision = False
            continue
        if indent == 0 and s.endswith(":") and s != "auxiliary:":
            in_aux = False
            in_vision = False
            continue
        if in_aux and indent == 2 and s == "vision:":
            in_vision = True
            continue
        if in_aux and indent == 2 and s.endswith(":") and s != "vision:":
            in_vision = False
            continue
        if in_vision and s.startswith("model:"):
            val = s.split(":", 1)[1].strip().strip("'\"")
            if val:
                out["vision"] = val

    return out


def load_profile_usage() -> list[dict]:
    """Scan default + profiles/*/config.yaml for wired models."""
    rows: list[dict] = []
    candidates: list[tuple[str, Path]] = [("default", HERMES_HOME / "config.yaml")]
    profiles_dir = HERMES_HOME / "profiles"
    if profiles_dir.is_dir():
        for p in sorted(profiles_dir.iterdir()):
            cfg = p / "config.yaml"
            if p.is_dir() and cfg.is_file():
                candidates.append((p.name, cfg))

    for name, path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = _simple_yaml_model_block(text)
        if not any([parsed["main"], parsed["subagent"], parsed["vision"], parsed["fallback"]]):
            continue
        fb = []
        for f in parsed["fallback"]:
            m = f.get("model") or ""
            prov = f.get("provider") or ""
            fb.append(f"{m}@{prov}" if m else prov)
        rows.append({
            "profile": name,
            "main": parsed["main"],
            "provider": parsed["provider"],
            "fallback": fb,
            "subagent": parsed["subagent"] or None,
            "vision": parsed["vision"] or None,
        })
    return rows


def badge(r: dict) -> str:
    """Compact capability badges for one Ollama row."""
    bits = []
    if r.get("tools"):
        bits.append("🛠")
    if r.get("vision"):
        bits.append("👁")
    if r.get("has_thinking"):
        bits.append(f"🧠{r.get('thinking')}")
    return " ".join(bits) if bits else "—"


def render_in_use(profiles: list[dict]) -> list[str]:
    lines = ["## In use (profiles)", ""]
    if not profiles:
        lines.append("_No profile configs found_")
        return lines

    # Group profiles that share the same main model + provider
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in profiles:
        main = p.get("main") or "?"
        prov = p.get("provider") or "?"
        groups[f"{main}@{prov}"].append(p)

    for key, ps in groups.items():
        profile_names = ", ".join(f"**{p['profile']}**" for p in ps)
        main, prov = key.split("@", 1)
        line = f"• `{main}` @{prov} → {profile_names}"
        # Show fallback chain from first profile (same across group by convention)
        p0 = ps[0]
        fb = p0.get("fallback") or []
        if fb:
            line += f"  ↳ {' / '.join(f'`{x}`' for x in fb if x)}"
        extras = []
        if p0.get("subagent"):
            extras.append(f"sub `{p0['subagent']}`")
        if p0.get("vision"):
            extras.append(f"👁 `{p0['vision']}`")
        if extras:
            line += f"  ({', '.join(extras)})"
        lines.append(line)
    lines.append("")
    return lines


def render_ollama_section(rows: list[dict], used_ids: set[str]) -> list[str]:
    rows = sorted(rows, key=lambda r: (r.get("usage_level") or 99, r["id"]))
    lines = [
        f"## Ollama Cloud — {len(rows)} models",
        "_GPU-time billing (Free / Pro $20 / Max $100). Cost = usage level._",
        "",
    ]

    by_level: dict[int, list[dict]] = {}
    unknown: list[dict] = []
    for r in rows:
        lvl = r.get("usage_level")
        if lvl in USAGE_LABELS:
            by_level.setdefault(lvl, []).append(r)
        else:
            unknown.append(r)

    for lvl in (1, 2, 3, 4):
        group = by_level.get(lvl) or []
        if not group:
            continue
        emoji = USAGE_EMOJI.get(lvl, "⚪")
        lines.append(f"**{emoji} {USAGE_LABELS[lvl]}** ({len(group)})")
        for r in group:
            star = " ★" if r["id"] in used_ids else ""
            lines.append(
                f"• `{r['id']}`{star} — ctx {format_tokens(r.get('context'))}, "
                f"size {format_params(r.get('params'))}, {badge(r)}"
            )
        lines.append("")

    if unknown:
        lines.append(f"**⚪ Cost unknown** ({len(unknown)})")
        for r in unknown:
            star = " ★" if r["id"] in used_ids else ""
            lines.append(
                f"• `{r['id']}`{star} — ctx {format_tokens(r.get('context'))}, "
                f"size {format_params(r.get('params'))}, {badge(r)}"
            )
        lines.append("")

    lines.append("_★ = wired in a profile · 🛠 tools · 👁 vision · 🧠 thinking_")
    lines.append("")
    return lines


def render_comparison_table(rows: list[dict]) -> list[str]:
    """Compact comparison table: all models side-by-side by value (cheapest first)."""
    if not rows:
        return []

    def value_key(r: dict) -> tuple:
        # Lower usage_level = cheaper. Within same tier, smaller params = cheaper.
        return (r.get("usage_level") or 99, r.get("params") or 10**18, r["id"])

    sorted_rows = sorted(rows, key=value_key)

    lines = ["## Model Comparison (by value)", ""]
    lines.append(
        "| Model | Params | Ctx | 🛠 | 👁 | 🧠 | Tier |"
    )
    lines.append(
        "|-------|--------|-----|:--:|:--:|:--:|:----:|"
    )
    for r in sorted_rows:
        mid = r["id"]
        params = format_params(r.get("params"))
        ctx = format_tokens(r.get("context"))
        tools = "✅" if r.get("tools") else "—"
        vision = "✅" if r.get("vision") else "—"
        think = r.get("thinking") or "—"
        lvl = r.get("usage_level")
        tier = f"{USAGE_EMOJI.get(lvl, '⚪')} {USAGE_LABELS.get(lvl, 'L?')}" if lvl else "⚪ L?"
        lines.append(
            f"| `{mid}` | {params} | {ctx} | {tools} | {vision} | {think} | {tier} |"
        )
    lines.append("")
    lines.append("_Sorted by cost (cheapest first). Same tier ≈ same GPU-time cost._")
    lines.append("")
    return lines


def render_picks(rows: list[dict]) -> list[str]:
    """Actionable shortlist from live Ollama catalog."""
    if not rows:
        return []

    def cost_key(r: dict) -> tuple:
        return (r.get("usage_level") or 99, r.get("params") or 10**18, r["id"])

    with_tools = [r for r in rows if r.get("tools")]
    with_vision = [r for r in rows if r.get("vision")]
    with_think = [r for r in rows if r.get("has_thinking")]
    cheap_think = sorted(with_think, key=cost_key)
    cheap_vision = sorted(with_vision, key=cost_key)
    cheap_tools = sorted(with_tools, key=cost_key)
    heavy_think = sorted(
        with_think,
        key=lambda r: (-(r.get("usage_level") or 0), -(r.get("params") or 0), r["id"]),
    )

    lines = ["## Picks (Ollama Cloud)", ""]

    def one(label: str, r: dict | None) -> None:
        if not r:
            lines.append(f"• **{label}**: —")
            return
        lvl = USAGE_LABELS.get(r.get("usage_level") or 0, "L?")
        lines.append(
            f"• **{label}**: `{r['id']}` ({lvl}, "
            f"ctx {format_tokens(r.get('context'))}, {badge(r)})"
        )

    one("Cheapest + tools", cheap_tools[0] if cheap_tools else None)
    one("Cheapest + vision", cheap_vision[0] if cheap_vision else None)
    one("Cheapest + thinking", cheap_think[0] if cheap_think else None)
    one("Max thinking (costliest)", heavy_think[0] if heavy_think else None)

    # Mid-tier agentic (tools + not L4)
    agentic = [
        r for r in with_tools
        if r.get("usage_level") in (1, 2) and not r.get("vision")
    ]
    agentic = sorted(agentic, key=cost_key)
    one("Best cheap agentic", agentic[0] if agentic else None)

    lines.append("")
    return lines


def family_of(mid: str) -> str:
    m = mid.lower()
    if m.startswith("claude"):
        return "claude"
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"):
        return "openai"
    if m.startswith("gemini"):
        return "gemini"
    if "grok" in m:
        return "xai"
    return "other"


def is_copilot_noise(mid: str) -> bool:
    """Hide dated snapshots and dead legacy IDs from the daily digest."""
    m = mid.lower()
    # Dated snapshots (e.g. gpt-4-0613, claude-3-opus-20240229)
    if re.search(r"\d{4}-\d{2}-\d{2}", m):
        return True
    # Old GPT-3.5 line
    if m.startswith("gpt-3.5"):
        return True
    # Legacy GPT-4 base/preview (not gpt-4o-mini, not gpt-4.1+)
    if m in ("gpt-4", "gpt-4-o-preview", "gpt-4o", "gpt-4o-mini"):
        return True
    # gpt-4.0xxx and gpt-4.1 etc are legacy; keep gpt-4.1 only if it's truly current
    # Rule: gpt-4.* (dot release) that aren't in our known-current set are noise
    if re.match(r"^gpt-4\.", m) and m not in ("gpt-4.1",):
        return True
    # Internal / non-chat models
    if "trajectory" in m or "compaction" in m or "embed" in m:
        return True
    return False


def render_copilot_section(models: list[dict], pricing_data: dict, used_ids: set[str]) -> list[str]:
    current = [m for m in models if not is_copilot_noise(m["id"])]
    hidden = len(models) - len(current)
    current = sorted(current, key=lambda m: (family_of(m["id"]), m["id"]))
    lines = [
        f"## GitHub Copilot — {len(current)} current"
        + (f" (+{hidden} legacy hidden)" if hidden else ""),
        "_$/Mtok ≈ OpenRouter cross-ref (not Copilot bill)._",
        "",
    ]

    by_fam: dict[str, list[dict]] = {}
    for m in current:
        by_fam.setdefault(family_of(m["id"]), []).append(m)

    fam_order = ["claude", "openai", "gemini", "xai", "other"]
    for fam in fam_order:
        group = by_fam.get(fam) or []
        if not group:
            continue
        lines.append(f"**{fam}** ({len(group)})")
        for m in group:
            mid = m["id"]
            star = " ★" if mid in used_ids else ""
            ctx = format_tokens(m["max_prompt_tokens"])
            out = format_tokens(m["max_output_tokens"])
            pricing = match_pricing(mid, pricing_data)
            if pricing:
                p_in = price_per_mtok(pricing["prompt"])
                p_out = price_per_mtok(pricing["completion"])
                price = f"{p_in}/{p_out}"
            else:
                price = "n/a"
            lines.append(f"• `{mid}`{star} — {ctx}→{out}, {price}")
        lines.append("")

    lines.append("_★ = wired in a profile · price = $/M in / $/M out_")
    lines.append("")
    return lines


def collect_used_model_ids(profiles: list[dict]) -> set[str]:
    used: set[str] = set()
    for p in profiles:
        for key in ("main", "subagent", "vision"):
            v = p.get(key)
            if v:
                used.add(v)
                # also bare name without tag
                if ":" in v:
                    used.add(v.split(":", 1)[0])
        for fb in p.get("fallback") or []:
            # forms: model@provider or provider
            if "@" in fb:
                used.add(fb.split("@", 1)[0])
            elif fb and fb not in ("github-copilot", "cursor", "ollama-cloud", "openai"):
                used.add(fb)
    return {x for x in used if x}


def render_telegram_brief(
    now: str,
    profiles: list[dict],
    ollama_rows: list[dict],
    copilot_models: list[dict],
    cursor_models: list[str],
    cursor_cache_age: float | None,
    ollama_ok: bool,
    copilot_ok: bool,
    cursor_ok: bool,
    budget: dict | None = None,
    spend: dict | None = None,
    spend_picks: dict | None = None,
) -> list[str]:
    """Action-first Telegram brief. Target: ~60 lines / ~1800 chars.

    Structure:
      1. Header + routing mode (5 lines)
      2. Budget gauges (6 lines)
      3. TODAY: active config snippets for current mode (12 lines)
      4. Top waste signal if any (3 lines)
      5. Today's best model picks (4 lines)
      6. Active profile wiring (compact) (5 lines)
      7. Footer hint
    """
    budget = budget or {}
    spend  = spend  or {}
    spend_picks = spend_picks or {}
    ollama_live = budget.get("ollama_live", {})
    wk_frac  = ollama_live.get("weekly_usage_frac") if ollama_live.get("ok") else None
    act_cost = ollama_live.get("activity_cost_usd", 0.0) if ollama_live.get("ok") else 0.0
    wk_rem   = budget.get("week_days_remaining", 7)
    credits_burned = budget.get("cop_credits_burned", 0.0)
    credits_proj   = budget.get("cop_credits_projected_eom", 0.0)
    credit_cap     = 20000
    proj_pct       = credits_proj / credit_cap * 100 if credit_cap else 0

    # ── Routing mode ──────────────────────────────────────────────────────────
    if wk_frac is not None and wk_frac > 0.90:
        mode_icon, mode_name = "🔴", "FRUGAL"
        mode_action = "Ollama exhausted — Cursor pinned only"
        heavy_cfg = ("cursor", "claude-sonnet-4.6")
        mid_cfg   = ("cursor", "kimi-k2.7-code")
        light_cfg = ("cursor", "gpt-5.4-nano")
    elif wk_frac is not None and wk_frac > 0.70 and wk_rem < 3:
        mode_icon, mode_name = "🟠", "CONSERVATIVE"
        mode_action = "Ollama tight — L1 + Cursor fallback"
        heavy_cfg = ("ollama-cloud", "gemma4:31b")
        mid_cfg   = ("ollama-cloud", "gpt-oss:20b")
        light_cfg = ("ollama-cloud", "gpt-oss:20b")
    elif wk_frac is None or wk_frac < 0.40:
        mode_icon, mode_name = "🟢", "POWER"
        mode_action = "Ollama healthy — L2/L3 freely"
        heavy_cfg = ("ollama-cloud", "kimi-k2.7-code")
        mid_cfg   = ("ollama-cloud", "deepseek-v4-flash")
        light_cfg = ("ollama-cloud", "gemma4:31b")
    else:
        mode_icon, mode_name = "🟡", "BALANCED"
        mode_action = "Ollama mid — L1/L2 + Cursor fallback"
        heavy_cfg = ("ollama-cloud", "deepseek-v4-flash")
        mid_cfg   = ("ollama-cloud", "gemma4:31b")
        light_cfg = ("ollama-cloud", "gpt-oss:20b")

    # ── Budget status icons ───────────────────────────────────────────────────
    cop_icon = "🔴" if proj_pct > 95 else ("🟡" if proj_pct > 80 else "🟢")
    oll_icon = "🔴" if (wk_frac or 0) > 0.90 else ("🟠" if (wk_frac or 0) > 0.70 else "🟢")

    # ── Best model pick from spend data ──────────────────────────────────────
    best = spend_picks.get("best_daily") or {}
    best_name = best.get("model", "—")
    best_tps   = best.get("ops", 0)

    # Most costly model (by estimated USD) across all providers
    most_costly = spend.get("most_costly") if spend.get("ok") else None
    prov_totals = spend.get("provider_totals", {}) if spend.get("ok") else {}

    lines: list[str] = []

    # 1. Header
    lines += [
        f"📊 **Hermes Daily — {now}**",
        f"{mode_icon} **{mode_name}** — {mode_action}",
        "",
    ]

    # 2. Budget gauges
    oll_pct_str = f"{(wk_frac or 0)*100:.0f}%"
    lines += [
        "**Budget**",
        f"```",
        f"🐙 Copilot  {credits_burned:>5,.0f}/{credit_cap:,} cr  {cop_icon} EOM {proj_pct:.0f}%",
        f"🖱 Cursor   $200 flat + $400 key (pay-as-you-go)",
        f"☁️  Ollama   {oll_pct_str} quota used  {oll_icon}  ${act_cost:.2f}/4wk",
        "```",
        "",
    ]

    # 3. Active config — paste-ready snippets
    lines += [
        f"**{mode_icon} Apply today ({mode_name})**",
        "```yaml",
        f"# Heavy (apollo, orchestrator, reviewer)",
        f"model: {{default: {heavy_cfg[1]}, provider: {heavy_cfg[0]}}}",
        f"",
        f"# Mid (default, specifier, deployer)",
        f"model: {{default: {mid_cfg[1]}, provider: {mid_cfg[0]}}}",
        f"",
        f"# Light (athena, nihongo-*)",
        f"model: {{default: {light_cfg[1]}, provider: {light_cfg[0]}}}",
        "```",
        "",
    ]

    # 4. Top waste signal (if any)
    cop_30d_cr = budget.get("cop_credits_burned", 0) * 100   # rough 30d from MTD
    # Check for cursor:auto in profiles
    auto_profiles = [p["profile"] for p in profiles
                     if (p.get("main") or "") in ("auto", "default")
                     and (p.get("provider") or "") == "cursor"]
    if auto_profiles:
        lines += [
            "⚠️ **cursor:auto active** in: " + ", ".join(f"`{p}`" for p in auto_profiles),
            "_Auto picks $5–10/M models silently — pin explicit model above._",
            "",
        ]
    elif proj_pct > 95:
        lines += [
            f"⚠️ **Copilot credits projecting {proj_pct:.0f}% EOM** — shift heavy to Cursor flat",
            "",
        ]

    # 5. Best model today (from 24h spend) + most costly
    if best_name and best_name != "—":
        lines += [
            "**Best model today (Ollama)**",
            f"• `{best_name}` — {best_tps:.0f} tok/s out",
            "",
        ]

    if most_costly and most_costly.get("cost_usd", 0) > 0:
        lines += [
            f"**💸 Most costly 24h** (`{most_costly['provider']}`)",
            f"• `{most_costly['model']}` — ${most_costly['cost_usd']:.2f}"
            f" ({_fmt_int(most_costly['intok'])} in / {_fmt_int(most_costly['outtok'])} out tok)",
            "",
        ]

    # 5b. Per-provider current spend rundown
    if prov_totals:
        cop_t = prov_totals.get("copilot", {})
        cur_t = prov_totals.get("cursor", {})
        oll_t = prov_totals.get("ollama-cloud", {})
        rundown: list[str] = []
        if cop_t.get("cost_usd", 0) > 0:
            rundown.append(
                f"🐙 Copilot ${cop_t['cost_usd']:.2f} · {_fmt_int(cop_t.get('calls',0))} calls")
        if cur_t.get("calls", 0) > 0:
            rundown.append(
                f"🖱 Cursor {_fmt_int(cur_t.get('calls',0))} calls (flat)")
        if oll_t.get("calls", 0) > 0:
            rundown.append(
                f"☁️ Ollama {_fmt_int(oll_t.get('calls',0))} calls")
        if rundown:
            lines.append("**24h spend rundown**")
            lines += [f"• {r}" for r in rundown]
            lines.append("")

    # 6. Active wiring (compact)
    by_main: dict[str, list[str]] = {}
    for p in profiles:
        key = f"{p.get('main','?')} @{p.get('provider','?')}"
        by_main.setdefault(key, []).append(p["profile"])
    lines.append("**Wired profiles**")
    for key, names in sorted(by_main.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:6]:
        who = ", ".join(f"`{n}`" for n in sorted(names))
        lines.append(f"• `{key}` → {who}")
    lines.append("")

    # 7. Footer
    n_models = len(ollama_rows) + len(copilot_models) + len(cursor_models)
    lines += [
        f"_📎 Full report attached ({n_models} models · 30d analysis · config snippets)_",
    ]

    return lines


def markdown_to_simple_html(md: str, title: str) -> str:
    """Tiny Markdown→HTML for Telegram document open-in-browser.

    Handles ## headers, **bold**, `code`, • bullets. No external deps.
    """
    esc = html_mod.escape
    body_parts: list[str] = []
    in_ul = False
    in_table = False
    table_header_done = False

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            body_parts.append("</ul>")
            in_ul = False

    def close_table() -> None:
        nonlocal in_table, table_header_done
        if in_table:
            body_parts.append("</tbody></table>")
            in_table = False
            table_header_done = False

    def inline(s: str) -> str:
        s = esc(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", s)
        return s

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line:
            close_ul()
            close_table()
            continue

        # ── Table rows ──────────────────────────────────────────────────────
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            # separator row like |---|---| → skip
            if re.match(r"^[\s\-:|]+$", line):
                continue
            if not in_table:
                body_parts.append("<table>")
                body_parts.append("<thead><tr>")
                for c in cells:
                    body_parts.append(f"<th>{inline(c)}</th>")
                body_parts.append("</tr></thead>")
                body_parts.append("<tbody>")
                in_table = True
            else:
                body_parts.append("<tr>")
                for c in cells:
                    body_parts.append(f"<td>{inline(c)}</td>")
                body_parts.append("</tr>")
            continue

        close_table()

        if line.startswith("## "):
            close_ul()
            body_parts.append(f"<h2>{esc(line[3:])}</h2>")
            continue
        if line.startswith("# "):
            close_ul()
            body_parts.append(f"<h1>{esc(line[2:])}</h1>")
            continue

        bullet = False
        content = line
        if line.startswith("• ") or line.startswith("- "):
            bullet = True
            content = line[2:]
        elif line.startswith("* ") and not line.startswith("*In") and not line.startswith("*Hermes"):
            # avoid eating italic lines that start with *word*
            if len(line) > 2 and line[1] == " ":
                bullet = True
                content = line[2:]

        # inline: `code` then **bold** (defined above the loop)
        if bullet:
            if not in_ul:
                body_parts.append("<ul>")
                in_ul = True
            body_parts.append(f"<li>{inline(content)}</li>")
        else:
            close_ul()
            # bold section headers like **🟢 L1 light** (3)
            if content.startswith("**") and content.endswith("**"):
                body_parts.append(f"<h3>{inline(content)}</h3>")
            elif content.startswith("_") and content.endswith("_"):
                body_parts.append(f"<p class='note'>{inline(content)}</p>")
            else:
                body_parts.append(f"<p>{inline(content)}</p>")

    close_ul()
    body = "\n".join(body_parts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{esc(title)}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    font-family: -apple-system, system-ui, sans-serif;
    background: #0f1115; color: #e8eaed;
    margin: 0; padding: 16px 14px 48px;
    line-height: 1.45; font-size: 15px;
  }}
  h1 {{ font-size: 1.25rem; margin: 0 0 12px; }}
  h2 {{
    font-size: 1.05rem; margin: 22px 0 8px;
    border-bottom: 1px solid #2a2f3a; padding-bottom: 4px;
    color: #9ecbff;
  }}
  h3 {{ font-size: 0.95rem; margin: 14px 0 6px; color: #c4c7ce; }}
  ul {{ margin: 0 0 8px; padding-left: 1.1rem; }}
  li {{ margin: 3px 0; }}
  code {{
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.88em; background: #1a1f2a; padding: 1px 5px;
    border-radius: 4px; color: #f0c674;
  }}
  table {{
    width: 100%; border-collapse: collapse; margin: 8px 0;
    font-size: 0.85em;
  }}
  th, td {{
    padding: 5px 8px; text-align: left; white-space: nowrap;
    border-bottom: 1px solid #2a2f3a;
  }}
  th {{
    color: #9ecbff; font-weight: 600; background: #161a24;
    position: sticky; top: 0;
  }}
  td {{ color: #e8eaed; }}
  tr:hover td {{ background: #1a1f2a; }}
  p {{ margin: 6px 0; }}
  p.note {{ color: #8b919a; font-size: 0.88em; }}
  strong {{ color: #fff; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def write_report_files(full_md: str, stamp: str) -> tuple[Path, Path]:
    """Persist full catalog; return (md_path, html_path)."""
    REPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    day = stamp[:10] if len(stamp) >= 10 else datetime.now().strftime("%Y-%m-%d")
    md_path = REPORT_CACHE_DIR / f"models-report-{day}.md"
    html_path = REPORT_CACHE_DIR / f"models-report-{day}.html"
    md_path.write_text(full_md, encoding="utf-8")
    title = f"Hermes Models — {day}"
    html_path.write_text(markdown_to_simple_html(full_md, title), encoding="utf-8")
    # prune older than 14 days
    cutoff = datetime.now().timestamp() - 14 * 86400
    for p in REPORT_CACHE_DIR.glob("models-report-*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            pass
    return md_path, html_path


# ─── Actual-spend analysis (from local telemetry) ───────────────────────────
# Ollama Cloud bills by GPU/compute time but only publishes coarse L1–L4 tiers.
# We reconstruct a real per-model cost/efficiency signal from Hermes' own
# state.db: token volumes (session_model_usage) + wall-clock request→reply
# latency (messages.timestamp) as a direct proxy for the hidden GPU-seconds.

SPEND_OLLAMA_LIKE = "%ollama.com%"
SPEND_COPILOT_LIKE = "%githubcopilot.com%"
SPEND_CURSOR_NATIVE = "cursor://%"   # old native cursor://agent
SPEND_CURSOR_GO = "%127.0.0.1:9101%"  # cursor-go-adapter


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[int(round((p / 100) * (len(xs) - 1)))]


def analyze_spend(window_days: int = 1) -> dict:
    """Return per-model spend/efficiency over the last `window_days`.

    Covers Ollama Cloud (latency-attributed), Copilot, and Cursor providers.
    Computes estimated USD cost per model using BUDGET_CONFIG pricing tables.

    Metrics per model:
      provider    = ollama-cloud | copilot | cursor
      calls, sessions, in_tok, out_tok
      cost_usd    = estimated USD cost (Copilot + Cursor only; Ollama = activity API)
      io_ratio    = in_tok / out_tok  (high => paying to re-read context)
      compute_s   = sum of wall-clock latency (Ollama GPU-sec proxy)
      share       = compute_s / total compute_s
      out_per_s   = out_tok / compute_s  (throughput; higher = better value)
      med_s, p90_s = responsiveness / tail risk
      subagent_calls = calls made under a delegation task (task != '')

    Falls back gracefully (returns {'ok': False, ...}) if the DB is missing.
    """
    result: dict = {"ok": False, "window_days": window_days, "rows": [],
                    "total_compute": 0.0, "reason": "",
                    "provider_totals": {}}
    if not STATE_DB.is_file():
        result["reason"] = f"no state.db at {STATE_DB}"
        return result

    cutoff = datetime.now().timestamp() - window_days * 86400
    try:
        db = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error as e:
        result["reason"] = f"open failed: {e}"
        return result

    try:
        c = db.cursor()

        cols = {r[1] for r in c.execute(
            "PRAGMA table_info(session_model_usage)").fetchall()}
        has_task = "task" in cols
        has_lastseen = "last_seen" in cols

        task_sel = "SUM(CASE WHEN task <> '' THEN api_call_count ELSE 0 END)" \
            if has_task else "0"

        time_clause = ""
        time_params: list = []
        if has_lastseen:
            time_clause = " AND COALESCE(last_seen, first_seen, 0) >= ?"
            time_params = [cutoff]

        # ── Helper: fetch usage rows for a given billing_base_url pattern ──
        def _query_usage(url_like: str) -> dict[str, dict]:
            rows_out: dict[str, dict] = {}
            for model, calls, intok, outtok, sess, sub in c.execute(f"""
                SELECT model,
                       SUM(api_call_count),
                       SUM(input_tokens),
                       SUM(output_tokens),
                       COUNT(DISTINCT session_id),
                       {task_sel}
                FROM session_model_usage
                WHERE billing_base_url LIKE ?{time_clause}
                GROUP BY model
            """, [url_like] + time_params):
                if not (calls or outtok):
                    continue
                rows_out[model] = dict(calls=calls or 0, intok=intok or 0,
                                       outtok=outtok or 0, sess=sess or 0,
                                       sub_calls=sub or 0)
            return rows_out

        ollama_usage = _query_usage(SPEND_OLLAMA_LIKE)
        copilot_usage = _query_usage(SPEND_COPILOT_LIKE)
        # Merge cursor://agent (native) + cursor-go-adapter (:9101) into one dict
        cursor_native = _query_usage(SPEND_CURSOR_NATIVE)
        cursor_go     = _query_usage(SPEND_CURSOR_GO)
        cursor_usage: dict[str, dict] = {}
        for src in (cursor_native, cursor_go):
            for model, u in src.items():
                if model in cursor_usage:
                    for k in ("calls", "intok", "outtok", "sess", "sub_calls"):
                        cursor_usage[model][k] += u[k]
                else:
                    cursor_usage[model] = dict(u)

        all_usage = {**ollama_usage}  # start with Ollama (has latency data)

        # ── USD cost estimation ──────────────────────────────────────────────
        # Copilot: use BUDGET_CONFIG model_pricing (per-M tokens)
        cop_pricing = BUDGET_CONFIG.get("copilot", {}).get("model_pricing", {})
        _sonnet_in, _sonnet_out = 3.00, 15.00  # fallback

        def _cop_usd(model: str, intok: int, outtok: int) -> float:
            # Normalize: claude-sonnet-4-6 → claude-sonnet-4.6
            import re
            norm = re.sub(r'(?<=\d)-(\d+)$', r'.\1', model)
            rate = cop_pricing.get(norm) or cop_pricing.get(model)
            in_r, out_r = (rate if rate else (_sonnet_in, _sonnet_out))
            return (intok / 1_000_000 * in_r) + (outtok / 1_000_000 * out_r)

        # Cursor: FLAT plan ($200/mo) covers standard models for the primary Business key.
        # FALLBACK key is pay-as-you-go — charge OR rates.
        # We can't distinguish per-call which key was used, so charge the full OR rate
        # (conservative — if all traffic is FALLBACK, cost = OR rates; if all flat, $0).
        def _cursor_usd(model: str, intok: int, outtok: int) -> float:
            # Use same OR_STATIC lookup as copilot
            import re
            norm = re.sub(r'(?<=\d)-(\d+)$', r'.\1', model)
            # OR_STATIC rates (subset most common cursor models)
            OR_RATES: dict[str, tuple[float, float]] = {
                "gpt-5.4-nano": (0.20, 1.25),
                "gpt-5-mini": (0.25, 2.00),
                "gpt-5.4-mini": (0.75, 4.50),
                "kimi-k2.7-code": (0.73, 3.50),
                "gemini-3.6-flash": (1.50, 7.50),
                "gpt-5.3-codex-low": (1.75, 14.00),
                "gpt-5.3-codex": (1.75, 14.00),
                "claude-sonnet-4.6": (3.00, 15.00),
                "deepseek-v4-flash": (0.14, 0.28),
            }
            rate = OR_RATES.get(norm) or OR_RATES.get(model)
            if not rate:
                rate = (1.75, 14.00)  # fallback: codex-level estimate
            return (intok / 1_000_000 * rate[0]) + (outtok / 1_000_000 * rate[1])

        # Build combined rows with provider tag + cost
        all_rows_raw: list[dict] = []

        for model, u in ollama_usage.items():
            all_rows_raw.append({**u, "model": model, "provider": "ollama-cloud",
                                  "cost_usd": 0.0})  # Ollama cost from activity API

        for model, u in copilot_usage.items():
            cost = _cop_usd(model, u["intok"], u["outtok"])
            all_rows_raw.append({**u, "model": model, "provider": "copilot",
                                  "cost_usd": cost})

        for model, u in cursor_usage.items():
            all_rows_raw.append({**u, "model": model, "provider": "cursor",
                                  "cost_usd": _cursor_usd(model, u["intok"], u["outtok"])})

        if not all_rows_raw:
            result["reason"] = "no usage in window across all providers"
            return result

        # ── Wall-clock latency (Ollama only — Copilot/Cursor too fast/variable) ──
        sess_rows = c.execute(f"""
            SELECT DISTINCT session_id FROM session_model_usage
            WHERE billing_base_url LIKE ?{time_clause}
        """, [SPEND_OLLAMA_LIKE] + time_params).fetchall()
        sess_ids = [r[0] for r in sess_rows]

        sess_model: dict[str, str] = {}
        for sid in sess_ids:
            mr = c.execute("""
                SELECT model, SUM(output_tokens) FROM session_model_usage
                WHERE session_id=? AND billing_base_url LIKE ?
                GROUP BY model ORDER BY 2 DESC LIMIT 1
            """, (sid, SPEND_OLLAMA_LIKE)).fetchone()
            if mr:
                sess_model[sid] = mr[0]

        from collections import defaultdict
        lat: dict[str, list[float]] = defaultdict(list)
        for sid in sess_ids:
            model = sess_model.get(sid)
            if not model:
                continue
            prev = None
            for role, ts in c.execute("""
                SELECT role, timestamp FROM messages
                WHERE session_id=? AND timestamp IS NOT NULL
                  AND timestamp >= ?
                ORDER BY timestamp ASC
            """, (sid, cutoff)):
                if role == "assistant" and prev is not None:
                    dt = ts - prev
                    if 0 < dt < 1800:
                        lat[model].append(dt)
                prev = ts

        rows = []
        for r in all_rows_raw:
            model = r["model"]
            xs = lat.get(model, [])
            compute = sum(xs)
            io = (r["intok"] / r["outtok"]) if r["outtok"] else float("inf")
            ops = (r["outtok"] / compute) if compute else 0.0
            rows.append(dict(
                model=model, compute=compute, med=_pct(xs, 50),
                p90=_pct(xs, 90), io=io, ops=ops, samples=len(xs),
                provider=r["provider"], cost_usd=r["cost_usd"],
                calls=r["calls"], intok=r["intok"], outtok=r["outtok"],
                sess=r["sess"], sub_calls=r["sub_calls"]))

        total_compute = sum(r["compute"] for r in rows if r["provider"] == "ollama-cloud")

        # Provider totals
        prov_totals: dict[str, dict] = {}
        for r in rows:
            p = r["provider"]
            t = prov_totals.setdefault(p, {"calls": 0, "intok": 0, "outtok": 0, "cost_usd": 0.0})
            t["calls"] += r["calls"]
            t["intok"] += r["intok"]
            t["outtok"] += r["outtok"]
            t["cost_usd"] += r["cost_usd"]

        # Most costly model across all providers (by estimated USD)
        costly = max(rows, key=lambda r: r["cost_usd"]) if rows else None

        result.update(ok=True, rows=rows, total_compute=total_compute,
                      provider_totals=prov_totals, most_costly=costly)
        return result
    except sqlite3.Error as e:
        result["reason"] = f"query failed: {e}"
        return result
    finally:
        db.close()


def _spend_score(r: dict) -> float:
    """Value score: throughput per unit context-bloat. Higher = better value.

    out_per_s rewards fast token production; dividing by log(io_ratio)
    penalizes models that burn compute re-reading huge context for little
    output. Guards against zero/degenerate rows.
    """
    import math
    ops = r.get("ops") or 0.0
    io = r.get("io") or 1.0
    if io in (float("inf"), 0):
        io = 1000.0
    return ops / max(1.0, math.log10(max(io, 1.0)) + 1.0)


def pick_best_models(spend: dict) -> dict:
    """Data-driven daily picks from actual spend.

    Returns {'best_daily', 'best_subagent', 'quota_hog', 'worst_value',
             'ranked'} — each an enriched row dict or None.
    """
    out = {"best_daily": None, "best_subagent": None, "quota_hog": None,
           "worst_value": None, "ranked": []}
    rows = [r for r in spend.get("rows", []) if r.get("calls")]
    if not rows:
        return out

    # Require a minimum of real latency samples for value ranking; models with
    # too little signal can't be trusted for "best of the day".
    scored = [r for r in rows if r.get("samples", 0) >= 3]
    fallback = scored or rows
    for r in fallback:
        r["score"] = _spend_score(r)

    ranked = sorted(fallback, key=lambda r: -r["score"])
    out["ranked"] = ranked

    # Best daily driver: highest value score among models with meaningful use.
    out["best_daily"] = ranked[0] if ranked else None

    # Best subagent: among models actually used for delegation (sub_calls>0),
    # pick highest value; else fall back to cheapest-fast general model
    # (best throughput with low io_ratio) since subagents want fast+cheap.
    sub_rows = [r for r in fallback if r.get("sub_calls", 0) > 0]
    if sub_rows:
        out["best_subagent"] = max(sub_rows, key=lambda r: r["score"])
    elif ranked:
        # heuristic: subagents favor speed & low context bloat
        cand = sorted(
            fallback,
            key=lambda r: (-(r.get("ops") or 0), r.get("io") or 9e9),
        )
        out["best_subagent"] = cand[0]

    # Quota hog: most compute share.
    total = spend.get("total_compute") or 1.0
    hog = max(rows, key=lambda r: r.get("compute", 0))
    hog["share"] = 100 * hog.get("compute", 0) / total
    out["quota_hog"] = hog

    # Worst value: lowest score among models with real samples.
    if scored:
        out["worst_value"] = min(scored, key=lambda r: _spend_score(r))

    return out


def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "—"


def render_spend_section(spend: dict, picks: dict) -> list[str]:
    """Full markdown block for the catalog: actual spend + data-driven picks."""
    wd = spend.get("window_days", 1)
    label = "last 24h" if wd == 1 else f"last {wd}d"
    lines = [f"## Actual Spend — {label} (Ollama Cloud telemetry)", ""]

    if not spend.get("ok"):
        lines.append(f"_No spend data: {spend.get('reason', 'unavailable')}_")
        lines.append("")
        return lines

    total = spend.get("total_compute") or 0.0
    lines.append(
        "_Ollama Cloud bills hidden GPU-time (only L1–L4 published). "
        "Below reconstructs real cost from token volume + wall-clock latency._")
    lines.append("")
    lines.append(f"Total compute proxy: **{total/3600:.1f} GPU-h** ({label})")
    lines.append("")

    # Data-driven recommendation callout.
    lines.append("### 🎯 Data-driven picks")
    bd = picks.get("best_daily")
    bs = picks.get("best_subagent")
    hog = picks.get("quota_hog")
    wv = picks.get("worst_value")

    def _io_str(r: dict) -> str:
        io = r.get("io")
        return "inf" if io == float("inf") else f"{io:.0f}"

    if bd:
        lines.append(
            f"• **Best model today**: `{bd['model']}` — "
            f"{bd.get('ops', 0):.0f} out-tok/s, io {_io_str(bd)}, "
            f"med {bd.get('med', 0):.1f}s")
    if bs:
        why = "used as subagent" if bs.get("sub_calls", 0) > 0 else "fast+lean heuristic"
        lines.append(
            f"• **Best subagent**: `{bs['model']}` — "
            f"{bs.get('ops', 0):.0f} out-tok/s ({why})")
    if hog:
        lines.append(
            f"• **Quota hog**: `{hog['model']}` — "
            f"{hog.get('share', 0):.0f}% of compute "
            f"({_fmt_int(hog.get('calls'))} calls)")
    if wv and wv is not bd:
        lines.append(
            f"• **Worst value**: `{wv['model']}` — "
            f"{wv.get('ops', 0):.0f} out-tok/s, io "
            f"{_io_str(wv)} (consider avoiding)")
    lines.append("")

    # Full ranking table.
    ranked = picks.get("ranked") or spend.get("rows", [])
    if ranked:
        lines.append("### Spend ranking (by value score)")
        lines.append("")
        lines.append("| Model | Calls | Sess | in→out tok | io | cmpt_s | out/s | med_s |")
        lines.append("|-------|------:|----:|-----------|---:|-------:|------:|------:|")
        for r in ranked:
            io = _io_str(r)
            io_out = f"{_fmt_int(r['intok'])}→{_fmt_int(r['outtok'])}"
            lines.append(
                f"| `{r['model']}` | {_fmt_int(r['calls'])} | {r.get('sess', 0)} "
                f"| {io_out} | {io} | {r['compute']:.0f} | {r.get('ops', 0):.0f} "
                f"| {r.get('med', 0):.1f} |")
        lines.append("")
        lines.append(
            "_Score = throughput ÷ context-bloat. Higher io = paying to re-read "
            "context. Trim high-compute low-out/s rows first._")
        lines.append("")
    return lines


def render_spend_brief(spend: dict, picks: dict) -> list[str]:
    """Short mobile lines for the Telegram body — the headline answer."""
    if not spend.get("ok"):
        return ["⚙️ Spend: ❌ " + spend.get("reason", "unavailable")]
    lines = ["**Spend picks (real, 24h)**"]
    bd = picks.get("best_daily")
    bs = picks.get("best_subagent")
    hog = picks.get("quota_hog")
    if bd:
        lines.append(
            f"• 🏆 Best today: `{bd['model']}` ({bd.get('ops', 0):.0f} tok/s)")
    if bs:
        lines.append(f"• 🤖 Best subagent: `{bs['model']}`")
    if hog:
        lines.append(
            f"• 🔥 Quota hog: `{hog['model']}` ({hog.get('share', 0):.0f}%)")
    total = spend.get("total_compute") or 0.0
    lines.append(f"• ⏱ {total/3600:.1f} GPU-h burned (24h)")
    return lines


# ─── Budget Optimizer ────────────────────────────────────────────────────────
# Known plan budgets. Copilot and Cursor are flat monthly subs (no per-token
# billing exposed). Ollama Cloud is the only one with live quota + cost data.
# We express all three as a "budget envelope" to reason about remaining runway.

BUDGET_CONFIG = {
    "copilot": {
        "name": "GitHub Copilot",
        "icon": "🐙",
        "type": "monthly_flat",
        "monthly_usd": 100.0,     # Copilot Max ($100/mo)
        # Credit system: 1 AI credit = $0.01 USD  →  token-based per-model pricing
        # Copilot Max: 20,000 included AI credits/mo (base 10K + flex 10K)
        # User spending cap set to 20K credits = $200 total (plan + overage hard cap).
        "included_credits": 20_000,   # credits bundled in plan
        "credit_cap": 20_000,         # hard spending cap (user's GH budget setting)
        "credit_cap_usd": 200.0,      # = credit_cap × $0.01
        "credit_usd_rate": 0.01,      # 1 AI credit = $0.01 USD (fixed by GH)
        # Per-model token pricing ($/M tokens) from:
        # https://docs.github.com/en/copilot/reference/ai-models/models-and-pricing-for-github-copilot
        "model_pricing": {
            # model_id: (in_per_mtok, out_per_mtok)  — used to compute credit burn
            "claude-haiku-4.5":          (1.00,   5.00),
            "claude-sonnet-4":           (3.00,  15.00),
            "claude-sonnet-4.5":         (3.00,  15.00),
            "claude-sonnet-4.6":         (3.00,  15.00),
            "claude-sonnet-5":           (2.00,  10.00),  # promo until 2026-08-31
            "claude-opus-4.5":           (5.00,  25.00),
            "claude-opus-4.6":           (5.00,  25.00),
            "claude-opus-4.7":           (5.00,  25.00),
            "claude-opus-4.8":           (5.00,  25.00),
            "claude-opus-4.8-fast-mode": (10.00, 50.00),
            "claude-opus-5":             (5.00,  25.00),
            "claude-fable-5":            (10.00, 50.00),
            "gpt-5-mini":                (0.25,   2.00),
            "gpt-5.3-codex":             (1.75,  14.00),
            "gpt-5.4":                   (2.50,  15.00),
            "gpt-5.4-mini":              (0.75,   4.50),
            "gpt-5.4-nano":              (0.20,   1.25),
            "gpt-5.5":                   (5.00,  30.00),
            "gpt-5.6-luna":              (0.20,   1.20),
            "gpt-5.6-sol":               (5.00,  30.00),
            "gpt-5.6-terra":             (2.00,  12.00),
            "gemini-3.1-pro":            (2.00,  12.00),
            "gemini-3.5-flash":          (1.50,   9.00),
            "gemini-3.6-flash":          (1.50,   7.50),
            "grok-4.5":                  (2.00,   6.00),
            "kimi-k2.7-code":            (0.95,   4.00),
            "mai-code-1-flash":          (0.75,   4.50),
            "raptor-mini":               (0.25,   2.00),
            "qwen2.5":                   (0.50,   2.00),
        },
        "note": (
            "Copilot Max $100/mo → 20K AI credits included. "
            "1 credit = $0.01 USD. Hard cap: $200/mo (20K credits). "
            "Credit burn computed from MTD token counts × per-model rate."
        ),
    },
    "cursor": {
        "name": "Cursor Agent",
        "icon": "🖱",
        "type": "monthly_flat",
        "monthly_usd": 200.0,     # Business/Max plan ($200/mo) — flat, no per-token metering
        # Fallback API key provisioned separately: additional ~$400/mo budget
        # (pay-as-you-go via OpenRouter or direct provider key wired into Cursor)
        "fallback_budget_usd": 400.0,
        "total_budget_usd": 600.0,  # flat $200 + fallback $400
        "note": (
            "Cursor flat $200/mo (Business). "
            "Fallback API key adds ~$400/mo pay-as-you-go. "
            "Total budget: $600/mo. Per-token cost only hits fallback key."
        ),
    },
    "ollama-cloud": {
        "name": "Ollama Cloud",
        "icon": "☁️",
        "type": "weekly_quota_plus_usd",
        "note": "Weekly request quota (L1–L4 GPU-time buckets) + activity cost",
    },
}

# Normalize provider keys from state.db to budget keys
PROVIDER_BUDGET_MAP = {
    "copilot": "copilot",
    "github-copilot": "copilot",
    "cursor": "cursor",
    "ollama-cloud": "ollama-cloud",
}


def fetch_ollama_budget_data(api_key: str) -> dict:
    """Fetch /api/usage for live weekly quota fraction + 4-week cost."""
    try:
        data = http_json(
            "https://ollama.com/api/usage",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
    except Exception as e:
        return {"ok": False, "reason": str(e)}

    out: dict = {"ok": True}
    weekly = (data.get("limits") or {}).get("weekly") or {}
    out["weekly_usage_frac"] = weekly.get("usage")
    out["weekly_models"] = weekly.get("models") or []
    session = (data.get("limits") or {}).get("session") or {}
    out["session_usage_frac"] = session.get("usage")
    activity = data.get("activity") or {}
    try:
        out["activity_cost_usd"] = float(activity.get("cost") or 0)
    except (TypeError, ValueError):
        out["activity_cost_usd"] = 0.0
    out["activity_period"] = (activity.get("period") or {}).get("type")
    out["activity_starting"] = (activity.get("period") or {}).get("starting_at")
    out["activity_models"] = activity.get("models") or []
    return out


def _budget_query_all_profiles(window_seconds: float) -> dict[str, dict]:
    """Roll up token/call counts by budget_key across all profiles."""
    cutoff = datetime.now().timestamp() - window_seconds
    agg: dict[str, dict] = {}

    dbs: list[tuple[str, Path]] = []
    if STATE_DB.is_file():
        dbs.append(("default", STATE_DB))
    profiles_dir = HERMES_HOME / "profiles"
    if profiles_dir.is_dir():
        for p in sorted(profiles_dir.iterdir()):
            db = p / "state.db"
            if p.is_dir() and db.is_file():
                dbs.append((p.name, db))

    for _profile, db_path in dbs:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
            cur = conn.cursor()
            cols = {r[1] for r in cur.execute("PRAGMA table_info(session_model_usage)").fetchall()}
            has_lastseen = "last_seen" in cols
            has_cache = "cache_read_tokens" in cols
            where = (f"COALESCE(last_seen, first_seen, 0) >= {cutoff}"
                     if has_lastseen else "1=1")
            cache_sel = "SUM(cache_read_tokens)" if has_cache else "0"
            rows = cur.execute(f"""
                SELECT billing_provider, model,
                       SUM(api_call_count), SUM(input_tokens),
                       SUM(output_tokens), {cache_sel}
                FROM session_model_usage
                WHERE {where}
                GROUP BY billing_provider, model
            """).fetchall()
            conn.close()
            for r in rows:
                raw_prov = r[0]
                if isinstance(raw_prov, bytes):
                    raw_prov = raw_prov.decode()
                model = r[1]
                if isinstance(model, bytes):
                    model = model.decode()
                calls, intok, outtok, cache = (
                    r[2] or 0, r[3] or 0, r[4] or 0, r[5] or 0)
                bkey = PROVIDER_BUDGET_MAP.get(raw_prov or "")
                if not bkey:
                    continue
                slot = agg.setdefault(bkey, {
                    "calls": 0, "in_tok": 0, "out_tok": 0,
                    "cache_read": 0, "models": {}
                })
                slot["calls"] += calls
                slot["in_tok"] += intok
                slot["out_tok"] += outtok
                slot["cache_read"] += cache
                m = slot["models"].setdefault(
                    model, {"calls": 0, "in_tok": 0, "out_tok": 0})
                m["calls"] += calls
                m["in_tok"] += intok
                m["out_tok"] += outtok
        except Exception:
            pass
    return agg


def _fmt_mtok(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def analyze_budget(ollama_key: str) -> dict:
    """Pull all data needed for the budget optimizer section."""
    now = datetime.now()
    month_start_ts = datetime(now.year, now.month, 1).timestamp()
    month_elapsed_days = max(
        (now.timestamp() - month_start_ts) / 86400, 0.5)
    days_remaining = max(30 - month_elapsed_days, 0)

    # Week window: last Monday 00:00 local
    weekday = now.weekday()
    week_start_ts = (now.timestamp()
                     - weekday * 86400
                     - (now.hour * 3600 + now.minute * 60 + now.second))
    week_elapsed_days = max((now.timestamp() - week_start_ts) / 86400, 0.25)
    week_days_remaining = max(7 - week_elapsed_days, 0)

    mtd = _budget_query_all_profiles(now.timestamp() - month_start_ts)
    last30 = _budget_query_all_profiles(30 * 86400)

    daily_rate: dict[str, dict] = {
        bkey: {"calls": d["calls"] / 30, "in_tok": d["in_tok"] / 30}
        for bkey, d in last30.items()
    }
    projected_month: dict[str, int] = {
        bkey: int(d["calls"] / 30 * 30)
        for bkey, d in last30.items()
    }

    # ── Copilot credit burn estimate ─────────────────────────────────────────
    # Compute credits burned from MTD token counts × per-model pricing.
    # 1 AI credit = $0.01 USD.  cost_usd = in_tok/1M × in_rate + out_tok/1M × out_rate
    # credits_burned = cost_usd / 0.01
    cop_cfg = BUDGET_CONFIG["copilot"]
    cop_model_pricing = cop_cfg.get("model_pricing", {})
    cop_credit_rate = cop_cfg.get("credit_usd_rate", 0.01)
    cop_mtd = mtd.get("copilot", {})
    cop_credits_burned: float = 0.0
    cop_credits_by_model: dict[str, float] = {}
    for mid, mdata in cop_mtd.get("models", {}).items():
        itok = mdata.get("in_tok", 0)
        otok = mdata.get("out_tok", 0)
        # Normalise: strip version suffix noise (e.g. "claude-opus-4.8" OK)
        rates = cop_model_pricing.get(mid)
        if rates is None:
            # fuzzy: try lower-cased and strip trailing version suffixes
            for k in cop_model_pricing:
                if mid.startswith(k) or k.startswith(mid):
                    rates = cop_model_pricing[k]
                    break
        if rates:
            in_rate, out_rate = rates
        else:
            # Unknown model — use mid-range Sonnet pricing as conservative estimate
            in_rate, out_rate = 3.00, 15.00
        cost_usd = (itok / 1_000_000 * in_rate) + (otok / 1_000_000 * out_rate)
        credits = cost_usd / cop_credit_rate
        cop_credits_burned += credits
        cop_credits_by_model[mid] = credits

    # Project EOM credits based on MTD burn rate
    cop_credit_daily_rate = cop_credits_burned / max(month_elapsed_days, 1)
    cop_credits_projected_eom = cop_credit_daily_rate * 30

    ollama_live: dict = {}
    if ollama_key:
        ollama_live = fetch_ollama_budget_data(ollama_key)

    return {
        "now": now,
        "month_elapsed_days": month_elapsed_days,
        "days_remaining": days_remaining,
        "week_elapsed_days": week_elapsed_days,
        "week_days_remaining": week_days_remaining,
        "mtd": mtd,
        "daily_rate": daily_rate,
        "projected_month": projected_month,
        "ollama_live": ollama_live,
        "cop_credits_burned": cop_credits_burned,
        "cop_credits_by_model": cop_credits_by_model,
        "cop_credit_daily_rate": cop_credit_daily_rate,
        "cop_credits_projected_eom": cop_credits_projected_eom,
    }


def render_budget_section(
    budget: dict,
    ollama_rows: list[dict] | None = None,
    pricing_data: dict | None = None,
) -> list[str]:
    """Full budget optimizer markdown section."""
    now: datetime = budget["now"]
    days_rem = budget["days_remaining"]
    wk_rem = budget["week_days_remaining"]
    mtd = budget["mtd"]
    daily_rate = budget["daily_rate"]
    projected = budget["projected_month"]
    ollama_live = budget["ollama_live"]

    lines = [
        f"## Budget Optimizer — {now.strftime('%Y-%m-%d')}",
        "",
        (f"Month: **{budget['month_elapsed_days']:.1f}d elapsed**, "
         f"~{days_rem:.1f}d remaining  |  "
         f"Week: **{budget['week_elapsed_days']:.1f}d elapsed**, "
         f"~{wk_rem:.1f}d remaining"),
        "",
    ]

    # ── GitHub Copilot ───────────────────────────────────────────────────────
    cop_cfg = BUDGET_CONFIG["copilot"]
    cop_data = mtd.get("copilot", {})
    cop_rate = daily_rate.get("copilot", {})
    cop_proj = projected.get("copilot", 0)
    cop_mtd_calls = cop_data.get("calls", 0)
    cop_mtd_intok = cop_data.get("in_tok", 0)
    cop_mtd_cache = cop_data.get("cache_read", 0)
    cop_daily_calls = cop_rate.get("calls", 0)
    cop_cache_denom = cop_mtd_intok + cop_mtd_cache
    cop_cache_eff = (cop_mtd_cache / cop_cache_denom * 100) if cop_cache_denom else 0.0

    # Credit gauge
    credit_cap = cop_cfg["credit_cap"]           # 20,000
    included = cop_cfg["included_credits"]        # 20,000
    credits_burned = budget.get("cop_credits_burned", 0.0)
    credits_proj_eom = budget.get("cop_credits_projected_eom", 0.0)
    credit_pct = credits_burned / credit_cap * 100 if credit_cap else 0
    proj_pct = credits_proj_eom / credit_cap * 100 if credit_cap else 0
    cost_usd_burned = credits_burned * cop_cfg["credit_usd_rate"]
    cost_usd_proj = credits_proj_eom * cop_cfg["credit_usd_rate"]
    overage_proj = max(credits_proj_eom - included, 0)
    overage_usd = overage_proj * cop_cfg["credit_usd_rate"]

    if proj_pct >= 95:
        credit_alert = f"🔴 CREDIT ALERT: projected {proj_pct:.1f}% EOM ({credit_pct:.1f}% used MTD)"
    elif proj_pct >= 80 or credit_pct >= 70:
        credit_alert = f"🟡 Credit watch: projected {proj_pct:.1f}% EOM ({credit_pct:.1f}% used MTD)"
    else:
        credit_alert = f"🟢 Credit healthy: {credit_pct:.1f}% MTD — projected {proj_pct:.1f}% EOM"

    lines += [
        f"### {cop_cfg['icon']} {cop_cfg['name']} — ${cop_cfg['monthly_usd']:.0f}/mo (Copilot Max)",
        "",
        credit_alert,
        "",
        "| Metric | MTD | Projected EOM |",
        "|--------|-----|---------------|",
        (f"| AI credits burned | {credits_burned:,.0f} / {credit_cap:,} "
         f"({credit_pct:.1f}%) | {credits_proj_eom:,.0f} ({proj_pct:.1f}%) |"),
        (f"| Est. cost (tokens) | ${cost_usd_burned:.2f} | ${cost_usd_proj:.2f} |"),
        (f"| Overage (above {included:,} incl.) | — | "
         f"{overage_proj:,.0f} credits (${overage_usd:.2f}) |"),
        (f"| API calls | {cop_mtd_calls:,} | {cop_proj:,} |"),
        (f"| Input tokens | {_fmt_mtok(cop_mtd_intok)} | — |"),
        (f"| Cache efficiency | {cop_cache_eff:.1f}% | — |"),
        "",
    ]
    # Top models by credit burn
    cop_credits_by_model = budget.get("cop_credits_by_model", {})
    if cop_credits_by_model:
        lines.append("**Top models by credit burn (MTD)**")
        for mid, cr in sorted(cop_credits_by_model.items(), key=lambda x: -x[1])[:5]:
            md = cop_data.get("models", {}).get(mid, {})
            lines.append(
                f"• `{mid}` — {cr:,.0f} credits (${cr * 0.01:.2f})"
                f", {md.get('calls', 0):,} calls"
            )
        lines.append("")
    lines.append(
        f"_1 AI credit = $0.01. Plan includes {included:,} credits/mo. "
        f"Hard cap {credit_cap:,} credits (${credit_cap * 0.01:.0f}). "
        "Cache reuse amortizes long sessions — good for: apollo, reviewer, orchestrator._"
    )
    lines.append("")

    # ── Cursor ───────────────────────────────────────────────────────────────
    cur_cfg = BUDGET_CONFIG["cursor"]
    cur_data = mtd.get("cursor", {})
    cur_rate = daily_rate.get("cursor", {})
    cur_proj = projected.get("cursor", 0)
    cur_mtd_calls = cur_data.get("calls", 0)
    cur_mtd_intok = cur_data.get("in_tok", 0)
    cur_daily_calls = cur_rate.get("calls", 0)

    lines += [
        f"### {cur_cfg['icon']} {cur_cfg['name']} — ${cur_cfg['monthly_usd']:.0f}/mo flat",
        "",
        "| Metric | MTD | Daily avg | Projected EOM |",
        "|--------|-----|-----------|---------------|",
        (f"| API calls | {cur_mtd_calls:,} | {cur_daily_calls:.0f}/d"
         f" | {cur_proj:,} |"),
        (f"| Input tokens | {_fmt_mtok(cur_mtd_intok)}"
         f" | {_fmt_mtok(int(cur_rate.get('in_tok', 0)))} | — |"),
        "| Cache hit tokens | — | — | — |",
        "",
    ]
    cur_models = sorted(
        cur_data.get("models", {}).items(), key=lambda x: -x[1]["calls"])
    if cur_models:
        lines.append("**Top models MTD**")
        for mid, md in cur_models[:5]:
            lines.append(f"• `{mid}` — {md['calls']:,} calls, {_fmt_mtok(md['in_tok'])} in")
        lines.append("")
    cur_unit = cur_cfg["monthly_usd"] / max(cur_proj, 1) * 1000
    lines.append(
        f"_${cur_unit:.2f}/1K calls at current volume. "
        "Projected EOM based on 30d avg (not MTD pace — too early in month). "
        "model=auto picks dynamically — good for: delegation, coding agents, worktrees._")
    lines.append("")

    # ── Ollama Cloud ─────────────────────────────────────────────────────────
    oll_cfg = BUDGET_CONFIG["ollama-cloud"]
    oll_data = mtd.get("ollama-cloud", {})
    oll_rate = daily_rate.get("ollama-cloud", {})
    oll_mtd_calls = oll_data.get("calls", 0)
    oll_daily_calls = oll_rate.get("calls", 0)

    lines.append(
        f"### {oll_cfg['icon']} {oll_cfg['name']} — weekly request quota + activity cost")
    lines.append("")

    act_cost = 0.0
    if ollama_live.get("ok"):
        wk_frac = ollama_live.get("weekly_usage_frac")
        sess_frac = ollama_live.get("session_usage_frac")
        act_cost = ollama_live.get("activity_cost_usd", 0.0)
        act_period = ollama_live.get("activity_period", "4-week")
        wk_models = ollama_live.get("weekly_models") or []
        act_models = ollama_live.get("activity_models") or []

        if wk_frac is not None:
            pct = wk_frac * 100
            expected = (budget["week_elapsed_days"] / 7) * 100
            delta = pct - expected
            pace_icon = ("🔴" if pct > 90 else
                         "🟠" if delta > 10 else
                         "🟡" if delta > 0 else "🟢")
            pace_label = ("CRITICAL" if pct > 90 else
                          "overpacing" if delta > 10 else
                          "ahead" if delta > 0 else "on track")
            lines.append(
                f"**Weekly quota**: {pct:.1f}% used, expected {expected:.1f}% "
                f"— {pace_icon} **{pace_label}**")
            lines.append(
                f"~{wk_rem:.1f}d remaining in week, "
                f"{(1 - wk_frac) * 100:.1f}% quota left")
            lines.append("")

        # Session quota: very short burst window, usually ~0% — skip unless notable
        if sess_frac is not None and sess_frac > 0.30:
            lines.append(f"**Session quota**: {sess_frac * 100:.1f}% used (burst window)")
            lines.append("")

        lines.append(
            f"**Activity cost** ({act_period}): **${act_cost:.2f}**")
        if act_models:
            lines.append("Top by cost:")
            for m in act_models[:5]:
                cost_str = f"${float(m.get('cost', 0)):.2f}" if m.get("cost") else "—"
                lines.append(
                    f"• `{m['name']}` — {m.get('request_count', 0):,} reqs, {cost_str}")
        lines.append("")

        if wk_models:
            lines.append("**Weekly request breakdown**")
            for m in wk_models[:8]:
                lines.append(
                    f"• `{m['name']}` — {m.get('request_count', 0):,} reqs")
            lines.append("")
    else:
        reason = ollama_live.get("reason", "unavailable")
        lines.append(f"_⚠️ Live quota fetch failed: {reason}_")
        lines.append("")

    lines += [
        "| Metric | MTD (telemetry) | Daily avg |",
        "|--------|-----------------|-----------|",
        (f"| API calls | {oll_mtd_calls:,} | {oll_daily_calls:.0f}/d |"),
        (f"| Input tokens | {_fmt_mtok(oll_data.get('in_tok', 0))}"
         f" | {_fmt_mtok(int(oll_rate.get('in_tok', 0)))} |"),
        "",
    ]
    oll_models = sorted(
        oll_data.get("models", {}).items(), key=lambda x: -x[1]["calls"])
    if oll_models:
        lines.append("**Top models MTD (telemetry)**")
        for mid, md in oll_models[:5]:
            lines.append(f"• `{mid}` — {md['calls']:,} calls, {_fmt_mtok(md['in_tok'])} in")
        lines.append("")

    # ── Routing Recommendations ───────────────────────────────────────────────
    lines.append("### 🧭 Routing recommendations")
    lines.append("")
    recs: list[str] = []

    wk_frac = (ollama_live.get("weekly_usage_frac")
               if ollama_live.get("ok") else None)

    # Derive current routing mode from live data
    # Modes:  frugal | conservative | balanced | power | max
    if wk_frac is not None and wk_frac > 0.90:
        routing_mode = "frugal"        # Ollama exhausted, flat subs only
    elif wk_frac is not None and wk_frac > 0.70 and wk_rem < 3:
        routing_mode = "conservative"  # Ollama tight, L1 only
    elif wk_frac is None or wk_frac < 0.40:
        routing_mode = "power"         # Ollama healthy, use freely
    else:
        routing_mode = "balanced"      # Normal

    MODE_LABELS = {
        "frugal":       "🔴 FRUGAL — Ollama quota exhausted",
        "conservative": "🟠 CONSERVATIVE — Ollama quota tight",
        "balanced":     "🟡 BALANCED — normal week",
        "power":        "🟢 POWER — Ollama quota healthy",
    }
    lines.append(f"**Active mode: {MODE_LABELS[routing_mode]}**")
    lines.append("")

    # Per-mode Ollama signal
    if wk_frac is not None:
        if routing_mode == "frugal":
            recs.append(
                "🔴 **Ollama quota critical** — 2.3% left this week. "
                "Stop Ollama L2+ immediately. Move all profiles to Copilot or Cursor.")
        elif routing_mode == "conservative":
            recs.append(
                "🟠 **Ollama quota tight** — only L1 models safe (`gpt-oss:20b`, `gemma4:31b`). "
                "Avoid `minimax-m2.7`, `kimi-*`, `glm-*` until quota resets.")
        elif routing_mode == "power":
            recs.append(
                "🟢 **Ollama quota healthy** — L2/L3 models available this week.")

    # Copilot cache signal
    if cop_cache_eff > 60:
        recs.append(
            f"🟢 **Copilot cache {cop_cache_eff:.0f}%** — long sessions reuse context cheaply. "
            "apollo, reviewer, orchestrator are well-placed here.")
    elif cop_cache_eff < 20 and cop_mtd_calls > 100:
        recs.append(
            f"🟡 **Copilot cache {cop_cache_eff:.0f}%** — short sessions wasting cache. "
            "Pin a shared system-prompt prefix across sessions to warm the cache.")

    # Cost comparison
    if cop_daily_calls > 0 and cur_daily_calls > 0:
        cop_unit_v = cop_cfg["monthly_usd"] / max(cop_proj, 1) * 1000
        cur_unit_v = cur_cfg["monthly_usd"] / max(cur_proj, 1) * 1000
        if cop_unit_v < cur_unit_v:
            recs.append(
                f"📊 **Copilot cheapest/call** (${cop_unit_v:.2f}/1K vs Cursor ${cur_unit_v:.2f}/1K). "
                "Heavy agentic sessions → Copilot. Coding delegation → Cursor.")
        else:
            recs.append(
                f"📊 **Cursor cheapest/call** (${cur_unit_v:.2f}/1K vs Copilot ${cop_unit_v:.2f}/1K). "
                "Heavy agentic sessions → Cursor. Cache-heavy reviews → Copilot.")

    if act_cost > 0:
        monthly_est = act_cost / 4 * 4.33
        act_models_live = ollama_live.get("activity_models") or []
        top2 = act_models_live[:2]
        driver_str = ", ".join(
            f"`{m['name']}` ${float(m.get('cost',0)):.2f}"
            for m in top2 if m.get("cost")
        )
        recs.append(
            f"💰 **Ollama 4-week spend ${act_cost:.2f}** (≈${monthly_est:.2f}/mo). "
            + (f"Cost drivers: {driver_str}. " if driver_str else "")
            + "L1 models near-zero.")

    if not recs:
        recs.append("No anomalies — all providers within normal range.")

    for rec in recs:
        lines.append(f"• {rec}")
    lines.append("")

    # ── Recommended profile configs (per routing mode) ────────────────────────
    lines.append("### 📋 Recommended profile configs")
    lines.append("")
    lines.append(
        "_Paste these into the relevant profile's `config.yaml` under `model:`. "
        "Apply the tier that matches today's active mode above._")
    lines.append("")

    # We always emit all 4 presets so you can compare/apply any of them.
    # Current mode is flagged with ◀ ACTIVE.
    # NOTE: cursor:auto is BANNED — picks $5-10/M models silently (opus/fable).
    # Always pin an explicit model. Fallback $400 key only for deliberate premium use.
    presets = [
        {
            "name": "FRUGAL",
            "icon": "🔴",
            "desc": "Ollama quota exhausted — Cursor flat + Copilot cache sessions only",
            "when": "Ollama weekly quota > 90%",
            "heavy": {   # apollo / orchestrator / reviewer
                "model": "claude-sonnet-4.6",
                "provider": "cursor",
                "fallback": 'fallback_providers: \'[{"provider":"copilot","model":"claude-sonnet-4.6"}]\'',
            },
            "mid": {     # default / specifier / deployer
                "model": "kimi-k2.7-code",
                "provider": "cursor",
                "fallback": 'fallback_providers: \'[{"provider":"copilot","model":"claude-sonnet-4.6"}]\'',
            },
            "light": {   # athena / nihongo-*
                "model": "gpt-5.4-nano",
                "provider": "cursor",
                "fallback": 'fallback_providers: \'[{"provider":"copilot","model":"gpt-5-mini"}]\'',
            },
        },
        {
            "name": "CONSERVATIVE",
            "icon": "🟠",
            "desc": "Ollama tight — L1 only + Cursor pinned fallback",
            "when": "Ollama weekly quota 70–90% and ≤3d remaining",
            "heavy": {
                "model": "gemma4:31b",
                "provider": "ollama-cloud",
                "fallback": 'fallback_providers: \'[{"provider":"cursor","model":"claude-sonnet-4.6"},{"provider":"copilot","model":"claude-sonnet-4.6"}]\'',
            },
            "mid": {
                "model": "gpt-oss:20b",
                "provider": "ollama-cloud",
                "fallback": 'fallback_providers: \'[{"provider":"cursor","model":"kimi-k2.7-code"}]\'',
            },
            "light": {
                "model": "gpt-oss:20b",
                "provider": "ollama-cloud",
                "fallback": 'fallback_providers: \'[{"provider":"cursor","model":"gpt-5.4-nano"}]\'',
            },
        },
        {
            "name": "BALANCED",
            "icon": "🟡",
            "desc": "Normal week — Ollama L1/L2 primary, Cursor flat fallback",
            "when": "Default / mid-week / quota 40–70%",
            "heavy": {
                "model": "deepseek-v4-flash",
                "provider": "ollama-cloud",
                "fallback": 'fallback_providers: \'[{"provider":"cursor","model":"claude-sonnet-4.6"},{"provider":"copilot","model":"claude-sonnet-4.6"}]\'',
            },
            "mid": {
                "model": "gemma4:31b",
                "provider": "ollama-cloud",
                "fallback": 'fallback_providers: \'[{"provider":"cursor","model":"kimi-k2.7-code"}]\'',
            },
            "light": {
                "model": "gpt-oss:20b",
                "provider": "ollama-cloud",
                "fallback": 'fallback_providers: \'[{"provider":"cursor","model":"gpt-5.4-nano"}]\'',
            },
        },
        {
            "name": "POWER",
            "icon": "🟢",
            "desc": "Ollama quota healthy — L2/L3 freely, Cursor pinned for overflow",
            "when": "Ollama weekly quota < 40%",
            "heavy": {
                "model": "kimi-k2.7-code",
                "provider": "ollama-cloud",
                "fallback": 'fallback_providers: \'[{"provider":"ollama-cloud","model":"deepseek-v4-flash"},{"provider":"cursor","model":"claude-sonnet-4.6"}]\'',
            },
            "mid": {
                "model": "deepseek-v4-flash",
                "provider": "ollama-cloud",
                "fallback": 'fallback_providers: \'[{"provider":"ollama-cloud","model":"gemma4:31b"},{"provider":"cursor","model":"kimi-k2.7-code"}]\'',
            },
            "light": {
                "model": "gemma4:31b",
                "provider": "ollama-cloud",
                "fallback": 'fallback_providers: \'[{"provider":"cursor","model":"gpt-5.4-nano"}]\'',
            },
        },
    ]

    tier_profiles = {
        "heavy": "apollo, orchestrator, reviewer",
        "mid":   "default, specifier, deployer, researcher",
        "light": "athena, nihongo-ale, nihongo-jose",
    }

    for preset in presets:
        active_tag = " ◀ **ACTIVE TODAY**" if preset["name"] == routing_mode.upper() else ""
        lines.append(
            f"#### {preset['icon']} {preset['name']}{active_tag}")
        lines.append(f"_When: {preset['when']}_")
        lines.append(f"_{preset['desc']}_")
        lines.append("")
        for tier in ("heavy", "mid", "light"):
            cfg = preset[tier]
            lines.append(
                f"**{tier.capitalize()} profiles** ({tier_profiles[tier]})")
            lines.append("```yaml")
            lines.append("model:")
            lines.append(f"  default: {cfg['model']}")
            lines.append(f"  provider: {cfg['provider']}")
            if cfg.get("fallback"):
                lines.append(cfg["fallback"])
            lines.append("```")
        lines.append("")

    # ── Summary table ─────────────────────────────────────────────────────────
    lines += [
        "### Summary",
        "",
        "| Provider | Plan | MTD calls | Daily rate | Est. value/1K calls |",
        "|----------|------|----------:|----------:|---------------------|",
    ]
    for bkey, cfg in BUDGET_CONFIG.items():
        d = mtd.get(bkey, {})
        calls = d.get("calls", 0)
        dr_calls = daily_rate.get(bkey, {}).get("calls", 0)
        proj = projected.get(bkey, 0)
        if cfg["type"] == "monthly_flat":
            plan = f"${cfg['monthly_usd']:.0f}/mo"
            unit = f"${cfg['monthly_usd'] / max(proj, 1) * 1000:.2f}"
        else:
            plan = "quota+cost"
            unit = f"${act_cost:.2f} (4wk)" if ollama_live.get("ok") else "—"
        lines.append(
            f"| {cfg['icon']} {cfg['name']} | {plan}"
            f" | {calls:,} | {dr_calls:.0f}/d | {unit} |")
    lines.append("")
    lines.append(
        "_Value/1K calls = plan cost ÷ projected monthly volume × 1000. "
        "Lower = better value at current pace. Ollama shows 4-week actual cost._")
    lines.append("")
    return lines


def render_consumption_analysis(budget: dict) -> list[str]:
    """30-day consumption analysis with per-profile tuning table.

    Reads directly from the 30d DB window baked into `budget`.
    Shows: cost by provider, top models by cost, waste signals,
    and a pinned-model tuning table the user can action daily.
    """
    import sqlite3 as _sq
    from pathlib import Path as _P
    from datetime import datetime as _dt

    HERMES_HOME = _P.home() / ".hermes"
    cutoff = _dt.now().timestamp() - 30 * 86400

    PRICING3 = {
        "claude-opus-4.8":       (5.00, 0.50, 25.00),
        "claude-opus-4.6":       (5.00, 0.50, 25.00),
        "claude-opus-4.7":       (5.00, 0.50, 25.00),
        "claude-opus-4.5":       (5.00, 0.50, 25.00),
        "claude-sonnet-4.6":     (3.00, 0.30, 15.00),
        "claude-sonnet-4.5":     (3.00, 0.30, 15.00),
        "claude-haiku-4.5":      (1.00, 0.10,  5.00),
        "gpt-5-mini":            (0.25, 0.025, 2.00),
        "gpt-5.4-nano":          (0.20, 0.02,  1.25),
        "gpt-5.6-luna":          (0.20, 0.02,  1.20),
        "gpt-5.4-mini":          (0.75, 0.075, 4.50),
        "gpt-5.3-codex":         (1.75, 0.175,14.00),
        "gpt-5.1":               (1.25, 0.125,10.00),
        "gemini-3.6-flash":      (1.50, 0.15,  7.50),
        "gemini-3.5-flash":      (1.50, 0.15,  9.00),
        "grok-4.5":              (2.00, 0.20,  6.00),
        "kimi-k2.7-code":        (0.73, 0.073, 3.50),
        "kimi-k2.6":             (0.60, 0.060, 3.41),
        "kimi-k2.5":             (0.57, 0.057, 2.85),
        "kimi-k3":               (3.00, 0.30, 15.00),
        "minimax-m2.7":          (0.20, 0.02,  0.80),
        "minimax-m2.5":          (0.20, 0.02,  0.80),
        "minimax-m3":            (0.80, 0.08,  3.20),
        "deepseek-v4-flash":     (0.14, 0.014, 0.28),
        "deepseek-v4-flash:0731":(0.14, 0.014, 0.28),
        "deepseek-v4-pro":       (3.00, 0.30, 12.00),
        "gemma4:31b":            (0.03, 0.003, 0.12),
        "gpt-oss:20b":           (0.03, 0.003, 0.12),
        "gpt-oss:120b":          (0.20, 0.02,  0.80),
        "qwen3.5:397b":          (0.20, 0.02,  0.80),
        "glm-5.1":               (0.80, 0.08,  3.20),
        "glm-5.2":               (0.80, 0.08,  3.20),
        "hermes-agent":          (0.20, 0.02,  0.80),
        "auto":                  (3.00, 0.30, 15.00),   # conservative estimate for cursor:auto
        "default":               (3.00, 0.30, 15.00),
        "mai-code-1-flash":      (0.75, 0.075, 4.50),
    }

    COPILOT_PROVS = {"copilot", "github-copilot", "moa"}
    CURSOR_PROVS  = {"cursor"}

    MONTHLY_BUDGETS = {
        "copilot":     200.0,
        "cursor_flat": 600.0,   # $200 flat + $400 fallback key
        "ollama_quota": 10.0,
    }

    # Safe cursor pinned models (auto is banned — can hit $5-10/M)
    CURSOR_SAFE = {
        "claude-sonnet-4.6": ("heavy agents, long ctx",   3.00),
        "gpt-5.3-codex":     ("agentic coding, 400K ctx", 1.75),
        "gemini-3.6-flash":  ("research, 1M ctx",         1.50),
        "kimi-k2.7-code":    ("code, tools, <$1/M",       0.95),
        "gpt-5.4-mini":      ("mid general, reasoning",   0.75),
        "gpt-5.4-nano":      ("light/fast, cheapest",     0.20),
    }

    # Collect 30d data across all profiles
    dbs = []
    if (HERMES_HOME / "state.db").is_file():
        dbs.append(("default", HERMES_HOME / "state.db"))
    prof_dir = HERMES_HOME / "profiles"
    if prof_dir.is_dir():
        for p in sorted(prof_dir.iterdir()):
            db = p / "state.db"
            if p.is_dir() and db.is_file():
                dbs.append((p.name, db))

    raw: dict[tuple, dict] = {}
    for profile, db_path in dbs:
        try:
            conn = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            cur = conn.cursor()
            cols = {r[1] for r in cur.execute("PRAGMA table_info(session_model_usage)").fetchall()}
            has_ls = "last_seen" in cols
            has_cache = "cache_read_tokens" in cols
            where = f"COALESCE(last_seen, first_seen, 0) >= {cutoff}" if has_ls else "1=1"
            cache_sel = "SUM(cache_read_tokens)" if has_cache else "0"
            rows = cur.execute(f"""
                SELECT billing_provider, model,
                       SUM(api_call_count), SUM(input_tokens),
                       SUM(output_tokens), {cache_sel}
                FROM session_model_usage WHERE {where}
                GROUP BY billing_provider, model
            """).fetchall()
            conn.close()
            for r in rows:
                prov  = (r[0] or "").decode() if isinstance(r[0], bytes) else (r[0] or "")
                model = (r[1] or "").decode() if isinstance(r[1], bytes) else (r[1] or "")
                e = raw.setdefault((prov, model),
                    {"calls": 0, "in_tok": 0, "out_tok": 0, "cache": 0, "profiles": set()})
                e["calls"]  += r[2] or 0
                e["in_tok"] += r[3] or 0
                e["out_tok"]+= r[4] or 0
                e["cache"]  += r[5] or 0
                e["profiles"].add(profile)
        except Exception:
            pass

    # Compute costs
    summary = []
    for (prov, model), d in raw.items():
        in_m, out_m, cache_m = d["in_tok"]/1e6, d["out_tok"]/1e6, d["cache"]/1e6
        mkey = model.split(":")[0] if ":" in model and model not in PRICING3 else model
        r = PRICING3.get(model) or PRICING3.get(mkey) or (3.00, 0.30, 15.00)
        cost = in_m * r[0] + cache_m * r[1] + out_m * r[2]
        billing = ("copilot" if prov in COPILOT_PROVS
                   else "cursor_flat" if prov in CURSOR_PROVS
                   else "ollama_quota")
        summary.append({
            "prov": prov, "model": model, "calls": d["calls"],
            "in_m": in_m, "out_m": out_m, "cache_m": cache_m,
            "cost": cost, "credits": cost / 0.01,
            "billing": billing, "rate": r[0],
            "profiles": d["profiles"],
        })
    summary.sort(key=lambda x: -x["cost"])

    by_billing: dict[str, dict] = {}
    for s in summary:
        e = by_billing.setdefault(
            s["billing"], {"calls": 0, "cost": 0.0, "credits": 0.0, "in_m": 0.0})
        e["calls"]   += s["calls"]
        e["cost"]    += s["cost"]
        e["credits"] += s["credits"]
        e["in_m"]    += s["in_m"]

    total_value = sum(e["cost"] for e in by_billing.values())
    total_budget = sum(MONTHLY_BUDGETS.values())

    lines: list[str] = []
    lines.append("---")
    lines.append("")
    lines.append("### 📈 30-Day Consumption Analysis")
    lines.append("")
    lines.append("_Token value at market rates — what you'd pay if fully metered._")
    lines.append("")

    # Provider summary table
    lines.append("| Provider | Budget/mo | 30d Token Value | Ratio | Calls |")
    lines.append("|----------|----------:|----------------:|------:|------:|")
    for billing, e in sorted(by_billing.items(), key=lambda x: -x[1]["cost"]):
        budget = MONTHLY_BUDGETS.get(billing, 0)
        ratio = e["cost"] / budget if budget else 0
        flag = "🔴 OVER" if ratio > 1 else ("🟡 OK" if ratio > 0.7 else "🟢 UNDER")
        cr = f" ({e['credits']:,.0f} cr)" if billing == "copilot" else ""
        lines.append(
            f"| {'🐙 Copilot' if billing=='copilot' else '🖱 Cursor' if billing=='cursor_flat' else '☁️ Ollama'}"
            f" | ${budget:,.0f} | ${e['cost']:,.0f}{cr} | {ratio:.1f}x {flag} | {e['calls']:,} |"
        )
    lines.append(
        f"| **Total** | **${total_budget:,.0f}** | **${total_value:,.0f}** "
        f"| **{total_value/total_budget:.1f}x** | **{sum(e['calls'] for e in by_billing.values()):,}** |"
    )
    lines.append("")

    # Top 10 cost drivers
    lines.append("**Top cost drivers (30d)**")
    lines.append("")
    lines.append("| # | Provider | Model | Calls | In M | Cache M | $/M in | Est $ | Credits |")
    lines.append("|---|----------|-------|------:|-----:|--------:|-------:|------:|--------:|")
    for i, s in enumerate(summary[:10], 1):
        cr = f"{s['credits']:,.0f}" if s["billing"] == "copilot" else "—"
        prov_icon = "🐙" if s["billing"] == "copilot" else ("🖱" if s["billing"] == "cursor_flat" else "☁️")
        lines.append(
            f"| {i} | {prov_icon} `{s['prov']}` | `{s['model']}` "
            f"| {s['calls']:,} | {s['in_m']:.1f} | {s['cache_m']:.1f} "
            f"| ${s['rate']:.2f} | ${s['cost']:,.0f} | {cr} |"
        )
    lines.append("")

    # Waste signals
    cop_rows = [s for s in summary if s["billing"] == "copilot"]
    cop_total = sum(s["cost"] for s in cop_rows)
    opus_cost = sum(s["cost"] for s in cop_rows if "opus" in s["model"])
    opus_calls= sum(s["calls"] for s in cop_rows if "opus" in s["model"])
    cursor_rows = [s for s in summary if s["billing"] == "cursor_flat"]
    cursor_auto = next((s for s in cursor_rows if s["model"] == "auto"), None)

    lines.append("**⚠️ Waste signals**")
    lines.append("")
    if opus_cost > 0 and cop_total > 0:
        pct = opus_cost / cop_total * 100
        cop_cap = MONTHLY_BUDGETS["copilot"]
        equiv_months = cop_total / cop_cap
        lines.append(
            f"• **Copilot Opus overuse**: {pct:.0f}% of Copilot spend ({opus_calls:,} calls, "
            f"${opus_cost:,.0f}) was Opus ($5/M). Same volume at Sonnet ($3/M) = "
            f"${opus_cost * 0.6:,.0f} saved. 30d total ${cop_total:,.0f} = "
            f"{equiv_months:.1f}x your ${cop_cap:.0f}/mo cap."
        )
    if cursor_auto:
        lines.append(
            f"• **cursor:auto active** ({cursor_auto['calls']:,} calls, {cursor_auto['in_m']:.0f}M tokens): "
            f"auto can silently pick $5–10/M models. At this volume on Opus = "
            f"${cursor_auto['in_m'] * 5:.0f} from fallback key. **Pin explicit models.**"
        )
    cop_credit_30d = sum(s["credits"] for s in cop_rows)
    cop_monthly_cap_cr = 20000
    if cop_credit_30d > cop_monthly_cap_cr:
        lines.append(
            f"• **Copilot credit overrun**: {cop_credit_30d:,.0f} credits in 30d vs "
            f"{cop_monthly_cap_cr:,} included/mo ({cop_credit_30d/cop_monthly_cap_cr:.1f}x cap). "
            f"Hard cap = $200. Shift volume to Cursor flat to stay within."
        )
    lines.append("")

    # Per-profile tuning table
    lines.append("**🎯 Per-profile tuning recommendations**")
    lines.append("")
    lines.append(
        "_Based on 30d actual usage. `cursor:auto` banned — pins explicit models only. "
        "Fallback $400 key = pay-as-you-go, use only for deliberate premium tasks._"
    )
    lines.append("")
    lines.append("| Profile | Current heavy model | Recommended | Why | Saves |")
    lines.append("|---------|--------------------:|-------------|-----|------:|")

    TUNING = [
        ("apollo",       "copilot:claude-opus-4.8",  "cursor:claude-sonnet-4.6",
         "Flat absorbs 1M ctx; 40% cheaper than opus",   "~$200/mo credits"),
        ("orchestrator", "copilot:claude-opus-4.8",  "cursor:claude-sonnet-4.6",
         "Dispatch doesn't need Opus reasoning",          "~$80/mo credits"),
        ("reviewer",     "copilot:claude-opus-4.6",  "cursor:gpt-5.3-codex",
         "Best agentic code model at $1.75/M",            "~$150/mo credits"),
        ("specifier",    "copilot:claude-sonnet-4.6","cursor:kimi-k2.7-code",
         "Code-focused, tools, 3× cheaper",               "~$40/mo credits"),
        ("default",      "copilot:claude-sonnet-4.6","ollama:gemma4:31b → cursor:kimi-k2.7-code",
         "L1 quota-cheap first, Cursor flat fallback",    "~$60/mo credits"),
        ("researcher",   "ollama:minimax-m2.7",       "ollama:deepseek-v4-flash → cursor:gemini-3.6-flash",
         "Same L2 price, 1M ctx for long docs",           "quota neutral"),
        ("athena",       "custom:minimax-m2.7",       "ollama:gpt-oss:20b → cursor:gpt-5.4-nano",
         "L1 quota-cheap, assistant tasks don't need L2",  "~$20/mo quota"),
        ("nihongo-*",    "ollama:gpt-oss:20b",        "ollama:gpt-oss:20b (keep)",
         "Already optimal for language tutor",            "—"),
    ]
    for t in TUNING:
        lines.append(f"| `{t[0]}` | `{t[1]}` | `{t[2]}` | {t[3]} | {t[4]} |")
    lines.append("")

    # Safe pinned Cursor model reference
    lines.append("**✅ Cursor safe models (pinned, flat plan — no fallback key impact)**")
    lines.append("")
    lines.append("| Model | $/M in | Best for |")
    lines.append("|-------|-------:|----------|")
    for model, (use, price) in sorted(CURSOR_SAFE.items(), key=lambda x: x[1][1]):
        lines.append(f"| `{model}` | ${price:.2f} | {use} |")
    lines.append("")
    lines.append(
        "❌ **Never use** `cursor:auto` — picks $5–10/M models (Opus/Fable) silently. "
        "At 289M tokens/mo that's $1,445–$2,890 from the fallback key."
    )
    lines.append("")
    return lines


def render_master_cost_table(
    ollama_rows: list[dict],
    pricing_data: dict | None = None,
) -> list[str]:
    """Unified model cost/power/benefit ranking across all three providers.

    Columns: Rank | Model | Provider(s) | Ctx | $/M in | $/M out | Tier/cost | Tools | Vision | Thinking
    Sorted by $/M in ascending (cheapest first). Ollama Cloud uses GPU-tier
    proxy cost since they don't expose per-token pricing.

    Ollama GPU tier → estimated $/M in proxy:
      L1 light  → ~$0.03  (quota-metered / cheapest tier)
      L2 mid    → ~$0.20
      L3 high   → ~$0.80
      L4 xheavy → ~$3.00
    These are rough proxies for ranking purposes only — not actual bills.
    """
    pricing_data = pricing_data or {}

    # ── Hardcoded OR pricing for models we know are on OR ────────────────────
    # Format: model_id (short) -> (ctx_k, in_per_mtok, out_per_mtok)
    # Values from live OR API call performed 2026-08-01.
    OR_STATIC: dict[str, tuple[int, float, float]] = {
        # Copilot + Cursor — Anthropic (OR live 2026-08-01 / GH Copilot docs)
        "claude-haiku-4.5":          (200_000,   1.00,   5.00),
        "claude-sonnet-4":           (1_000_000, 3.00,  15.00),
        "claude-sonnet-4.5":         (1_000_000, 3.00,  15.00),
        "claude-sonnet-4.6":         (1_000_000, 3.00,  15.00),
        "claude-sonnet-5":           (1_000_000, 2.00,  10.00),  # Cursor-only promo
        "claude-opus-4.5":           (200_000,   5.00,  25.00),
        "claude-opus-4.6":           (1_000_000, 5.00,  25.00),
        "claude-opus-4.7":           (1_000_000, 5.00,  25.00),
        "claude-opus-4.8":           (1_000_000, 5.00,  25.00),
        "claude-opus-4.8-fast-mode": (1_000_000,10.00,  50.00),
        "claude-opus-5":             (1_000_000, 5.00,  25.00),
        "claude-fable-5":            (1_000_000,10.00,  50.00),
        # OpenAI (OR live 2026-08-01)
        "gpt-5-mini":                (400_000,   0.25,   2.00),
        "gpt-5.1":                   (400_000,   1.25,  10.00),
        "gpt-5.2":                   (400_000,   1.75,  14.00),
        "gpt-5.3-codex":             (400_000,   1.75,  14.00),
        "gpt-5.4":                   (1_050_000, 2.50,  15.00),
        "gpt-5.4-mini":              (400_000,   0.75,   4.50),
        "gpt-5.4-nano":              (400_000,   0.20,   1.25),
        "gpt-5.5":                   (272_000,   5.00,  30.00),
        "gpt-5.5-pro":               (272_000,   5.00,  30.00),
        "gpt-5.6-luna":              (1_050_000, 0.10,   0.60),   # corrected ctx + price
        "gpt-5.6-luna-pro":          (1_050_000, 0.10,   0.60),
        "gpt-5.6-sol":               (1_050_000, 5.00,  30.00),
        "gpt-5.6-sol-pro":           (1_050_000, 5.00,  30.00),
        "gpt-5.6-terra":             (1_050_000, 1.00,   6.00),
        "gpt-5.6-terra-pro":         (1_050_000, 1.00,   6.00),
        "gpt-4.1":                   (1_047_576, 2.00,   8.00),
        # Google (OR live 2026-08-01 — using Cursor-id → OR-id mappings)
        "gemini-2.5-flash":          (1_048_576, 0.30,   2.50),
        "gemini-3-flash":            (1_048_576, 0.50,   3.00),   # gemini-3-flash-preview on OR
        "gemini-3.1-pro":            (1_048_576, 2.00,  12.00),   # gemini-3.1-pro-preview on OR
        "gemini-3.5-flash":          (1_048_576, 1.50,   9.00),
        "gemini-3.6-flash":          (1_048_576, 1.50,   7.50),
        # xAI (OR live)
        "grok-4.5":                  (500_000,   2.00,   6.00),
        # Moonshot AI / Kimi (OR live)
        "kimi-k2.7-code":            (262_144,   0.73,   3.50),
        "kimi-k3":                   (1_048_576, 3.00,  15.00),
        # GLM (OR live)
        "glm-5.2":                   (1_048_576, 0.76,   2.39),
        # DeepSeek (OR live — corrected from $0.20 to $0.14)
        "deepseek-v4-flash":         (1_048_576, 0.14,   0.28),
        "deepseek-v4-flash:0731":    (1_048_576, 0.14,   0.28),
        # Microsoft / GitHub specific
        "mai-code-1-flash":          (200_000,   0.75,   4.50),
        "raptor-mini":               (200_000,   0.25,   2.00),
        "qwen2.5":                   (128_000,   0.50,   2.00),
    }

    # Ollama Cloud GPU-tier → proxy $/M in (for ranking only, not billing)
    OLLAMA_TIER_PROXY: dict[int, float] = {1: 0.03, 2: 0.20, 3: 0.80, 4: 3.00, 99: 5.00}
    OLLAMA_TIER_LABEL: dict[int, str] = {
        1: "L1 ~$0.03", 2: "L2 ~$0.20", 3: "L3 ~$0.80",
        4: "L4 ~$3.00", 99: "L? unknown",
    }

    # Build flat list of all model entries
    entries: list[dict] = []

    # ── Ollama Cloud ──────────────────────────────────────────────────────────
    for r in ollama_rows:
        mid = r.get("id") or ""
        lvl = r.get("usage_level") or 99
        proxy = OLLAMA_TIER_PROXY.get(lvl, 5.0)
        ctx = r.get("context")
        params = r.get("params")
        entries.append({
            "model": mid,
            "providers": ["☁️ Ollama"],
            "ctx": ctx,
            "in_per_mtok": proxy,
            "out_per_mtok": proxy * 4,   # typical 1:4 ratio proxy
            "cost_label": OLLAMA_TIER_LABEL.get(lvl, "L? unknown"),
            "tools": r.get("tools", False),
            "vision": r.get("vision", False),
            "thinking": r.get("thinking") or "no",
            "params": params,
            "billing": "gpu-quota",
        })

    # ── Copilot + Cursor (OR static table) ───────────────────────────────────
    # Gather which models are on which flat-sub provider
    pmc_path = Path.home() / ".hermes" / "provider_models_cache.json"
    try:
        pmc = json.loads(pmc_path.read_text(encoding="utf-8"))
    except Exception:
        pmc = {}

    # Deduplicate: a model can be on both Copilot and Cursor
    model_providers: dict[str, list[str]] = {}
    for mid in pmc.get("copilot", {}).get("models", []):
        mid_norm = mid.strip()
        if mid_norm and mid_norm not in ("auto", "default"):
            model_providers.setdefault(mid_norm, []).append("🐙 Copilot")

    def _cursor_to_dot(m: str) -> str:
        """Convert Cursor dash-version IDs to dot form for dedup.
        claude-sonnet-4-6 → claude-sonnet-4.6
        claude-opus-4-8   → claude-opus-4.8
        Only converts the FINAL numeric segment(s) after the last word part.
        Strategy: find last alpha+digit boundary and dot-ify trailing -N[-N] suffix.
        """
        import re as _re
        # Replace only the very last group(s) of -<digit> at the tail
        return _re.sub(r'(?<=\d)-(\d+)$', r'.\1', m)

    for mid in pmc.get("cursor", {}).get("models", []):
        mid_raw = mid.strip()
        if not mid_raw or mid_raw in ("auto", "default"):
            continue
        # Prefer dot-normalised form; if it already exists (from Copilot), merge
        mid_dot = _cursor_to_dot(mid_raw)
        if mid_dot in model_providers:
            if "🖱 Cursor" not in model_providers[mid_dot]:
                model_providers[mid_dot].append("🖱 Cursor")
        elif mid_raw in model_providers:
            if "🖱 Cursor" not in model_providers[mid_raw]:
                model_providers[mid_raw].append("🖱 Cursor")
        else:
            # New model — store under dot form so OR lookup works
            model_providers[mid_dot] = ["🖱 Cursor"]

    # Resolve pricing for each flat-sub model

    def _cursor_norm(m: str) -> str:
        """claude-sonnet-4-6 → claude-sonnet-4.6 (for OR lookup)"""
        import re as _re
        return _re.sub(r'-(\d+)(?=-\d+|$)', r'.\1', m)

    for mid, providers in model_providers.items():
        if not providers:
            continue
        # Try OR static table with several key forms
        keys = [mid, _cursor_norm(mid), mid.replace("-", ".")]
        info = None
        for k in keys:
            if k in OR_STATIC:
                info = OR_STATIC[k]
                break
        if info is None:
            # Try live pricing_data (already fetched from OR)
            for k in keys:
                p = match_pricing(k, pricing_data)
                if p:
                    ctx_raw = p.get("context_length")
                    try:
                        in_v = float(p["prompt"]) * 1_000_000
                        out_v = float(p["completion"]) * 1_000_000
                    except (TypeError, ValueError):
                        in_v = out_v = 0.0
                    info = (ctx_raw, round(in_v, 4), round(out_v, 4))
                    break
        if info is None:
            # No pricing known — still include with N/A
            entries.append({
                "model": mid,
                "providers": providers,
                "ctx": None,
                "in_per_mtok": None,
                "out_per_mtok": None,
                "cost_label": "flat sub",
                "tools": None,
                "vision": None,
                "thinking": "?",
                "params": None,
                "billing": "flat",
            })
            continue

        ctx_k, in_v, out_v = info
        # Capability hints from model name
        m_lo = mid.lower()
        has_vision = any(x in m_lo for x in ("vision", "gemini", "claude", "gpt-4o", "grok", "sonnet", "opus", "haiku", "fable"))
        has_tools = True  # all listed models support tools
        thinking = "high" if any(x in m_lo for x in ("opus-5","opus-4","fable","gpt-5.4","gpt-5.5","gpt-5.6-sol","grok-4.5")) else \
                   "mid"  if any(x in m_lo for x in ("sonnet","gpt-5.1","gpt-5.2","gpt-5.3","gpt-5.4-mini","codex","gpt-4.1","gemini-3")) else "low"

        entries.append({
            "model": mid,
            "providers": providers,
            "ctx": ctx_k,
            "in_per_mtok": in_v,
            "out_per_mtok": out_v,
            "cost_label": "flat sub",
            "tools": has_tools,
            "vision": has_vision,
            "thinking": thinking,
            "params": None,
            "billing": "flat",
        })

    # ── Sort by in_per_mtok ascending (None / unknown last) ──────────────────
    def sort_key(e: dict) -> float:
        v = e.get("in_per_mtok")
        return v if v is not None else 9999.0

    entries.sort(key=sort_key)

    # ── Render ────────────────────────────────────────────────────────────────
    lines: list[str] = [
        "### 📊 Master model cost / power table",
        "",
        "_All models across all providers, sorted cheapest → most expensive ($/M input tokens)._",
        "_Ollama Cloud costs are GPU-tier proxies for ranking only — not actual billed amounts._",
        "",
        "| # | Model | Provider(s) | Ctx | $/M in | $/M out | 🛠 | 👁 | 🧠 |",
        "|---|-------|-------------|----:|-------:|--------:|:--:|:--:|:--:|",
    ]

    for i, e in enumerate(entries, 1):
        model = e["model"]
        provs = " ".join(e["providers"])
        ctx = format_tokens(e["ctx"]) if e["ctx"] else "?"
        in_v = e["in_per_mtok"]
        out_v = e["out_per_mtok"]

        if in_v is None:
            in_str = "n/a"
            out_str = "n/a"
        elif e["billing"] == "gpu-quota":
            in_str = f"~${in_v:.2f}"
            out_str = f"~${out_v:.2f}"
        else:
            in_str = f"${in_v:.2f}" if in_v >= 0.01 else f"${in_v:.3f}"
            out_str = f"${out_v:.2f}" if out_v >= 0.01 else f"${out_v:.3f}"

        tools_icon = "✅" if e["tools"] else ("—" if e["tools"] is None else "")
        vision_icon = "✅" if e["vision"] else ("—" if e["vision"] is None else "")
        think = e.get("thinking") or "?"
        think_icon = {"high": "🔥", "yes": "🔥", "mid": "💡", "low": "·", "no": "—"}.get(think, think)

        lines.append(
            f"| {i} | `{model}` | {provs} | {ctx} | {in_str} | {out_str}"
            f" | {tools_icon} | {vision_icon} | {think_icon} |"
        )

    lines.append("")
    lines.append(
        "_🧠 thinking: 🔥 high · 💡 mid · · low · — none. "
        "Ollama costs are GPU-tier proxies (L1~$0.03 / L2~$0.20 / L3~$0.80 / L4~$3.00 per M tokens)._"
    )
    lines.append("")
    return lines


def render_budget_brief(budget: dict) -> list[str]:
    """Compact Telegram budget block — fits in ~10 lines."""
    ollama_live = budget.get("ollama_live", {})
    wk_frac = ollama_live.get("weekly_usage_frac") if ollama_live.get("ok") else None
    act_cost = ollama_live.get("activity_cost_usd", 0.0) if ollama_live.get("ok") else 0.0
    credits_burned  = budget.get("cop_credits_burned", 0.0)
    credits_proj    = budget.get("cop_credits_projected_eom", 0.0)
    credit_cap      = 20000
    credit_pct      = credits_burned / credit_cap * 100 if credit_cap else 0
    proj_pct        = credits_proj   / credit_cap * 100 if credit_cap else 0

    # Routing mode
    wk_rem = budget.get("week_days_remaining", 7)
    if wk_frac is not None and wk_frac > 0.90:
        mode = "🔴 FRUGAL"
        mode_action = "Ollama exhausted → Cursor pinned models only"
    elif wk_frac is not None and wk_frac > 0.70 and wk_rem < 3:
        mode = "🟠 CONSERVATIVE"
        mode_action = "Ollama tight → L1 + Cursor fallback"
    elif wk_frac is None or wk_frac < 0.40:
        mode = "🟢 POWER"
        mode_action = "Ollama healthy → L2/L3 freely"
    else:
        mode = "🟡 BALANCED"
        mode_action = "Ollama mid → L1/L2 + Cursor"

    cop_icon = "🔴" if proj_pct > 95 else ("🟡" if proj_pct > 80 else "🟢")
    oll_icon = "🔴" if (wk_frac or 0) > 0.90 else ("🟠" if (wk_frac or 0) > 0.70 else "🟢")

    lines = [
        f"**Mode: {mode}** — {mode_action}",
        "",
        "```",
        f"🐙 Copilot  {credits_burned:>6,.0f}/{credit_cap:,} cr  {cop_icon} proj {proj_pct:.0f}%",
        f"🖱 Cursor   $200 flat + $400 fallback key",
        f"☁️  Ollama   {(wk_frac or 0)*100:.0f}% quota  {oll_icon}  ${act_cost:.2f} (4wk)",
        "```",
    ]
    return lines


def cursor_family_of(mid: str) -> str:
    m = mid.lower()
    if m.startswith("claude"):
        return "claude"
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"):
        return "openai"
    if m.startswith("gemini"):
        return "gemini"
    if "grok" in m:
        return "xai"
    if m.startswith("kimi"):
        return "kimi"
    if m.startswith("glm"):
        return "glm"
    if m.startswith("composer"):
        return "composer"
    return "other"


def fetch_cursor_models() -> tuple[list[str], float | None]:
    """Return Cursor model list.

    Priority:
    1. Live fetch from cursor-go-adapter at :9101/v1/models (available in cron).
    2. provider_models_cache.json (stale fallback from interactive sessions).
    """
    # 1. Live fetch from cursor-go-adapter
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            "http://127.0.0.1:9101/v1/models",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        models_raw = [m["id"] for m in data.get("data", []) if m.get("id")]
        visible = [m for m in models_raw if m not in ("auto", "default")]
        if visible:
            return visible, 0.0  # age=0 = fresh
    except Exception:
        pass  # fall through to cache

    # 2. Cache fallback
    if not PROVIDER_MODELS_CACHE.is_file():
        return [], None
    try:
        data = json.loads(PROVIDER_MODELS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return [], None
    cursor = data.get("cursor", {})
    models = cursor.get("models", [])
    at = cursor.get("at")
    age_h = (datetime.now().timestamp() - at) / 3600 if at else None
    visible = [m for m in models if m not in ("auto", "default")]
    return visible, age_h


def render_cursor_section(
    models: list[str],
    age_h: float | None,
    used_ids: set[str],
    pricing_data: dict | None = None,
) -> list[str]:
    """Render Cursor model catalog grouped by family with ctx + price cross-ref."""
    if not models:
        return ["## Cursor", "❌ no models in cache", ""]

    pricing_data = pricing_data or {}
    age_str = f" — cache {age_h:.1f}h old" if age_h is not None else ""
    lines = [
        f"## Cursor — {len(models)} models{age_str}",
        "_Model list from provider_models_cache (refreshed each interactive session)._",
        "_ctx + price = OpenRouter cross-ref (indicative, not Cursor bill)._",
        "",
    ]

    by_fam: dict[str, list[str]] = {}
    for mid in sorted(models):
        fam = cursor_family_of(mid)
        by_fam.setdefault(fam, []).append(mid)

    for fam in CURSOR_FAMILY_ORDER:
        group = by_fam.get(fam) or []
        if not group:
            continue
        lines.append(f"**{fam}** ({len(group)})")
        for mid in group:
            star = " ★" if mid in used_ids else ""
            # Cursor model IDs use all-dashes (claude-sonnet-4-6).
            # OR uses provider/model-name with dots for version (anthropic/claude-sonnet-4.6).
            # Strategy: replace last hyphen-separated numeric segment(s) with dots,
            # e.g. claude-sonnet-4-6 → claude-sonnet-4.6, claude-opus-4-8 → claude-opus-4.8
            import re as _re
            def _cursor_to_or(m: str) -> str:
                # Replace trailing -N or -N-N patterns with .N or .N.N
                return _re.sub(r'-(\d+)(?=-\d+|$)', r'.\1', m)
            mid_or = _cursor_to_or(mid)
            pricing = (match_pricing(mid, pricing_data)
                       or match_pricing(mid_or, pricing_data)
                       or match_pricing(mid.replace("-", "."), pricing_data))
            if pricing:
                ctx_raw = pricing.get("context_length")
                ctx = format_tokens(ctx_raw) if ctx_raw else "?"
                p_in = price_per_mtok(pricing["prompt"])
                p_out = price_per_mtok(pricing["completion"])
                lines.append(f"• `{mid}`{star} — ctx {ctx}, {p_in}/{p_out}")
            else:
                lines.append(f"• `{mid}`{star}")
        lines.append("")

    lines.append("_★ = wired in a profile · price = $/M in / $/M out_")
    lines.append("")
    return lines


def emit_tier_map(ollama_rows: list[dict]) -> None:
    """Write a compact per-model capability/tier map for the quota watchdog.

    The watchdog uses this to recommend a cheaper substitute model (lowest
    usage_level that still satisfies tools/vision/thinking needs) when quota is
    overpacing — without re-fetching or re-classifying anything itself.
    """
    out = {}
    for r in ollama_rows:
        mid = r.get("id")
        if not mid:
            continue
        out[mid] = {
            "usage_level": r.get("usage_level") or 99,
            "params": r.get("params"),
            "tools": bool(r.get("tools")),
            "vision": bool(r.get("vision")),
            "thinking": r.get("thinking") or None,
        }
    try:
        TIER_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        TIER_MAP_PATH.write_text(
            json.dumps({"generated": datetime.now().isoformat(), "models": out}),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"⚠️  tier-map write failed: {e}", file=sys.stderr)


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M CST")
    full: list[str] = []
    full.append(f"# Hermes Models Report — {now}")
    full.append("")

    profiles = load_profile_usage()
    used_ids = collect_used_model_ids(profiles)

    # ── Fetch all data ────────────────────────────────────────────────────────
    spend = analyze_spend(window_days=1)
    spend_picks = pick_best_models(spend)
    ollama_key = get_ollama_api_key()
    ollama_ok = False
    ollama_rows: list[dict] = []
    if ollama_key:
        ids = fetch_ollama_cloud_ids(ollama_key)
        if ids:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                futs = {
                    pool.submit(enrich_ollama_model, ollama_key, mid): mid
                    for mid in ids
                }
                for fut in concurrent.futures.as_completed(futs):
                    try:
                        ollama_rows.append(fut.result())
                    except Exception as e:
                        mid = futs[fut]
                        print(f"⚠️  enrich failed for {mid}: {e}", file=sys.stderr)
            ollama_ok = True
            emit_tier_map(ollama_rows)

    pricing_data = fetch_openrouter_pricing()
    token = get_gh_token()
    copilot_ok = False
    copilot_models: list[dict] = []
    if token:
        copilot_models = fetch_copilot_models(token)
        copilot_ok = bool(copilot_models)

    cursor_models, cursor_cache_age = fetch_cursor_models()
    cursor_ok = bool(cursor_models)

    budget = analyze_budget(ollama_key)

    # ── .md file: action-first structure ─────────────────────────────────────
    # Section order: header → budget optimizer → 30d analysis → active spend →
    #   profile wiring → routing presets → master cost table → catalogs
    full: list[str] = []
    full.append(f"# Hermes Models Report — {now}")
    full.append("")

    # Budget + routing (most actionable — top of file)
    full.extend(render_budget_section(budget, ollama_rows, pricing_data))

    # 30d consumption analysis + per-profile tuning
    full.extend(render_consumption_analysis(budget))

    # Today's actual spend picks
    full.extend(render_spend_section(spend, spend_picks))

    # Active profile wiring
    full.extend(render_in_use(profiles))

    # ── Catalogs (reference — open the HTML for these) ────────────────────────
    if not ollama_key:
        full.append("## Ollama Cloud")
        full.append("❌ no `OLLAMA_API_KEY`")
        full.append("")
    elif not ollama_rows:
        full.append("## Ollama Cloud")
        full.append("❌ no models returned from `/v1/models`")
        full.append("")
    else:
        full.extend(render_picks(ollama_rows))
        full.extend(render_comparison_table(ollama_rows))
        full.extend(render_ollama_section(ollama_rows, used_ids))

    if not token:
        full.append("## GitHub Copilot")
        full.append("❌ no token (`gh auth token` failed)")
        full.append("")
    elif not copilot_models:
        full.append("## GitHub Copilot")
        full.append("❌ no models returned")
        full.append("")
    else:
        full.extend(render_copilot_section(copilot_models, pricing_data, used_ids))

    if not cursor_ok:
        full.append("## Cursor")
        full.append("❌ no models in provider_models_cache")
        full.append("")
    else:
        full.extend(render_cursor_section(cursor_models, cursor_cache_age, used_ids, pricing_data))

    # Master cost table last (big reference table)
    full.extend(render_master_cost_table(ollama_rows or [], pricing_data))

    full_md = "\n".join(full).rstrip() + "\n"
    md_path, html_path = write_report_files(full_md, now)

    brief = render_telegram_brief(
        now, profiles, ollama_rows, copilot_models,
        cursor_models, cursor_cache_age,
        ollama_ok, copilot_ok, cursor_ok,
        budget=budget, spend=spend, spend_picks=spend_picks,
    )
    # MEDIA lines: Hermes gateway uploads these as native Telegram documents
    brief.append(f"MEDIA:{html_path}")
    brief.append(f"MEDIA:{md_path}")

    print("\n".join(brief).rstrip() + "\n")

    if not copilot_ok and not ollama_ok and not cursor_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
