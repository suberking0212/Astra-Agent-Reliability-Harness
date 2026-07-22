"""Canonical effect identity, normalizer registration, and operation models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, Self

from pydantic import Field, model_validator

from .canonical import sha256_digest
from .task_contract import (
    FrozenContractModel,
    SubjectRef,
    TaskContract,
    TaskContractRef,
)


class EffectContractError(ValueError):
    """A raw request cannot be deterministically authorized and normalized."""


class EffectNormalizationCandidate(FrozenContractModel):
    effect_intent_ref: str
    normalized_parameters: Mapping[str, Any]


class EffectNormalizer(Protocol):
    normalizer_id: str
    normalizer_version: str

    def normalize(
        self,
        contract: TaskContract,
        raw_request: Mapping[str, Any],
    ) -> Sequence[EffectNormalizationCandidate]: ...


class CanonicalEffectRequest(FrozenContractModel):
    effect_schema_version: str = "1"
    normalizer_id: str
    normalizer_version: str
    task_contract_ref: TaskContractRef
    effect_intent_ref: str
    authority_domain: str
    effect_type: str
    effect_type_version: str
    subject_ref: SubjectRef
    normalized_parameters: Mapping[str, Any]
    normalized_parameters_hash: str
    effect_request_hash: str
    effect_identity: str

    @model_validator(mode="after")
    def validate_hash_chain(self) -> Self:
        if self.effect_schema_version != "1":
            raise EffectContractError(
                f"Unsupported effect schema {self.effect_schema_version!r}"
            )
        parameters_hash = sha256_digest(self.normalized_parameters)
        if self.normalized_parameters_hash != parameters_hash:
            raise EffectContractError("normalized_parameters_hash mismatch")
        request_hash = compute_effect_request_hash(self)
        if self.effect_request_hash != request_hash:
            raise EffectContractError("effect_request_hash mismatch")
        if self.effect_identity != f"effect:{request_hash}":
            raise EffectContractError("effect_identity mismatch")
        return self


def _effect_hash_payload(
    *,
    effect_schema_version: str,
    normalizer_id: str,
    normalizer_version: str,
    task_contract_ref: TaskContractRef | Mapping[str, Any],
    effect_intent_ref: str,
    authority_domain: str,
    effect_type: str,
    effect_type_version: str,
    subject_ref: SubjectRef | Mapping[str, Any],
    normalized_parameters_hash: str,
) -> dict[str, Any]:
    def dump(value: Any) -> Any:
        if isinstance(value, FrozenContractModel):
            return value.model_dump(mode="json", exclude_none=True)
        return value

    return {
        "effect_schema_version": effect_schema_version,
        "normalizer_id": normalizer_id,
        "normalizer_version": normalizer_version,
        "task_contract_ref": dump(task_contract_ref),
        "effect_intent_ref": effect_intent_ref,
        "authority_domain": authority_domain,
        "effect_type": effect_type,
        "effect_type_version": effect_type_version,
        "subject_ref": dump(subject_ref),
        "normalized_parameters_hash": normalized_parameters_hash,
    }


def compute_effect_request_hash(effect: CanonicalEffectRequest) -> str:
    return sha256_digest(
        _effect_hash_payload(
            effect_schema_version=effect.effect_schema_version,
            normalizer_id=effect.normalizer_id,
            normalizer_version=effect.normalizer_version,
            task_contract_ref=effect.task_contract_ref,
            effect_intent_ref=effect.effect_intent_ref,
            authority_domain=effect.authority_domain,
            effect_type=effect.effect_type,
            effect_type_version=effect.effect_type_version,
            subject_ref=effect.subject_ref,
            normalized_parameters_hash=effect.normalized_parameters_hash,
        )
    )


def build_canonical_effect_request(
    contract: TaskContract,
    *,
    effect_intent_ref: str,
    normalized_parameters: Mapping[str, Any],
    normalizer_id: str,
    normalizer_version: str,
) -> CanonicalEffectRequest:
    """Build the canonical identity after a versioned normalizer proves a match."""

    intent = contract.effect_intent(effect_intent_ref)
    parameters = dict(normalized_parameters)
    parameters_hash = sha256_digest(parameters)
    request_hash = sha256_digest(
        _effect_hash_payload(
            effect_schema_version="1",
            normalizer_id=normalizer_id,
            normalizer_version=normalizer_version,
            task_contract_ref=contract.ref,
            effect_intent_ref=intent.effect_intent_id,
            authority_domain=intent.authority_domain,
            effect_type=intent.effect_type,
            effect_type_version=intent.effect_type_version,
            subject_ref=intent.subject_ref,
            normalized_parameters_hash=parameters_hash,
        )
    )
    return CanonicalEffectRequest(
        normalizer_id=normalizer_id,
        normalizer_version=normalizer_version,
        task_contract_ref=contract.ref,
        effect_intent_ref=intent.effect_intent_id,
        authority_domain=intent.authority_domain,
        effect_type=intent.effect_type,
        effect_type_version=intent.effect_type_version,
        subject_ref=intent.subject_ref,
        normalized_parameters=parameters,
        normalized_parameters_hash=parameters_hash,
        effect_request_hash=request_hash,
        effect_identity=f"effect:{request_hash}",
    )


class EffectNormalizerRegistry:
    """Static, version-keyed normalizer registry with fail-closed lookup."""

    def __init__(self) -> None:
        self._normalizers: dict[tuple[str, str], EffectNormalizer] = {}

    def register(self, normalizer: EffectNormalizer) -> None:
        key = (normalizer.normalizer_id, normalizer.normalizer_version)
        if key in self._normalizers:
            raise EffectContractError(f"Normalizer already registered: {key!r}")
        self._normalizers[key] = normalizer

    @property
    def registered_refs(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._normalizers))

    def normalize(
        self,
        contract: TaskContract,
        raw_request: Mapping[str, Any],
        *,
        normalizer_id: str,
        normalizer_version: str,
    ) -> CanonicalEffectRequest:
        normalizer = self._normalizers.get((normalizer_id, normalizer_version))
        if normalizer is None:
            raise EffectContractError(
                f"Unknown normalizer/version: {normalizer_id}@{normalizer_version}"
            )
        candidates = tuple(normalizer.normalize(contract, raw_request))
        if len(candidates) != 1:
            raise EffectContractError(
                "Effect request must match exactly one authorized effect intent"
            )
        candidate = candidates[0]
        contract.effect_intent(candidate.effect_intent_ref)
        return build_canonical_effect_request(
            contract,
            effect_intent_ref=candidate.effect_intent_ref,
            normalized_parameters=candidate.normalized_parameters,
            normalizer_id=normalizer_id,
            normalizer_version=normalizer_version,
        )


def derive_idempotency_key(
    effect_identity: str,
    *,
    adapter_key_version: str = "1",
) -> str:
    """Derive a stable adapter replay key; model-supplied keys are never used."""

    return sha256_digest(
        {
            "effect_identity": effect_identity,
            "adapter_key_version": adapter_key_version,
        }
    )


class ExternalOperationStatus(str, Enum):
    PREPARED = "prepared"
    IN_FLIGHT = "in_flight"
    ACKNOWLEDGED = "acknowledged"
    CONFIRMED = "confirmed"
    INDETERMINATE = "indeterminate"
    FAILED = "failed"


class ExternalOperation(FrozenContractModel):
    operation_id: str
    task_id: str
    attempt_id: str
    execution_id: str
    task_contract_ref: TaskContractRef
    authority_domain: str
    effect_identity: str
    effect_request_hash: str
    effect_type: str
    effect_type_version: str
    subject_ref: SubjectRef
    idempotency_key: str
    approval_ref: str | None = None
    external_operation_id: str | None = None
    status: ExternalOperationStatus
    version: int = Field(ge=1)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    acknowledged_at: datetime | None = None
    confirmed_at: datetime | None = None

    @classmethod
    def from_effect(
        cls,
        effect: CanonicalEffectRequest,
        *,
        operation_id: str,
        task_id: str,
        attempt_id: str,
        execution_id: str,
        status: ExternalOperationStatus = ExternalOperationStatus.PREPARED,
        approval_ref: str | None = None,
        adapter_key_version: str = "1",
        created_at: datetime | None = None,
    ) -> Self:
        return cls(
            operation_id=operation_id,
            task_id=task_id,
            attempt_id=attempt_id,
            execution_id=execution_id,
            task_contract_ref=effect.task_contract_ref,
            authority_domain=effect.authority_domain,
            effect_identity=effect.effect_identity,
            effect_request_hash=effect.effect_request_hash,
            effect_type=effect.effect_type,
            effect_type_version=effect.effect_type_version,
            subject_ref=effect.subject_ref,
            idempotency_key=derive_idempotency_key(
                effect.effect_identity,
                adapter_key_version=adapter_key_version,
            ),
            approval_ref=approval_ref,
            status=status,
            version=1,
            created_at=created_at or datetime.now(timezone.utc),
        )
