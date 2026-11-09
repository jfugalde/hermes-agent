# agent-skills (vendored)

Upstream: https://github.com/addyosmani/agent-skills
Pinned commit: 301b02b12bff98e4688e6984af6556802322bbde (main, 2026-11-01)
License: MIT (see LICENSE — attribution kept)
Vendored: 2026-11-09
Review: `~/.hermes/skills/software-development/third-party-skill-adoption-review`

## What was taken

- `skills/` — all 25 SKILL.md files, plus their `references/` and `scripts/`
  (`constraint-driven-development/scripts/quality-gate.mjs`, `idea-refine/scripts/`).
- `evals/` — upstream's own eval harness (tier 1 structural validators + tier 2
  trigger/routing evals). Kept so the bundle can be re-validated in place:
  `node scripts/run-evals.js --min-rank1 95` from `scripts/`.
- `scripts/` — upstream validators (`validate-skills.js`, `validate-versions.js`, ...).

## What was NOT taken

- `hooks/` — injects a second skill router on SessionStart. Hermes routes skills
  natively from frontmatter descriptions; a second router would double-route.
  Upstream's own hook header warns against this.
- `.claude/`, `.gemini/`, `.codex-plugin/`, `.opencode/`, `commands/`, `agents/` —
  host-specific slash commands and subagent definitions in formats Hermes does not
  read. Not portable.
- `docs/` — setup guides for other hosts. Read once during review, not installed.

## Where it loads

Added as a second `skills.external_dirs` entry in the config of every engineering
profile: default, apollo, deployer, orchestrator, researcher, reviewer, reviewer2,
specifier, supp-ops.

Deliberately NOT in `~/.hermes/skills`: nihongo-jose (and athena) inherit that dir
via their own `external_dirs`, and José excluded athena/nihon from this rollout.

## Updating

Re-clone upstream at a new sha into `~/.hermes/cache/scratch/`, then re-copy
`skills/ evals/ scripts/ LICENSE` over this tree and bump the sha above. Run
`node scripts/run-evals.js --min-rank1 95` afterwards — a rank-1 drop means the
new descriptions collide with each other and the bundle needs a description fix
upstream before it is worth keeping.

## Validators

Run from this directory:

```bash
node scripts/validate-skills.js .        # OK
node scripts/validate-reference-links.js .   # OK
node scripts/validate-artifact-paths.js .    # OK
node scripts/run-evals.js --min-rank1 95     # 150 checks, rank-1 100% (97/97)
```

`scripts/validate-versions.js` is EXPECTED to fail here: it asserts that
`plugin.json` and the four host plugin manifests (`.claude-plugin/`,
`.codex-plugin/`, `.agents/`) carry the same version. This tree deliberately
ships none of the host manifests, so there is nothing to compare.

## Known overlap with the local library

Deliberate. These duplicates local skills by subject, not by content:
`code-review-and-quality` (~github-code-review, pr-review-swarm-pattern),
`git-workflow-and-versioning` (~git-workflow/*), `test-driven-development`,
`observability-and-instrumentation`, `ci-cd-and-automation`,
`planning-and-task-breakdown` (~agentic-execution-plan-format).

Filling real gaps (no local equivalent by name): `constraint-driven-development`,
`source-driven-development`, `doubt-driven-development`, `interview-me`,
`idea-refine`, `deprecation-and-migration`, `shipping-and-launch`,
`documentation-and-adrs`.
