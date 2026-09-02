#!/bin/bash
# Orchestrator profile gateway — inject Ollama/Cursor secrets only.
# Do NOT load default ~/.hermes/.env (contains TELEGRAM_BOT_TOKEN → 409 with default gateway).
set -euo pipefail
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/profiles/orchestrator}"
export VIRTUAL_ENV="${VIRTUAL_ENV:-$HOME/.hermes/hermes-agent/venv}"
export PATH="${VIRTUAL_ENV}/bin:$HOME/.local/bin:$PATH"

# Shared op:// refs (must not sit in a Hermes-read .env — prefer-dotenv returns raw op://).
SECRET_OP="$HOME/.hermes/.env.op"
OP_SERVICE_ACCOUNT_TOKEN_FILE="${HOME}/.config/op/service-account-token"
if [[ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" && -f "${OP_SERVICE_ACCOUNT_TOKEN_FILE}" ]]; then
  OP_SERVICE_ACCOUNT_TOKEN="$(tr -d '\n\r' <"${OP_SERVICE_ACCOUNT_TOKEN_FILE}" || true)"
  export OP_SERVICE_ACCOUNT_TOKEN
fi

HERMES_ENV_FIX=(env -u OLLAMA_BASE_URL -u OLLAMA_HOST -u OLLAMA_PROXY_URL)
_op_args=()
[[ -f "$SECRET_OP" ]] && _op_args+=(--env-file="$SECRET_OP")
# Profile-local .env only (no telegram clash with default)
[[ -f "$HERMES_HOME/.env" ]] && _op_args+=(--env-file="$HERMES_HOME/.env")

GH_TOKEN=$(gh auth token 2>/dev/null || true)
export GH_TOKEN

if command -v op >/dev/null 2>&1 && [[ ${#_op_args[@]} -gt 0 ]] && [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
  echo "[hermes-gateway-orchestrator-run] op-resolved secrets from .env.op" >&2
  exec op run "${_op_args[@]}" -- "${HERMES_ENV_FIX[@]}" \
    "${VIRTUAL_ENV}/bin/python" -m hermes_cli.main --profile orchestrator gateway run
fi
echo "[hermes-gateway-orchestrator-run] op unavailable — starting without cloud secrets" >&2
exec "${HERMES_ENV_FIX[@]}" "${VIRTUAL_ENV}/bin/python" -m hermes_cli.main --profile orchestrator gateway run
