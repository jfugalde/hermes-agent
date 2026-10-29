---
name: third-party-skill-adoption-review
description: "Use when vetting an external skill or gate-tool repo."
version: 1.0.1
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [skill-adoption, code-review, gh-cli, verification, license]
    related_skills: [ci-gate-integrity, spike]
---

# Third-Party Skill / Tool-Bundle Adoption Review

For "how valuable is <github url>?", "should we install this skill?", "is this toolchain worth
it?". The deliverable is NOT a code review — it is a **purchase decision**: what is substance,
what is hype, what to take, what to reject, and one concrete pilot next.

Composes with `ci-gate-integrity` (what a gate must prove before you trust it) and `spike`
(throwaway probe discipline). Read the remote repo through `gh api` before cloning it.

## When to Use

- "How valuable is <repo url>?", "worth adopting?", "should we install this skill?"
- A colleague or feed points at an agent skill / prompt pack / quality-gate toolchain and you
  must say take-it-or-leave-it with evidence.
- Before adding any third-party SKILL.md, plugin, or gate tool to this user's library.
- Not for: reviewing your own repo for bugs, or a line-by-line PR diff. This skill is a
  purchase decision.

## Step 1 — Measure substance before reading a word

```bash
gh api repos/<owner>/<repo> \
  --jq '{size,stars:.stargazers_count,forks:.forks_count,created:.created_at,pushed:.pushed_at,license:.license.spdx_id,open_issues:.open_issues_count}'
gh api repos/<owner>/<repo>/contents --jq '.[]|"\(.type) \(.size) \(.name)"'
gh api repos/<owner>/<repo>/commits --jq '.[]|"\(.sha[0:7]) \(.commit.author.date) \(.commit.message|split("\n")[0])"'
```

Then count the prose:

```bash
gh repo clone <owner>/<repo> $SCRATCH/<repo> -- --depth=1 -q
wc -w -l -c $SCRATCH/<repo>/SKILL.md
```

**Stars measure the author's audience, not the artifact.** A repo can carry thousands of
stars on a few kilobytes of prose and four commits in four days. Say that ratio out loud in
the verdict — it is the single most useful line in the review. Counter-signals that do carry
weight: a runnable tool, its own test suite, an explicit license, an upstream with a real tree.

## Step 2 — Diff the declared lineage FIRST

Most agent skills are derivatives. The README says so in one clause; treat that clause as the
thesis of the review.

```bash
gh api repos/<upstream>/<repo>/contents/<dir> --jq '[.[]|select(.type=="dir")|.name]|length, .'
gh api repos/<upstream>/<repo>/contents/<dir>/<skill>/SKILL.md --jq .content | base64 -d | head -25
```

Compare the derivative's section names against the upstream's file names. If every principle
maps 1:1 onto an upstream directory, the derivative is a **compression**, and the question
becomes whether the compression kept the mechanism or kept only the slogan. Upstream leaves
usually carry Why / Pattern / Stop; a one-line derivative that drops them produces sentences
nobody can act on — a reader has to load the original to interpret them. Name that in the
verdict with one side-by-side example.

## Step 3 — License, before recommending an install

- No `LICENSE` file plus an unanswered "what license?" issue = legally unresolvable. Do not
  recommend vendoring it into the user's repos; recommend reading it and rewriting the rules.
- `0BSD` / `MIT` / `Apache-2.0` tools are safe to vendor; MIT still obliges attribution.
- Separate the skill's license from the tools' licenses — bundles mix them routinely.

## Step 4 — Run the tools on a throwaway probe, and check the math by hand

Never run an unknown quality tool inside the user's repo. Probe under `$SCRATCH`:

```bash
mkdir -p $SCRATCH/probe && cd $SCRATCH/probe
# smallest fixture that produces a KNOWN metric value
<tool> --no-coverage .     # then again with coverage
```

Verify the formula arithmetically on the degenerate case — the cheapest check, and the one that
catches a mislabelled column: zero-coverage input must land exactly on the closed-form value
(e.g. `CRAP = CC^2*(1-cov)^3 + CC` gives `CC=6, cov=0` -> `42.0`). If the numbers reconcile by
hand the tool is real; if not, everything downstream of it is decoration.

Mechanics that bite: these tools shell out to bare `python3` / `pytest` / `coverage` from PATH,
not from the project, so prepend the interpreter that has the deps
(`PATH=$VENV/bin:$PATH <tool> .`). A tool that self-bootstraps a `.venv` on first run also
installs `coverage`/`pytest` into the target project when missing — fine in a container, rude
on a host; say so in the verdict.

## Step 5 — Break the input; a gate you did not write must fail CLOSED

Remove the thing the gate depends on and see whether it refuses to report. The pass/fail rule
lives in `ci-gate-integrity` Rule 10. Do not restate it.

For the CRAP family, the fail-open lines and the only wrapper worth keeping are in
`references/quality-gate-toolchains.md`. Run
`~/.hermes/skills/devops/ci-gate-integrity/scripts/crap-gate.sh`, not raw crapper.

## Step 6 — Consistency sweep across the bundle's own defaults

Read every threshold the bundle and its docs declare and put them side by side: a skill that
says "under 10" next to a tool whose README calls 5-30 "worth a look" next to a duplication
default of `0.82` for a rule that says `0.8` is a bundle that will not enforce what it claims.
Pick ONE number per metric, state it in the verdict, and say which document loses.

## Step 7 — Count the authority the bundle grants the agent

Adopting a skill is adopting its permissions. Grep for anything that hands the agent a decision
the user has already gated:

```bash
grep -nE 'push to (main|the main)|reclaim the disk|delete|force|--no-verify|skip' $SCRATCH/<repo>/SKILL.md
```

Push-to-main-at-the-end and "the agent decides what is safe to delete" are disqualifying as
written; recommend the rewrite ("commit often, stop before pushing"; "list what it would remove
and wait") instead of the tool.

## Step 8 — Overlap check against the local library

Before recommending an install, count and grep the user's own skills — a 700-word prose skill
that restates discipline the library already encodes adds nothing, especially when its trigger
is a slash command nobody will type, so it will never load.

```bash
find ~/.hermes/skills -name SKILL.md | wc -l
grep -rlE '<one principle per line>' ~/.hermes/skills --include=SKILL.md | head
```

If a native port of the upstream already exists for the user's harness, name it as the
alternative — and mark it unvetted unless you actually read it.

## Deliverable shape

Plain text for CLI. In order:

1. **Verdict line first**, split by use: as a read / as something to install / the tools alone —
   different numbers, and their disagreement IS the finding.
2. Substance numbers with the command that produced them (bytes, words, commits, files, tests).
3. What is genuinely valuable, and what you verified by running rather than reading.
4. Caveats that would cost the user time: fail-open gates, interpreter side effects, license,
   contradictory thresholds, authority grants.
5. Two or three lines worth stealing verbatim, quoted exactly.
6. One concrete next pilot offered as a question ("want me to run <tool> on <their repo> and
   report the offenders?") — never a vague "should we adopt it?".

## Pitfalls

| Pitfall | Guard |
|---|---|
| Star count read as quality | Count files, words, commits, tests; report the ratio |
| Accepting "based on X" without reading X | Fetch the upstream tree; map 1:1 and check whether Why/Pattern/Stop survived |
| Recommending an install with no license | No LICENSE + open license question = read-and-rewrite, not vendor |
| Trusting the tool's exit code or a green report | Delete the measurement input first; a report written after "could not measure" is not a gate |
| Verifying a metric by reading its docs | Recompute one row by hand on the 0% branch |
| Running the probe inside the user's repo | Probe in `$SCRATCH`; never clone an unknown tool into a live tree |
| Quoting the bundle's threshold as its behaviour | Grep every threshold (skill + tool README + defaults) and reconcile |
| Judging a skill by its prose only | Grep for push/delete/force grants; permissions travel with the skill |
| Reviewing in the abstract | Every claim in the verdict must come from a command you ran |
