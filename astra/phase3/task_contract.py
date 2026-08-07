"""Frozen, business-agnostic Task Contract v1 models and validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import sha256_digest, without_key


FORBIDDEN_WORKFLOW_FIELDS = frozenset(
    {"next_tool", "tool_arguments", "ordered_steps", "workflow", "business_plan"}
)


class ContractValidationError(ValueError):
    """Raised when a mapping violates a frozen Task Contract invariant."""


class FrozenContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolAccessMode(str, Enum):
    READ = "read"
    EFFECT = "effect"
    RUNTIME_PRIMITIVE = "runtime_primitive"
    RESULT_SUBMISSION = "result_submission"


class ExecutionType(str, Enum):
    """Structured execution path selected by the Task producer.

    ``direct_response`` and ``tool_execution`` are base execution paths.
    ``interaction_required`` and ``approval_required`` are initial waiting
    branches. Runtime lifecycle waits remain ``waiting_input`` and
    ``waiting_approval`` and never become execution types.
    """

    DIRECT_RESPONSE = "direct_response"
    TOOL_EXECUTION = "tool_execution"
    INTERACTION_REQUIRED = "interaction_required"
    APPROVAL_REQUIRED = "approval_required"


class EnforcementPoint(str, Enum):
    RUNTIME = "runtime"
    TOOL_GATEWAY = "tool_gateway"
    EFFECT_NORMALIZER = "effect_normalizer"
    COMPLETION_GATE = "completion_gate"


class ApprovalUsageSemantics(str, Enum):
    SINGLE_EFFECT_SINGLE_USE = "single_effect_single_use"
    SINGLE_EFFECT_REUSABLE_UNTIL_EXPIRY = "single_effect_reusable_until_expiry"


class SubjectRef(FrozenContractModel):
    authority_domain: str
    type: str
    id: str
    version: str | int | None = None


class Objective(FrozenContractModel):
    description: str


class InputSnapshot(FrozenContractModel):
    schema_id: str
    schema_version: str
    values: Mapping[str, Any]
    content_hash: str


class CapabilityRef(FrozenContractModel):
    capability_id: str
    capability_version: str

    @property
    def ref(self) -> str:
        return f"{self.capability_id}@{self.capability_version}"


class ResolvedTool(FrozenContractModel):
    tool_name: str
    tool_version: str
    schema_hash: str
    capability_ref: str
    access_mode: ToolAccessMode


class Constraint(FrozenContractModel):
    constraint_id: str
    constraint_version: str
    kind: str
    enforcement_point: EnforcementPoint
    configuration: Mapping[str, Any] = Field(default_factory=dict)


class AuthorizedEffect(FrozenContractModel):
    effect_intent_id: str
    effect_type: str
    effect_type_version: str
    authority_domain: str
    subject_ref: SubjectRef
    parameter_constraints: Mapping[str, Any] = Field(default_factory=dict)
    max_confirmed_occurrences: int = Field(ge=1)
    approval_requirement_ref: str | None = None


class ApprovalRequirement(FrozenContractModel):
    approval_requirement_id: str
    approval_requirement_version: str
    effect_intent_refs: tuple[str, ...]
    risk_class: str
    approver_policy_ref: str
    usage_semantics: ApprovalUsageSemantics
    validity_policy: Mapping[str, Any] | None = None

    @property
    def ref(self) -> str:
        return (
            f"{self.approval_requirement_id}@"
            f"{self.approval_requirement_version}"
        )


class CompletionContractRef(FrozenContractModel):
    contract_id: str
    contract_version: str


class TaskLimits(FrozenContractModel):
    max_attempts: int = Field(ge=1)
    task_deadline: str
    max_executions_per_attempt: int = Field(ge=1)
    attempt_deadline: str | None = None
    max_feedback_cycles: int = Field(ge=0)
    max_reconcile_cycles: int = Field(ge=0)


class TaskContractRef(FrozenContractModel):
    contract_id: str
    contract_version: str
    contract_hash: str


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _walk_keys(child)


def compute_task_contract_hash(contract: Mapping[str, Any] | "TaskContract") -> str:
    """Compute the v1 hash over every semantic field except ``contract_hash``."""

    if isinstance(contract, TaskContract):
        payload = contract.model_dump(mode="json", exclude_none=True)
    else:
        payload = deepcopy(dict(contract))
    return sha256_digest(without_key(payload, "contract_hash"))


class TaskContract(FrozenContractModel):
    schema_version: str
    contract_id: str
    contract_version: str
    contract_hash: str
    task_type: str
    execution_type: ExecutionType
    objective: Objective
    subject_refs: tuple[SubjectRef, ...]
    input_snapshot: InputSnapshot
    allowed_capabilities: tuple[CapabilityRef, ...]
    resolved_tools: tuple[ResolvedTool, ...]
    constraints: tuple[Constraint, ...]
    authorized_effects: tuple[AuthorizedEffect, ...]
    approval_requirements: tuple[ApprovalRequirement, ...]
    completion_contract_ref: CompletionContractRef
    limits: TaskLimits

    @model_validator(mode="before")
    @classmethod
    def reject_workflow_surface(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            forbidden = FORBIDDEN_WORKFLOW_FIELDS.intersection(_walk_keys(value))
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ContractValidationError(
                    f"Task Contract contains forbidden workflow fields: {names}"
                )
        return value

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "1":
            raise ContractValidationError(f"Unsupported Task Contract schema: {value}")
        return value

    @model_validator(mode="after")
    def validate_contract_invariants(self) -> Self:
        expected_hash = compute_task_contract_hash(self)
        if self.contract_hash != expected_hash:
            raise ContractValidationError(
                f"Task Contract hash mismatch: expected {expected_hash}"
            )

        capability_refs = [item.ref for item in self.allowed_capabilities]
        if len(capability_refs) != len(set(capability_refs)):
            raise ContractValidationError("Duplicate allowed capability reference")
        for tool in self.resolved_tools:
            if tool.capability_ref not in capability_refs:
                raise ContractValidationError(
                    f"Tool {tool.tool_name!r} references an unauthorized capability"
                )

        intent_ids = [item.effect_intent_id for item in self.authorized_effects]
        if len(intent_ids) != len(set(intent_ids)):
            raise ContractValidationError("effect_intent_id must be unique")
        # Effect tool visibility is a capability envelope, not proof that a
        # concrete side effect has already been authorized. Exact effect
        # intents may be appended by the governed runtime after the Agent
        # proposes a fully normalized call. This keeps planning dynamic while
        # preserving fail-closed execution at the Gateway boundary.

        requirement_refs = [item.ref for item in self.approval_requirements]
        if len(requirement_refs) != len(set(requirement_refs)):
            raise ContractValidationError("Approval requirement refs must be unique")
        for requirement in self.approval_requirements:
            if not requirement.effect_intent_refs:
                raise ContractValidationError(
                    f"Approval requirement {requirement.ref!r} has no effect intents"
                )
            unknown = set(requirement.effect_intent_refs).difference(intent_ids)
            if unknown:
                raise ContractValidationError(
                    f"Approval requirement {requirement.ref!r} references unknown intents"
                )
        for intent in self.authorized_effects:
            if (
                intent.approval_requirement_ref is not None
                and intent.approval_requirement_ref not in requirement_refs
            ):
                raise ContractValidationError(
                    f"Effect intent {intent.effect_intent_id!r} references an unknown "
                    "approval requirement"
                )
            if intent.approval_requirement_ref is not None:
                requirement = self.approval_requirement(
                    intent.approval_requirement_ref
                )
                if intent.effect_intent_id not in requirement.effect_intent_refs:
                    raise ContractValidationError(
                        "Approval requirement does not bind the referencing effect intent"
                    )
        return self

    @classmethod
    def materialize(cls, value: Mapping[str, Any]) -> Self:
        """Insert the normative content hash, then validate the full envelope."""

        payload = deepcopy(dict(value))
        payload.pop("contract_hash", None)
        payload["contract_hash"] = compute_task_contract_hash(payload)
        return cls.model_validate(payload)

    @property
    def ref(self) -> TaskContractRef:
        return TaskContractRef(
            contract_id=self.contract_id,
            contract_version=self.contract_version,
            contract_hash=self.contract_hash,
        )

    def effect_intent(self, effect_intent_id: str) -> AuthorizedEffect:
        matches = [
            item
            for item in self.authorized_effects
            if item.effect_intent_id == effect_intent_id
        ]
        if len(matches) != 1:
            raise ContractValidationError(
                f"Expected one authorized effect intent {effect_intent_id!r}; "
                f"found {len(matches)} among "
                f"{[item.effect_intent_id for item in self.authorized_effects]!r}"
            )
        return matches[0]

    def approval_requirement(self, ref: str) -> ApprovalRequirement:
        matches = [item for item in self.approval_requirements if item.ref == ref]
        if len(matches) != 1:
            raise ContractValidationError(
                f"Expected one approval requirement {ref!r}"
            )
        return matches[0]
