# Local gate: reproduce CI on the host when CI cannot run

Use when a repo's Actions are dead (billing, seat limits, runner outage) and PRs must
still be adjudicated. The gate is a script plus a BASELINE, not a script alone.

## 1. Extract the authoritative step list

The workflow file is the spec. Do not invent steps from memory of "what CI does".

```bash
ls .github/workflows/
sed -n '1,60p' .github/workflows/ci.yml      # jobs, services, env: NODE_VERSION
sed -n '/^  [a-z-]*:/,/^  [a-z-]*:/p' .github/workflows/ci.yml | grep -nE 'name:|run:|working-directory|uses:'
```

Record per phase: the exact `run:` command, its `working-directory`, and the runtime
version pinned in `env:`. A step that runs `npx tsc -p tsconfig.typecheck.json` in
`backend/` must run there locally too — see the cwd trap in §5.

## 2. Write the gate as a script with honest exit codes

`scripts/local-gate.sh <worktree> [--phases p1,p2] [--only <step>] [--json-out f]`

Rules that make it usable rather than decorative:

- `set -euo pipefail`; every step records `PASS|FAIL|SKIP`, its duration, rc, and a log path.
- One disposable Postgres per run (`docker run --rm -P`, random host port) and drop it at
the end; `TEST_DATABASE_URL` must never point at a shared/dev DB.
- Pin the runtime the workflow pins (`node -v` printed at the top).
- Clean up containers and temp dirs even on failure.
- Steps that need a secret or an external service report **SKIPPED-WITH-REASON**.
  A skip counted as a pass is how a gate becomes theatre.

## 3. Run MAIN first — the baseline is half the gate

Absolute pass/fail is useless in a repo whose suite is already partly broken.

```bash
git worktree add --detach "$(git rev-parse --show-toplevel)-worktrees/gate-baseline" origin/main
bash scripts/local-gate.sh "$(git rev-parse --show-toplevel)-worktrees/gate-baseline" --json-out gates/baseline.json
```

The verdict for a PR is the **set difference of failing step names** against that
baseline, not a count:

```bash
comm -13 <(jq -r '.steps[]|select(.status=="FAIL")|.id' gates/baseline.json | sort) \
         <(jq -r '.steps[]|select(.status=="FAIL")|.id' gates/pr-<n>.json | sort)
```

Empty output (plus no new SKIPs) = the PR adds nothing. Quote it that way: "same four
failing steps as main", never "62/67 passed" — the second number invites a merge on a
suite that is broken for reasons unrelated to the PR.

## 4. Run each PR in its own worktree

```bash
git worktree add /home/<u>/<repo>-worktrees/pr-<n> refs/pr/<n>   # or the branch name
```

One worktree per PR, never the shared checkout (it holds foreign WIP). Expect a fresh
worktree to need `npm ci --ignore-scripts` plus a built workspace package before any
typecheck step can mean anything.

## 5. False-FAIL signatures — this is the failure mode that burns a session

A gate step that fails in **0–1 s** with one of these is the gate, not the PR:

| Signature | Cause |
|---|---|
| `rc=127`, empty log | command not on PATH in the step's cwd |
| `The specified path does not exist: 'tsconfig.typecheck.json'` | step ran from repo root instead of `backend/` |
| `npm error Missing script: "db:check-journal"` | same cwd bug, resolved in the wrong `package.json` |
| every `afterAll` failing with `Cannot read properties of undefined (reading 'end')` | test DB was never provisioned; the whole file failed at collection, not assertion |
| identical failures on main and on every PR | baseline condition |

Before blaming a PR for a FAIL, open its log and check duration + first line. Fix the
script's `working-directory`, re-run main and the PR, and only then produce a matrix.
Reporting an unbaselined matrix is worse than reporting nothing: it launders a broken
harness into "PR is red".

## 6. What the gate does not cover

The script replaces the *steps*, not the *review*. Pair it with a test-quality pass
(does the new test assert behaviour or identity? does the PR delete a guard? is the
migration entry contiguous and its `when` above main's max?). A green local gate on a
suite of identity assertions is still a merge you will regret.

## 7. Pushed is not gated

A pre-push hook that was bypassed (`SKIP_GATES=1`, `--no-verify`, `HUSKY=0`) means the
push carries no gate result, even when the hook's own failure was environmental.
Detect it in the crew report and in the reflog before calling a PR verified:

```bash
grep -rn 'SKIP_GATES\|--no-verify' <the agent's log or the hook wrapper>
```

Then re-run the gate properly on that head rather than accepting "tests passed" from a
separate standalone run — a subset run proves the subset, not the gate.
