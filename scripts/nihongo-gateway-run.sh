#!/bin/bash
# Nihongo profile gateway runner with 1Password secrets (OLLAMA_API_KEY, etc.)
# Usage: nihongo-gateway-run.sh <nihongo-ale|nihongo-jose>
set -euo pipefail

PROFILE="${1:?profile required (nihongo-ale|nihongo-jose)}"
case "$PROFILE" in
  nihongo-ale|nihongo-jose) ;;
  *) echo "unknown profile: $PROFILE" >&2; exit 1 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"
PROFILE_DIR="${HOME}/.hermes/profiles/${PROFILE}"
TELEGRAM_ENV="${PROFILE_DIR}/.telegram-env"

export PATH="$HOME/.local/bin:$HOME/.hermes/bin:/usr/local/bin:/usr/bin:/bin"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export VIRTUAL_ENV="${VIRTUAL_ENV:-$HOME/.hermes/hermes-agent/venv}"
export PATH="${VIRTUAL_ENV}/bin:${PATH}"
export HERMES_PROFILE="$PROFILE"

# Per-profile API ports (match previous systemd units)
if [[ "$PROFILE" == "nihongo-jose" ]]; then
  export API_SERVER_PORT=8650
  DEFAULT_ALLOWED_USERS=514866495
else
  export API_SERVER_PORT=8651
  DEFAULT_ALLOWED_USERS=1780867489
fi
export API_SERVER_KEY="${API_SERVER_KEY:-nihongo-jose-local}"
export API_SERVER_HOST="${API_SERVER_HOST:-0.0.0.0}"

# Bot token from profile file (chmod 600); do not put secrets in unit files.
if [[ -f "${TELEGRAM_ENV}" ]]; then
  # shellcheck disable=SC1090
  set -a
  # only export TELEGRAM_* lines
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^TELEGRAM_ ]] || continue
    export "$line"
  done <"${TELEGRAM_ENV}"
  set +a
fi

export TELEGRAM_ALLOWED_USER_IDS="${TELEGRAM_ALLOWED_USER_IDS:-$DEFAULT_ALLOWED_USERS}"
export TELEGRAM_ALLOWED_USERS="${TELEGRAM_ALLOWED_USERS:-$TELEGRAM_ALLOWED_USER_IDS}"

# Allow TTS audio delivery from per-profile cache dirs
export HERMES_MEDIA_ALLOW_DIRS="${HERMES_MEDIA_ALLOW_DIRS:-${HOME}/.hermes/profiles/${PROFILE}/cache/audio}"

OP_SERVICE_ACCOUNT_TOKEN_FILE="${HOME}/.config/op/service-account-token"
if [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]] && [[ "${OP_SERVICE_ACCOUNT_TOKEN}" =~ ^[[:space:]]*$ ]]; then
  unset OP_SERVICE_ACCOUNT_TOKEN
fi
if [[ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" && -f "${OP_SERVICE_ACCOUNT_TOKEN_FILE}" ]]; then
  _sa="$(tr -d '\n\r' <"${OP_SERVICE_ACCOUNT_TOKEN_FILE}" || true)"
  if [[ -n "${_sa}" && ! "${_sa}" =~ ^[[:space:]]*$ ]]; then
    export OP_SERVICE_ACCOUNT_TOKEN="${_sa}"
  fi
fi
unset _sa || true

GH_TOKEN=$(gh auth token 2>/dev/null || true)
export GH_TOKEN

# Strip local Ollama URL env so ollama-cloud hits https://ollama.com/v1
HERMES_ENV_FIX=(env -u OLLAMA_BASE_URL -u OLLAMA_HOST -u OLLAMA_PROXY_URL)

HERMES_BIN="${VIRTUAL_ENV}/bin/python"

if command -v op >/dev/null 2>&1 && [[ -f "${ENV_FILE}" ]] && [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
  echo "[nihongo-gateway-run] ${PROFILE}: op-resolved secrets from ${ENV_FILE}" >&2
  exec op run --env-file="${ENV_FILE}" -- "${HERMES_ENV_FIX[@]}" \
    bash -c '
      export TELEGRAM_BOT_TOKEN="'"${TELEGRAM_BOT_TOKEN:-}"'"
      export TELEGRAM_ALLOWED_USERS="'"${TELEGRAM_ALLOWED_USERS}"'"
      export TELEGRAM_ALLOWED_USER_IDS="'"${TELEGRAM_ALLOWED_USER_IDS}"'"
      export API_SERVER_PORT="'"${API_SERVER_PORT}"'"
      export API_SERVER_KEY="'"${API_SERVER_KEY}"'"
      export API_SERVER_HOST="'"${API_SERVER_HOST}"'"
      export HERMES_PROFILE="'"${PROFILE}"'"
      exec "'"${HERMES_BIN}"'" -m hermes_cli.main -p "'"${PROFILE}"'" gateway run
    '
fi

echo "[nihongo-gateway-run] ${PROFILE}: op unavailable — launching without cloud secrets" >&2
exec "${HERMES_ENV_FIX[@]}" "${HERMES_BIN}" -m hermes_cli.main -p "${PROFILE}" gateway run
