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
    ttl=300,
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

## Update: Automatic gateway short-circuit implemented

The live-gateway integration is now implemented in the `hermes-agent` checkout
on branch `feat/semantic-response-cache`. It shares the same eligibility gate,
feature flag, and `SemanticCache` instance as the existing `hermes -z` oneshot
path so the two callers never drift.

- `agent/semantic_response_cache.py` — shared gate. Exposes
  `is_cache_enabled(cfg)`, `is_cache_eligible(prompt, toolsets, cfg)`,
  `get_cache()`, and `get_cache_ttl(cfg)`. Off by default; enabled via
  `HERMES_SEMANTIC_CACHE_ENABLED` or `semantic_cache.enabled`. Eligible only
  when zero toolsets are enabled and the prompt is short and doesn't look like
  code/shell/file/tool-call intent.
- `gateway/run.py` — `_maybe_get_cached_agent_result()` wraps the plain-chat
  `self._run_agent()` call in `_handle_message_with_agent`. The lookup runs off
  the event loop via `asyncio.to_thread()` so the Ollama embedding call does
  not stall the gateway. On a hit it returns a minimal `agent_result` dict
  (`final_response`, `completed`, `failed`, `api_calls=0`, `cache_hit=True`);
  on a miss or error it returns `None` and the normal provider path runs.
- `gateway/run.py` — `_semantic_cache_store_sync()` writes freshly-generated,
  eligible responses back to the cache after a real turn, so later similar
  questions can be served without a provider call.

### Why this is safe

- Tool-enabled turns are never cached or served from cache: the gate receives
  the fully resolved toolset list for the turn and rejects any non-empty list.
- Slash commands are dispatched before this path, so `/cacheask` and other
  commands are unaffected.
- The conservative regex denylist rejects code generation, shell/file
  operations, and tool-call-shaped prompts even for tool-free turns.
- Any cache-layer error fails open: the real provider is always called.

### TTL

`semantic_cache.ttl_seconds` is configurable; the default is now 300 seconds
(5 minutes) for chat responses.

### How to verify

1. Unit tests (no live services):

   ```bash
   cd ~/.hermes/hermes-agent
   .venv/bin/python -m pytest tests/gateway/test_semantic_cache_gateway_shortcircuit.py tests/hermes_cli/test_oneshot_semantic_cache.py -v
   ```

2. Offline cache-hit demo in this worktree:

   ```bash
   cd ~/.hermes/.worktrees/semantic-cache-gateway
   python3 scripts/test_semantic_cache_hit.py --demo
   ```

3. Live end-to-end (requires local Ollama with `qwen3-embedding:0.6b`):

   ```bash
   export HERMES_SEMANTIC_CACHE_ENABLED=1
   # restart the gateway
   ```

   Send the same short, tool-free question twice from a connected platform.
   The second turn should be a near-instant cache hit with `api_calls=0`.

### Remaining limitations

- The cache ignores conversation history; a tool-free turn can hit cache even
  with rich context. This matches the existing oneshot behavior and is an
  accepted trade-off for low-stakes Q&A.
- On a `gateway.multiplex_profiles`-enabled gateway, the cache check uses the
  process `Path.home()`, not the resolved profile home for the message source.
  This is a pre-existing limitation of the shared cache module, not introduced
  by this change.
