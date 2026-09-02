#!/bin/bash
# Apollo profile gateway runner with 1Password secrets
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"

export PATH="$HOME/.local/bin:$HOME/.hermes/bin:/usr/local/bin:/usr/bin:/bin"

# Bootstrap OP_SERVICE_ACCOUNT_TOKEN from the well-known token file if not
# already in the environment (mirrors hermes-gateway-run.sh bootstrap pattern).
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

# Inject GH_TOKEN from the gh CLI so the Copilot provider can authenticate.
# op run overrides the environment, so we must export it before exec.
GH_TOKEN=$(gh auth token 2>/dev/null || true)
export GH_TOKEN

# API server port — avoid conflict with default gateway (8642)
export API_SERVER_PORT=8648

HERMES_ENV_FIX=(env -u OLLAMA_BASE_URL -u OLLAMA_HOST -u OLLAMA_PROXY_URL)

if command -v op >/dev/null 2>&1 && [[ -f "${ENV_FILE}" ]] && [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
  echo "[apollo-gateway-run] launching with op-resolved secrets from ${ENV_FILE}" >&2
  exec op run --env-file="${ENV_FILE}" -- "${HERMES_ENV_FIX[@]}" \
    bash -c '
      unset TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USER_ID TELEGRAM_ALLOWED_USERS
      export API_SERVER_PORT=8648
      exec apollo gateway run
    '
fi

echo "[apollo-gateway-run] op/.env unavailable — launching apollo gateway without cloud secrets" >&2
exec "${HERMES_ENV_FIX[@]}" apollo gateway run
