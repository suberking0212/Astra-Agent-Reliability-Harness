"""Astra Task Runtime entry points and Phase 2 compatibility helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .domain import RuntimeInvocation
from .phase3.canonical import sha256_digest
from .phase3.governance import GovernanceStore
from .phase3.task_contract import FrozenContractModel, TaskContract
from .storage import AstraStore

if TYPE_CHECKING:
    from .phase3.governance import (
        GovernanceApplicationResult,
        RuntimeGovernanceCore,
    )


class CommandIdentityConflict(ValueError):
    """A command identity was reused with different semantic input."""

    code = "identity_conflict"


class TaskSubmissionResult(FrozenContractModel):
    command_id: str
    task_id: str
    task_state: str
    task_version: int
    attempt_id: str
    attempt_state: str
    attempt_version: int
    run_request_id: str
    run_request_state: str


class TaskRuntime:
    """Minimal durable Task Runtime introduced by Phase 4 Milestone 1."""

    def __init__(self, store: AstraStore) -> None:
        self.store = store
        self.governance_store = GovernanceStore.from_astra_store(store)

    def submit_task(
        self,
        *,
        command_id: str,
        task_id: str,
        contract: TaskContract,
        priority: int = 0,
        ready_at: str | None = None,
    ) -> TaskSubmissionResult:
        """Atomically persist a Task, initial Attempt, Run Request and result."""

        payload_hash = sha256_digest(
            {
                "command_type": "submit_task",
                "task_id": task_id,
                "contract": contract,
                "priority": priority,
                "ready_at": ready_at,
            }
        )
        with self.store.transaction() as connection:
            existing = connection.execute(
                """
                SELECT command_type, payload_hash, result_json
                FROM phase4_runtime_commands
                WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["command_type"] != "submit_task"
                    or existing["payload_hash"] != payload_hash
                ):
                    raise CommandIdentityConflict(
                        f"identity_conflict: command_id {command_id!r}"
                    )
                return TaskSubmissionResult.model_validate_json(
                    existing["result_json"]
                )

            if connection.execute(
                "SELECT 1 FROM phase3_tasks WHERE task_id = ?", (task_id,)
            ).fetchone():
                raise CommandIdentityConflict(
                    f"identity_conflict: task_id {task_id!r}"
                )

            now = datetime.now(timezone.utc).isoformat()
            actual_ready_at = ready_at or now
            attempt_id = "attempt:" + str(uuid4())
            run_request_id = "run-request:" + str(uuid4())
            result = TaskSubmissionResult(
                command_id=command_id,
                task_id=task_id,
                task_state="pending",
                task_version=1,
                attempt_id=attempt_id,
                attempt_state="active",
                attempt_version=1,
                run_request_id=run_request_id,
                run_request_state="pending",
            )

            connection.execute(
                """
                INSERT INTO phase3_tasks(
                    task_id, contract_json, contract_id, contract_version,
                    contract_hash, state, version, current_attempt_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?)
                """,
                (
                    task_id,
                    contract.model_dump_json(),
                    contract.contract_id,
                    contract.contract_version,
                    contract.contract_hash,
                    attempt_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO phase3_attempts(
                    attempt_id, task_id, ordinal, state, version, created_at
                ) VALUES (?, ?, 1, 'active', 1, ?)
                """,
                (attempt_id, task_id, now),
            )
            connection.execute(
                """
                INSERT INTO phase4_run_requests(
                    run_request_id, task_id, attempt_id, reason, state,
                    priority, ready_at, created_by_command_id, created_at
                ) VALUES (?, ?, ?, 'task_submitted', 'pending', ?, ?, ?, ?)
                """,
                (
                    run_request_id,
                    task_id,
                    attempt_id,
                    priority,
                    actual_ready_at,
                    command_id,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO phase4_runtime_commands(
                    command_id, command_type, payload_hash, result_json,
                    created_at
                ) VALUES (?, 'submit_task', ?, ?, ?)
                """,
                (command_id, payload_hash, result.model_dump_json(), now),
            )
            return result


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
