"""Generic external observation and confirmation contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class ExternalObservation:
    external_object_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.external_object_id:
            raise ValueError("observation_identity_missing")


class ConfirmationStatus(str, Enum):
    CONFIRMED = "confirmed"
    INDETERMINATE = "indeterminate"
    FAILED = "failed"


@dataclass(frozen=True)
class ExternalOperationConfirmation:
    status: ConfirmationStatus
    observation: ExternalObservation | None = None
    reason: str | None = None
