---
name: test-assertion-integrity
description: Use when a defect survived a green test suite.
triggers:
  - a defect survived a green test suite
  - a bug is localised to a non-first element (week 2, second item, later page)
  - a test loops over [0] of a collection that has more elements
  - writing a regression test after fixing a bug
  - an assertion contains an `or` between two expectations
  - reviewing tests a subagent authored
  - the invariant holds for the sample but not the whole artifact
  - a guard test compares thresholds quoted from a design doc
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [testing, verification, assertions, code-review, pitfalls, subagents]
    related_skills: [agent-code-verification-pitfalls, leaf-subagent-test-verification-apollo, verify-before-close]
---

# Test Assertion Integrity

A green suite is a claim about **coverage**, not about the artifact. Three ways an assertion
passes without inspecting what it claims to protect. All three shipped real defects.

## 1. The scoped assertion — covers element [0], defect is in element N>1

The test does exercise real production code; it just iterates the wrong slice. `collection[0]` is
asserted, the invariant is reported as holding, and the violation sits later in the collection.
The author truthfully reports "N tests pass" — the scoping is the defect.

Real case (Sep 2026, workout-generator template family): three assertions — a per-day minutes
budget, zero null-RPE, and no-within-day-duplicate-ids — all iterated
`program.phases[0].weeks[0].days`. The actual defect (one exercise prescribed twice in a single
session) was in **week 2**. Suite green, generated program wrong. The same class recurred inside
that repo's own test file: the duplicate-id check ran over week 1 while the duplicate was in week 2.

**A defect localised to a non-first element is a signal to re-read the assertions, not only the
content.** Before debugging content, grep for first-element iteration and ask whether the invariant
claims to cover more:

```bash
grep -nE '\[0\]\.|\.slice\(0,\s*1\)|weeks\[0\]|days\[0\]|items\[0\]|pages\[0\]' <spec-file>
```

```js
// WRONG — covers week 1 of 3
program.phases[0].weeks[0].days.forEach(day => assert(day));
// RIGHT — cover every element the invariant claims
program.phases[0].weeks.flatMap(w => w.days).forEach(day => assert(day));
```

**Repair order:** fix the SCOPE first, re-run, and let it surface the content bug — then fix the
content. Fixing content against a partially-scoped assertion only hides the next instance.

## 2. The vacuously-true `or` — accepts a substitute for what you require

An assertion that passes on an alternative proves nothing about the thing you actually require.

```python
# WRONG — a missing CARRY passes because TROTE is present
assert patterns.has(CARRY) or patterns.has(TROTE)

# RIGHT — assert each requirement separately
assert patterns.has(CARRY)
```

The real case: `CARRY || TROTE` is exactly how a missing movement pattern reached a real athlete's
generated program. An `or` in an assertion is a claim that the branches are **interchangeable**.
When one branch is a weaker stand-in, the test can never fail on the thing you care about.

## 3. The un-failable assertion — passes before AND after the fix

A test that passes both before and after your fix proves nothing. This repo shipped exactly that:
a tag-based assertion passed while the content it was meant to remove was still present in the
output.

**Prove every new assertion can fail — mutate the fix back:**

```bash
cp <file> /tmp/<ns>-bak-<topic>.<ext>     # namespace it; parallel agents clobber /tmp
# revert ONLY the fix
<run the specific spec>                   # MUST fail, and for the expected reason
cp /tmp/<ns>-bak-<topic>.<ext> <file>     # restore
```

If it does not fail, the assertion is scoped wrong, unreachable (dead branch), or vacuously true
(see 1 and 2). Fix the assertion, not the code.

### 3b. The single-option `<select>` — `value === expected` cannot catch a wrong id

A preselection test that stubs a **one-row** list proves only "not null", never "the right row".
React writes the controlled `value` onto the DOM node; jsdom (like a real browser) leaves the
**first** option selected when no option matches. So with options `[7]`, every wrong id —
`1`, `9`, a hardcoded legacy default — reads back as `"7"` and the assertion passes.

Measured on a real Settings form (kryu-os business-identity review, Oct 2026): with the fixture
below, restoring the old `business_id: 1` left both tests green. Characterization:

| controlled value | options | `select.value` | discriminates? |
|---|---|---|---|
| `1` | `[7]` | `"7"` | **no** — test passes with the bug |
| `9` | `[7, 9]` | `"9"` | yes (matches a non-first option) |
| `1` | `[7, 9]` | `"7"` | **no** — 2 rows alone is NOT enough |
| `9` | `[7]` | `"7"` | **no** |

So "stub two rows" is necessary but **not sufficient**: if the value you assert happens to be
`options[0]`, a wrong id still reads back as `options[0]` and the test stays green. The expected
value must be one that only the correct implementation selects — i.e. NOT the first option — or
assert the submitted payload instead.

Two repairs, either one:

```ts
// A) assert the payload the form submits, not the widget it renders
await clickSave();
assert.equal(createSpy.mock.calls[0][0].business_id, 7);   // fails on a hardcoded 1

// B) fixture with >=2 rows where the EXPECTED value is NOT option[0]
//    (order it the way the API really returns rows, not sorted-by-id)
stubRows([{ id: 2, name: 'Aurea' }, { id: 1, name: 'Rufino' }]);
assert.equal(select.selectedIndex, 1);   // = Rufino; a positional pick gives index 0
```

Generalize: **any assertion on a widget that silently falls back to its first option** (select,
radio group, tab strip) is un-failable for "wrong but valid" inputs. Stub at least two entries and
assert a value that only the correct implementation can produce.

## 4. The doc-derived constant — the guard asserts the design doc, not the code

A budget/cascade invariant test (`workerTimeout > serverWorstCase`, `clientTimeout > serverTimeout`)
that rebuilds the arithmetic from **numbers quoted in the design document** instead of importing the
constants the code actually uses. It stays green forever while the real composition drifts.

Real case (kryu-os timeouts): the guard computed `acquire + render + 2 * (gcsPut * 2 + backoff)`
from the policy table (75 500) while the shipped code bounded the render operation at
`PDF_BUDGET.requestPath` 45 000 and each upload at `GCS_UPLOAD_BUDGET_MS` 30 000 — real worst case
105 000 against a 90 000 worker top. Doubling `GCS_UPLOAD_BUDGET_MS` to 60 000 left the guard green:
proof it measured the document, not the code.

**Rule: an invariant guard imports every term from production module scope; no literals, no
`EXTERNAL_TIMEOUTS.a * 2 + 250` substitutes.** Then mutate each term and confirm the guard goes red:

```bash
# mutate ONE real constant, run only the guard, expect RED, revert
sed -i 's/GCS_UPLOAD_BUDGET_MS = 30_000/GCS_UPLOAD_BUDGET_MS = 60_000/' <module>
<run guard spec>   # must fail
```

Cross-cutting invariants only hold **after** the branches are combined — verify them on an
integration worktree, not per-PR:

```bash
git worktree add -b review/integration <path> <branch-a>
git merge --no-edit <branch-b> && git merge --no-edit <branch-c>
ln -sfn <main-repo>/backend/node_modules backend/node_modules   # reuse one install
```

Then re-grep, re-run `tsc`, the full suite, and one probe per invariant term.

## 5. The hand-invoked hook — the test calls the callback the loop was supposed to call

A retry/timeout policy exposes hooks (`onAttempt`, `onRetry`). The test imports the options object
and calls the hook **itself**, then asserts the message it produced:

```ts
// WRONG as the primary proof: the retry loop is not exercised at all
const msg = String(GCS_UPLOAD_OPTIONS.onAttempt!(1, 3, err));
assert.match(msg, /intento 1\/3/);
```

Remove the hook call from the loop — or pass the wrong attempt number — and this stays green. It
proves the formatter, not the wiring; the "did we retry?" claim is untested.

Repair: drive a real retry and observe the hook from the outside.

```ts
transport.put.mockRejectedValueOnce(new ServiceUnavailable()).mockResolvedValueOnce({ ok: 1 });
await upload(...);
assert.equal(onAttempt.mock.calls.length, 2);          // the loop called it per attempt
assert.equal(onRetry.mock.calls.length, 1);            // and only after the failure
assert.match(String(onAttempt.mock.calls[0][0]), /intento 1\/3/);  // format, secondary
```

Generalize: when the claim is "an effect happened" (retried, persisted, invalidated), the
assertion's subject must be the effect or the call the loop made. An assertion whose subject is a
**log string** is a formatting contract — useful for ops greps, never the proof.

## 6. Position-as-proof — "this is the latest entry"

A shared ordering (migration journal, changelog, registry array) and the test asserts the author is
last:

```ts
// WRONG — identity, not behaviour
assert.equal(journal.entries.at(-1)!.tag, '0065_hot_read_indices');
assert.equal(journal.entries.at(-1)!.idx, 62);
```

It says nothing about the SQL the migration adds, goes red on the next migration anyone merges,
and — worse — a wrong `when` (older than the base branch's max) makes the runner skip the entry
silently while this test still passes on the author's own branch.

Repair: assert the invariants, and read the base branch from git rather than the working copy:

```ts
const main = JSON.parse(gitShow('origin/main', 'backend/src/db/migrations/meta/_journal.json'));
assert.ok(mine.when > Math.max(...main.entries.map(e => e.when)));  // strictly greater
assert.equal(mine.tag, basename(mine.file, '.sql'));                // tag ↔ file
// strictly increasing `when`; `idx` unique (and contiguous where the repo requires it)
```

## Content-level checks a suite never covers

Assertions only protect what someone thought to write. For generated/derived artifacts, a typecheck
and a green suite say nothing about **content plausibility**. Add explicit content assertions for
the artifact's domain. Examples that were all real gaps:

- **Domain-implausible values** pass structural asserts: a maximal eccentric curl prescribed at
  15-20 reps, or plyometric work at 15-20 reps. Assert the *class*, not the instance — explosive /
eccentric / maximal-intent work stays low-rep (`<= 10`), steady-state and mobility may be high-rep.
- **Exact-shape vs membership.** `expect(arr).toContain(X)` passes on `[X, Y, Z]`. When the
  requirement is "only X", assert `expect(arr).toEqual([X])`. A membership assert where the
  invariant is exclusivity is a silent hole.
- **Weeks/repeats/iterations must differ** where variety is a requirement — flatten and compare
  the collections, and compare the *volume* too, not just a label.

## Reviewing tests a subagent authored

A subagent reporting "N tests pass" on assertions IT wrote is a self-report about its own coverage
scoping. When handed a test file from a fan-out:

1. Read the assertions, not the summary — check scoping (1) and vacuity (2) first.
2. Independently re-derive the artifact from source rather than trusting the count (see the
data-artifact and documentation sections in `agent-code-verification-pitfalls`).
3. Confirm each assertion fails when the fix is reverted (3).

## Verification checklist

- [ ] Every assertion iterates every element its invariant claims (no `[0]`-only loops)
- [ ] No `or` between expectations unless the branches are genuinely interchangeable
- [ ] Each new assertion was proven to FAIL with the fix reverted
- [ ] Select/radio/tab assertions stub >=2 entries, so a wrong-but-valid value can fail (§3b)
- [ ] Budget/cascade guard tests import the real constants; mutating one turns them red
- [ ] Membership (`toContain`) used only where membership — not exclusivity — is the requirement
- [ ] Hooks/callbacks are observed after a real run of the loop, not invoked by the test itself (§5)
- [ ] No assertion whose subject is a log string where the claim is an effect (§5)
- [ ] Ordering invariants assert strictly-increasing/unique/tag↔file, never "I am the last entry" (§6)
- [ ] Journal assertions compare `when` against the base branch's journal read via `git show` (§6)
- [ ] Domain-implausible values have an explicit content assertion

## Relationship to other skills

Overlaps deliberately with the protected `agent-code-verification-pitfalls` (subagent code
verification; its §1 false-green mirrored-logic test is the sibling failure mode) and
`leaf-subagent-test-verification-apollo` (leaf-delivered tests being wrong). Both are
externally-owned / read-only to autonomous curation, so this lesson could not be added there.
If they are adopted (`hermes curator adopt <name>`), fold §1-3 into them and retire this skill.

A loosened matcher (`toContain` turned into `.some()`, `exact: true` turned into `exact: false`)
is `test-suite-integrity` Rule 1, not this skill. Do not merge the two.
