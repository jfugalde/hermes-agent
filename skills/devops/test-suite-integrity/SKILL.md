---
name: test-suite-integrity
description: Use when a passing test suite may not be verifying.
version: 1.0.0
tags: [testing, integrity, assertions, accessibility, delegation]
---

# Test Suite Integrity

## When to use

- A suite went green after a change you did not fully understand.
- You are about to accept a delegated agent's "tests updated / all green" report.
- A locator or assertion keeps timing out and the temptation is to relax it.
- A suite has not actually run in a long time (newly wired to CI, or previously `skipped`).

This skill covers whether the suite is still WORTH anything — not how to make it pass.
For why a specific CI result is red/skipped, see `ci-result-attribution`.
For a skipped downstream job or an adopted gate that writes a score it did not measure, see
`ci-gate-integrity`. For an assertion that cannot fail even when the code is wrong, see
`test-assertion-integrity`. `ci-gate-integrity` Rules 3 and 4 point here; do not copy them back.

## Rule 1 — A loosened assertion is worse than a deleted test

A permissive matcher turns a real failure into a green test that verifies nothing,
permanently, and it looks like progress in the diff.

```ts
// strict -> permissive: REJECT. Both of these pass with the bug still present.
- expect(names).toContain('Scapular Push-Up');
+ expect(names.some(n => n.toLowerCase().includes('pull'))).toBe(true);

- getByRole('radio', { name: 'Intermediate', exact: true })
+ getByRole('radio', { name: 'Intermediate', exact: false })
```

The first passes for a `DB Pullover` (a warm-up stretch, not a pull) — it gutted the very
test guarding the bug being fixed. The second was `exact: false` applied to silence a real
accessibility defect.

**Guard — diff the assertion BODY, not the test name:**

```bash
git diff <base>..<branch> -- tests/ \
  | grep -E '^[+-].*(toContain|\.some\(|\.toBe\(true\)|exact:|toHaveLength|toBeGreaterThan)'
```

Reject any strict -> permissive transition. Require assertions to be **assertive by pattern
or name**: prefer an enum/pattern (`=== 'vertical_pull'`) over a display string, because
names drift and a substring match can be satisfied by the wrong element.

This recurs specifically with delegated agents under time pressure. Diffing the assertions
(every time) is what catches it — not trusting the summary.

## Rule 2 — A locator failure is evidence; decide product-vs-test ONCE per failure

A form-control timeout is usually a REAL product defect, not test drift: the suite is
reporting that a user with a screen reader cannot use the form either. Read the DOM the
failure produced before touching anything — the Playwright report carries an accessibility
snapshot at failure time:

```bash
gh run download <run-id> -n playwright-report -D /tmp/pw
# <hash>.md inside is the page snapshot: element ABSENT vs present-but-UNNAMED
```

**Then fix that side:**

| Failure | Real cause | Fix side |
|---|---|---|
| `getByLabel(...)` timeout | `<label htmlFor="x">` with no matching `<input id="x">` — no accessible name | PRODUCT: add the `id` |
| `getByRole('radio', {name, exact:true})` timeout | wrapping label concatenates name + description ("Intermediate 1-3 years") | PRODUCT: add `aria-label={option.label}` |
| `getByRole('progressbar')` timeout | progress bar is a styled `<div>` with only `style={{width}}` | PRODUCT: add `role` + `aria-valuemin/max/now` |
| `selectOption(...)` timeout | the product has NO `<select>` for that field (it uses radios) | TEST: drive the control the user clicks |
| `locator.check()` on a radio | input is visually hidden (`opacity:0; position:absolute`) with the label as the click target | TEST: click the label (legitimate pattern — do not remove it) |

Never add a control to the product to satisfy a test, and never widen the locator to make a
real accessibility defect disappear.

## Rule 3 — A suite that never ran is not a suite that passes

A job reported `skipped` produced no signal. Before trusting any count from it, confirm the
job actually EXECUTED. A suite wired in but never reached can hide a long list of real
defects, and the first time it runs it will surface them all at once — expect a cascade, and
treat each failure as its own root cause rather than "the suite is just broken".

## Rule 4 — Verify the PUSHED ref, not the working tree or a summary

"I updated the tests" can mean "in the working tree only". A PR holding the new code but
the stale test is red by construction:

```bash
git show origin/<branch>:tests/<file>     # the bytes CI will run
```

## Rule 5 — A RED suite may be an environment artifact; attribute before reporting it

The mirror image of Rule 1: reporting a red that the code did not cause is as damaging as
hiding one, because it blocks a clean landing and sends the implementer chasing a ghost.
Monorepo test runs in a fresh worktree fail for missing installs and missing workspace
builds long before they fail for code.

**Attribute every red before writing it into a verdict — reproduce it with the CI recipe:**

1. Reinstall the way CI does, in the worktree, instead of borrowing a sibling checkout's
   `node_modules`. Partial shared installs surface as `Cannot find package 'x'` (a whole
   file collapses to `(0 test)`), and the missing package is usually declared in
   `package.json` — check `grep '"x"' package.json` before believing a red.
2. Build workspace packages the CI builds. A `file:` dependency with no `dist/` yields
   `Failed to resolve entry for package "@scope/pkg"` across many unrelated files. Grep
   the workflow for `working-directory: packages/` + `npm run build` and run those first.
3. Re-run. Only reds that survive step 2 are candidates for the verdict.

**Then prove pre-existence for anything that survives.** If the failure is a pure-data
assertion (journal/index/catalog ordering), compare the inputs between the pre-change
commit and HEAD — not the whole suite:

```bash
git diff --quiet <pre-change>:<test-file> HEAD:<test-file> && echo TEST-IDENTICAL
git diff <pre-change>:<data-file> HEAD:<data-file> | head   # empty => pre-existing
```

Byte-identical test + byte-identical data that no change touched means the red predates the
work: report it as `pre_existing: true` with its own severity and its own card, never as a
defect of the change under review. A stale "must be the LAST entry" assertion is the classic
case: any later append breaks it while every real invariant (contiguity, uniqueness,
monotonic time) still holds — the fix is to relax the test, not to reorder the append-only
data.

## Rule 6 — A regression test must be SEEN to fail on the old code

A test written alongside a fix that also passes on the pre-change code is decoration: it
locks nothing and will not catch the bug returning. Prove the bite before accepting it:

```bash
git stash push <the fix>        # or: git checkout <base> -- <source file>
<run that single test>          # MUST fail, for the reason you expect
 git stash pop
<run that single test>          # passes
```

Ask the implementer for that fail-before/pass-after pair explicitly and reject "tests added"
with no proof — under a green badge nobody re-derives it later.

The mirror case belongs in the same review: a PR that DELETES a test covering the behaviour
it changes is hiding a regression, not tidying up.

```bash
git diff --diff-filter=D --name-only <base>..<branch> -- '*test*' '*spec*'
```

A schema/contract assertion removed in the same commit that relaxes the schema is the
classic signature. Restore it as an explicit test of the NEW contract, and require the
deletion to be justified in the PR body.

## Rule 7 — "self-test OK" that skipped its core checks is a SKIP, not a pass

Hand-rolled `--self-test` entry points guard their real assertions behind an optional import
and then report success anyway:

```python
try:
    import genanki
except ImportError:
    print("skip: genanki not installed, identity checks skipped")
...
print("SELF-TEST OK (0 failures).")   # exit 0, reads as green
```

The skipped group is usually the invariant that matters — identity/guid stability across
regenerations, schema mapping, the thing that needs the external library. "0 failures" counts
assertions that ran, and this ran none of them.

**Guard — enumerate the skips, then rerun with the dependency present:**

```bash
grep -nE 'skip|ImportError|except .*Error|pytest.mark.skip|describe.skip' <script-or-test>
<install requirements into a throwaway venv> && <rerun the self-test>
```

Report the assertion count (`N checks ran`) rather than `ok`, and never quote a self-test as
proof of a data-integrity property when its output line itself says a group was skipped.

## Rule 8 — An integration suite that talks to a live system is gated, attributed and reaped

Three separate failures hide behind "11 red in the integration file":

**The gate does not gate.** A skip guard written *inside* the file only skips the block it wraps —
the real `describe`s further down still register and run:

```ts
if (!process.env.X_INTEGRATION) {
  describe.skip('Integration (X_INTEGRATION not set)', () => { it('skipped', () => {}) })
}
// … and then the file's actual describes, which run UNCONDITIONALLY
```

Gate at file scope — `describe.skipIf(!process.env.X_INTEGRATION)('…', () => { … })` around the whole
suite, or `const d = cond ? describe : describe.skip` — and delete the placeholder test. Then prove
both directions: without the env var the file reports **skipped**, with it the tests **execute**.

**Contract drift is test-side, not product-side — but only after you look.** A live endpoint answering
`{session_id, session_type:'chat', started_at}` while the helper declares
`{session:{id,type,state}, greeting}` produces a cascade of `Cannot read properties of undefined` and
auth/rate-limit assertions that "fail" because the request never got that far. Diff the real response
(`curl` the running app) against the helper's declared type before choosing a side; when the shape is
stale, rewrite the helper and the assertions to the CURRENT contract. What you must not do is loosen
them to `toBeTruthy()` — that trades a red for a green that verifies nothing (Rule 1).

**A suite that writes to the live DB owes a cleanup and a count.** Take row counts before and after,
close and delete only the rows the run created (check FK order — messages before sessions), and paste
both numbers as evidence. A green suite that leaves orphan sessions behind is a slow-motion data
defect, and on a table already known to be polluted it silently masks the real count.

**A file that throws at module scope fails COLLECTION, and the summary hides it.** A top-level
`if (!process.env.X) throw new Error(...)`, or a client built and a `beforeAll` cleanup called at module
scope, kills the whole file before any test registers. The run then prints something like
`Test Files 1 failed | 14 passed (15)` next to `Tests 230 passed | 3 skipped` and a NON-ZERO exit — read as
"green" by anyone skimming the Tests line. That file's N tests are simply absent from the total.

**Guard — count tests, not just failures:** sum the tests each file should contribute and compare with
the reported total; a shortfall means a file died at collection, not that it passed. Then move the
client construction and every hook inside the gated `describe`, and prove `0 failed` files *and*
`$? = 0`, not just zero failed tests.

**An env var being "set" is not the same as reachable from the runner.** A DSN lifted out of a running
container (`docker exec <svc> printenv <VAR>`) usually names a compose-internal host (`postgres`),
which resolves inside the network and gives `getaddrinfo EAI_AGAIN` on the host. `docker port <svc>`
returning nothing confirms the service is unpublished — the port numbers you see listening may belong
to other projects. Before believing a green integration run, print only the host component of the DSN
and check it resolves where the suite actually runs; otherwise run the suite inside the compose network
(`docker run --rm --network <project>_default -v $PWD:/app -w /app … npx vitest run`) and say which mode
you used. Never repoint a DSN at a lookalike port from another stack to manufacture a pass.

## Pitfalls

| Pitfall | Guard |
|---|---|
| Assertion relaxed to reach green | Diff assertion BODIES; reject `toContain`->`some()`, `exact:true`->`exact:false`, equality->range |
| Substring matcher passes for the wrong element | Assert by enum/pattern, not by display-name substring |
| Locator timeout "fixed" in the test when the product is at fault | Read the a11y snapshot; decide product vs test once, fix that side |
| Test written against an imagined UI (`selectOption` with no `<select>`) | Confirm the control exists in the DOM; a whole spec of these means it never ran |
| Trusting a "tests pass" self-report | Re-run the suite and read the real pass/fail counts yourself |
| Env red reported as a code defect | `npm ci` in the worktree + build workspace packages before attributing; re-run |
| Red blamed on the change under review | `git diff --quiet <pre-change>:<file> HEAD:<file>` on test AND data; identical ⇒ pre-existing |
| "Must be the last entry" assertions on append-only data | Keep contiguity/uniqueness/monotonicity invariants; drop the positional one |
| New test passes on the pre-fix code too | Revert the fix locally; demand fail-before/pass-after evidence per test |
| `--self-test` exits 0 after skipping its core checks | Read the skip/import guard, rerun with deps installed, report the assertion count not "ok" |
| PR deletes the test guarding the behaviour it changes | `git diff --diff-filter=D --name-only` over test paths; restore as a new-contract assertion |
| In-file skip guard while the rest of the file runs | Gate the whole suite (`describe.skipIf`) at file scope; prove skipped-without-env AND executing-with-env |
| Integration red caused by a stale response shape | `curl` the running app, diff against the helper's declared type, fix the TEST helper (never relax to `toBeTruthy()`) |
| Suite creates live rows and never removes them | Row counts before/after + targeted DELETE by created ids, FK order respected |
| A file died at collection; summary still reads "N passed" | Compare total tests against the sum of expected per-file counts; require `0 failed` FILES and exit 0 |
| DSN copied from a container names an internal host | Print the host component, confirm it resolves from the runner, or run inside the compose network |
| Top-level `throw` / client / `beforeAll` outside the gate | Move construction and hooks inside `describe.skipIf(...)`; a throw at module scope fails collection, not one test |
