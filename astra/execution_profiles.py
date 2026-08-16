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


def materialize_hermes_learning_config(config: dict[str, Any], profile: "ExecutionProfile") -> None:
    """Apply profile-owned Hermes learning settings to a mutable config."""
    agent = config.setdefault("agent", {})
    if not isinstance(agent, dict):
        agent = {}
        config["agent"] = agent
    skills = agent.setdefault("skills", {})
    if not isinstance(skills, dict):
        skills = {}
        agent["skills"] = skills
    memory = agent.setdefault("memory", {})
    if not isinstance(memory, dict):
        memory = {}
        agent["memory"] = memory
    skills["creation_nudge_interval"] = 15 if profile.enable_self_improvement else 0
    memory["nudge_interval"] = 10 if profile.enable_self_improvement else 0


class ExecutionProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    profile_version: str = "1"
    astra_governed: bool
    skip_context_files: bool
    skip_memory: bool
    enable_skills: bool
    enable_self_improvement: bool
    # Hermes CLI toolsets are an allowlist boundary, not merely a hint to the
    # model.  Governed profiles must not inherit the broad coding toolset.
    enabled_toolsets: tuple[str, ...] = ("hermes-cli",)
    disabled_toolsets: tuple[str, ...] = ()
    launcher_guidance: str = ""

    @property
    def ref(self) -> str:
        return f"{self.profile_id}@{self.profile_version}"

    @property
    def profile_hash(self) -> str:
        return sha256_digest(self.model_dump(mode="json"))

    def hermes_toolsets(self, business_tools: tuple[str, ...]) -> list[str]:
        # Plugin toolsets are discovered dynamically by Hermes and must not be
        # placed in platform_toolsets (that causes Hermes' static validator to
        # reject them as unknown).  The profile allowlist therefore contains
        # only native Hermes toolsets; Astra tools are injected by the plugin.
        return list(self.enabled_toolsets)

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

ASTRA_CONTROLLED_DEBUG = ExecutionProfile(
    profile_id="astra_controlled_debug",
    astra_governed=True,
    skip_context_files=True,
    skip_memory=True,
    enable_skills=False,
    enable_self_improvement=False,
    enabled_toolsets=("hermes-cli",),
    disabled_toolsets=("file", "terminal", "skills", "memory", "code_execution"),
    launcher_guidance=(
        "You are running inside Astra Stage 1 governed execution. "
        "For business facts and actions, use the visible Astra tools and "
        "their Runtime results as authoritative. Do not inspect the Astra "
        "repository or use side channels to answer a business request. "
        "If an Astra tool is unavailable or fails, report its governed "
        "failure/next_action; never substitute source exploration."
    ),
)

ASTRA_FULL_HERMES = ExecutionProfile(
    profile_id="astra_full_hermes",
    astra_governed=True,
    skip_context_files=False,
    skip_memory=False,
    enable_skills=True,
    enable_self_improvement=True,
    launcher_guidance=(
        "You are running inside Astra governed execution with full native Hermes "
        "capabilities. Use Astra capabilities for authoritative business facts "
        "and actions; Runtime results, receipts, evidence, and completion are "
        "authoritative. Native file, terminal, code, browser, skills, and memory "
        "remain available for ordinary project work, but never use them to access "
        "authoritative business data or bypass the Capability Broker."
    ),
)

# Backwards-compatible Python symbol for callers from the Stage 1 harness.
ASTRA_CONTROLLED = ASTRA_CONTROLLED_DEBUG

_PROFILES = {
    profile.profile_id: profile
    for profile in (
        INSTRUMENTED_HERMES_BASELINE,
        ASTRA_CONTROLLED_DEBUG,
        ASTRA_FULL_HERMES,
    )
}

# ``astra_controlled`` was the pre-profile name.  Keep it as a read-only
# lookup alias so existing persisted task metadata and scripts remain valid,
# while new launches identify the profile explicitly as debug.
_PROFILE_ALIASES = {"astra_controlled": "astra_controlled_debug"}


def get_execution_profile(profile_id: str) -> ExecutionProfile:
    profile_id = _PROFILE_ALIASES.get(profile_id, profile_id)
    try:
        return _PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(f"unknown execution profile: {profile_id}") from exc


def execution_profiles() -> tuple[ExecutionProfile, ...]:
    return tuple(_PROFILES.values())
