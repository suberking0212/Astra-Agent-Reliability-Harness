#!/usr/bin/env bash
# Prepare Astra-owned services, then hand the terminal to Hermes' native CLI.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python}"
HERMES_BIN="${HERMES_BIN:-hermes}"
SANDBOX_HOST="${ASTRA_BUSINESS_SANDBOX_HOST:-127.0.0.1}"
SANDBOX_PORT="${ASTRA_BUSINESS_SANDBOX_PORT:-8765}"
SANDBOX_DATABASE="${ASTRA_BUSINESS_SANDBOX_DATABASE:-$PROJECT_ROOT/var/business-sandbox.sqlite3}"
SANDBOX_TOKEN="${ASTRA_BUSINESS_SANDBOX_TOKEN:-local-sandbox-token}"
LOG_FILE="$PROJECT_ROOT/var/business-sandbox.log"

export ASTRA_BUSINESS_SANDBOX_ENDPOINT="http://$SANDBOX_HOST:$SANDBOX_PORT"
export ASTRA_BUSINESS_SANDBOX_TOKEN="$SANDBOX_TOKEN"
export ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN="${ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN:-commerce.local-sandbox}"
export ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN="${ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN:-support.local-sandbox}"
export HERMES_ENABLE_PROJECT_PLUGINS=true
export HERMES_HOME="${HERMES_HOME:-$PROJECT_ROOT/var/hermes-home}"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$PROJECT_ROOT/var"
mkdir -p "$HERMES_HOME"
if [[ ! -f "$HERMES_HOME/config.yaml" ]]; then
    cp "$PROJECT_ROOT/.hermes/astra-runtime-config.yaml" "$HERMES_HOME/config.yaml"
fi
"$PYTHON_BIN" -m business_sandbox --database "$SANDBOX_DATABASE" seed

if ! lsof -nP -iTCP:"$SANDBOX_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    "$PYTHON_BIN" -m business_sandbox --database "$SANDBOX_DATABASE" \
        serve --host "$SANDBOX_HOST" --port "$SANDBOX_PORT" >"$LOG_FILE" 2>&1 &
fi

exec "$HERMES_BIN" "$@"
