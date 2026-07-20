"""Hermes-backed implementation of Astra's stable AgentExecutor port."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..budget import BudgetLedger
from ..domain import (
    ExecutionEventSink,
    ExecutionResult,
    ExecutionStatus,
    RuntimeInvocation,
)
from ..result_validator import MinimalResultValidator
from ..storage import AstraStore
from ..tool_gateway import AstraToolGateway
from ..trace import NeutralTraceCollector
from .context import ExecutionBridgeContext, registry


class HermesExecutor:
    def __init__(
        self,
        *,
        hermes_root: str | Path,
        store: AstraStore,
        gateway: AstraToolGateway,
        validator: MinimalResultValidator,
        budget: BudgetLedger,
        trace: NeutralTraceCollector,
        client_factory: Callable[[RuntimeInvocation], Any] | None = None,
    ) -> None:
        self.hermes_root = Path(hermes_root)
        self.store = store
        self.gateway = gateway
        self.validator = validator
        self.budget = budget
        self.trace = trace
        self.client_factory = client_factory
        self._agents: dict[str, Any] = {}

    async def execute(
        self,
        invocation: RuntimeInvocation,
        event_sink: ExecutionEventSink,
    ) -> ExecutionResult:
        self.store.start_execution(
            invocation.execution_id,
            invocation.task_id,
            invocation.attempt_id,
        )
        context = ExecutionBridgeContext(
            invocation=invocation,
            store=self.store,
            gateway=self.gateway,
            validator=self.validator,
            budget=self.budget,
            trace=self.trace,
        )
        registry.register(context)
        context.emit("ExecutionStarted", "hermes_executor_adapter")
        hermes_result: Mapping[str, Any] = {}
        caught: BaseException | None = None
        try:
            hermes_result = await asyncio.to_thread(
                self._run_hermes,
                invocation,
                context,
            )
        except BaseException as exc:
            caught = exc
            context.emit(
                "ExecutionError",
                "hermes_executor_adapter",
                {"type": type(exc).__name__, "message": str(exc)},
            )
        finally:
            registry.unregister(invocation.task_id)

        result = self._map_result(invocation, context, hermes_result, caught)
        won = self.store.finalize_execution(
            execution_id=invocation.execution_id,
            status=result.status.value,
            termination_reason=result.termination_reason,
            payload={
                "task_outcome_validated": result.task_outcome_validated,
                "agent_turn_finished": result.agent_turn_finished,
            },
        )
        result = result.model_copy(
            update={"metadata": {"exactly_once_winner": won}}
        )
        if won:
            context.emit(
                "ExecutionEnded",
                "hermes_executor_adapter",
                {
                    "status": result.status.value,
                    "termination_reason": result.termination_reason,
                },
            )
        for event in context.events:
            await event_sink(event)
        return result

    async def cancel(self, execution_id: str, reason: str) -> None:
        agent = self._agents.get(execution_id)
        if agent is not None:
            await asyncio.to_thread(agent.interrupt, reason)

    def _run_hermes(
        self,
        invocation: RuntimeInvocation,
        context: ExecutionBridgeContext,
    ) -> Mapping[str, Any]:
        root = str(self.hermes_root)
        inserted = root not in sys.path
        if inserted:
            sys.path.insert(0, root)
        try:
            from hermes_cli.plugins import discover_plugins
            from hermes_state import SessionDB
            from run_agent import AIAgent

            # model_tools performs plugin discovery at module import time. In a
            # long-lived process (or a test suite that changes HERMES_HOME),
            # that import may have happened before the Astra plugin became
            # active, so explicitly discover against the current manager.
            discover_plugins()

            provider = invocation.provider_config
            session_id = invocation.session_handle or (
                f"astra-{invocation.task_id}-{invocation.attempt_id}"
            )
            task_contract = json.dumps(
                invocation.task_contract,
                ensure_ascii=False,
                sort_keys=True,
            )
            feedback = json.dumps(
                list(invocation.feedback),
                ensure_ascii=False,
                sort_keys=True,
            )
            agent = AIAgent(
                base_url=str(provider.get("base_url", "http://127.0.0.1:9/v1")),
                api_key=str(provider.get("api_key", "astra-dummy-key")),
                provider=str(provider.get("provider", "custom")),
                api_mode=str(provider.get("api_mode", "chat_completions")),
                model=str(provider.get("model", "astra-scripted-provider")),
                max_iterations=int(invocation.limits.get("max_agent_steps", 12)),
                tool_delay=0,
                enabled_toolsets=[
                    "astra_runtime",
                    *[
                        f"astra_business_{tool_name}"
                        for tool_name in invocation.allowed_tools
                    ],
                ],
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_db=SessionDB(),
                session_id=session_id,
                ephemeral_system_prompt=(
                    "ASTRA_TASK_CONTRACT=" + task_contract + "\n"
                    "ASTRA_ALLOWED_TOOLS="
                    + json.dumps(list(invocation.allowed_tools), sort_keys=True)
                    + "\nASTRA_RUNTIME_FEEDBACK="
                    + feedback
                    + "\nFor tool_execution tasks, call submit_task_result with "
                    "outcome, evidence_refs, and receipt_refs before finishing."
                ),
            )
            if self.client_factory is not None:
                agent.client = self.client_factory(invocation)
                agent._disable_streaming = True
            context.bind_interrupt(agent.interrupt)
            self._agents[invocation.execution_id] = agent
            try:
                result = agent.run_conversation(
                    invocation.user_request,
                    task_id=invocation.task_id,
                )
            finally:
                self._agents.pop(invocation.execution_id, None)
            handle = result.get("session_id") or session_id
            self.store.save_session_handle(invocation.execution_id, handle)
            return result
        finally:
            if inserted:
                sys.path.remove(root)

    def _map_result(
        self,
        invocation: RuntimeInvocation,
        context: ExecutionBridgeContext,
        hermes_result: Mapping[str, Any],
        caught: BaseException | None,
    ) -> ExecutionResult:
        pending = self.store.pending_interaction(invocation.execution_id)
        validated = bool(context.result_receipt and context.result_receipt.valid)
        agent_turn_finished = bool(hermes_result.get("completed"))
        if pending:
            status = (
                ExecutionStatus.WAITING_APPROVAL
                if pending["kind"] == "approval"
                else ExecutionStatus.WAITING_INPUT
            )
            reason = f"interaction_requested:{pending['kind']}"
        elif context.budget_limit_reason:
            status = ExecutionStatus.LIMIT_EXCEEDED
            reason = context.budget_limit_reason
        elif caught is not None:
            status = ExecutionStatus.FAILED
            reason = "adapter_exception"
        elif validated:
            status = ExecutionStatus.SUCCEEDED
            reason = "task_outcome_validated"
        elif hermes_result.get("interrupted"):
            status = ExecutionStatus.INTERRUPTED
            reason = str(hermes_result.get("interrupt_reason") or "interrupted")
        else:
            status = ExecutionStatus.FAILED
            reason = "task_outcome_not_validated"

        session_handle = hermes_result.get("session_id") or invocation.session_handle
        error = None
        if caught is not None:
            error = {"type": type(caught).__name__, "message": str(caught)}
        elif hermes_result.get("error"):
            error = {"type": "hermes_error", "message": hermes_result.get("error")}
        return ExecutionResult(
            execution_id=invocation.execution_id,
            status=status,
            agent_turn_finished=agent_turn_finished,
            task_outcome_validated=validated,
            assistant_output=hermes_result.get("final_response"),
            submitted_result=context.submitted_result,
            result_receipt=(
                context.result_receipt.model_dump(mode="json")
                if context.result_receipt
                else None
            ),
            session_handle=session_handle,
            termination_reason=reason,
            usage=self.budget.usage(invocation.execution_id),
            error=error,
            metadata={},
        )
