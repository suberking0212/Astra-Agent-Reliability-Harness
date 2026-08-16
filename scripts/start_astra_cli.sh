#!/usr/bin/env bash
# Prepare Astra-owned services, then hand the terminal to Hermes' native CLI.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python}"
# Prefer an explicitly configured Hermes executable.  The checked-in Hermes
# source snapshot also carries its development virtual environment, so use it
# when Hermes has not been installed globally (or its bin directory is not on
# PATH).
HERMES_BIN="${HERMES_BIN:-}"
if [[ -z "$HERMES_BIN" ]]; then
    BUNDLED_HERMES_BIN="$PROJECT_ROOT/hermes-agent-main/.venv/bin/hermes"
    if [[ -x "$BUNDLED_HERMES_BIN" ]]; then
        HERMES_BIN="$BUNDLED_HERMES_BIN"
    else
        HERMES_BIN="hermes"
    fi
fi
SANDBOX_HOST="${ASTRA_BUSINESS_SANDBOX_HOST:-127.0.0.1}"
SANDBOX_PORT="${ASTRA_BUSINESS_SANDBOX_PORT:-8765}"
SANDBOX_DATABASE="${ASTRA_BUSINESS_SANDBOX_DATABASE:-$HOME/.astra-agent-reliability/business-sandbox.sqlite3}"
SANDBOX_TOKEN="${ASTRA_BUSINESS_SANDBOX_TOKEN:-local-sandbox-token}"
LOG_FILE="${ASTRA_BUSINESS_SANDBOX_LOG_FILE:-$PROJECT_ROOT/var/business-sandbox.log}"
RUNTIME_HOST="${ASTRA_RUNTIME_HOST:-127.0.0.1}"
RUNTIME_PORT="${ASTRA_RUNTIME_PORT:-8766}"
RUNTIME_LOG_FILE="${ASTRA_RUNTIME_LOG_FILE:-$PROJECT_ROOT/var/astra-runtime.log}"
RUNTIME_PID_FILE="${ASTRA_RUNTIME_PID_FILE:-$PROJECT_ROOT/var/astra-runtime.pid}"
RUNTIME_PLUGIN_AUTH="${ASTRA_RUNTIME_PLUGIN_AUTH:-}"
ASTRA_EXECUTION_PROFILE="${ASTRA_EXECUTION_PROFILE:-astra_full_hermes}"
if [[ -z "$RUNTIME_PLUGIN_AUTH" ]]; then
    if ! command -v openssl >/dev/null 2>&1; then
        echo "Refusing to launch Hermes: openssl is required to create Runtime transport authentication." >&2
        exit 78
    fi
    RUNTIME_PLUGIN_AUTH="$(openssl rand -hex 32)"
fi

export ASTRA_BUSINESS_SANDBOX_ENDPOINT="http://$SANDBOX_HOST:$SANDBOX_PORT"
export ASTRA_BUSINESS_SANDBOX_TOKEN="$SANDBOX_TOKEN"
export ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN="${ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN:-commerce.local-sandbox}"
export ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN="${ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN:-support.local-sandbox}"
export HERMES_ENABLE_PROJECT_PLUGINS=true
# Keep Hermes' own mutable state outside the project runtime directory.  The
# project ``var/`` tree is intentionally denied to the sandboxed Agent.
export HERMES_HOME="${HERMES_HOME:-$HOME/.astra-agent-reliability-hermes-home}"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"

if [[ "$HERMES_BIN" == */* ]]; then
    hermes_available=0
    [[ -x "$HERMES_BIN" ]] || hermes_available=1
else
    hermes_available=0
    command -v "$HERMES_BIN" >/dev/null 2>&1 || hermes_available=1
fi
if (( hermes_available )); then
    cat >&2 <<EOF
Hermes CLI was not found: $HERMES_BIN

Install Hermes with the upstream installer, make its bin directory available on PATH,
or set HERMES_BIN to the Hermes executable you want Astra to launch.
EOF
    exit 127
fi

# Integration preflight must run with the same Python environment as Hermes;
# the system interpreter may know Astra but not Hermes' ``hermes_cli`` package.
HERMES_PYTHON_BIN="${HERMES_PYTHON_BIN:-}"
if [[ -z "$HERMES_PYTHON_BIN" && "$HERMES_BIN" == */bin/hermes ]]; then
    candidate_python="${HERMES_BIN%/bin/hermes}/bin/python"
    if [[ -x "$candidate_python" ]]; then
        HERMES_PYTHON_BIN="$candidate_python"
    fi
fi
HERMES_PYTHON_BIN="${HERMES_PYTHON_BIN:-$PYTHON_BIN}"

mkdir -p "$PROJECT_ROOT/var"
mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$RUNTIME_LOG_FILE")" "$(dirname "$RUNTIME_PID_FILE")"
mkdir -p "$(dirname "$SANDBOX_DATABASE")"
SANDBOX_DATABASE_DIR="$(cd "$(dirname "$SANDBOX_DATABASE")" && pwd -P)"
mkdir -p "$HERMES_HOME"
if [[ ! -f "$HERMES_HOME/config.yaml" ]]; then
    cp "$PROJECT_ROOT/.hermes/astra-runtime-config.yaml" "$HERMES_HOME/config.yaml"
fi
# This launcher owns an Astra+Hermes session.  Converge the mutable Hermes
# config on every launch so an older config (including known/disabled plugin
# toolsets) cannot silently select a different Astra profile.  The default is
# the product profile; set ASTRA_EXECUTION_PROFILE=astra_controlled_debug for
# the restrictive Stage 1 troubleshooting surface.
export ASTRA_EXECUTION_PROFILE
if [[ "$ASTRA_EXECUTION_PROFILE" == "astra_controlled_debug" || "$ASTRA_EXECUTION_PROFILE" == "astra_controlled" ]]; then
    export HERMES_IGNORE_RULES=1
else
    unset HERMES_IGNORE_RULES
fi
"$PYTHON_BIN" - "$HERMES_HOME/config.yaml" <<'PY'
from pathlib import Path
import os
import sys
import yaml

from astra.execution_profiles import get_execution_profile, materialize_hermes_learning_config

path = Path(sys.argv[1])
config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
profile_id = os.environ.get("ASTRA_EXECUTION_PROFILE", "astra_full_hermes")
profile = get_execution_profile(profile_id)
plugins = config.setdefault("plugins", {})
enabled = plugins.setdefault("enabled", [])
if not isinstance(enabled, list):
    enabled = []
    plugins["enabled"] = enabled
if "astra-runtime" not in enabled:
    enabled.append("astra-runtime")
# Apply the selected governed execution profile.  Native Hermes tools remain
# available in ``astra_full_hermes``; only the debug profile subtracts them.
agent = config.setdefault("agent", {})
agent["disabled_toolsets"] = list(profile.disabled_toolsets)
materialize_hermes_learning_config(config, profile)
skills_config = config["agent"]["skills"]
memory_config = config["agent"]["memory"]
curator = config.setdefault("curator", {})
if not isinstance(curator, dict):
    curator = {}
curator["enabled"] = bool(profile.enable_self_improvement)
config["curator"] = curator
config["astra_execution_profile"] = {
    "profile_id": profile.profile_id,
    "profile_version": profile.profile_version,
    "governed": profile.astra_governed,
    "skills": profile.enable_skills,
    "self_improvement": profile.enable_self_improvement,
    "skill_creation_nudge_interval": skills_config["creation_nudge_interval"],
    "memory_nudge_interval": memory_config["nudge_interval"],
    "disabled_toolsets": list(profile.disabled_toolsets),
}
config["astra_launcher_guidance"] = profile.launcher_guidance
path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
PY
# Astra integration is one launcher-owned, ephemeral guidance source.  Remove
# historical Astra-specific Skills rather than disabling Hermes Skills: those
# old playbooks make the Runtime protocol look like a repository investigation
# task and can conflict with the visible plugin capability.
rm -rf "$HERMES_HOME/skills/productivity/astra-governed-business-ops" \
       "$HERMES_HOME/skills/data-science/astra-governed-task-submission" \
       "$HERMES_HOME/skills/security/governed-data-boundaries" \
       "$HERMES_HOME/skills/astra-business-sandbox"
"$PYTHON_BIN" -m business_sandbox --database "$SANDBOX_DATABASE" seed

if ! lsof -nP -iTCP:"$SANDBOX_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    "$PYTHON_BIN" -m business_sandbox --database "$SANDBOX_DATABASE" \
        serve --host "$SANDBOX_HOST" --port "$SANDBOX_PORT" >"$LOG_FILE" 2>&1 &
fi

# The Runtime service, not Hermes, owns the business endpoint/token.  Hermes
# receives only this governed API; raw terminal commands cannot read the
# authority credential from their environment.
# Never silently reuse a Runtime carrying a previous launch's credential.  A
# stale owned Runtime is stopped and recreated; an unrelated listener fails
# closed rather than producing a Plugin/Runtime 401 split-brain.
if [[ -f "$RUNTIME_PID_FILE" ]]; then
    old_pid="$(<"$RUNTIME_PID_FILE")"
    if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
        old_command="$(ps -p "$old_pid" -o command= 2>/dev/null || true)"
        if [[ "$old_command" == *"astra.runtime_service"* ]]; then
            kill "$old_pid" 2>/dev/null || true
            for _ in {1..20}; do
                kill -0 "$old_pid" 2>/dev/null || break
                sleep 0.05
            done
        fi
    fi
    rm -f "$RUNTIME_PID_FILE"
fi
# Migrate runtimes created before the PID file existed: only remove a listener
# whose command is this Astra Runtime service.  Any unrelated listener remains
# a hard fail-closed condition.
for listener_pid in $(lsof -t -nP -iTCP:"$RUNTIME_PORT" -sTCP:LISTEN 2>/dev/null || true); do
    listener_command="$(ps -p "$listener_pid" -o command= 2>/dev/null || true)"
    if [[ "$listener_command" == *"python"*"-m astra.runtime_service"* ]]; then
        kill "$listener_pid" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 "$listener_pid" 2>/dev/null || break
            sleep 0.05
        done
    else
        echo "Refusing to launch Hermes: Runtime port $RUNTIME_PORT is already occupied." >&2
        exit 78
    fi
done
if lsof -nP -iTCP:"$RUNTIME_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Refusing to launch Hermes: Runtime port $RUNTIME_PORT is still occupied." >&2
    exit 78
fi
ASTRA_RUNTIME_SERVICE_SERVER=1 ASTRA_RUNTIME_PLUGIN_AUTH="$RUNTIME_PLUGIN_AUTH" "$PYTHON_BIN" -m astra.runtime_service \
    --host "$RUNTIME_HOST" --port "$RUNTIME_PORT" >"$RUNTIME_LOG_FILE" 2>&1 &
runtime_pid=$!
echo "$runtime_pid" >"$RUNTIME_PID_FILE"
for _ in {1..40}; do
    if lsof -nP -iTCP:"$RUNTIME_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$runtime_pid" 2>/dev/null; then
        echo "Refusing to launch Hermes: Runtime failed during initialization." >&2
        exit 78
    fi
    sleep 0.05
done
if ! lsof -nP -iTCP:"$RUNTIME_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Refusing to launch Hermes: Runtime did not become ready." >&2
    exit 78
fi
export ASTRA_RUNTIME_ENDPOINT="http://$RUNTIME_HOST:$RUNTIME_PORT"
export ASTRA_RUNTIME_PLUGIN_AUTH="$RUNTIME_PLUGIN_AUTH"
export HERMES_EPHEMERAL_SYSTEM_PROMPT="Astra tools provide authoritative external capabilities. For governed business requests, use the matching astra_* capability directly. If it is unavailable, report an Astra/Hermes integration failure; do not use terminal, skills, file search, curl, or another Hermes process as a fallback."
# This preflight verifies Runtime readiness only. The real parent session's
# provider schema is audited inside that same process by the Astra plugin.
ASTRA_HERMES_BIN="$HERMES_BIN" \
PYTHONPATH="$PROJECT_ROOT:$PROJECT_ROOT/hermes-agent-main${PYTHONPATH:+:$PYTHONPATH}" \
    "$HERMES_PYTHON_BIN" "$PROJECT_ROOT/scripts/verify_astra_hermes_integration.py"
# The actual Hermes process must receive the transport credential so its
# project plugin can capture and remove it during plugin discovery. Do not
# unset it here: preflight is a separate process, so its in-memory capture
# cannot authorize the subsequently exec'd CLI. ``plugin.register`` pops the
# value before Hermes can construct an Agent or launch terminal children.
# Test probes are never part of the Hermes launch contract.
unset ASTRA_ALLOW_TEST_PROBE
unset ASTRA_BUSINESS_SANDBOX_ENDPOINT ASTRA_BUSINESS_SANDBOX_TOKEN \
    ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN

# This is an OS boundary, not an environment-variable convention.  The
# profile deliberately leaves Hermes' configured model provider available but
# denies the complete authoritative-data directory and the raw Business
# Sandbox port on every address family/interface.  Directory-level rules cover
# SQLite sidecars (-wal/-shm) and temporary files as well as the main database.
# If sandbox-exec is unavailable or rejects the profile, do not launch Hermes:
# a non-isolated terminal would violate the governed-business boundary.
if ! command -v sandbox-exec >/dev/null 2>&1; then
    echo "Refusing to launch Hermes: macOS sandbox-exec is unavailable; no equivalent isolation is configured." >&2
    exit 78
fi
SANDBOX_PROFILE_FILE="$(mktemp "${TMPDIR:-/tmp}/astra-hermes-profile.XXXXXX")"
chmod 600 "$SANDBOX_PROFILE_FILE"
cat >"$SANDBOX_PROFILE_FILE" <<EOF
(version 1)
(allow default)
(deny file-read* (subpath "$SANDBOX_DATABASE_DIR"))
(deny file-write* (subpath "$SANDBOX_DATABASE_DIR"))
(deny file-read* (subpath "$PROJECT_ROOT/var"))
(deny file-write* (subpath "$PROJECT_ROOT/var"))
(deny file-read* (subpath "$PROJECT_ROOT/business_sandbox"))
(deny file-write* (subpath "$PROJECT_ROOT/business_sandbox"))
(deny file-read* (literal "$PROJECT_ROOT/astra/business.py"))
(deny file-write* (literal "$PROJECT_ROOT/astra/business.py"))
(deny file-read* (subpath "$PROJECT_ROOT/scripts"))
(deny file-write* (subpath "$PROJECT_ROOT/scripts"))
(deny network-outbound (remote tcp "*:$SANDBOX_PORT"))
EOF
echo "Astra Hermes sandbox profile: $SANDBOX_PROFILE_FILE" >&2
if ! sandbox-exec -f "$SANDBOX_PROFILE_FILE" /usr/bin/true >/dev/null 2>&1; then
    echo "Refusing to launch Hermes: sandbox-exec profile is not accepted on this macOS host." >&2
    exit 78
fi
export ASTRA_PARENT_SESSION_AUDIT_FILE="${ASTRA_PARENT_SESSION_AUDIT_FILE:-$HERMES_HOME/logs/astra-parent-session-provider-tools.json}"
exec sandbox-exec -f "$SANDBOX_PROFILE_FILE" "$HERMES_PYTHON_BIN" -m astra.hermes_adapter.launch "$HERMES_BIN" "$@"
