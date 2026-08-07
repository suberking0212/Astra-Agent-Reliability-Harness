"""Task-level policy decisions and Runtime CAS precondition checks."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from .canonical import sha256_digest
from .completion import CompletionStatus, CompletionValidationResult
from .effects import ExternalOperationStatus
from .task_contract import (
    FORBIDDEN_WORKFLOW_FIELDS,
    FrozenContractModel,
    TaskContractRef,
    _walk_keys,
)


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PolicyAction(str, Enum):
    COMPLETE = "complete"
    CONTINUE_WITH_FEEDBACK = "continue_with_feedback"
    START_NEW_ATTEMPT = "start_new_attempt"
    REQUEST_INPUT = "request_input"
    REQUEST_APPROVAL = "request_approval"
    RECONCILE = "reconcile"
    FAIL = "fail"
    ESCALATE = "escalate"


class DecisionPoint(str, Enum):
    EXECUTION_ENDED = "execution_ended"
    COMPLETION_VALIDATED = "completion_validated"
    INTERACTION_CHANGED = "interaction_changed"
    RECONCILIATION_COMPLETED = "reconciliation_completed"
    ATTEMPT_LIMIT_REACHED = "attempt_limit_reached"


class DecisionApplication(str, Enum):
    APPLIED = "applied"
    STALE = "stale"
    NOT_APPLIED = "not_applied"


class DecisionContext(FrozenContractModel):
    decision_context_id: str
    decision_point: DecisionPoint
    trigger_id: str
    task_id: str
    task_version: int = Field(ge=0)
    task_contract_ref: TaskContractRef
    attempt_id: str
    attempt_version: int = Field(ge=0)
    evidence_snapshot_id: str
    fact_watermark: int = Field(ge=0)
    completion_validation_id: str | None = None
    interaction_snapshot_version: int = Field(default=0, ge=0)
    policy_id: str = "astra.default_task_policy"
    policy_version: str = "1"
    context_hash: str

    @classmethod
    def materialize(cls, **values: Any) -> "DecisionContext":
        payload = dict(values)
        payload.pop("context_hash", None)
        payload.setdefault("interaction_snapshot_version", 0)
        payload.setdefault("policy_id", "astra.default_task_policy")
        payload.setdefault("policy_version", "1")
        payload["context_hash"] = sha256_digest(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_context_hash(self) -> "DecisionContext":
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("context_hash", None)
        if self.context_hash != sha256_digest(payload):
            raise ValueError("DecisionContext context_hash mismatch")
        return self


class PolicyFeedback(FrozenContractModel):
    facts: tuple[str, ...] = ()
    unmet_requirements: tuple[str, ...] = ()
    active_constraints: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()


class InteractionSpec(FrozenContractModel):
    kind: str
    reason_code: str
    required_information: tuple[str, ...] = ()
    approval_requirement_ref: str | None = None
    effect_identity: str | None = None
    effect_request_hash: str | None = None
    permission_scope: str | None = None
    evidence_refs: tuple[str, ...] = ()
    active_constraints: tuple[str, ...] = ()


class PolicyDecision(FrozenContractModel):
    decision_id: str
    decision_key: str
    policy_id: str
    policy_version: str
    task_id: str
    attempt_id: str
    expected_task_version: int = Field(ge=0)
    expected_attempt_version: int = Field(ge=0)
    expected_task_contract_ref: TaskContractRef
    expected_interaction_snapshot_version: int = Field(ge=0)
    expected_completion_validation_id: str | None = None
    decision_context_id: str
    context_hash: str
    action: PolicyAction
    reason_code: str
    finding_refs: tuple[str, ...] = ()
    evaluation_refs: tuple[str, ...] = ()
    fact_refs: tuple[str, ...] = ()
    feedback: PolicyFeedback | None = None
    interaction_spec: InteractionSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_workflow_fields(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            forbidden = FORBIDDEN_WORKFLOW_FIELDS.intersection(_walk_keys(value))
            if forbidden:
                raise ValueError("PolicyDecision cannot contain workflow fields")
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> "PolicyDecision":
        payload = decision_key_payload(self)
        if self.decision_key != sha256_digest(payload):
            raise ValueError("PolicyDecision decision_key mismatch")
        if self.action == PolicyAction.REQUEST_APPROVAL:
            spec = self.interaction_spec
            if (
                spec is None
                or spec.kind != "approval"
                or not spec.approval_requirement_ref
                or not spec.effect_identity
                or not spec.effect_request_hash
                or not spec.permission_scope
            ):
                raise ValueError(
                    "request_approval requires an exact effect-bound interaction spec"
                )
        if self.action == PolicyAction.REQUEST_INPUT:
            if self.interaction_spec is None or self.interaction_spec.kind != "user_input":
                raise ValueError("request_input requires a user_input interaction spec")
        return self


def decision_key_payload(decision: PolicyDecision | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(decision, PolicyDecision):
        return {
            "task_id": decision.task_id,
            "decision_point_context_hash": decision.context_hash,
            "policy_id": decision.policy_id,
            "policy_version": decision.policy_version,
            "action": decision.action.value,
            "reason_code": decision.reason_code,
        }
    return dict(decision)


def make_policy_decision(
    context: DecisionContext,
    *,
    action: PolicyAction,
    reason_code: str,
    finding_refs: tuple[str, ...] = (),
    evaluation_refs: tuple[str, ...] = (),
    fact_refs: tuple[str, ...] = (),
    feedback: PolicyFeedback | None = None,
    interaction_spec: InteractionSpec | None = None,
) -> PolicyDecision:
    key_payload = {
        "task_id": context.task_id,
        "decision_point_context_hash": context.context_hash,
        "policy_id": context.policy_id,
        "policy_version": context.policy_version,
        "action": action.value,
        "reason_code": reason_code,
    }
    decision_key = sha256_digest(key_payload)
    return PolicyDecision(
        decision_id="decision:" + decision_key,
        decision_key=decision_key,
        policy_id=context.policy_id,
        policy_version=context.policy_version,
        task_id=context.task_id,
        attempt_id=context.attempt_id,
        expected_task_version=context.task_version,
        expected_attempt_version=context.attempt_version,
        expected_task_contract_ref=context.task_contract_ref,
        expected_interaction_snapshot_version=context.interaction_snapshot_version,
        expected_completion_validation_id=context.completion_validation_id,
        decision_context_id=context.decision_context_id,
        context_hash=context.context_hash,
        action=action,
        reason_code=reason_code,
        finding_refs=finding_refs,
        evaluation_refs=evaluation_refs,
        fact_refs=fact_refs,
        feedback=feedback,
        interaction_spec=interaction_spec,
    )


def choose_policy_action(
    *,
    input_complete: bool,
    approval_required: bool,
    approval_matched: bool,
    external_operation_status: ExternalOperationStatus | None,
    completion_status: CompletionStatus,
    approval_denied: bool = False,
    governed_effect_failed: bool = False,
) -> tuple[PolicyAction, str]:
    """Frozen normal mapping without prescribing any Hermes tool sequence."""

    if approval_denied:
        return PolicyAction.FAIL, "approval_denied_effect_not_executed"
    if external_operation_status == ExternalOperationStatus.INDETERMINATE:
        return PolicyAction.RECONCILE, "external_operation_indeterminate"
    if governed_effect_failed:
        return PolicyAction.CONTINUE_WITH_FEEDBACK, "governed_effect_failed"
    if not input_complete:
        return PolicyAction.REQUEST_INPUT, "required_input_missing"
    if approval_required and not approval_matched:
        return PolicyAction.REQUEST_APPROVAL, "required_approval_missing"
    if completion_status == CompletionStatus.SATISFIED:
        return PolicyAction.COMPLETE, "completion_requirements_satisfied"
    if completion_status == CompletionStatus.INDETERMINATE:
        return PolicyAction.RECONCILE, "completion_evidence_indeterminate"
    if completion_status == CompletionStatus.EVALUATOR_ERROR:
        return PolicyAction.ESCALATE, "completion_evaluator_error"
    return PolicyAction.CONTINUE_WITH_FEEDBACK, "completion_requirements_unsatisfied"


class DecisionApplicationSnapshot(FrozenContractModel):
    task_id: str
    task_version: int = Field(ge=0)
    task_state: TaskState
    attempt_id: str
    attempt_version: int = Field(ge=0)
    task_contract_ref: TaskContractRef
    interaction_snapshot_version: int = Field(default=0, ge=0)
    completion_validation_id: str | None = None
    pending_blocking_interaction: bool = False
    unresolved_reconciliation: bool = False
    required_external_operation_statuses: tuple[ExternalOperationStatus, ...] = ()


def apply_policy_decision(
    decision: PolicyDecision,
    current: DecisionApplicationSnapshot,
    *,
    completion: CompletionValidationResult | None = None,
) -> DecisionApplication:
    """Pure CAS/completion-gate verdict; only a Runtime transaction may mutate state."""

    if (
        decision.task_id != current.task_id
        or decision.attempt_id != current.attempt_id
        or decision.expected_task_version != current.task_version
        or decision.expected_attempt_version != current.attempt_version
        or decision.expected_task_contract_ref != current.task_contract_ref
        or decision.expected_interaction_snapshot_version
        != current.interaction_snapshot_version
        or decision.expected_completion_validation_id
        != current.completion_validation_id
    ):
        return DecisionApplication.STALE
    if current.task_state in {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    }:
        return DecisionApplication.NOT_APPLIED
    if not policy_action_allowed(current.task_state, decision.action):
        return DecisionApplication.NOT_APPLIED
    if decision.action != PolicyAction.COMPLETE:
        return DecisionApplication.APPLIED
    if (
        completion is None
        or completion.status != CompletionStatus.SATISFIED
        or current.completion_validation_id != completion.completion_validation_id
        or current.pending_blocking_interaction
        or current.unresolved_reconciliation
        or any(
            status
            in {
                ExternalOperationStatus.PREPARED,
                ExternalOperationStatus.IN_FLIGHT,
                ExternalOperationStatus.ACKNOWLEDGED,
                ExternalOperationStatus.INDETERMINATE,
            }
            for status in current.required_external_operation_statuses
        )
    ):
        return DecisionApplication.NOT_APPLIED
    return DecisionApplication.APPLIED


def policy_action_allowed(task_state: TaskState, action: PolicyAction) -> bool:
    """Enforce the frozen Task lifecycle without adding business states."""

    if task_state == TaskState.RUNNING:
        return True
    if task_state == TaskState.RECONCILING:
        return action in {
            PolicyAction.COMPLETE,
            PolicyAction.CONTINUE_WITH_FEEDBACK,
            PolicyAction.START_NEW_ATTEMPT,
            PolicyAction.FAIL,
            PolicyAction.ESCALATE,
        }
    if task_state == TaskState.WAITING_APPROVAL:
        return action in {PolicyAction.FAIL, PolicyAction.ESCALATE}
    return False
