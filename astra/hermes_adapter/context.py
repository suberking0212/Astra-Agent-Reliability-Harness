"""Thread-safe execution context used by the fixed Hermes bridge plugin."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from ..budget import BudgetLedger
from ..domain import (
    ExecutionEvent,
    InteractionKind,
    InteractionRequest,
    ResultReceipt,
    RuntimeInvocation,
    ToolInvocationContext,
)
from ..phase3.effects import CanonicalEffectRequest
from ..result_validator import MinimalResultValidator
from ..runtime import TaskRuntime
from ..storage import AstraStore
from ..tool_gateway import AstraToolGateway
from ..trace import NeutralTraceCollector


class ExecutionBridgeContext:
    def __init__(
        self,
        *,
        invocation: RuntimeInvocation,
        store: AstraStore,
        gateway: AstraToolGateway,
        validator: MinimalResultValidator,
        budget: BudgetLedger,
        trace: NeutralTraceCollector,
        runtime: TaskRuntime,
    ) -> None:
        self.invocation = invocation
        self.store = store
        self.gateway = gateway
        self.validator = validator
        self.budget = budget
        self.trace = trace
        self.runtime = runtime
        if self.runtime.store is not store:
            raise ValueError("ExecutionBridgeContext must share AstraStore authority")
        self.trace_id = str(uuid4())
        self.events: list[ExecutionEvent] = []
        self.submitted_result: Mapping[str, Any] | None = None
        self.result_receipt: ResultReceipt | None = None
        self.interaction: InteractionRequest | None = None
        self.budget_limit_reason: str | None = None
        self._interrupt: Callable[[str], None] | None = None
        self._lock = threading.RLock()

    def bind_interrupt(self, callback: Callable[[str], None]) -> None:
        self._interrupt = callback

    def emit(
        self, event_type: str, source: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        event = ExecutionEvent(
            event_type=event_type,
            execution_id=self.invocation.execution_id,
            task_id=self.invocation.task_id,
            attempt_id=self.invocation.attempt_id,
            source=source,
            payload=payload or {},
        )
        with self._lock:
            self.events.append(event)
        self.trace.event(event, self.trace_id)

    def invoke_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        tool_call_id: str | None,
    ) -> str:
        args = dict(arguments)
        context = ToolInvocationContext(
            execution_id=self.invocation.execution_id,
            task_id=self.invocation.task_id,
            attempt_id=self.invocation.attempt_id,
            tool_call_id=tool_call_id or str(uuid4()),
            allowed_tools=self.invocation.allowed_tools,
            # This field is intentionally empty at the Hermes boundary. The
            # Gateway derives the formal key only after canonical identity and
            # exact approval have been established.
            idempotency_key=None,
        )
        self.emit(
            "ToolCall",
            "hermes_bridge_plugin",
            {"tool_name": tool_name, "arguments": args},
        )
        result = self.gateway.invoke(context, tool_name, args)
        if result.error and result.error.get("type") == "approval_required":
            effect_payload = result.error.get("canonical_effect_request")
            if isinstance(effect_payload, Mapping):
                interaction = self._request_exact_approval(
                    CanonicalEffectRequest.model_validate(effect_payload)
                )
                result = result.model_copy(
                    update={
                        "error": {
                            **dict(result.error),
                            "suspension_requested": True,
                            "interaction": interaction.model_dump(mode="json"),
                        }
                    }
                )
        self.emit(
            "ToolResult",
            "astra_tool_gateway",
            {
                "tool_name": tool_name,
                "ok": result.ok,
                "receipt_id": result.receipt_id,
                "error": result.error,
            },
        )
        return result.model_dump_json()

    def request_approval(
        self,
        prompt: str,
        tool_name: str | None,
        arguments: Mapping[str, Any],
    ) -> str:
        """Route a proactive Hermes approval request through exact normalization."""

        if not tool_name:
            return self.request_interaction(
                InteractionKind.USER_INPUT,
                prompt,
                {
                    "requested_interaction_kind": InteractionKind.APPROVAL.value,
                    "reason_code": "exact_effect_request_required",
                    "required_information": ["tool_name", "arguments"],
                },
            )
        context = ToolInvocationContext(
            execution_id=self.invocation.execution_id,
            task_id=self.invocation.task_id,
            attempt_id=self.invocation.attempt_id,
            tool_call_id="approval-normalization:" + str(uuid4()),
            allowed_tools=self.invocation.allowed_tools,
        )
        try:
            effect = self.gateway.canonicalize_effect_request(
                context, tool_name, arguments
            )
        except (PermissionError, RuntimeError, ValueError):
            return self.request_interaction(
                InteractionKind.USER_INPUT,
                prompt,
                {
                    "requested_interaction_kind": InteractionKind.APPROVAL.value,
                    "reason_code": "exact_effect_request_required",
                    "required_information": ["canonical_effect_request"],
                    "tool_name": tool_name,
                    "arguments": dict(arguments),
                },
            )
        interaction = self._request_exact_approval(effect)
        return json.dumps(
            {
                "ok": True,
                "suspension_requested": True,
                "interaction": interaction.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def _request_exact_approval(
        self, effect: CanonicalEffectRequest
    ) -> InteractionRequest:
        with self._lock:
            if self.interaction is None:
                self.interaction = self.runtime.request_approval(
                    execution_id=self.invocation.execution_id,
                    effect=effect,
                )
                self.emit(
                    "InteractionRequest",
                    "astra_task_runtime",
                    self.interaction.model_dump(mode="json"),
                )
            elif self.interaction.kind != InteractionKind.APPROVAL:
                raise RuntimeError("Execution already owns a different Interaction")
            interaction = self.interaction
        if self._interrupt is not None:
            self._interrupt("astra_waiting_approval")
        return interaction

    def request_interaction(
        self,
        kind: InteractionKind,
        prompt: str,
        payload: Mapping[str, Any] | None = None,
    ) -> str:
        with self._lock:
            if self.interaction is None:
                self.interaction = self.runtime.request_interaction(
                    execution_id=self.invocation.execution_id,
                    kind=kind,
                    prompt=prompt,
                    payload=payload,
                )
                self.emit(
                    "InteractionRequest",
                    "astra_task_runtime",
                    self.interaction.model_dump(mode="json"),
                )
        if self._interrupt is not None:
            self._interrupt(f"astra_waiting_{self.interaction.kind.value}")
        return json.dumps(
            {
                "ok": True,
                "suspension_requested": True,
                "interaction": self.interaction.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def submit_task_result(
        self,
        outcome: Mapping[str, Any],
        evidence_refs: Sequence[str],
        receipt_refs: Sequence[str],
    ) -> str:
        self.submitted_result = {
            "outcome": dict(outcome),
            "evidence_refs": list(evidence_refs),
            "receipt_refs": list(receipt_refs),
        }
        self.emit(
            "ResultSubmission",
            "hermes_bridge_plugin",
            self.submitted_result,
        )
        self.result_receipt = self.validator.validate(
            self.invocation,
            outcome,
            evidence_refs,
            receipt_refs,
        )
        self.emit(
            "ResultValidation",
            "minimal_result_validator",
            self.result_receipt.model_dump(mode="json"),
        )
        return self.result_receipt.model_dump_json()

    def observe_provider_call(self, usage: Mapping[str, Any] | None) -> None:
        self.budget.observe_provider_call(
            self.invocation.execution_id,
            usage,
        )
        self.emit(
            "ProviderCall",
            "hermes_observer",
            {"usage": dict(usage or {})},
        )
        decision = self.budget.decide(
            self.invocation.execution_id,
            self.invocation.limits,
        )
        if not decision.allowed:
            self.budget_limit_reason = decision.reason
            if self._interrupt is not None:
                self._interrupt(decision.reason or "astra_budget_limit")


class ExecutionContextRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_task_id: dict[str, ExecutionBridgeContext] = {}

    def register(self, context: ExecutionBridgeContext) -> None:
        with self._lock:
            if context.invocation.task_id in self._by_task_id:
                raise RuntimeError(
                    f"Task already has an active execution: {context.invocation.task_id}"
                )
            self._by_task_id[context.invocation.task_id] = context

    def get(self, task_id: str | None) -> ExecutionBridgeContext | None:
        if not task_id:
            return None
        with self._lock:
            return self._by_task_id.get(task_id)

    def unregister(self, task_id: str) -> None:
        with self._lock:
            self._by_task_id.pop(task_id, None)


registry = ExecutionContextRegistry()
