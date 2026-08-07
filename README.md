# Astra Agent Reliability Harness

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

The script starts no Astra CLI; it replaces itself with the configured native
`hermes` executable. The adapter registers the public
`astra_runtime_status` tool, which returns a localized Runtime state.

## Public-interface boundary

The adapter uses only Hermes' documented project-plugin contract:

- project plugin discovery (`HERMES_ENABLE_PROJECT_PLUGINS=true`);
- `PluginContext.register_command`;
- `PluginContext.register_tool`.

No Hermes source is modified, copied, imported as an implementation detail, or
monkey-patched.

Hermes 0.18.2 does not expose a public API to bind an Astra Task to the active
CLI session, inject per-task Runtime context, resume an execution in that
session, or map `waiting_input` / `waiting_approval` to its interactive loop.
Consequently automatic Task execution, interactive approval/input handoff,
`/task`, and `/resume` are intentionally unavailable. Hermes also reserves its
native `/status` and `/cancel` names, so Astra does not override them. The
Runtime fails closed rather than creating a second loop or relying on Hermes
internals.
