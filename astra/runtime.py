"""Thin Phase 2 runtime helpers for interaction resolution and new-turn resume."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .domain import RuntimeInvocation
from .storage import AstraStore

if TYPE_CHECKING:
    from .phase3.governance import (
        GovernanceApplicationResult,
        RuntimeGovernanceCore,
    )


class Phase2Runtime:
    """Creates a fresh execution turn after persisted interaction resolution.

    The runtime deliberately does not restore a Python stack. It reuses only
    the opaque Hermes session handle and injects the persisted resolution as
    structured feedback into a new RuntimeInvocation.
    """

    def __init__(
        self,
        store: AstraStore,
        governance_core: "RuntimeGovernanceCore | None" = None,
    ) -> None:
        self.store = store
        self.governance_core = governance_core

    def apply_governance_decision(
        self, decision_id: str
    ) -> "GovernanceApplicationResult":
        """Delegate an immutable decision to the composed governance component."""

        if self.governance_core is None:
            raise RuntimeError("Runtime Governance Core is not configured")
        return self.governance_core.apply_decision(decision_id)

    def resolve_and_resume(
        self,
        previous: RuntimeInvocation,
        *,
        interaction_id: str,
        resolution: Mapping[str, Any],
        new_execution_id: str,
    ) -> RuntimeInvocation:
        interaction = self.store.get_interaction(interaction_id)
        if interaction is None:
            raise KeyError(interaction_id)
        if interaction["task_id"] != previous.task_id:
            raise ValueError("Interaction does not belong to the task")
        if interaction["status"] != "pending":
            raise ValueError("Interaction is not pending")

        self.store.resolve_interaction(interaction_id, resolution)
        prior_execution = self.store.get_execution(previous.execution_id) or {}
        session_handle = (
            prior_execution.get("session_handle") or previous.session_handle
        )
        feedback = [
            *previous.feedback,
            {
                "type": "InteractionResolution",
                "interaction_id": interaction_id,
                "kind": interaction["kind"],
                "resolution": dict(resolution),
            },
        ]
        return previous.model_copy(
            update={
                "execution_id": new_execution_id,
                "session_handle": session_handle,
                "feedback": tuple(feedback),
            }
        )
