---
name: ci-gate-integrity
description: Use when a green or skipped CI gate may be lying.
version: 1.0.0
tags: [ci, github-actions, testing, integrity, verification]
---

# CI Gate Integrity

## When to use

- A PR's checks look "mostly green" and you are about to merge on that basis.
- A job reports `skipped` and you are tempted to read it as "not relevant".
- A fix chain has needed several CI cycles and keeps surfacing new failures.
- A delegated agent reports a gate passed, or that it 'fixed the tests'.
- A step fails with a missing module/file, and the path is absent from your branch
  but present on another open PR.

## Rule 1 — Read the JOB GRAPH, not the run conclusion

When a job is declared as a `needs:` prerequisite of others, its failure makes the
downstream jobs `skipped` — they never run and produce NO signal. Reading only the run
conclusion (or the job list without the dependency) makes a skipped E2E look benign.

```bash
gh run view <run-id> --json jobs -q '.jobs[] | "\(.name) -> \(.status)/\(.conclusion)"'
```

If any prerequisite is `failure`, everything downstream is an **unknown**, not a pass.
Fix the prerequisite FIRST (often a one-line type error), then re-run to get real
signal. A suite that has never executed can hide a long list of real defects behind a
single type error — and the failure message will point at the WRONG layer (it says
"lint failed", not "E2E is broken").

Also: re-run a stale failing workflow before trusting its verdict. A red from weeks ago
may not reproduce; a red that persists across a fresh run is real.

## Rule 2 — A gate result reported by another agent is a CLAIM

A delegated agent that says "`tsc` exit 0", "lint passed", "230/230 green" may be wrong,
and in a gated pipeline a false pass is expensive (it skips the rest). Re-run the gate
yourself before pushing or merging:

```bash
npx tsc --noEmit; echo "TSC=$?"
npm run lint >/dev/null 2>&1; echo "LINT=$?"
```

Demand NUMBERS in the brief (counts, exit codes, exact file lists) and verify the ones
that matter read-only. A summary of process is not evidence of outcome.

## Rule 3 — A loosened assertion is worse than a deleted test

Owned by `test-suite-integrity` Rule 1, including the diff guard and the strict-to-permissive
examples. Do not restate it here. A CI job that went green because an assertion was loosened
is that rule, not a job-graph problem.

## Rule 4 — A locator/assertion failure is evidence, not noise

Owned by `test-suite-integrity` Rule 2, including the product-vs-test table. Decide once per
failure and fix that side. Do not restate it here.

## Rule 5 — Verify the PUSHED ref and the MERGED default branch

An uncommitted edit is invisible to CI, and a merge claim is not a merge:

```bash
git show origin/<branch>:<file>        # the bytes CI will actually run
gh pr view <n> --json state,mergedAt,mergeCommit
git show origin/main:<file> | grep -n '<the-change>'
```

## Rule 6 — Know when to stop the fix chain

Early cycles surface real defects; later cycles chase type errors and correct-but-hidden
elements. Past roughly five cycles without the gate going green, STOP and report the
verified state — what is proven, what is unproven, the next diagnostic step — so the
owner decides. Do not spend unbounded autonomous cycles on a production repo, and do not
merge on an unresolved red.

## Rule 7 — a red step whose missing module lives on a SIBLING, unmerged PR

A CI step can fail with `MODULE_NOT_FOUND` / `Cannot find module` for a path that is
ABSENT from your branch but PRESENT on another open PR. Your branch is not broken — it
is stacked on a PR that has not landed yet. The signal looks exactly like a defect in
your own diff, and the reflex (add the file, or delete the step) is wrong both times.

Detect it before touching your code:

```bash
gh api 'repos/O/R/contents/<missing-path>?ref=<your-head>' --jq .message   # 404 on YOUR branch
gh api 'repos/O/R/contents/<missing-path>?ref=<sibling-branch>' --jq .size   # present there
```

Do NOT copy the file into your branch. It belongs to the sibling PR; landing it twice
creates a duplicate file and a merge conflict when the sibling merges.

Do NOT delete the gate either. The step is a real guard that must go live the moment the
sibling lands. Rewrite it so it is CONDITIONAL on the dependency existing:

```bash
if [[ ! -f src/<module>.ts ]]; then
  echo 'SKIP: requires <sibling PR> (the <module> module) to be merged first'
  exit 0            # a skip, not a failure
fi
# ...the real assertions...
```

Verify BOTH paths: with the module present the assertions must PASS, and with it absent
the step must print the skip and exit 0. Report both outputs.

To test the module-present path without contaminating your branch, put YOUR script into a
checkout that already has the sibling's module (its worktree), run it there, then delete
the copies and confirm `git status` is clean again — never `git add` the borrowed file.
After the sibling merges, rebase: the module arrives from main, the guard's own
commit that took the sibling's entrypoint is dropped automatically (`patch contents
already upstream`), and the skip path flips to real assertions with no code change.

When that flip happens, prove it from the CI LOG, not from the green run: read the step
conclusion AND grep the job log for the guard's PASS lines, so 'green' cannot mean
'still skipping':

```bash
gh run view <run-id> --log --job <job-id> | grep -a '\[guard-check\]'
```

**The same dependency invalidates the ASSERTIONS, not just the import.** A guard that
asserts literal log strings encodes the contract of the design being replaced. When the
sibling changes the contract, rewrite the assertion against the NEW contract — e.g. an
empty DB takes one path and a populated DB takes another — and keep a positive case for
each branch, or the gate passes on one path while silently ceasing to test the other.

Consequence for ordering: merge the dependency FIRST, then the dependent. Until then the
dependent's CI is green by SKIP, not by proof — say that in the report instead of
implying the gate ran.

## Rule 8 — When CI cannot run at all, the gate is LOCAL — and it needs a BASELINE

Billing blocks, seat limits and runner outages make a whole repo's badges meaningless:
every PR is red for the same reason and no PR has ever run. Neither colour carries
signal, so stop reading it and build the gate yourself, mirroring
`.github/workflows/*.yml` step for step.

A local gate is only evidence when you run it on **`origin/main` first** and judge each
PR by the *set difference of failing step names* against that baseline. Absolute counts
(62/67) invite a merge on a suite that is already broken for unrelated reasons.

Expect false FAILs from the harness itself — a step failing in 0–1 s with `rc=127`, an
empty log, `The specified path does not exist: 'tsconfig…'`, or `npm error Missing
script` is a cwd/PATH bug in the script, not a defect in the PR. Full recipe, false-FAIL
signature table and the baseline diff command: [`references/local-gate-baseline.md`](references/local-gate-baseline.md).

Two consequences worth stating in every report:

- Steps needing a secret or external service are **SKIPPED-WITH-REASON**, never PASS.
- A push that bypassed a failing pre-push hook (`SKIP_GATES=1`, `--no-verify`) is *not*
  gated, even when the hook failed for environmental reasons; re-run the gate on that
  head instead of accepting a separate subset run as proof.

## Rule 9 — When you AUTHOR the gate, an unconfigured gate must FAIL

Rule 8 has you build the gate yourself. The gate then inherits every failure mode this skill
warns about, so design it against them from the first line:

- **Absent configuration is a FAIL, never a pass.** No allowed paths, no health source, no
expected artifact — each is a red check with a reason string, not a silent skip. A checker that
greens when nobody told it what to check is the "skipped read as passing" failure wearing a
different hat, and it is worse than no gate because it produces evidence-shaped output.
- **Inject every check's input as a parameter** (URL, container name, file path, expected value)
instead of adding a `--test-mode` flag. Then the gate is unit-testable against fixtures without
restarting, breaking or even reaching a live service — and the fixtures prove behaviour a live
target can only sample.
- **`git status --porcelain` collapses an untracked DIRECTORY to one line** (`?? app/`), so a
scope or forbidden-path check never sees the files inside it and passes. Use `-uall`, or the
check silently misses exactly the case it exists to catch. Pair it with `--no-renames` on the
diff so a rename yields both sides.
- **Word-boundary the error-log regex.** A bare `error` substring flags a healthy service on an
INFO line containing an empty `ErrorCode:` field; a gate that red-lines good deploys teaches
everyone to ignore it. Match `\b(error|fatal|panic|traceback|exception|cannot|failed)\b` and let
callers pass their own pattern verbatim.
- **Prove it in three directions before trusting it:** it PASSES on a known-healthy live target,
it FAILS on a deliberately wrong expected value (wrong image digest / wrong path scope), and it
fails CLEANLY (detail string, no traceback) when the tool it needs is missing or times out.

## Rule 10 — A gate you ADOPT must be proven to fail CLOSED

Rule 9 is about gates you write. For a third-party gate you are considering adopting, the
first test is not "does it pass" but **"does it refuse to report when it cannot measure"**.
Delete or stub the thing it depends on (the coverage producer, the test collector), run it, and
read the output:

A tool that PRINTS that it could not measure and writes a report anyway is producing
evidence-shaped output. A green score then means "the measurement run was broken", which is
worse than no gate, because it looks like proof. Detect it before adopting:

- remove the coverage producer -> does it still print a score?
- make the runner collect zero tests -> does a report still land on disk?
- fail the baseline -> does it still rewrite the snapshot and exit 0?

If any answer is yes, do not wire its exit code into CI. Wrap it: parse the output for the
could-not-measure lines, assert the coverage artifact instrumented a non-zero number of lines,
and exit non-zero yourself. Then the wrapper is the gate and the tool is a calculator.

The only wrapper worth keeping is `scripts/crap-gate.sh` in this skill. It runs crapper on
paths you pass (never the whole tree), refuses `--no-coverage`, refuses to let crapper
pip-install `coverage` into the project, and deletes `.metrics/crap.edn` when the run says
coverage was missing or no tests ran. Default threshold is 30, not 10. Do not wire it into
pre-push until a changed-function histogram picks the number. Dryer and mutator stay manual.
Fail-open lines and the threshold policy live in the third-party-skill-adoption-review skill, file references/quality-gate-toolchains.md.

## Pitfalls

| Pitfall | Guard |
|---|---|
| Downstream job `skipped` read as passing | Read `.jobs[].status/conclusion`; a failed prerequisite makes all downstream an unknown |
| Stale red verdict taken at face value | Re-run the workflow once; only a persistent red is real |
| Trusting a self-reported gate result | Re-run tsc/lint yourself; require numbers, not summaries |
| Test relaxed to go green | Diff assertion bodies; reject strict->permissive (`toContain`->`some()`, `exact:true`->`exact:false`) |
| Locator timeout "fixed" in the test when the product is at fault | Read the a11y snapshot; decide product vs test once and fix that side |
| Merging because most checks are green | A skipped/failed prerequisite invalidates the 'green' reading |
| A step fails on a module that exists only on a sibling unmerged PR | Check the path on BOTH refs; never copy the file into your branch — make the step conditional (SKIP + exit 0) and rewrite its assertions against the sibling's NEW contract |
| Whole repo red on every branch with "payments failed / spending limit" | That is billing, not code: build the local gate (Rule 8) and say plainly that CI carries no signal |
| Local gate matrix produced without a main baseline | Run main first; judge by the diff of failing step names (Rule 8) |
| Gate step fails in 0–1 s (`rc=127`, missing tsconfig, missing npm script) | Harness cwd/PATH bug — fix the script and re-run before blaming the PR |
| "Pushed and mergeable" read as "gated" | Check for `SKIP_GATES=1`/`--no-verify`; a bypassed hook leaves no gate result |
| Backlog size quoted from a digest | `gh pr list` defaults to 30; re-count with `--limit 500` before stating a number |
| Gate greens because nothing configured it what to check | Absent config (paths, sources, expected values) is a FAIL with a reason string |
| Untracked directory hides its files from a scope/forbidden check | `git status --porcelain -uall`; pair with `--no-renames` on the diff |
| Adopted third-party gate reports after "could not measure" | Delete its input first; wrap it and fail on the could-not-measure lines + non-trivial coverage |
| Log-error regex flags healthy services on an empty `ErrorCode:` field | Word-boundary the alternation; don't substring-match `error` |
