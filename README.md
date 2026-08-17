# Astra Agent Reliability Harness

Astra supplies governed Task Runtime, evidence, and business-capability
boundaries around a Hermes Agent CLI. Hermes remains the conversation loop and
agent runtime; Astra does not create a second CLI or agent loop.

## Start

```bash
./scripts/start_astra_cli.sh
```

The launcher starts the local Business Sandbox and Astra Runtime service, then
executes the configured native Hermes CLI. It uses the workspace Hermes binary
when available; set `HERMES_BIN` to select another executable.

## Current Model Surface

The current model-visible Astra surface contains one semantic capability:

```text
astra_get_latest_customer_order(customer_id)
```

It returns an authoritative business result with receipt/evidence references.
Task, execution, lease, fencing, transport-authentication, approval, and
Gateway mechanics are not model-visible.

## Limits

- Hermes integration is currently pinned to 0.18.2.
- There is no public Hermes Task-to-session execution API, native pause/resume
  handoff, or approval UI callback.
- Local scripted-provider tests are not live-provider or learning-effectiveness
  evidence.

## Documentation

[Current implementation status](docs/STATUS.md) is the sole current status
source. Frozen ownership and Runtime contracts are indexed by
[Documentation Authority](docs/AUTHORITY.md). Historical protocols, phase
plans, and version investigations are retained under [docs/history](docs/history/).
