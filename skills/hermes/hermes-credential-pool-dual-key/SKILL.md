---
name: hermes-credential-pool-dual-key
description: "Use when enabling/verifying same-provider key failover."
version: 1.0.0
category: homelab
tags: [hermes, credential-pool, failover, ollama-cloud, api-key, dual-key]
trigger: |
  User wants a secondary/fallback key for the same provider (e.g. two Ollama
  Cloud accounts) so quota exhaustion on one auto-rotates to the other, or asks
  to verify a provider's key pool is wired into the Hermes runtime.
---

# Hermes Credential Pool — Same-Provider Key Failover

Hermes has a **native credential pool** (`agent/credential_pool.py`) for
same-provider failover across multiple API keys. This is DIFFERENT from
`fallback_providers` (which is a cross-provider chain). The pool is the right
mechanism for "when key A is depleted, use key B" on the SAME provider.

## When to use

- User wants a secondary/fallback key for the same provider (e.g. two Ollama
  Cloud accounts) so quota exhaustion on one auto-rotates to the other.
- You need to verify a provider's key pool is actually wired into the runtime.

## How it works

- Pool entries live in `auth.json` under `credential_pool.<provider>[]`.
- Each entry: `label`, `auth_type`, `priority`, `source` (e.g. `env:OLLAMA_API_KEY`),
  `last_status`, `last_error_code`, `base_url`.
- Selection strategy per provider via `credential_pool_strategies` in
  config.yaml. Default = `fill_first` (use priority 0 until exhausted, then
  rotate). Others: `round_robin`, `random`, `least_used`.
- On **429 (rate-limit/quota)** or **402 (billing)**, the pool marks the current
  entry `exhausted` and rotates to the next. Cooldown TTL: 401 → 5 min,
  429/402 → 1 hour.
- Runtime resolution: `resolve_runtime_provider` uses the pool for any provider
  that is NOT `openrouter` (`should_use_pool = provider != "openrouter"`).
  `agent._credential_pool` is seeded from the pool and
  `recover_with_credential_pool` handles the 429/402 rotation reactively.

## Verify the pool is active (not just present in auth.json)

```bash
# Repo root IS ~/.hermes (NOT ~/.hermes/hermes-agent), and the venv is .venv.
cd /home/vivojf/.hermes
./.venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from agent.credential_pool import load_pool
pool = load_pool('ollama-cloud')
print('provider:', getattr(pool,'provider',None))
print('has_credentials:', pool.has_credentials() if pool else None)
print('strategy:', getattr(pool,'_strategy',None))
print('num entries:', len(pool._entries) if pool else 0)
"
```

Expected for a working dual-key setup:
```
provider: ollama-cloud
has_credentials: True
strategy: fill_first
num entries: 2
```

## Inspect pool entries

```bash
cd /home/vivojf/.hermes/profiles/apollo
hermes auth list | grep -A4 "ollama-cloud"
# or raw:
python3 -c "
import json
d = json.load(open('auth.json'))
for e in d.get('credential_pool',{}).get('ollama-cloud',[]):
    print(f\"{e['label']}: priority={e['priority']} status={e.get('last_status')} err={e.get('last_error_code')} src={e.get('source')}\")
"
```

## Key facts / pitfalls

- **Env-seeded pool entries** (`source: env:OLLAMA_API_KEY`) are auto-created
  from `.env` vars. The `.env` already carries `OLLAMA_API_KEY` (primary) and
  `OLLAMA_API_KEY_FALLBACK` (secondary) — both must be non-empty for the pool
  to have 2 usable entries.
- **The pool shadows the plain env var** for non-openrouter providers. If the
  pool has entries, the runtime uses the pool, not the raw `key_env` value.
- **`hermes auth status <provider>` may report "logged out"** even when the
  pool is healthy — that status reflects OAuth/singleton auth, not the pool.
  Don't trust it as a health signal; use `load_pool` / `auth list` instead.
- **No config change needed** for a working dual-key setup — if `auth list`
  shows 2 entries and `load_pool` returns `has_credentials: True`, the
  failover is already live. Don't add `credential_pool_strategies` unless you
  want a non-default strategy.
- **429 cooldown is 1 hour** — after the primary is marked exhausted, it stays
  out of rotation for an hour even if quota resets sooner. This is by design.
- **`credential_lifecycle.py` prunes env-seeded pool entries** when the
  corresponding `.env` var is removed/changed — so keep both keys in `.env` or
  the pool silently drops to one entry.
- **Use the venv python** (`./.venv/bin/python` from `~/.hermes`) for `load_pool` checks — the
  system python3 lacks `httpx` and other Hermes deps.
