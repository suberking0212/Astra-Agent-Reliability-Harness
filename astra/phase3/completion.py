"""Evidence snapshots, requirement evaluators, and generic completion aggregation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from pydantic import Field, model_validator

from .canonical import sha256_digest
from .effects import ExternalOperation
from .task_contract import FrozenContractModel, TaskContractRef


class EvidenceSnapshot(FrozenContractModel):
    evidence_snapshot_id: str
    task_id: str
    attempt_id: str
    task_version: int = Field(ge=0)
    attempt_version: int = Field(ge=0)
    task_contract_ref: TaskContractRef
    receipt_refs: tuple[str, ...] = ()
    receipt_hashes: Mapping[str, str] = Field(default_factory=dict)
    interaction_refs: Mapping[str, int] = Field(default_factory=dict)
    external_operations: tuple[ExternalOperation, ...] = ()
    effect_refs: Mapping[str, str] = Field(default_factory=dict)
    approval_refs: Mapping[str, int | str] = Field(default_factory=dict)
    business_observation_refs: tuple[str, ...] = ()
    fact_watermark: int = Field(ge=0)
    collector_version: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    content_hash: str

    @classmethod
    def materialize(cls, **values: Any) -> "EvidenceSnapshot":
        payload = dict(values)
        payload.pop("content_hash", None)
        payload.setdefault("receipt_refs", ())
        payload.setdefault("receipt_hashes", {})
        payload.setdefault("interaction_refs", {})
        payload.setdefault("external_operations", ())
        payload.setdefault("effect_refs", {})
        payload.setdefault("approval_refs", {})
        payload.setdefault("business_observation_refs", ())
        payload.setdefault("created_at", datetime.now(timezone.utc))
        payload["content_hash"] = sha256_digest(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_content_hash(self) -> "EvidenceSnapshot":
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_hash", None)
        if self.content_hash != sha256_digest(payload):
            raise ValueError("EvidenceSnapshot content_hash mismatch")
        return self


class RequirementStatus(str, Enum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    UNKNOWN = "unknown"
    EVALUATOR_ERROR = "evaluator_error"


class CompletionStatus(str, Enum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    INDETERMINATE = "indeterminate"
    EVALUATOR_ERROR = "evaluator_error"


class EvaluatorRef(FrozenContractModel):
    evaluator_id: str
    evaluator_version: str


class CompletionRequirement(FrozenContractModel):
    requirement_id: str
    description: str
    required: bool = True
    evaluator: EvaluatorRef
    configuration: Mapping[str, Any] = Field(default_factory=dict)
    required_evidence: tuple[str, ...] = ()


class CompletionContract(FrozenContractModel):
    contract_id: str
    contract_version: str
    task_type: str
    requirements: tuple[CompletionRequirement, ...]
    aggregation_policy: str = "all_required"

    @model_validator(mode="after")
    def validate_contract(self) -> "CompletionContract":
        if self.aggregation_policy != "all_required":
            raise ValueError("Phase 3 v1 only supports all_required aggregation")
        identifiers = [item.requirement_id for item in self.requirements]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Completion requirement IDs must be unique")
        return self


class SubmittedResult(FrozenContractModel):
    submitted_result_id: str
    task_id: str
    attempt_id: str
    execution_id: str
    outcome: Mapping[str, Any]
    evidence_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()


class RequirementEvaluation(FrozenContractModel):
    evaluation_id: str
    requirement_id: str
    evaluator_id: str
    evaluator_version: str
    submitted_result_id: str
    evidence_snapshot_id: str
    status: RequirementStatus
    evidence_refs: tuple[str, ...] = ()
    observed_fact_refs: tuple[str, ...] = ()
    message: str = ""
    details: Mapping[str, Any] = Field(default_factory=dict)


class RequirementEvaluator(Protocol):
    evaluator_id: str
    evaluator_version: str

    def evaluate(
        self,
        requirement: CompletionRequirement,
        submitted_result: SubmittedResult,
        evidence_snapshot: EvidenceSnapshot,
    ) -> RequirementEvaluation: ...


class RequirementEvaluatorRegistry:
    """Versioned pure evaluator registry; unknown or broken plugins fail closed."""

    def __init__(self) -> None:
        self._evaluators: dict[tuple[str, str], RequirementEvaluator] = {}

    def register(self, evaluator: RequirementEvaluator) -> None:
        key = (evaluator.evaluator_id, evaluator.evaluator_version)
        if key in self._evaluators:
            raise ValueError(f"Evaluator already registered: {key!r}")
        self._evaluators[key] = evaluator

    def evaluate(
        self,
        requirement: CompletionRequirement,
        submitted_result: SubmittedResult,
        evidence_snapshot: EvidenceSnapshot,
    ) -> RequirementEvaluation:
        key = (
            requirement.evaluator.evaluator_id,
            requirement.evaluator.evaluator_version,
        )
        evaluation_id = "evaluation:" + sha256_digest(
            {
                "requirement_id": requirement.requirement_id,
                "evaluator_id": key[0],
                "evaluator_version": key[1],
                "submitted_result_id": submitted_result.submitted_result_id,
                "evidence_snapshot_id": evidence_snapshot.evidence_snapshot_id,
            }
        )
        evaluator = self._evaluators.get(key)
        if evaluator is None:
            return RequirementEvaluation(
                evaluation_id=evaluation_id,
                requirement_id=requirement.requirement_id,
                evaluator_id=key[0],
                evaluator_version=key[1],
                submitted_result_id=submitted_result.submitted_result_id,
                evidence_snapshot_id=evidence_snapshot.evidence_snapshot_id,
                status=RequirementStatus.EVALUATOR_ERROR,
                message="Evaluator is not registered",
                details={"error_type": "unknown_evaluator"},
            )
        try:
            result = evaluator.evaluate(
                requirement, submitted_result, evidence_snapshot
            )
        except Exception as exc:  # plugin failures are data, not implicit success
            return RequirementEvaluation(
                evaluation_id=evaluation_id,
                requirement_id=requirement.requirement_id,
                evaluator_id=key[0],
                evaluator_version=key[1],
                submitted_result_id=submitted_result.submitted_result_id,
                evidence_snapshot_id=evidence_snapshot.evidence_snapshot_id,
                status=RequirementStatus.EVALUATOR_ERROR,
                message="Evaluator raised an exception",
                details={"error_type": type(exc).__name__},
            )
        if (
            result.requirement_id != requirement.requirement_id
            or result.evaluator_id != key[0]
            or result.evaluator_version != key[1]
            or result.submitted_result_id != submitted_result.submitted_result_id
            or result.evidence_snapshot_id != evidence_snapshot.evidence_snapshot_id
        ):
            raise ValueError("Evaluator returned an evaluation for different inputs")
        return result


class CompletionValidationResult(FrozenContractModel):
    completion_validation_id: str
    contract_id: str
    contract_version: str
    submitted_result_id: str
    evidence_snapshot_id: str
    status: CompletionStatus
    requirement_evaluation_refs: tuple[str, ...]
    unmet_requirement_ids: tuple[str, ...]
    unknown_requirement_ids: tuple[str, ...]


def aggregate_completion(
    contract: CompletionContract,
    submitted_result: SubmittedResult,
    evidence_snapshot: EvidenceSnapshot,
    evaluations: Sequence[RequirementEvaluation],
) -> CompletionValidationResult:
    """Apply the frozen, business-neutral ``all_required`` precedence."""

    by_requirement = {item.requirement_id: item for item in evaluations}
    required = [item for item in contract.requirements if item.required]
    if any(item.requirement_id not in by_requirement for item in required):
        raise ValueError("Missing required RequirementEvaluation")
    relevant = [by_requirement[item.requirement_id] for item in required]
    if any(item.status == RequirementStatus.EVALUATOR_ERROR for item in relevant):
        status = CompletionStatus.EVALUATOR_ERROR
    elif any(item.status == RequirementStatus.UNSATISFIED for item in relevant):
        status = CompletionStatus.UNSATISFIED
    elif any(item.status == RequirementStatus.UNKNOWN for item in relevant):
        status = CompletionStatus.INDETERMINATE
    else:
        status = CompletionStatus.SATISFIED

    unmet = tuple(
        item.requirement_id
        for item in relevant
        if item.status == RequirementStatus.UNSATISFIED
    )
    unknown = tuple(
        item.requirement_id
        for item in relevant
        if item.status == RequirementStatus.UNKNOWN
    )
    validation_id = "completion:" + sha256_digest(
        {
            "contract_id": contract.contract_id,
            "contract_version": contract.contract_version,
            "submitted_result_id": submitted_result.submitted_result_id,
            "evidence_snapshot_id": evidence_snapshot.evidence_snapshot_id,
            "requirement_evaluation_refs": [
                item.evaluation_id for item in relevant
            ],
        }
    )
    return CompletionValidationResult(
        completion_validation_id=validation_id,
        contract_id=contract.contract_id,
        contract_version=contract.contract_version,
        submitted_result_id=submitted_result.submitted_result_id,
        evidence_snapshot_id=evidence_snapshot.evidence_snapshot_id,
        status=status,
        requirement_evaluation_refs=tuple(
            item.evaluation_id for item in relevant
        ),
        unmet_requirement_ids=unmet,
        unknown_requirement_ids=unknown,
    )
