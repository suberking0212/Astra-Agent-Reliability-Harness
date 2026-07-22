"""Evidence snapshots, requirement evaluators, and generic completion aggregation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from pydantic import Field, model_validator

from .canonical import sha256_digest
from .effects import (
    CanonicalEffectRequest,
    ExternalOperation,
    ExternalOperationStatus,
)
from .task_contract import FrozenContractModel, TaskContractRef


def build_evidence_snapshot_identity(
    *,
    task_id: str,
    collection_trigger_id: str,
    authoritative_versions: Mapping[str, Any],
    collector_version: str,
) -> tuple[str, str]:
    """Return the frozen authoritative-version hash and Snapshot identity."""

    authoritative_versions_hash = sha256_digest(authoritative_versions)
    evidence_snapshot_id = "snapshot:" + sha256_digest(
        {
            "task_id": task_id,
            "collection_trigger_id": collection_trigger_id,
            "authoritative_versions_hash": authoritative_versions_hash,
            "collector_version": collector_version,
        }
    )
    return authoritative_versions_hash, evidence_snapshot_id


class BusinessObservation(FrozenContractModel):
    """One immutable, versioned observation captured from an authority."""

    observation_id: str
    observation_version: int = Field(ge=1)
    task_id: str
    operation_id: str
    authority_domain: str
    effect_identity: str
    effect_request_hash: str
    external_object_id: str
    state: Mapping[str, Any]
    content_hash: str

    @classmethod
    def materialize(cls, **values: Any) -> "BusinessObservation":
        payload = dict(values)
        payload.pop("content_hash", None)
        payload["content_hash"] = sha256_digest(
            {
                "observation_id": payload["observation_id"],
                "task_id": payload["task_id"],
                "operation_id": payload["operation_id"],
                "authority_domain": payload["authority_domain"],
                "effect_identity": payload["effect_identity"],
                "effect_request_hash": payload["effect_request_hash"],
                "external_object_id": payload["external_object_id"],
                "state": payload["state"],
            }
        )
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_content_hash(self) -> "BusinessObservation":
        expected = sha256_digest(
            {
                "observation_id": self.observation_id,
                "task_id": self.task_id,
                "operation_id": self.operation_id,
                "authority_domain": self.authority_domain,
                "effect_identity": self.effect_identity,
                "effect_request_hash": self.effect_request_hash,
                "external_object_id": self.external_object_id,
                "state": self.state,
            }
        )
        if self.content_hash != expected:
            raise ValueError("BusinessObservation content_hash mismatch")
        return self


class EvidenceSnapshot(FrozenContractModel):
    evidence_snapshot_id: str
    task_id: str
    attempt_id: str
    collection_trigger_id: str
    authoritative_versions_hash: str
    authoritative_versions: Mapping[str, Any]
    task_version: int = Field(ge=0)
    attempt_version: int = Field(ge=0)
    task_contract_ref: TaskContractRef
    execution_refs: Mapping[str, str] = Field(default_factory=dict)
    receipt_refs: tuple[str, ...] = ()
    receipt_hashes: Mapping[str, str] = Field(default_factory=dict)
    interaction_refs: Mapping[str, int] = Field(default_factory=dict)
    interaction_states: Mapping[str, str] = Field(default_factory=dict)
    external_operations: tuple[ExternalOperation, ...] = ()
    canonical_effects: tuple[CanonicalEffectRequest, ...] = ()
    effect_refs: Mapping[str, str] = Field(default_factory=dict)
    approval_refs: Mapping[str, str] = Field(default_factory=dict)
    business_observations: tuple[BusinessObservation, ...] = ()
    business_observation_refs: tuple[str, ...] = ()
    completion_refs: Mapping[str, str] = Field(default_factory=dict)
    pending_interaction_kinds: tuple[str, ...] = ()
    approval_required: bool = False
    approval_matched: bool = True
    submitted_result_present: bool = False
    runtime_counters: Mapping[str, int] = Field(default_factory=dict)
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
        payload.setdefault("execution_refs", {})
        payload.setdefault("receipt_refs", ())
        payload.setdefault("receipt_hashes", {})
        payload.setdefault("interaction_refs", {})
        payload.setdefault("interaction_states", {})
        payload.setdefault("external_operations", ())
        payload.setdefault("canonical_effects", ())
        payload.setdefault("effect_refs", {})
        payload.setdefault("approval_refs", {})
        payload.setdefault("business_observations", ())
        payload.setdefault("business_observation_refs", ())
        payload.setdefault("completion_refs", {})
        payload.setdefault("pending_interaction_kinds", ())
        payload.setdefault("approval_required", False)
        payload.setdefault("approval_matched", True)
        payload.setdefault("submitted_result_present", False)
        payload.setdefault("runtime_counters", {})
        payload.setdefault("created_at", datetime.now(timezone.utc))
        expected_versions_hash = sha256_digest(payload["authoritative_versions"])
        if payload.get("authoritative_versions_hash") != expected_versions_hash:
            raise ValueError("EvidenceSnapshot authoritative_versions_hash mismatch")
        payload["content_hash"] = sha256_digest(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_content_hash(self) -> "EvidenceSnapshot":
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_hash", None)
        if self.authoritative_versions_hash != sha256_digest(
            self.authoritative_versions
        ):
            raise ValueError("EvidenceSnapshot authoritative_versions_hash mismatch")
        _, expected_id = build_evidence_snapshot_identity(
            task_id=self.task_id,
            collection_trigger_id=self.collection_trigger_id,
            authoritative_versions=self.authoritative_versions,
            collector_version=self.collector_version,
        )
        if self.evidence_snapshot_id != expected_id:
            raise ValueError("EvidenceSnapshot identity mismatch")
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
        effect_bindings: list[str] = []
        for requirement in self.requirements:
            if (
                requirement.evaluator.evaluator_id
                != AuthorizedEffectConfirmedEvaluator.evaluator_id
                or requirement.evaluator.evaluator_version
                != AuthorizedEffectConfirmedEvaluator.evaluator_version
            ):
                continue
            effect_intent_ref = requirement.configuration.get("effect_intent_ref")
            if isinstance(effect_intent_ref, str):
                effect_bindings.append(effect_intent_ref)
        if len(effect_bindings) != len(set(effect_bindings)):
            raise ValueError(
                "One authorized effect intent cannot satisfy multiple requirements"
            )
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

    @property
    def registered_refs(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._evaluators))

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
            result.evaluation_id != evaluation_id
            or result.requirement_id != requirement.requirement_id
            or result.evaluator_id != key[0]
            or result.evaluator_version != key[1]
            or result.submitted_result_id != submitted_result.submitted_result_id
            or result.evidence_snapshot_id != evidence_snapshot.evidence_snapshot_id
        ):
            return RequirementEvaluation(
                evaluation_id=evaluation_id,
                requirement_id=requirement.requirement_id,
                evaluator_id=key[0],
                evaluator_version=key[1],
                submitted_result_id=submitted_result.submitted_result_id,
                evidence_snapshot_id=evidence_snapshot.evidence_snapshot_id,
                status=RequirementStatus.EVALUATOR_ERROR,
                message="Evaluator returned an evaluation for different inputs",
                details={"error_type": "invalid_evaluator_output"},
            )
        return result


class AuthorizedEffectConfirmedEvaluator:
    """Frozen evaluator for the Round 2 authoritative-effect requirement."""

    evaluator_id = "astra.authorized_effect_confirmed"
    evaluator_version = "1"

    def evaluate(
        self,
        requirement: CompletionRequirement,
        submitted_result: SubmittedResult,
        evidence_snapshot: EvidenceSnapshot,
    ) -> RequirementEvaluation:
        configuration = dict(requirement.configuration)
        required_evidence = set(requirement.required_evidence)
        supported_evidence = {
            "external_operation",
            "business_state",
            "approval",
            "canonical_effect",
        }
        unknown_evidence = required_evidence.difference(supported_evidence)
        if unknown_evidence:
            raise ValueError(
                "Unsupported required evidence: "
                + ", ".join(sorted(unknown_evidence))
            )
        if not {"external_operation", "business_state"}.issubset(
            required_evidence
        ):
            raise ValueError(
                "authorized effect completion requires external_operation "
                "and business_state evidence"
            )
        supported_keys = {
            "effect_intent_ref",
            "authority_domain",
            "effect_type",
            "effect_type_version",
            "subject_ref",
            "normalized_parameters",
            "expected_observation",
            "approval_required",
        }
        unknown_keys = set(configuration).difference(supported_keys)
        if unknown_keys:
            raise ValueError(
                "Unsupported authorized-effect evaluator configuration: "
                + ", ".join(sorted(unknown_keys))
            )
        effect_intent_ref = configuration.get("effect_intent_ref")
        if not isinstance(effect_intent_ref, str) or not effect_intent_ref:
            raise ValueError("effect_intent_ref is required")
        expected_parameters = configuration.get("normalized_parameters", {})
        expected_observation = configuration.get("expected_observation", {})
        if not isinstance(expected_parameters, Mapping):
            raise ValueError("normalized_parameters must be a mapping")
        if not isinstance(expected_observation, Mapping):
            raise ValueError("expected_observation must be a mapping")
        expected_subject = configuration.get("subject_ref")
        if expected_subject is not None and not isinstance(expected_subject, Mapping):
            raise ValueError("subject_ref must be a mapping")
        approval_required = configuration.get("approval_required", False)
        if not isinstance(approval_required, bool):
            raise ValueError("approval_required must be a boolean")

        canonical_by_identity = {
            effect.effect_identity: effect
            for effect in evidence_snapshot.canonical_effects
        }
        matching: list[tuple[ExternalOperation, CanonicalEffectRequest]] = []
        for operation in evidence_snapshot.external_operations:
            effect = canonical_by_identity.get(operation.effect_identity)
            if effect is None:
                continue
            if (
                operation.task_contract_ref != evidence_snapshot.task_contract_ref
                or effect.task_contract_ref != evidence_snapshot.task_contract_ref
                or effect.effect_request_hash != operation.effect_request_hash
                or effect.effect_intent_ref != effect_intent_ref
                or evidence_snapshot.effect_refs.get(operation.effect_identity)
                != operation.effect_request_hash
            ):
                continue
            if (
                configuration.get("authority_domain") is not None
                and effect.authority_domain != configuration["authority_domain"]
            ):
                continue
            if (
                configuration.get("effect_type") is not None
                and effect.effect_type != configuration["effect_type"]
            ):
                continue
            if (
                configuration.get("effect_type_version") is not None
                and effect.effect_type_version
                != configuration["effect_type_version"]
            ):
                continue
            if expected_subject is not None and effect.subject_ref.model_dump(
                mode="json", exclude_none=True
            ) != dict(expected_subject):
                continue
            if any(
                effect.normalized_parameters.get(key) != value
                for key, value in expected_parameters.items()
            ):
                continue
            if approval_required and (
                operation.approval_ref is None
                or operation.approval_ref not in evidence_snapshot.approval_refs
            ):
                continue
            matching.append((operation, effect))

        confirmed = tuple(
            item for item in matching
            if item[0].status == ExternalOperationStatus.CONFIRMED
        )
        matched_observation_refs: list[str] = []
        for operation, effect in confirmed:
            for observation in evidence_snapshot.business_observations:
                if (
                    observation.task_id != evidence_snapshot.task_id
                    or observation.operation_id != operation.operation_id
                    or observation.authority_domain != operation.authority_domain
                    or observation.effect_identity != effect.effect_identity
                    or observation.effect_request_hash != effect.effect_request_hash
                    or observation.external_object_id
                    != operation.external_operation_id
                ):
                    continue
                if any(
                    observation.state.get(key) != value
                    for key, value in effect.normalized_parameters.items()
                ):
                    continue
                if any(
                    observation.state.get(key) != value
                    for key, value in expected_observation.items()
                ):
                    continue
                matched_observation_refs.append(observation.observation_id)

        if matched_observation_refs:
            status = RequirementStatus.SATISFIED
        elif confirmed:
            status = RequirementStatus.UNSATISFIED
        elif not matching or any(
            operation.status
            in {
                ExternalOperationStatus.PREPARED,
                ExternalOperationStatus.IN_FLIGHT,
                ExternalOperationStatus.ACKNOWLEDGED,
                ExternalOperationStatus.INDETERMINATE,
            }
            for operation, _ in matching
        ):
            status = RequirementStatus.UNKNOWN
        else:
            status = RequirementStatus.UNSATISFIED
        evaluation_id = "evaluation:" + sha256_digest(
            {
                "requirement_id": requirement.requirement_id,
                "evaluator_id": self.evaluator_id,
                "evaluator_version": self.evaluator_version,
                "submitted_result_id": submitted_result.submitted_result_id,
                "evidence_snapshot_id": evidence_snapshot.evidence_snapshot_id,
            }
        )
        return RequirementEvaluation(
            evaluation_id=evaluation_id,
            requirement_id=requirement.requirement_id,
            evaluator_id=self.evaluator_id,
            evaluator_version=self.evaluator_version,
            submitted_result_id=submitted_result.submitted_result_id,
            evidence_snapshot_id=evidence_snapshot.evidence_snapshot_id,
            status=status,
            evidence_refs=tuple(
                dict.fromkeys(
                    [operation.operation_id for operation, _ in matching]
                    + matched_observation_refs
                )
            ),
            observed_fact_refs=tuple(matched_observation_refs),
        )


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
