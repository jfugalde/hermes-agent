# Semantic Response Cache Gateway Integration

## Goal

Wire the existing `clawmem_semantic_cache.py` module into the Hermes gateway so
low-stakes, repetitive Q&A can be answered from a local Ollama + SQLite semantic
cache instead of invoking the agent/LLM.

## Chosen Integration Point: `/cacheask` command hook

The safest place to add a short-circuit is the existing command-hook mechanism
in `gateway/run.py` (around line 11806). It already supports `emit_collect` on
`command:<canonical>` and honors a returned dict with
`{"decision": "handled", "message": "..."}` by returning the message directly,
without ever calling the agent.

**Why this is the safest option:**

- No changes to `gateway/run.py` or any core message-processing path.
- No risk of breaking tool calling, session persistence, or streaming delivery.
- The cache is opt-in for the user (they must type `/cacheask <question>`).
- Hook errors are caught and logged by `HookRegistry`; they never block the
  pipeline.

## Files Created/Changed

| File | Action | Purpose |
|------|--------|---------|
| `~/.hermes/scripts/clawmem_semantic_cache.py` | Modified | Added a read-only `get(prompt)` method so hooks can query without side effects. |
| `~/.hermes/hooks/semantic-cache/HOOK.yaml` | Created | Hook metadata: registers `command:cacheask`. |
| `~/.hermes/hooks/semantic-cache/handler.py` | Created | Hook handler: parses args, queries the cache, returns the cached answer or a "no hit" message. |
| `~/.hermes/plans/semantic-cache-gateway-integration.md` | Created | This design doc and test/verification notes. |

## How the Hook Works

1. The user types `/cacheask What is the default Hermes model?`.
2. The gateway fires `command:cacheask` via `emit_collect` in `gateway/run.py`.
3. `handler.py` extracts the question text from the hook context (`args` / `raw_args`).
4. It calls `SemanticCache().get(question)`, which normalizes the prompt, embeds
   it via local Ollama, and scans the SQLite cache for a cosine-similar hit
   above the configured threshold (default 0.94).
5. If a hit is found, the handler returns `{"decision": "handled", "message": "*[cached]*\n\n<response>"}`.
6. If no hit is found, it returns a friendly "no cached answer" message.

## How to Test or Verify

### 1. Load the cache module

```bash
python3 -c "import sys; sys.path.insert(0, '/home/vivojf/.hermes/scripts'); from clawmem_semantic_cache import SemanticCache; print('OK')"
```

### 2. Seed a test entry

```bash
python3 - <<'PY'
import sys
sys.path.insert(0, '/home/vivojf/.hermes/scripts')
from clawmem_semantic_cache import SemanticCache

cache = SemanticCache()
answer = cache.get_or_call(
    "What is the default Hermes model?",
    lambda p: "The default Hermes model is gemma4:31b via Ollama Cloud.",
    ttl=3600,
)
print("Seeded:", answer)
print("Cached get:", cache.get("What is the default Hermes model?"))
print("Semantic get:", cache.get("tell me the default Hermes model"))
PY
```

### 3. Test the hook handler directly

```bash
python3 - <<'PY'
import sys
sys.path.insert(0, '/home/vivojf/.hermes/scripts')
sys.path.insert(0, '/home/vivojf/.hermes/hooks/semantic-cache')
import handler

print(handler.handle("command:cacheask", {"args": ""}))
print(handler.handle("command:cacheask", {"args": "What is the default Hermes model?"}))
print(handler.handle("command:cacheask", {"args": "something not cached"}))
PY
```

### 4. Verify the gateway discovers the hook

Restart the Hermes gateway and watch the logs for:

```
[hooks] Loaded hook 'semantic-cache' for events: ['command:cacheask']
```

### 5. End-to-end smoke test

In a chat session, type:

```
/cacheask What is the default Hermes model?
```

If the cache was seeded, the gateway should return the cached answer without
any LLM latency.

## Future Work: Automatic Plain-Chat Short-Circuit

To answer common questions automatically (without the user typing `/cacheask`),
the gateway would need a new `agent:before` decision hook emitted right before
`gateway/run.py` calls `_run_agent` (around line 14154). The hook would need to:

1. Return a `{"decision": "handled", "message": "..."}` dict to skip the agent.
2. Be gated by a feature flag (e.g. `gateway.semantic_cache.enabled` or
   `HERMES_SEMANTIC_CACHE_AUTO=1`) so it is off by default.
3. Safely persist the user message and the assistant response to the session
   transcript, or skip persistence entirely and rely on the adapter to send the
   message. **This is the main blocker** — `gateway/run.py` currently persists
   transcript rows inside / after `_run_agent`, so a clean short-circuit would
   require reusing that persistence logic or adding a dedicated write path.

Because of the session-persistence complexity, the automatic path is left as a
design sketch rather than a code change in this POC.

## Blockers and Risks

| Risk | Mitigation in this POC |
|------|------------------------|
| Modifying `run.py` could break session state, tool calling, or delivery. | Avoided entirely by using the command-hook short-circuit. |
| Ollama embedding service may be down. | `SemanticCache.get()` swallows exceptions and returns `None`; the hook returns a graceful error message. |
| Cache returns a stale or wrong answer for a high-stakes question. | Hook is opt-in (`/cacheask`) and clearly marks responses as `[cached]`. The cache itself is documented as safe only for low-stakes Q&A. |
| Hook import path fails if `~/.hermes/scripts` is not on `sys.path`. | Handler adds `~/.hermes/scripts` to `sys.path` at load time and falls back to `HERMES_HOME` / `~/.hermes`. |
| Wildcard `command:*` hook would fire for every slash command. | Hook is registered only for `command:cacheask`, so it has no overhead for other commands. |

## Open Questions

1. Should the gateway also auto-populate the cache via an `agent:end` hook that
   stores responses? This would make `/cacheask` useful without manual seeding.
2. Should we add a TTL-aware cache warmup script for known FAQs?
3. Should the auto-short-circuit design reuse `_run_agent`'s persistence path or
   write a minimal transcript row directly in the hook handler?

## Repo Scope Note (why there's no `gateway/run.py` edit here)

This worktree's repository root is `~/.hermes` itself (a git repo tracking
this home directory's `scripts/`, `hooks/`, and `plans/`, per its
`.gitignore`) — **not** the Hermes gateway source checkout. `git ls-tree -r
HEAD` here contains no `gateway/`, `agent/`, or `hermes_cli/` paths, so there
is no `_run_agent` call site or command-hook registry to edit from this
worktree; the line-number references above describe the separate source
checkout at `~/.hermes/hermes-agent` (a different git repository, not a
worktree of this one).

That separate checkout already has a real, non-hook integration on branch
`feat/semantic-response-cache` (commits `0a0a408d1`, `18fa459bd`):
`agent/semantic_response_cache.py` gates eligibility on an explicit
`HERMES_SEMANTIC_CACHE_ENABLED` flag / `semantic_cache.enabled` config key,
zero active toolsets, prompt length, and a regex denylist for code/tool/shell
intent, then wires `SemanticCache` into `hermes_cli/oneshot.py`'s one-shot
(`hermes -z`) path, with a test in
`tests/hermes_cli/test_oneshot_semantic_cache.py`. That work lives outside
this worktree and was left untouched here.

The verification script added in this worktree
(`scripts/test_semantic_cache_hit.py`) exercises the shared
`SemanticCache`/`clawmem_semantic_cache.py` primitive directly (offline, with
a fake embedding function) so the cache-hit behavior can be demonstrated and
regression-tested from this worktree without depending on either gateway
checkout.
