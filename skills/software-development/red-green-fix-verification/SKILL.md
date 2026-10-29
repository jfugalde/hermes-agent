---
name: red-green-fix-verification
description: Use when proving a fix with fail-before/pass-after tests.
version: 1.0.0
tags: [testing, git, verification, regression, workflow]
---

# RED->GREEN fix verification

Class of task: ship a small fix together with tests that demonstrably fail on the current
code and pass after it, plus a non-regression check on the suites that already existed. The
deliverable is EVIDENCE (real command output for both states), not a claim.

## Procedure

0. **Baseline the WHOLE suite before the first edit** and write the counts down. A fresh branch
   or worktree whose suite is already red turns "which of these failures are mine?" into
   arithmetic, and it is the only way to say "I added 5 tests and changed nothing else" with
   evidence. Repo-level totals, not just the file you are about to touch:
   ```bash
   npm test 2>&1 | tail -4      # Test Files 34 passed (34) / Tests 254 passed (254)
   ```
   If the baseline is NOT green, decide then whether you own it — a pre-existing red is a separate
   finding for the report, never something to quietly fix inside a feature branch.
1. **Write the test file so it COLLECTS on the unpatched tree.** This is the step that is
gotten wrong. See the module-import rule below.
2. **Pin both directions.** Keep at least two tests that were already green (the default /
   no-arg behaviour, the legacy input schema). RED should read `N failed, 2 passed`, not an
   all-red file.
3. **Capture RED.** Stash ONLY the tracked code files, leaving the untracked new test in
   place:
   ```bash
   git stash push -- path/to/code_a.py path/to/code_b.py   # NOT -u
   <test runner> test_new_file                             # -> N failed, M passed
   git stash pop
   ```
4. **Capture GREEN** with the same command after restoring the change.
5. **Non-regression.** Run every existing suite that touches the changed functions, one file
   at a time so you get per-file counts:
   ```bash
   for f in <existing suites>; do $PY -m pytest "$f" -q -p no:cacheprovider --tb=no; done
   ```
   Grep the tree for OTHER consumers of a changed helper before widening its signature.
6. **Classify any failure before reporting it.** A failure that is not yours is not a
   regression — prove which it is with a control run against the base ref (stash the change,
   rerun, show the identical failures). Never report an environment artifact as a regression,
   and never report a green you did not actually run.

## Variant: proving a ONE-LINE fix (sed revert + `git checkout`)

When the entire fix is a single literal inside a larger file — a value added to an allowlist, one
more branch in a switch, a changed predicate — reverting that one line with `sed` (or the `patch` tool, when edits must go through tools rather
than a shell) is faster and safer than `git stash push -- <path>`: there is no stash entry to lose
and no untracked-test bookkeeping.

```bash
# 1. GREEN on the real tree first, so you know the pass count to return to
npx vitest run <the single test file>          # -> N passed

# 2. Revert ONLY the fix line, in place
sed -i "s/<exact new line>/<exact old line>/" <file>
grep -n "<the literal>" <file>                 # PROVE the revert landed
npx vitest run <the single test file>          # -> 1 failed | N-1 passed

# 3. Restore from git, never by retyping
git checkout -- <file>
grep -n "<the literal>" <file>                 # PROVE the fix is back
git status --porcelain                         # MUST print nothing
```

The correct RED signature is `1 failed | N-1 passed`: the new test alone red, every pre-existing
test still green. An all-red file means the revert broke the module (usually a `sed` that matched
more than the intended line), not that the new test is strong.

Rules that generalize:

- **Revert the FIX, never the test.** If the assertion lives in the same file you just edited, you
  are measuring your own edit.
- **Prove the revert landed before running.** A `sed` that matched nothing produces a clean green
  and tempts you into reporting the new test as weak when it is doing its job.
- **Restore with `git checkout -- <file>`, then require an empty `git status --porcelain`.** A
  hand-reverted file left in a worktree is how a "verified" branch ships without its fix.
- **Say which state each count came from** in the report ("16/16 with the fix, 1 failed on the
  reverted allowlist") — a RED count with no named pre-state is unfalsifiable.

### Trap in the restore step: `git checkout <ref> -- <path>` also writes the INDEX

`git checkout <commit> -- <path>` (used to resurrect a pre-fix version from another ref) stages
that blob, not only the worktree. The later `git checkout -- <path>` then restores the PRE-FIX
file from the index: the run comes back green-looking with your fix silently gone.

```bash
git checkout <pre-fix-ref> -- src/route.ts    # RED state, ALSO staged
git checkout HEAD -- src/route.ts             # correct restore
git diff HEAD --stat                          # MUST print nothing
git status --porcelain                        # a staged 'M ' pre-fix blob is easy to skim past
```

## Variant: bite-testing a FEATURE that landed on top of a moved base

- **Bite the smallest behaviour, not the whole file.** Reverting the file to the base ref (feature
  absent) usually kills the new test at module import — `import { decodeX } from ...` when the
  symbol does not exist is a collection error, and RED then says nothing about your assertions.
  Revert the single predicate / branch / look-ahead in place, keep the export, and RED becomes the
  assertion you wrote: `expected [] to deeply equal [2, 5]`.
- **One aspect, one revert.** When the new tests cover different aspects (paging key,
  `page` + cursor precedence, last-page marker, input validation), revert each aspect separately.
  A single revert that reddens four tests proves only that one of them is wired.
- **Quote the assertion message, not only the count.** `expected 'MjAyNi0xMC0wM3w2' to be null`
  names the behaviour that regressed; `1 failed` does not.
- **No pre-feature code path? Then say the pre-state is "feature absent".** Run the new test
  against the ref without the feature and report the failure mode (missing field, ignored param),
  labelled as absence rather than as a bug you fixed.
- **A merge that brings in 100+ commits is not a pre-fix baseline.** Do not present "the test would
  have failed on the old branch" as evidence; bite on the state where the fixed behaviour is
  removed, in the tree you are actually shipping.

## Variant: a TEST-ONLY change (invariant assertions, harness fixes)

When the deliverable is tightening an existing test rather than changing product code, there is no
code hunk to revert — so bite the DATA the assertion reads instead.

- **Redden it by restoring the bad input, not by touching src.** Put the registry / fixture /
  journal entry back in its colliding or stale form, run, then restore. Borrow the bad blob with
  `git checkout <pre-fix-ref> -- <data file>` and restore with `git checkout HEAD -- <data file>`:
  plain `git checkout -- <data file>` restores the INDEX copy, which is the bad blob you just
  staged — the tree comes back dirty or, worse, ships the bad data.
- **The RED message must name the invariant.** `expected 109 to be 110` or
  `expected 1793880000000 to be greater than 1793880000000` is evidence; a collection error or an
  all-red file is not.
- **Re-bite after re-anchoring.** An assertion relaxed for durability can silently stop catching the
  bug it existed for. After changing the anchor, rerun the pre-fix bite and keep the run only if it
  is red for the ORIGINAL reason.
- **Durability check before shipping: ask what the NEXT upstream change does to this assertion.**
  "Mine is the last entry / is the maximum / is the only one" goes red on an unrelated merge and
  teaches the next agent to bump the constant. Assert the relation to the immediate neighbour, or
  anchor the scenario on a local slice (`entries.slice(0, own)`) instead of on the global tail.

## Variant: the second claim is a query PLAN, not a test

When the fix is "this now uses the index", the plan is evidence and it lies at small scale. `EXPLAIN`
with `SET enable_seqscan=off` proves only that the index is *usable* — it forces the planner, so it
will pick any index that matches the predicate even when the index is worthless. On a 250-row table
the same query with seqscan ON is a `Seq Scan` and a reviewer learns nothing about whether the index
earns its keep.

- **Pad to realistic volume before judging.** Generate enough rows the planner has real statistics
  (tens of thousands, spread over the tenants/periods the index prefixes), `ANALYZE`, then take the
  plan with seqscan ON. A `Bitmap Index Scan` chosen *without* forcing is the claim worth reporting;
  a forced index scan plus a seqscan-on `Seq Scan` is a half-answer and should be reported as such.
- **Report the plan with and without the index, both seqscan states.** `without index + seqscan off
  -> Seq Scan` is what proves the index is the only remaining path.
- **Seed and tear down inside one transaction.** `BEGIN; insert fixtures; CREATE INDEX; ANALYZE;
  EXPLAIN ...; ROLLBACK;` leaves a shared disposable DB exactly as found, `EXPLAIN` included, and
  no `DROP INDEX` bookkeeping to forget. If the index must be created outside the tx, `DROP INDEX
  IF EXISTS` and then re-query `pg_indexes` to prove it is gone.
- **Cross-check the plan claim against the RESULT, not the plan text.** Run the aggregate under the
  fixed predicate and under the buggy one on the same seeded rows (`1500` vs `6500`). A plan can
  look right while the predicate is wrong, and a predicate can be right while the plan is unchanged.

## Variant: a feature that legitimizes a previously-INVALID input

Class of task: the request is "support value X", and the existing suite *asserts that X is an
error*. Then RED has two distinct sources, and shipping only one of them leaves the branch
contradicting itself:

1. The new spec (X works end to end).
2. The legacy assertions that encoded the old contract — `it('rejects X')` and, worse, an
   invalid-value list that contains X (`[1, X, 7, 8, 0]`). Those two go red the instant the fix
   lands, look like collateral damage, and are the ones reviewers ask about.

Procedure:

- **Grep for the value you are legitimizing before touching code**, not after CI goes red:
  `rg -n "rejects? .*<value>|invalid.*\[.*<value>" tests/`. Invert those assertions in the SAME
  commit as the fix and name them in the report ("the two assertions that pinned the old contract
  were inverted"). A feature PR that also edits a test reads as a cheat unless you say why.
- **Enumerate every gate that hardcodes the old domain.** A single value is usually clamped or
  re-validated in several places — the Zod union, the TypeScript literal union, the LLM prompt's
  JSON schema, the fallback clamp (`Math.max(<old floor>, ...)`). Grep for the old floor constant
  and fix all of them; a prompt schema left behind makes the model emit the old value forever.
- **Prove the ARTIFACT, not only the unit.** For a generator/pipeline feature, run the real CLI on
  a real input and assert on the emitted output: total count, one line per period, and the
  discriminator field (e.g. "3 periods × 2 sessions = 6, every one of type full_body"). Unit tests
  pass happily while the pipeline still emits the old shape.
- **Keep the pre-fix artifact as evidence.** If a previous run of the same input produced the
  wrong shape (five sessions for a two-day request), save that JSON next to the new one; the
  diff between the two artifacts is the most convincing line in the whole report.

## Variant: proving a whole-FILE swap (shell scripts, multi-line fixes)

When the fix spans several lines of one file — and especially when the test harness copies that
file into a sandbox anyway — swap the file instead of stashing. Save the pre-fix blob BEFORE the
first edit:

```bash
cp scripts/x.sh "$SCRATCH/x.sh.prefix"          # before editing
# ... edit, harness goes green ...
cp scripts/x.sh "$SCRATCH/x.sh.fixed"
cp "$SCRATCH/x.sh.prefix" scripts/x.sh
grep -n '<buggy line>' scripts/x.sh              # PROVE the swap landed
bash scripts/test-x.sh; echo "PRE-FIX EXIT=$?"   # MUST fail
cp "$SCRATCH/x.sh.fixed" scripts/x.sh
bash scripts/test-x.sh; echo "FIXED EXIT=$?"     # MUST pass
git status --porcelain                           # only your intended files
```

Extra credit: if the bug had two independent defects, also run the intermediate state (one
defect fixed). A red run there is the only honest evidence the second defect is real rather than
a theory. For the bash-specific traps this class keeps hitting — loop control inside a subshell,
over-escaped CLI arguments, assembled-vs-resolved path compares — see `bash-script-guards`.

## Pitfall: the test file must survive the UNPATCHED tree

- **Never `from module import NEW_SYMBOL` at top level when `NEW_SYMBOL` is added by the fix.**
  On the unpatched tree that raises `ImportError` during collection, pytest reports a single
  `1 error` and runs ZERO tests — you lose the per-test failures that are the whole point of
  RED. Import the module and reference the new attribute at CALL time:
  ```python
  import agent.credential_pool as cp          # collects fine on unpatched code
  def test_x():
      assert cp._ttl(429, provider="ollama-cloud") == cp.NEW_CONSTANT
  ```
- **Passing a new keyword is already a RED failure.** `f(x, provider="...")` against a
  signature lacking `provider` raises `TypeError` per test — a clean failure that needs no new
  symbol to exist. Prefer this over asserting on a constant that doesn't exist yet.

## Pitfalls: capturing the baseline and editing

- **Do not `git stash -u` when the checkout is a runtime home.** Its untracked set IS the
  product (skill library, live data). Stash explicit tracked paths only, and stash the CODE,
  never the new test you are measuring.
- **Never paste diff-style `+` / `-` line prefixes into a patch tool's replacement text.**
  The tool writes them literally as file content; the returned diff renders your stray `+`
  indistinguishably from a diff marker, so the corruption only surfaces on the next read as an
  `IndentationError`. Type the replacement body clean.
- **Check the repo's commit-author convention before committing, not after.**
  `git log -20 --format='%an <%ae>' | sort | uniq -c | sort -rn` — a checkout configured for a
  bot/deployer identity produces commits that fail contributor-attribution gates and do not map
  to a GitHub account. Re-author the branch with `git reset --soft <base> && git reset &&
  git add <path>... && git commit -m ...`, or `git commit --amend --reset-author` for a single tip.

## Pitfall: resuming after an interrupted bite-test

A bite-test (mutate the FIX to prove the test is strong) is a transient state, and a run that dies
mid-way (timeout, iteration budget, killed worker) leaves the tree MUTATED with the fix uncommitted.
On resume the gates can come back green while you are measuring the buggy code again.

- **Re-read the diff before trusting any gate on a resumed run.** Grep for the exact mutated
  literal (`grep -n 'business_id: 1' <file>`): if it is there, you inherited a half-done bite-test,
  not a half-done fix.
- **A green suite on a mutated tree is the trap, not the evidence.** The mutation reproduces the
  old bug, so the tests that should catch it are exactly the ones that were vacuous — a green run
  may mean the fix was never applied in the places that matter.
- **Keep the fixed blob in scratch and restore by copy, never `git checkout`**, when the fix is not
  yet committed: `git checkout -- <file>` reverts to the branch HEAD and silently throws the fix
  away. `cp <file> $SCRATCH/fixed` → mutate → test → `cp $SCRATCH/fixed <file>` → compare `sha256sum`
  against the sha you recorded before mutating (and against `git show HEAD:<file>` once committed).

## Pitfall: a new test that nothing runs is not coverage

Adding `tests/foo.test.ts` proves nothing if no `package.json` script and no CI step invoke it:
the file sits in the tree, reviews read it as coverage, and it never executes. Before reporting a
new test as green, prove the runner actually picked it up — the per-file line in the runner output
(`✓ tests/unit/engine/full-body-2day.test.ts (5 tests)`) or the CI step that calls it. If the test
belongs to a frontend package with no test script, wiring it is part of the change, not a follow-up.

## Reporting

- The PR body / final report carries the EXACT commands and the RAW counts for RED and GREEN,
  and per-file counts for non-regression. Do not summarise a count you did not see printed.
- State plainly which failures you are NOT claiming to have fixed and why (environment,
  pre-existing, out of scope).
- Do not cite PR/issue numbers you did not verify, and do not claim a test you did not run.

## Repo note

A repo whose checkout lives under a guarded home directory (the Hermes runtime tree under
`~/.hermes`) will show spurious guard failures when a worktree of it is nested under that home:
`import run_agent` probes `<repo-parent>/manifest.json` and `tests/home_io_guard.py` refuses I/O
under the real home. Run the suite with the shared checkout's installed venv
(`<shared>/.venv/bin/python -m pytest <file>`) and classify the failure with a control run on the
base ref — it does not trip on a CI checkout outside the home.
