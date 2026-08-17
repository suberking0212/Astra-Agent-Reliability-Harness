# Stage 2B-0: Hermes public command API spike

> **Historical notice:** This feasibility investigation retains its original
> result. Current implementation status is in [docs/STATUS.md](../STATUS.md);
> this document is not current implementation authority.

Status: PASS for session binding and fail-closed behavior; NO-GO for implicit agent resume.

## Verified

- Hermes `register_command` handlers receive only `raw_args`; they do not receive
  `session_id` as an argument.
- Astra can bind the current CLI session from the public `on_session_start` and
  `pre_llm_call` hook payloads and read that binding inside a command handler.
- A command can safely perform a read-only Runtime lookup while the binding is
  unambiguous.
- `on_session_reset` and `on_session_finalize` clear the binding. Observing a
  second CLI session makes the binding ambiguous; commands then fail closed.

## Not assumed

Hermes CLI slash-command dispatch invokes the handler and prints its return
value. It does not automatically enqueue a user message or start another model
turn. The spike command therefore returns a short structured result and does
not inject context, resume Execution, claim work, or select tools.

The next stage may add `/astra-input` only after this boundary is accepted. A
`pre_llm_call` resume context should be introduced only if a real CLI E2E proves
that plain command return text is insufficient for natural continuation.
