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
from ..result_validator import MinimalResultValidator
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
    ) -> None:
        self.invocation = invocation
        self.store = store
        self.gateway = gateway
        self.validator = validator
        self.budget = budget
        self.trace = trace
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
        idempotency_key = args.pop("idempotency_key", None)
        context = ToolInvocationContext(
            execution_id=self.invocation.execution_id,
            task_id=self.invocation.task_id,
            attempt_id=self.invocation.attempt_id,
            tool_call_id=tool_call_id or str(uuid4()),
            allowed_tools=self.invocation.allowed_tools,
            idempotency_key=idempotency_key,
        )
        self.emit(
            "ToolCall",
            "hermes_bridge_plugin",
            {"tool_name": tool_name, "arguments": args},
        )
        result = self.gateway.invoke(context, tool_name, args)
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

    def request_interaction(
        self,
        kind: InteractionKind,
        prompt: str,
        payload: Mapping[str, Any] | None = None,
    ) -> str:
        with self._lock:
            if self.interaction is None:
                self.interaction = self.store.request_interaction(
                    execution_id=self.invocation.execution_id,
                    task_id=self.invocation.task_id,
                    attempt_id=self.invocation.attempt_id,
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
            self._interrupt(f"astra_waiting_{kind.value}")
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

