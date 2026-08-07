"""Stable Astra domain contracts that do not expose Hermes private types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    LIMIT_EXCEEDED = "limit_exceeded"


class InteractionKind(str, Enum):
    USER_INPUT = "user_input"
    APPROVAL = "approval"


class InteractionPurpose(str, Enum):
    """Exact waiting purpose; lifecycle state remains on Task/Execution."""

    CLARIFICATION = "clarification"
    APPROVAL = "approval"


class ExecutionUsage(FrozenModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cost_status: str = "unknown"
    cost_source: str = "none"


class RuntimeInvocation(FrozenModel):
    execution_id: str
    task_id: str
    attempt_id: str
    user_request: str
    task_contract: Mapping[str, Any]
    allowed_tools: Sequence[str]
    provider_config: Mapping[str, Any]
    limits: Mapping[str, Any] = Field(default_factory=dict)
    session_handle: str | None = None
    feedback: Sequence[Mapping[str, Any]] = Field(default_factory=tuple)
    evidence_receipt_ids: Sequence[str] = Field(default_factory=tuple)
    run_request_id: str | None = None
    lease_owner_id: str | None = None
    lease_token: str | None = None
    lease_expires_at: str | None = None
    execution_profile: str = "astra_controlled"
    execution_profile_version: str = "1"
    execution_profile_hash: str | None = None
    hermes_home: str | None = None


class ExecutionResult(FrozenModel):
    execution_id: str
    status: ExecutionStatus
    agent_turn_finished: bool
    task_outcome_validated: bool
    assistant_output: str | None = None
    submitted_result: Mapping[str, Any] | None = None
    result_receipt: Mapping[str, Any] | None = None
    session_handle: str | None = None
    termination_reason: str
    usage: ExecutionUsage = Field(default_factory=ExecutionUsage)
    error: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = Field(default_factory=dict)


class ExecutionEvent(FrozenModel):
    event_type: str
    execution_id: str
    task_id: str
    attempt_id: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    source: str
    payload: Mapping[str, Any] = Field(default_factory=dict)


class ToolInvocationContext(FrozenModel):
    execution_id: str
    task_id: str
    attempt_id: str
    tool_call_id: str
    allowed_tools: Sequence[str]
    idempotency_key: str | None = None
    approval_token: str | None = None
    run_request_id: str | None = None
    lease_owner_id: str | None = None
    lease_token: str | None = None


class ToolResult(FrozenModel):
    ok: bool
    tool_name: str
    data: Mapping[str, Any] = Field(default_factory=dict)
    error: Mapping[str, Any] | None = None
    receipt_id: str | None = None
    side_effect: bool = False


class ResultReceipt(FrozenModel):
    receipt_id: str
    execution_id: str
    task_id: str
    valid: bool
    outcome: Mapping[str, Any]
    evidence_refs: Sequence[str]
    receipt_refs: Sequence[str]
    checks: Mapping[str, bool]
    errors: Sequence[str] = Field(default_factory=tuple)


class InteractionRequest(FrozenModel):
    interaction_id: str
    execution_id: str
    task_id: str
    attempt_id: str
    kind: InteractionKind
    purpose: InteractionPurpose
    prompt: str
    status: str = "pending"
    payload: Mapping[str, Any] = Field(default_factory=dict)


ExecutionEventSink = Callable[[ExecutionEvent], Awaitable[None]]


class AgentExecutor(Protocol):
    async def execute(
        self,
        invocation: RuntimeInvocation,
        event_sink: ExecutionEventSink,
    ) -> ExecutionResult: ...

    async def cancel(self, execution_id: str, reason: str) -> None: ...
