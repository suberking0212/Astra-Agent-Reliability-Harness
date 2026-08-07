"""Versioned Hermes × Astra execution profiles.

Profiles configure an existing Hermes execution boundary.  They do not define
another agent loop or alter Astra governance semantics.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .phase3.canonical import sha256_digest


class ExecutionProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    profile_version: str = "1"
    astra_governed: bool
    skip_context_files: bool
    skip_memory: bool
    enable_skills: bool
    enable_self_improvement: bool

    @property
    def ref(self) -> str:
        return f"{self.profile_id}@{self.profile_version}"

    @property
    def profile_hash(self) -> str:
        return sha256_digest(self.model_dump(mode="json"))

    def hermes_toolsets(self, business_tools: tuple[str, ...]) -> list[str]:
        toolsets = ["astra_runtime", *[f"astra_business_{name}" for name in business_tools]]
        if self.enable_skills:
            toolsets.append("skills")
        if not self.skip_memory:
            toolsets.append("memory")
        return toolsets

    def identity(self, hermes_home: str | Path | None) -> Mapping[str, Any]:
        return {
            **self.model_dump(mode="json"),
            "profile_ref": self.ref,
            "profile_hash": self.profile_hash,
            "hermes_home": str(Path(hermes_home).resolve()) if hermes_home else None,
        }


INSTRUMENTED_HERMES_BASELINE = ExecutionProfile(
    profile_id="native_full",
    astra_governed=False,
    skip_context_files=False,
    skip_memory=False,
    enable_skills=True,
    enable_self_improvement=True,
)

ASTRA_CONTROLLED = ExecutionProfile(
    profile_id="astra_controlled",
    astra_governed=True,
    skip_context_files=True,
    skip_memory=True,
    enable_skills=False,
    enable_self_improvement=False,
)

ASTRA_FULL_HERMES = ExecutionProfile(
    profile_id="astra_full_hermes",
    astra_governed=True,
    skip_context_files=False,
    skip_memory=False,
    enable_skills=True,
    enable_self_improvement=True,
)

_PROFILES = {
    profile.profile_id: profile
    for profile in (
        INSTRUMENTED_HERMES_BASELINE,
        ASTRA_CONTROLLED,
        ASTRA_FULL_HERMES,
    )
}


def get_execution_profile(profile_id: str) -> ExecutionProfile:
    try:
        return _PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(f"unknown execution profile: {profile_id}") from exc


def execution_profiles() -> tuple[ExecutionProfile, ...]:
    return tuple(_PROFILES.values())
