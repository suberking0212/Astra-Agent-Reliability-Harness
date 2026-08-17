"""Astra-owned Runtime HTTP service.  Hermes receives no execution authority."""

from __future__ import annotations

import argparse
import json
import os
import hmac
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

from .capability_broker import CapabilityBroker, CapabilityDefinition
from .artifact_observations import ArtifactActionObservation
from .correlation_store import HermesCorrelationStore
from .domain import InteractionKind, ToolInvocationContext
from .hermes_adapter.plugin import _error
from .phase3.completion import CompletionContract
from .phase3.effects import CanonicalEffectRequest
from .phase3.task_contract import TaskContract
from .production import ProductionConfig, ProductionRuntime
from .tool_catalog import TOOL_CATALOG
from .tool_catalog import allowed_capabilities, resolved_tools
from .phase3.canonical import sha256_digest
from .runtime import LeaseFencedError


class _NoPublicExecutor:
    async def execute(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError("hermes_session_execution_not_supported")

    async def cancel(self, *_: Any, **__: Any) -> None:
        return None


@dataclass(frozen=True)
class _Context:
    execution_id: str
    task_id: str
    attempt_id: str
    run_request_id: str
    lease_owner_id: str
    lease_token: str
    allowed_tools: tuple[str, ...]


class RuntimeService:
    """Owns all Task/Attempt/Execution/Lease identity behind a business API."""

    def __init__(self, *, business: Any, domain_extensions: tuple[Any, ...],
                 capability_definitions: tuple[CapabilityDefinition, ...],
                 authority_domains: dict[str, str]) -> None:
        self.transport_credential = os.environ.get("ASTRA_RUNTIME_PLUGIN_AUTH", "").strip()
        if not self.transport_credential:
            raise RuntimeError("Runtime service missing plugin transport authentication")
        self.app = ProductionRuntime(ProductionConfig(
            database_path=os.environ.get("ASTRA_DATABASE", "var/astra.sqlite3"),
        ), business_factory=lambda _store: business,
           domain_extensions=domain_extensions,
           executor_factory=lambda _: _NoPublicExecutor())
        self._contexts: dict[str, _Context] = {}
        self.capability_broker = CapabilityBroker(self, capability_definitions)
        self.authority_domains = dict(authority_domains)
        self.correlations = HermesCorrelationStore(os.environ.get("ASTRA_CORRELATION_DATABASE", os.environ.get("ASTRA_DATABASE", "var/astra.sqlite3") + ".correlation"))

    def close(self) -> None:
        self.correlations.close()
        self.app.close()

    def _start(self, task_id: str, run_request_id: str, allowed: tuple[str, ...]) -> _Context:
        claim = self.app.runtime.claim_and_start_execution(
            lease_owner_id="astra-runtime-service:" + str(uuid4()),
            lease_duration_seconds=self.app.config.lease_duration_seconds,
            run_request_id=run_request_id,
        )
        if claim is None:
            raise RuntimeError("task_not_ready_for_governed_execution")
        self.app.runtime.begin_execution(execution_id=claim.execution_id, task_id=claim.task_id,
                                         attempt_id=claim.attempt_id, lease_owner_id=claim.lease_owner_id,
                                         lease_token=claim.lease_token)
        context = _Context(claim.execution_id, task_id, claim.attempt_id, claim.run_request_id,
                           claim.lease_owner_id, claim.lease_token, allowed)
        self._contexts[task_id] = context
        return context

    def _recover_context(self, task_id: str, allowed_tools: tuple[str, ...] | None = None) -> _Context | None:
        """Reclaim a durable continuation after lease loss or service restart."""
        recovery_owner = "astra-runtime-recovery:" + str(uuid4())
        self.app.runtime.recover_expired_leases(
            lease_owner_id=recovery_owner,
            lease_duration_seconds=self.app.config.lease_duration_seconds,
        )
        pending = self.app.store.query_one(
                """SELECT run_request_id FROM phase4_run_requests
                   WHERE task_id = ? AND state = 'pending'
                   ORDER BY created_at DESC LIMIT 1""", (task_id,)
        )
        if pending is None:
            return None
        if allowed_tools is None:
            task = self.app.store.query_one("SELECT contract_json FROM phase3_tasks WHERE task_id = ?", (task_id,))
            if task is None:
                return None
            allowed_tools = tuple(item.tool_name for item in TaskContract.model_validate_json(task["contract_json"]).resolved_tools)
        claim = self.app.runtime.claim_and_start_execution(
            lease_owner_id=recovery_owner,
            lease_duration_seconds=self.app.config.lease_duration_seconds,
            run_request_id=str(pending["run_request_id"]),
        )
        if claim is not None:
            self.app.runtime.begin_execution(
                execution_id=claim.execution_id, task_id=claim.task_id,
                attempt_id=claim.attempt_id, lease_owner_id=claim.lease_owner_id,
                lease_token=claim.lease_token,
            )
            context = _Context(claim.execution_id, task_id, claim.attempt_id,
                claim.run_request_id, claim.lease_owner_id, claim.lease_token, allowed_tools)
            self._contexts[task_id] = context
            return context
        return None

    def _context(self, task_id: str) -> _Context:
        context = self._contexts.get(task_id)
        if context is None:
            recovered = self._recover_context(task_id)
            if recovered is not None:
                return recovered
            if self.app.store.query_one("SELECT 1 FROM phase3_tasks WHERE task_id = ?", (task_id,)) is not None:
                raise RuntimeError("governed_execution_recovery_pending")
            raise RuntimeError("no_active_governed_execution_for_task")
        try:
            heartbeat = self.app.runtime.heartbeat_lease(execution_id=context.execution_id,
                    lease_owner_id=context.lease_owner_id, lease_token=context.lease_token,
                    lease_duration_seconds=self.app.config.lease_duration_seconds)
        except LeaseFencedError:
            heartbeat = None
        if heartbeat is None:
            # Lease expiry is an internal Runtime lifecycle event. Hermes sees
            # only the continued governed business operation.
            recovered = self._recover_context(task_id, context.allowed_tools)
            if recovered is not None:
                return recovered
            self._contexts.pop(task_id, None)
            raise RuntimeError("governed_execution_recovery_pending")
        return context

    def submit(self, args: dict[str, Any]) -> dict[str, Any]:
        requested = tuple(dict.fromkeys(str(name) for name in args.get("requested_tools", ())))
        if not requested:
            raise ValueError("requested_tools must not be empty")
        unknown = [name for name in requested if name not in TOOL_CATALOG]
        if unknown:
            raise ValueError("requested_tool_not_in_catalog:" + ",".join(unknown))
        # An effect tool may be requested as part of a user-originated task,
        # but this only exposes a governed proposal path.  The Gateway turns
        # the first exact call into an approval-bound dynamic intent; it does
        # not grant execution authority merely because Hermes named a tool.
        # Keeping the effect in the Task Contract is required for the
        # Gateway to reject unbound tools and to derive its receipt/evidence
        # from the one authoritative operation.
        subjects = tuple(dict(subject) for subject in args.get("subjects", ()))
        if not subjects:
            raise ValueError("subjects must not be empty")
        for subject in subjects:
            if not all(str(subject.get(key, "")).strip() for key in ("authority_domain", "type", "id")):
                raise ValueError("invalid_subject_scope")
        objective = str(args.get("objective", "")).strip()
        if not objective:
            raise ValueError("objective must not be empty")
        inputs = dict(args.get("inputs") or {})
        intent = {"objective": objective, "subjects": subjects, "requested_tools": requested, "inputs": inputs}
        contract = TaskContract.materialize({
            "schema_version": "1", "contract_id": "contract:" + sha256_digest(intent), "contract_version": "1",
            "task_type": "hermes_business_task", "execution_type": "tool_execution",
            "objective": {"description": objective}, "subject_refs": subjects,
            "input_snapshot": {"schema_id": "hermes.public.intent", "schema_version": "1", "values": inputs,
                                "content_hash": sha256_digest(inputs)},
            "allowed_capabilities": [item.model_dump(mode="json") for item in allowed_capabilities(requested)],
            "resolved_tools": [item.model_dump(mode="json") for item in resolved_tools(requested)],
            "constraints": [], "authorized_effects": [], "approval_requirements": [],
            "completion_contract_ref": {"contract_id": "completion:business-observation", "contract_version": "1"},
            "limits": {"max_attempts": 3, "task_deadline": "2099-01-01T00:00:00Z",
                       "max_executions_per_attempt": 4, "max_feedback_cycles": 2, "max_reconcile_cycles": 3},
        })
        result = self.app.submit_task(command_id=str(args["command_id"]), task_id=str(args["task_id"]),
                                      contract=contract, completion_contract=None)
        self._start(result.task_id, result.run_request_id, tuple(item.tool_name for item in contract.resolved_tools))
        return {"ok": True, "task_id": result.task_id, "business_status": "running",
                "next_action": {"kind": "business_tool",
                                 "allowed_tools": ["astra_business_" + name for name in requested]}}

    def status(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args["task_id"])
        detail = self.app.task_status(task_id)
        task = detail["task"]
        interactions = [{key: item.get(key) for key in ("interaction_id", "kind", "purpose", "status", "version", "prompt")}
                        for item in detail["interactions"] if item.get("status") == "pending"]
        state = str(task["state"])
        next_action = ("resolve_approval" if state == "waiting_approval" else
                       "resolve_input" if state == "waiting_input" else
                       "business_operation" if state == "running" else "none")
        return {"ok": True, "task_id": task_id, "business_status": state,
                "next_action": next_action, "pending_interactions": interactions}

    def resolve(self, args: dict[str, Any], expected_kind: str) -> dict[str, Any]:
        interaction_id = str(args["interaction_id"])
        row = self.app.store.query_one("SELECT kind FROM interactions WHERE interaction_id = ?", (interaction_id,))
        if row is None or row["kind"] != expected_kind:
            raise RuntimeError("interaction_kind_mismatch")
        result = self.app.resolve_interaction(command_id=str(args["command_id"]), interaction_id=interaction_id,
            expected_version=int(args["expected_version"]), resolution=dict(args["resolution"]))
        return {"ok": True, "task_id": result.task_id, "business_status": "pending",
                "next_action": "business_operation"}

    def business(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args.get("task_id", "")).strip()
        if not task_id:
            raise RuntimeError("no_active_governed_execution_for_task")
        context = self._context(task_id)
        if tool_name not in context.allowed_tools:
            raise RuntimeError("business_operation_not_authorized_by_contract")
        result = self.app.gateway.invoke(ToolInvocationContext(execution_id=context.execution_id,
            task_id=task_id, attempt_id=context.attempt_id, tool_call_id="runtime-tool:" + str(uuid4()),
            allowed_tools=context.allowed_tools, run_request_id=context.run_request_id,
            lease_owner_id=context.lease_owner_id, lease_token=context.lease_token), tool_name,
            dict(args["arguments"]))
        payload = result.model_dump(mode="json")
        error = payload.get("error") or {}
        if error.get("type") == "approval_required":
            interaction = self.app.runtime.request_approval(execution_id=context.execution_id,
                lease_owner_id=context.lease_owner_id, lease_token=context.lease_token,
                effect=CanonicalEffectRequest.model_validate(error["canonical_effect_request"]))
            self._contexts.pop(task_id, None)
            return {"ok": False, "task_id": task_id, "business_status": "waiting_approval",
                    "next_action": "resolve_approval", "interaction_id": interaction.interaction_id}
        return {"ok": bool(payload.get("ok")), "task_id": task_id, "business_status": "running",
                "next_action": "business_operation", "result": payload}

    def cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        result = self.app.runtime.cancel_task(command_id=str(args["command_id"]), task_id=str(args["task_id"]),
            expected_task_version=int(args["expected_task_version"]), reason=str(args.get("reason", "operator_cancelled")))
        self._contexts.pop(str(args["task_id"]), None)
        return {"ok": True, "task_id": result.task_id, "business_status": "cancelled", "next_action": "none"}

    def capabilities(self) -> dict[str, Any]:
        """Expose capability metadata, never backend implementation details."""
        return {
            "ok": True,
            "authority_domains": {
                **self.authority_domains,
            },
            "tools": sorted(TOOL_CATALOG),
            "capability_metadata": {
                name: {"capability_ref": tool.capability_ref,
                       "tool_version": tool.tool_version,
                       "access_mode": tool.access_mode.value,
                       "governed": tool.governed,
                       "authoritative_source": tool.authoritative_source,
                       "failure_policy": tool.failure_policy}
                for name, tool in sorted(TOOL_CATALOG.items())
            },
        }

    def capability(self, capability_name: str, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.pop("_hermes_session_id", "")).strip()
        def bind(task_id: str) -> None:
            if session_id:
                context = self._context(task_id)
                self.correlations.bind(session_id, task_id, "semantic_capability",
                                       context.attempt_id, context.execution_id)
        return self.capability_broker.invoke(capability_name, args, on_started=bind)

    def artifact_action_observation(self, args: dict[str, Any]) -> dict[str, Any]:
        observation = ArtifactActionObservation(**dict(args))
        correlation = self.correlations.lookup(observation.session_id)
        task_id = attempt_id = execution_id = None
        status = "unbound"
        if correlation is not None:
            task_id = str(correlation["astra_task_id"])
            attempt_id = correlation.get("attempt_id")
            execution_id = correlation.get("execution_id")
            if attempt_id and execution_id:
                status = "bound"
            else:
                task_id = attempt_id = execution_id = None
        with self.app.store.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO artifact_action_observations(
                   event_id, session_id, hermes_task_id, tool_call_id, turn_id, api_request_id,
                   task_id, attempt_id, execution_id, artifact_type, artifact_ref, action, status,
                   metadata_json, observed_at, correlation_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (observation.event_id, observation.session_id, observation.hermes_task_id,
                 observation.tool_call_id, observation.turn_id, observation.api_request_id,
                 task_id, attempt_id, execution_id, observation.artifact_type,
                 observation.artifact_ref, observation.action, observation.status,
                 json.dumps(dict(observation.metadata), sort_keys=True), observation.timestamp, status),
            )
        return {"ok": True, "event_id": observation.event_id, "correlation_status": status}

    def correlation(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        sid = str(args.get("hermes_session_id", "")).strip()
        if operation == "correlation_bind":
            row = self.correlations.bind(sid, str(args.get("astra_task_id", "")), str(args.get("source", "")) or None,
                                         str(args.get("attempt_id", "")) or None, str(args.get("execution_id", "")) or None)
            return {"ok": True, "correlation": row}
        if operation == "correlation_lookup":
            if not sid and str(args.get("astra_task_id", "")).strip():
                return {"ok": True, "correlations": self.correlations.lookup_by_task_id(str(args["astra_task_id"]))}
            return {"ok": True, "correlation": self.correlations.mark_seen(sid)}
        if operation == "correlation_release":
            return {"ok": True, "correlation": self.correlations.release(sid)}
        raise ValueError("unknown correlation operation")

    def dispatch(self, request: dict[str, Any]) -> str:
        try:
            args = request.get("args")
            if not isinstance(args, dict): raise ValueError("args must be object")
            operation = request.get("operation")
            if operation == "submit": value = self.submit(args)
            elif operation == "capabilities": value = self.capabilities()
            elif operation == "status": value = self.status(args)
            elif operation == "resolve_input": value = self.resolve(args, InteractionKind.USER_INPUT.value)
            elif operation == "resolve_approval": value = self.resolve(args, InteractionKind.APPROVAL.value)
            elif operation == "business": value = self.business(str(request["tool_name"]), args)
            elif operation == "capability":
                capability_name = str(args.pop("capability_name", request.get("capability_name", "")))
                value = self.capability(capability_name, args)
            elif operation == "artifact_action_observation": value = self.artifact_action_observation(args)
            elif operation == "cancel": value = self.cancel(args)
            elif operation in ("correlation_bind", "correlation_lookup", "correlation_release"): value = self.correlation(operation, args)
            else: raise ValueError("unknown business protocol operation")
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            message = str(exc)
            payload: dict[str, Any] = {"ok": False, "error": {"type": "business_protocol_rejected", "message": message}}
            if message == "no_active_governed_execution_for_task":
                payload["error"]["code"] = message
                payload["next_action"] = {"kind": "submit_task", "tool": "astra_submit_task",
                                           "required_fields": ["objective", "subjects", "requested_tools"]}
            elif message == "governed_execution_recovery_pending":
                payload["error"]["code"] = message
                payload["next_action"] = {"kind": "retry_business_operation"}
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class _Handler(BaseHTTPRequestHandler):
    service: RuntimeService
    def log_message(self, *_: Any) -> None: return
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/plugin": self.send_error(404); return
        try:
            presented = self.headers.get("X-Astra-Plugin-Auth", "")
            if not hmac.compare_digest(presented, self.service.transport_credential):
                self.send_error(401, "plugin authentication required"); return
            size = int(self.headers.get("Content-Length", "0"))
            body = json.dumps({"result": self.service.dispatch(json.loads(self.rfile.read(size)))}, ensure_ascii=False).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        except Exception as exc: self.send_error(400, str(exc))


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    from .hermes_composition import build_runtime_service_from_environment
    _Handler.service = build_runtime_service_from_environment()
    ThreadingHTTPServer((args.host, args.port), _Handler).serve_forever()

if __name__ == "__main__": main()
