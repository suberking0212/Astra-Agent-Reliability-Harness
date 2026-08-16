# Astra Agent Reliability Harness

## Stage 1 Governed Business Execution Closure

Stage 1 closes the governed business-execution boundary: Hermes sees only an
explicit business protocol (`astra_submit_task`, status, input/approval
resolution, cancellation, and `astra_business_*`).  It never receives an
execution token, execution/attempt ID, lease, fencing token, or Runtime
identity.  The independent Astra Runtime service resolves `task_id` to its
internal legal execution context and owns Gateway dispatch, evidence,
approval, reconciliation, and completion evaluation.

`task_id` is correlation only, not authority. The Plugin authenticates its
machine-to-machine Runtime request with a per-launch transport credential that
is never part of a tool schema, prompt, tool result, or terminal-child
environment; Runtime then authorizes
the Task against its internal execution, lease, and fencing state.

The launcher uses macOS `sandbox-exec` and fails closed if it is not usable.
It denies Hermes access to the authoritative Business Sandbox SQLite file and
raw Sandbox endpoint; business credentials stay only in the Runtime-service
process. The default `astra_full_hermes` profile restores Hermes' native file,
terminal, code-execution, browser, skills, memory, and curator capabilities.
Set `ASTRA_EXECUTION_PROFILE=astra_controlled_debug` to retain the restrictive
Stage 1 troubleshooting profile (the old `astra_controlled` name remains a
compatibility alias).

Hermes is the only CLI and conversation loop in this repository. Astra is a
Runtime and governance service, loaded into Hermes through the documented
project-plugin API.

```text
Hermes CLI
  ↓
Thin Astra project plugin
  ↓
Production Runtime → Governance → Tool Gateway → Business Sandbox
```

Start Hermes with the Astra plugin and local Business Sandbox:

```bash
./scripts/start_astra_cli.sh
```
直接运行下面命令重新选择/恢复 provider，选择模型：
```bash
HERMES_HOME="$HOME/.astra-agent-reliability-hermes-home" hermes-agent-main/.venv/bin/hermes model
```

The script starts no Astra CLI; it replaces itself with the configured native
`hermes` executable. It automatically uses the workspace's
`hermes-agent-main/.venv/bin/hermes` when available; otherwise it uses the
`hermes` executable on `PATH`. Override either choice with `HERMES_BIN`.
The adapter exposes semantic capabilities only. The current product capability
is `astra_get_latest_customer_order`; Runtime transport, authentication, task,
and execution mechanics are not model-visible.

`start_astra_cli.sh` starts a loopback Astra Runtime service before Hermes. The
service owns the Business Sandbox endpoint and credential; those values are
removed from the Hermes process environment before the CLI starts. The
authoritative Sandbox SQLite database defaults outside the workspace at
`~/.astra-agent-reliability/business-sandbox.sqlite3`, so it is not available
as a project file to Hermes terminal tools. The loopback Runtime API exposes
only governed operations, never the Business Sandbox credential or its raw
database interface.

Before creating the interactive session, the launcher enters Astra's in-process
session-binding adapter. The adapter attaches `astra_capabilities` through
Hermes' per-invocation toolset option, verifies Hermes' resolved schema contains
`astra_get_latest_customer_order`, and calls Hermes' installed console
entrypoint in that same process. It does not create a preflight chat or a second
Hermes process.

The project plugin records the real parent session's provider-facing tool list
at `~/.astra-agent-reliability-hermes-home/logs/astra-parent-session-provider-tools.json`.
If plugin registration or session projection fails, launch stops before the
model can use terminal, Skills, file search, curl, or another process as a
fallback.

## Public-interface boundary

The adapter uses only Hermes' documented project-plugin contract:

- project plugin discovery (`HERMES_ENABLE_PROJECT_PLUGINS=true`);
- `PluginContext.register_command`;
- `PluginContext.register_tool`.

Hermes 0.18.2 has no plugin callback that attaches a dynamic toolset while the
current CLI session is being constructed. Astra therefore adds the smallest
process-local adapter around Hermes' installed console entrypoint and documented
`--toolsets` invocation option. It does not implement a conversation loop.

No Hermes source is modified, copied, imported as an implementation detail, or
monkey-patched.

Hermes 0.18.2 does not expose a public API to bind an Astra Task to the active
CLI session, inject per-task Runtime context, resume an execution in that
session, or automatically map `waiting_input` / `waiting_approval` to its
interactive loop. The plugin therefore uses an explicit `task_id` business
protocol. Automatic session binding, native pause/resume, and approval UI
handoff remain intentionally unavailable.

## Stage 2A: CLI session correlation

**Stage 2A — Session ↔ Task Correlation + Resume Correlation: COMPLETE**

Stage 2A adds only an Astra-owned correlation store mapping the public Hermes
`hermes_session_id` to the authoritative Astra `task_id`. It is written only
after `astra_submit_task` returns a valid task ID; `on_session_start` never
creates or infers a task. `pre_llm_call` re-reads the store each turn, so native
`hermes --resume <session_id>` and `hermes --continue` recover the same task ID
from Astra storage. Conflicting remaps are rejected and logged fail-closed.

Only CLI session create/resume is supported. Reset/rotation, missing or
finalized IDs, unknown sessions, and gateway mode do not inherit a mapping.
`inject_message()`, waiting-input/approval handoff, and all
execution/attempt/lease/fencing/Runtime-identity fields remain out of scope.

This limitation does **not** rule out an explicit public tool protocol. Hermes
also reserves its native `/status` and `/cancel` names, so Astra does not
override them. The Runtime fails closed rather than creating a second loop or
relying on Hermes internals.
