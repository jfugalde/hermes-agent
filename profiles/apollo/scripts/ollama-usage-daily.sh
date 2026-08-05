#!/usr/bin/env bash
# ollama-usage-daily.sh — Daily provider usage report for Hermes Apollo profile
# Outputs token usage and estimated cost for the last 24 hours using
# a standard metric: USD per 1M tokens.

set -euo pipefail

PROFILE="apollo"
STATE_DB="/home/vivojf/.hermes/profiles/$PROFILE/state.db"

if [[ ! -f "$STATE_DB" ]]; then
  echo "⚠️ Ollama usage report: state.db not found for profile $PROFILE"
  exit 0
fi

# Use sqlite3 via python3 (since sqlite3 CLI may not be installed)
export PROFILE
export STATE_DB
python3 <<'EOF'
import sqlite3
import time
import os

profile = os.environ.get("PROFILE", "apollo")
state_db = os.environ.get("STATE_DB", "")

# Standard metric: USD per 1M input/output tokens.
# Provider defaults keep cross-provider reporting consistent.
MODEL_PRICING_USD_PER_M = {
    "gemma4:31b": (0.03, 0.12),
    "gpt-oss:20b": (0.03, 0.12),
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-flash:0731": (0.09, 0.18),  # pinned variant is CHEAPER than bare
    "kimi-k2.7-code": (0.73, 3.50),
    "nemotron-3-super": (0.20, 0.80),
}
PROVIDER_DEFAULT_PRICING_USD_PER_M = {
    "ollama-cloud": (0.20, 0.80),
    "openai": (5.00, 15.00),
    "anthropic": (3.00, 15.00),
    "google": (1.25, 5.00),
    "xai": (5.00, 15.00),
    "unknown": (0.20, 0.80),
}

def pricing_for(provider: str, model: str):
    provider = (provider or "unknown").strip().lower()
    model = (model or "").strip()
    if model in MODEL_PRICING_USD_PER_M:
        return MODEL_PRICING_USD_PER_M[model]
    # Prefix fallback for model variants, e.g. deepseek-v4-flash:xxxx
    for base, rates in MODEL_PRICING_USD_PER_M.items():
        if model.startswith(base + ":"):
            return rates
    return PROVIDER_DEFAULT_PRICING_USD_PER_M.get(provider, PROVIDER_DEFAULT_PRICING_USD_PER_M["unknown"])

def estimate_cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = pricing_for(provider, model)
    return ((input_tokens / 1_000_000.0) * in_rate) + ((output_tokens / 1_000_000.0) * out_rate)

if not os.path.exists(state_db):
    print("⚠️ Ollama usage report: state.db not found for profile {}".format(profile))
    exit(0)

conn = sqlite3.connect(state_db)
cursor = conn.cursor()

# Query usage in the last 24 hours across all providers
now = time.time()
day_ago = now - (24 * 3600)

cursor.execute("""
    SELECT 
        COALESCE(NULLIF(billing_provider, ''), 'unknown') as billing_provider,
        model,
        SUM(api_call_count) as calls,
        SUM(input_tokens) as input_tokens,
        SUM(output_tokens) as output_tokens,
        SUM(estimated_cost_usd) as est_cost
    FROM session_model_usage
    WHERE last_seen > ?
    GROUP BY billing_provider, model
    ORDER BY billing_provider, model
""", (day_ago,))

rows = cursor.fetchall()

if not rows:
    print("ℹ️ Provider usage (last 24h): no activity")
    conn.close()
    exit(0)

print("📊 Provider usage (last 24h, standard metric: USD per 1M tokens):")
total_calls = 0
total_input = 0
total_output = 0
total_est_cost = 0.0
for provider, model, calls, inp, out, cost in rows:
    provider = provider or "unknown"
    inp = int(inp or 0)
    out = int(out or 0)
    db_cost = float(cost or 0.0)
    computed_cost = estimate_cost_usd(provider, model, inp, out)
    final_cost = db_cost if db_cost > 0 else computed_cost
    in_rate, out_rate = pricing_for(provider, model)
    print("  [{}] {}: {} calls, {} in, {} out, rate ${:.2f}/${:.2f} per 1M (in/out), est. cost ${:.4f}".format(
        provider, model, calls, inp, out, in_rate, out_rate, final_cost))
    total_calls += calls
    total_input += inp
    total_output += out
    total_est_cost += final_cost

print("  TOTAL: {} calls, {} in, {} out, est. cost ${:.4f}".format(
    total_calls, total_input, total_output, total_est_cost))

conn.close()
EOF