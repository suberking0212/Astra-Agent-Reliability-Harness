"""Immutable Reliability Fact and Outbox value objects."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from .task_contract import FrozenContractModel, SubjectRef


class FactSource(FrozenContractModel):
    kind: str
    name: str
    version: str


class ReliabilityFact(FrozenContractModel):
    schema_version: str = "1"
    fact_id: str
    fact_type: str
    task_id: str
    attempt_id: str | None = None
    execution_id: str | None = None
    source: FactSource
    authority_scope: str
    source_event_id: str
    subject_ref: SubjectRef
    occurred_at: datetime
    recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    causation_fact_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = Field(default_factory=dict)


class OutboxMessage(FrozenContractModel):
    outbox_id: str
    fact_id: str
    topic: str
    payload: Mapping[str, Any]
    delivery_status: str = "pending"
    delivery_attempts: int = Field(default=0, ge=0)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
