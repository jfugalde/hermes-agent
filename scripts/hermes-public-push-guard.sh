#!/usr/bin/env bash
# Refuse overlay and personal branches on the public fork (jfugalde/hermes-agent).
# Product contributions (feat/fix/chore/sync) still push; they must not add overlay paths.
set -euo pipefail

deny_paths='^(vendor/|profiles/|skills/homelab/)'
deny_branch='^(google-accounts|feat/vendor-agent-skills)'

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [[ -n "$branch" && "$branch" != "HEAD" && "$branch" =~ $deny_branch ]]; then
  echo "public fork: refuse branch '$branch' (personal overlay). Overlay lives in jfugalde/hermes-profiles." >&2
  exit 1
fi

base="origin/main"
git rev-parse -q --verify "$base" >/dev/null 2>&1 || base="main"
# Added files only: deleting overlay from the public tree must be allowed.
if git rev-parse -q --verify "$base" >/dev/null 2>&1; then
  changed="$(git diff --name-only --diff-filter=A "$base"...HEAD || true)"
else
  changed="$(git diff --name-only --diff-filter=A --cached || true)"
fi

if printf '%s\n' "$changed" | grep -E "$deny_paths" ; then
  echo "public fork: refuse overlay paths (vendor/, profiles/, skills/homelab/)." >&2
  echo "Put those trees in jfugalde/hermes-profiles (private)." >&2
  exit 1
fi

if printf '%s\n' "$changed" | grep -E '(^|/)\.env$|(^|/)auth\.json$' ; then
  echo "public fork: refuse secret files (.env, auth.json)." >&2
  exit 1
fi

exit 0
