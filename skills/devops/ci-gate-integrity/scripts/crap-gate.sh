#!/usr/bin/env bash
# Fail-closed wrapper. crapper is the calculator; this script is the gate.
# crapper writes .metrics/crap.edn and can exit 0 when coverage did not run.
# Not for pre-push until a changed-function histogram picks CRAP_THRESHOLD.
# Default 30 is crapper's own "worth a look" line. Under-10 is a later decision:
# CC >= 10 fails a strict <10 gate even at 100% coverage.
set -euo pipefail

THRESHOLD="${CRAP_THRESHOLD:-30}"
CRAPPER="${CRAPPER:-crapper}"

if [[ "${1:-}" == "--threshold" ]]; then
  THRESHOLD="${2:?crap-gate: --threshold needs a number}"
  shift 2
fi

if [[ $# -eq 0 ]]; then
  echo "crap-gate: pass changed paths. Refusing a whole-tree run." >&2
  exit 2
fi

for arg in "$@"; do
  if [[ "$arg" == "--no-coverage" ]]; then
    echo "crap-gate: --no-coverage is not a CRAP measurement." >&2
    exit 2
  fi
done

if ! command -v "$CRAPPER" >/dev/null 2>&1; then
  echo "crap-gate: crapper is not on PATH (set CRAPPER= to a pinned checkout)." >&2
  exit 2
fi

if ! python3 -c 'import coverage' >/dev/null 2>&1; then
  echo "crap-gate: coverage is not importable. Refusing so crapper does not pip-install it into the project." >&2
  exit 2
fi

set +e
out="$("$CRAPPER" --threshold "$THRESHOLD" "$@" 2>&1)"
rc=$?
set -e
printf '%s\n' "$out"

if grep -qE 'coverage is missing|will score 0%|NO TESTS RAN|Coverage was still recorded' <<<"$out"; then
  rm -f .metrics/crap.edn
  echo "crap-gate: measurement failed. Snapshot removed. Not a score." >&2
  exit 2
fi

exit "$rc"
