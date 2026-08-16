"""Evidence envelope for production acceptance runs.

The envelope deliberately separates observed runtime facts from claims that the
run is eligible to support.  A report must also state claims it is prohibited
from making, preventing scripted or partial runs from being over-interpreted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProductionEvidenceReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_version: str = "1"
    provider_mode: str
    runtime_path: str
    business_boundary: str
    fault_mode: str
    authoritative_facts: tuple[Mapping[str, Any], ...] = Field(default_factory=tuple)
    eligible_claims: tuple[str, ...] = Field(default_factory=tuple)
    prohibited_claims: tuple[str, ...] = Field(default_factory=tuple)

    def model_dump_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("exclude_none", True)
        return super().model_dump_json(**kwargs)
