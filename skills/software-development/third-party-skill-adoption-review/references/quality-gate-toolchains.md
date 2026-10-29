# Quality-Gate Toolchain Anatomy (CRAP / duplication / mutation)

Depth for Steps 4-6 of the parent skill: what these tools are, how to run one, and the failure
modes to look for. Applies to any "deterministic quality gate" bundle, not just one author's.

## The three metrics worth having

| Metric | Formula / method | Threshold that is defensible |
|---|---|---|
| CRAP | `CRAP = CC^2 * (1 - coverage)^3 + CC` (CC = cyclomatic complexity) | first gate: 30 on changed functions only. Under 10 is later, and only on new functions: CC >= 10 fails a strict <10 gate even at 100% coverage |
| Duplication | Jaccard similarity of normalized structural fingerprints, per function | review candidates >= 0.8; also enforce a minimum line count (3-line twins are noise) |
| Mutation score | killed / (killed + survived) | kill survivors in changed functions; do not chase 100% globally |

Only CRAP is worth a gate. Dryer is a later manual review of candidates at or above 0.8, and
only after CRAP has caught real offenders. Mutator tests the tests and multiplies runtime by
the mutant count: one file, by hand, never pre-push. Do not adopt the trio together.

## Running one on a probe

```bash
gh repo clone <owner>/crapper $SCRATCH/crapper -- --depth=1 -q
cd $SCRATCH/crapper && ./crapper --no-coverage ../probe/src   # complexity only, no deps needed
./crapper --threshold 10 ../probe/src                          # exit 2 when the worst exceeds
```

- First run builds `.venv` and `pip install -e .` (needs `tree-sitter` + `tree-sitter-language-pack`).
- Python coverage path: `coverage run` (pytest if present, else unittest) -> `coverage lcov` ->
  `target/coverage/python/lcov.info`, then parsed back per line.
- Other languages: Go `coverage.out`, Java JaCoCo XML, Clojure coverage HTML forms, Rust lcov.
- Coverage discovery is glob-based under `target/coverage/**`; a report elsewhere is invisible
  without `--use-existing-coverage` or a `--coverage-command` override.
- The tools invoke bare `python3` / `pytest` / `coverage` from PATH, not from the project.
  Prepending a venv that has them is the difference between a real score and a `0.0%` column.
- `mutator` expects `crapper` checked out as a sibling directory (`../crapper`) — it takes
  function names and namespaces from there.

## Fail-open signatures (verify before trusting a green run)

| Output | Meaning |
|---|---|
| `coverage is missing ... will score 0%` + snapshot still written | measurement failed, artifact written anyway |
| `NO TESTS RAN ... Coverage was still recorded` + report written | test collection failed; the coverage file exists but covers nothing |
| missing coverage scored as `0.0%` | conservative direction (CRAP inflated) but indistinguishable in the table from a genuinely untested function |
| exit 0 + rewritten snapshot after a failed baseline | mutation tools should exit non-zero and leave the snapshot untouched |

The wrapper is `~/.hermes/skills/devops/ci-gate-integrity/scripts/crap-gate.sh`. It is the
gate. It requires paths, refuses `--no-coverage`, refuses to run when `coverage` is not
already importable, and deletes `.metrics/crap.edn` if the output contains a could-not-measure
line. Default threshold is 30 via `CRAP_THRESHOLD`. Do not wire it into pre-push until a
changed-function histogram picks the number.

## Reading the numbers back

- One snapshot file per run (e.g. `.metrics/crap.edn`, `.metrics/dry.edn`,
  `.metrics/mutate/<namespace>.edn`) is what makes these tools usable as a PR gate: diff two
  runs instead of re-reading a table.
- Rename or move creates a NEW entry — there is no identity matching across runs, so a rename
  reads as new offenders plus deletions.
- Differential mutation reruns only survivors and sites in functions whose text changed; that
  is what makes the gate affordable on a real repo. The first full run executes every covered
  mutant — start with one file.
- Never quote a CRAP number without its coverage column; complexity alone is not the score.
