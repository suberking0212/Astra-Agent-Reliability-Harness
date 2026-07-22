"""Exact Approval binding for canonical effect requests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from .effects import CanonicalEffectRequest
from .task_contract import (
    ApprovalUsageSemantics,
    FrozenContractModel,
    TaskContract,
    TaskContractRef,
)


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalBindingCode(str, Enum):
    MATCHED = "matched"
    REQUIRED = "approval_required"
    DENIED = "approval_denied"
    EXPIRED = "approval_expired"
    REVOKED = "approval_revoked"
    SCOPE_MISMATCH = "approval_scope_mismatch"
    EFFECT_MISMATCH = "approval_effect_mismatch"
    ALREADY_CONSUMED = "approval_already_consumed"
    STALE_CONTRACT = "approval_stale_contract"
    REQUIREMENT_MISMATCH = "approval_requirement_mismatch"
    POLICY_MISMATCH = "approval_policy_mismatch"


class ApprovalRequirementRef(FrozenContractModel):
    approval_requirement_id: str
    approval_requirement_version: str

    @property
    def ref(self) -> str:
        return (
            f"{self.approval_requirement_id}@"
            f"{self.approval_requirement_version}"
        )

    @classmethod
    def parse(cls, value: str) -> Self:
        try:
            identifier, version = value.rsplit("@", 1)
        except ValueError as exc:
            raise ValueError(f"Invalid approval requirement ref: {value!r}") from exc
        if not identifier or not version:
            raise ValueError(f"Invalid approval requirement ref: {value!r}")
        return cls(
            approval_requirement_id=identifier,
            approval_requirement_version=version,
        )


class ApprovalRequest(FrozenContractModel):
    approval_request_id: str
    interaction_id: str
    task_id: str
    attempt_id: str
    execution_id: str
    task_contract_ref: TaskContractRef
    approval_requirement_ref: ApprovalRequirementRef
    effect_identity: str
    effect_request_hash: str
    effect_summary: Mapping[str, Any]
    permission_scope: str
    risk_class: str
    approver_policy_ref: str
    requested_at: datetime
    expires_at: datetime | None = None
    status: str = "pending"
    version: int = Field(default=1, ge=1)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in {"pending", "resolved", "cancelled"}:
            raise ValueError(f"Invalid ApprovalRequest status: {value!r}")
        return value


class ApprovalResolution(FrozenContractModel):
    approval_resolution_id: str
    approval_request_id: str
    interaction_id: str
    decision: ApprovalDecision
    task_contract_ref: TaskContractRef
    effect_identity: str
    effect_request_hash: str
    approval_requirement_ref: ApprovalRequirementRef
    approver_subject_ref: str
    approver_policy_ref: str
    permission_scope: str
    resolved_at: datetime
    valid_from: datetime
    expires_at: datetime | None = None
    usage_semantics: ApprovalUsageSemantics
    resolution_version: str = "1"
    evidence_refs: tuple[str, ...] = ()
    reason: str | None = None
    revoked: bool = False
    usage_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.valid_from:
            raise ValueError("Approval expiry must follow valid_from")
        return self


class ApprovalBindingResult(FrozenContractModel):
    matched: bool
    code: ApprovalBindingCode


def verify_approval_binding(
    contract: TaskContract,
    effect: CanonicalEffectRequest,
    resolution: ApprovalResolution | None,
    *,
    permission_scope: str = "execute_effect",
    now: datetime | None = None,
    existing_operation_effect_identity: str | None = None,
) -> ApprovalBindingResult:
    """Verify every frozen field before an ExternalOperation is prepared."""

    intent = contract.effect_intent(effect.effect_intent_ref)
    requirement_ref = intent.approval_requirement_ref
    if requirement_ref is None:
        return ApprovalBindingResult(matched=True, code=ApprovalBindingCode.MATCHED)
    if resolution is None:
        return ApprovalBindingResult(matched=False, code=ApprovalBindingCode.REQUIRED)
    if resolution.decision != ApprovalDecision.APPROVED:
        return ApprovalBindingResult(matched=False, code=ApprovalBindingCode.DENIED)
    if resolution.task_contract_ref != contract.ref:
        return ApprovalBindingResult(
            matched=False, code=ApprovalBindingCode.STALE_CONTRACT
        )
    if resolution.approval_requirement_ref.ref != requirement_ref:
        return ApprovalBindingResult(
            matched=False, code=ApprovalBindingCode.REQUIREMENT_MISMATCH
        )
    requirement = contract.approval_requirement(requirement_ref)
    if resolution.approver_policy_ref != requirement.approver_policy_ref:
        return ApprovalBindingResult(
            matched=False, code=ApprovalBindingCode.POLICY_MISMATCH
        )
    if (
        resolution.effect_identity != effect.effect_identity
        or resolution.effect_request_hash != effect.effect_request_hash
    ):
        return ApprovalBindingResult(
            matched=False, code=ApprovalBindingCode.EFFECT_MISMATCH
        )
    if resolution.permission_scope != permission_scope:
        return ApprovalBindingResult(
            matched=False, code=ApprovalBindingCode.SCOPE_MISMATCH
        )
    current = now or datetime.now(timezone.utc)
    if current < resolution.valid_from or (
        resolution.expires_at is not None and current >= resolution.expires_at
    ):
        return ApprovalBindingResult(matched=False, code=ApprovalBindingCode.EXPIRED)
    if resolution.revoked:
        return ApprovalBindingResult(matched=False, code=ApprovalBindingCode.REVOKED)
    if (
        resolution.usage_semantics
        == ApprovalUsageSemantics.SINGLE_EFFECT_SINGLE_USE
        and resolution.usage_count > 0
        and existing_operation_effect_identity != effect.effect_identity
    ):
        return ApprovalBindingResult(
            matched=False, code=ApprovalBindingCode.ALREADY_CONSUMED
        )
    return ApprovalBindingResult(matched=True, code=ApprovalBindingCode.MATCHED)
