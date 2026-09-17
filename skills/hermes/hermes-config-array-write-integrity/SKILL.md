---
name: hermes-config-array-write-integrity
description: Use when a Hermes config array write silently no-ops.
version: 1.0.0
platforms: [linux]
tags: [hermes, config, cli, yaml, models, migration, ollama]
triggers:
  - Setting a nested array value in Hermes config via the CLI
  - Migrating models across hermes homes / profiles (retirement, deprecation, allowlist)
  - A config change reported success but the value did not change
  - Auditing the ollama-cloud model allowlist against the live catalog
  - Delegating config writes to a subagent and needing to verify them
related_skills: [hermes-model-routing, ollama-homelab, commit-around-foreign-wip]
---

# Hermes config array writes — silent corruption and verification

Use when writing Hermes config arrays via the CLI (model allowlists, MOA
`reference_models`, fallback chains) or bulk-migrating models across profiles.

## The pitfall (reproduced, not theorized)

`hermes config set` **exits 0 and reports success while corrupting the file** when
the path uses bracket indexing. Run in a throwaway `HERMES_HOME`:

```yaml
# baseline
moa:
  reference_models:
    - model: old-a
    - model: old-b
```

```bash
HERMES_HOME=/tmp/cfgtest hermes config set 'moa.reference_models[0].model' new-a
# exit=0  ← looks fine
```

Resulting file:

```yaml
moa:
  reference_models:
    - model: old-a        # ← UNCHANGED
    - model: old-b        # ← UNCHANGED
  reference_models[0]:    # ← PHANTOM KEY, not part of the array
    model: new-a
```

Verified 2026-09-17 against the installed CLI. The root cause is `_set_nested` in
`hermes_cli/config.py`: it splits the path on `.` and treats a segment as a list
index only when the segment is **numeric**. The literal string `reference_models[0]`
is never numeric, so it becomes a new *dict key* that happens to look like an index.
Nothing errors, because writing a scalar key is perfectly valid YAML.

**Consequences are silent and severe:**
- The array keeps every old value, so a "completed" model migration still runs the retired model.
- The config now carries junk keys (`reference_models[0]`) that no schema knows about.
- A subagent performing the writes reports success for each command — the exit codes are genuinely 0.

## Correct syntax

Use **dot-separated numeric** segments for list positions:

```bash
hermes config set moa.reference_models.1.model new-b     # OK — indexes element 1
hermes config set moa.reference_models.0.model new-a     # OK
```

Rule: `a.0.b`, never `a[0].b`. If a key legitimately contains `[`/`]`, you cannot
target it by path — edit the file (after backing it up) or use a script.

## Mandatory verification after ANY config write

An exit code of 0 is **not** evidence the value changed. Read it back and parse it:

```bash
# 1. Read back the effective value through the CLI
HERMES_HOME=<home> hermes config get <exact.path>

# 2. Parse the FILE — catches phantom keys the CLI happily ignores
grep -rn '\[[0-9]\]' "$HERMES_HOME/config.yaml"        # any hit = corruption present
python3 -c "import yaml,sys; yaml.safe_load(open('$HERMES_HOME/config.yaml'))"  # must parse

# 3. Assert the OLD value is gone, not just that the command ran
grep -c '<retired-model-string>' "$HERMES_HOME/config.yaml"   # expect 0
```

Removing a phantom key: `hermes config unset '<key-with-brackets>'` works, because
`unset` deletes by literal key. Verify the file parses afterwards.

## Model-retirement migration recipe (Ollama Cloud changes models ~quarterly)

When a provider retires models, the blast radius is bigger than "the default":
check defaults, tier models, MOA/fallback arrays, delegation models, **and** the
provider allowlist. Work per hermes home (`~/.hermes` plus each `profiles/<name>/`).

```bash
# 1. Enumerate every hermes home and every reference to a retired model
for h in ~/.hermes ~/.hermes/profiles/*/; do
  [ -f "$h/config.yaml" ] || continue
  echo "== $h"
  grep -nE '<retired-1>|<retired-2>' "$h/config.yaml"
done
```

```bash
# 2. Diff the allowlist against the LIVE catalog — do not trust the checked-in list.
#    /v1/models is the endpoint Hermes itself uses and is the authority for availability.
curl -s https://ollama.com/v1/models -H "Authorization: Bearer $OLLAMA_API_KEY" \
  | python3 -c "import json,sys; print('\n'.join(sorted(m['id'] for m in json.load(sys.stdin)['data'])))"
```

Rebuild the allowlist as a **sorted set** from the live catalog rather than
hand-editing entries — that removes dead entries *and* adds missing ones. Preserve
per-model metadata (`context_length`) for entries that carry it.

```bash
# 3. Write with the CLI and verify each one (step 2 of the verification block)
HERMES_HOME=<home> hermes config set model.default <new-model>
HERMES_HOME=<home> hermes config set model_tiers.mid.model <new-model>
```

```bash
# 4. Refresh model caches so pickers stop offering retired models.
#    Caches are per-home JSON; rewrite 'models' + 'cached_at' from the live list.
find ~/.hermes -name ollama_cloud_models_cache.json
```

**Prefer the live catalog over an email/list.** In the Sept 2026 Ollama retirement
(`deepseek-v4-flash:0731`, `glm-5.1`, `qwen3.5:397b` → `deepseek-v4.1-flash`,
`glm-5.3`, `glm-5.3-flash`) the allowlist had drifted independently of the
announced retirements: it listed entries the catalog no longer served and was
missing newer ones. Only the live diff reveals both directions.

## No gateway restart is needed for config changes

Hermes re-reads config per session. Config loading is mtime-keyed
(`read_raw_config`, `hermes_cli/config.py`), so a running gateway picks up edits on
the next read. Prove it instead of restarting: make one call and check the model
the session recorded.

```bash
python3 -c "
import sqlite3,datetime
c=sqlite3.connect('$HOME/.hermes/state.db')
for m,p,ts in c.execute('''select model,billing_provider,max(last_seen) from session_model_usage
                           group by model order by max(last_seen) desc limit 3'''):
    print(datetime.datetime.fromtimestamp(ts).strftime('%H:%M:%S'), m, p)
"
```

## Delegating config writes — verify, never trust the report

Config writes are mechanical, which makes them tempting to delegate. But a subagent
given `a[0].b` will produce a fully self-consistent success report for a file it
corrupted, because every command really did exit 0. If you delegate:

1. Give the exact command syntax (dot-numeric) in the brief, not a description of the goal.
2. On return, diff the config and parse it yourself — grep for `[0-9]` phantom keys.
3. Treat "N changes applied" as a claim, not an outcome.

Cheap and decisive: rehearse against a throwaway `HERMES_HOME` first.
```bash
mkdir -p /tmp/cfgtest && cp ~/.hermes/config.yaml /tmp/cfgtest/config.yaml
HERMES_HOME=/tmp/cfgtest hermes config set <path> <value>
diff /tmp/cfgtest/config.yaml ~/.hermes/config.yaml   # see exactly what would change
```

## Pitfalls

- `a[0].b` returns exit 0 and corrupts — the single most expensive trap here.
- Do not `grep` only the *new* value to confirm success; assert the *old* value is absent.
- `model.default` in a profile's config overrides the global one; migrate both.
- Provider-scoped names collide: `perplexity/deepseek-v4-flash-0731` and OpenRouter's
  `deepseek/deepseek-v4-flash` are **different providers** and are not affected by an
  Ollama Cloud retirement. Scope the grep by the `ollama-cloud` provider block.
- Back up each config before writing (`*.pre-<change>-<timestamp>`), and confirm the
  file still parses after every batch.
- Some entries use `- id: x` + `context_length:` mapping form rather than a bare
  string; a naive list rewrite drops their metadata.
