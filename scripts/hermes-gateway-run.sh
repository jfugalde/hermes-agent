#!/bin/bash
# Default Hermes gateway runner with 1Password secrets (CURSOR_API_KEY, etc.)
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="${HERMES_HOME}/.env"
# op:// refs must NOT live in .env — Hermes get_env_value_prefer_dotenv() returns
# the raw "op://…" string as the API key (401). Keep refs in .env.op for op run only.
OP_ENV_FILE="${HERMES_HOME}/.env.op"

export PATH="$HOME/.local/bin:$HOME/.hermes/bin:/usr/local/bin:/usr/bin:/bin"
export HERMES_HOME
export VIRTUAL_ENV="${VIRTUAL_ENV:-$HOME/.hermes/hermes-agent/venv}"
export PATH="${VIRTUAL_ENV}/bin:${PATH}"

# Bootstrap OP_SERVICE_ACCOUNT_TOKEN from the well-known token file if not
# already in the environment (same pattern as apollo/athena gateway runners).
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
GH_TOKEN=$(gh auth token 2>/dev/null || true)
export GH_TOKEN

HERMES_BIN="${VIRTUAL_ENV}/bin/hermes"
HERMES_CMD=(gateway run --replace)

# Strip local Ollama daemon URLs so provider ollama-cloud reaches https://ollama.com/v1
# (local providers keep explicit api: URLs in config.yaml).
HERMES_ENV_FIX=(env -u OLLAMA_BASE_URL -u OLLAMA_HOST -u OLLAMA_PROXY_URL)

# Hermes telegram adapter reads TELEGRAM_ALLOWED_USERS. Default profile .env
# historically uses TELEGRAM_NOTIFIER_ALLOWED_USER_ID (OpenClaw notifier).
_map_telegram_allowlist() {
  if [[ -n "${TELEGRAM_ALLOWED_USERS:-}" ]]; then
    return 0
  fi
  if [[ -n "${TELEGRAM_NOTIFIER_ALLOWED_USER_ID:-}" ]]; then
    export TELEGRAM_ALLOWED_USERS="${TELEGRAM_NOTIFIER_ALLOWED_USER_ID}"
  elif [[ -n "${TELEGRAM_ALLOWED_USER_ID:-}" ]]; then
    export TELEGRAM_ALLOWED_USERS="${TELEGRAM_ALLOWED_USER_ID}"
  elif [[ -n "${TELEGRAM_ALLOWED_USER_IDS:-}" ]]; then
    export TELEGRAM_ALLOWED_USERS="${TELEGRAM_ALLOWED_USER_IDS}"
  fi
}

# Infra .env carries shared API_SERVER_* (op:// key) used by apollo/athena and
# hermes-api.ryu-technologies.com → host.docker.internal:8642.
INFRA_ENV_FILE="${INFRA_ENV_FILE:-$HOME/infrastructure/.env}"

_op_env_args=()
[[ -f "${ENV_FILE}" ]] && _op_env_args+=(--env-file="${ENV_FILE}")
[[ -f "${OP_ENV_FILE}" ]] && _op_env_args+=(--env-file="${OP_ENV_FILE}")
[[ -f "${INFRA_ENV_FILE}" ]] && _op_env_args+=(--env-file="${INFRA_ENV_FILE}")

if command -v op >/dev/null 2>&1 && [[ ${#_op_env_args[@]} -gt 0 ]] && [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
  echo "[hermes-gateway-run] launching with op-resolved secrets (${_op_env_args[*]})" >&2
  _map_telegram_allowlist
  export API_SERVER_PORT="${API_SERVER_PORT:-8642}"
  export API_SERVER_HOST="${API_SERVER_HOST:-0.0.0.0}"
  export API_SERVER_ENABLED="${API_SERVER_ENABLED:-true}"
  exec op run "${_op_env_args[@]}" -- "${HERMES_ENV_FIX[@]}" "${HERMES_BIN}" "${HERMES_CMD[@]}"
fi

echo "[hermes-gateway-run] op/.env unavailable — launching gateway without cloud secret injection" >&2
_map_telegram_allowlist
exec "${HERMES_ENV_FIX[@]}" "${HERMES_BIN}" "${HERMES_CMD[@]}"
