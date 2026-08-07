"""Astra Task Runtime entry points and Phase 2 compatibility helpers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from .domain import (
    AgentExecutor,
    ExecutionEvent,
    ExecutionResult,
    InteractionKind,
    InteractionPurpose,
    InteractionRequest,
    RuntimeInvocation,
)
from .interaction_input import validate_interaction_response
from .dynamic_authority import effective_contract
from .phase3.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalRequirementRef,
    ApprovalResolution,
)
from .phase3.canonical import sha256_digest
from .phase3.governance import (
    AttemptState,
    GovernanceApplicationResult,
    GovernanceEvaluationResult,
    GovernanceStore,
    RuntimeGovernanceCore,
)
from .phase3.completion import CompletionContract, CompletionValidationResult
from .phase3.effects import (
    CanonicalEffectRequest,
    ExternalOperation,
    ExternalOperationStatus,
)
from .phase3.facts import FactSource, ReliabilityFact
from .phase3.policy import (
    DecisionApplication,
    DecisionApplicationSnapshot,
    PolicyAction,
    PolicyDecision,
    TaskState,
    apply_policy_decision,
)
from .observations import ConfirmationStatus, ExternalOperationConfirmation
from .phase3.task_contract import (
    FrozenContractModel,
    SubjectRef,
    TaskContract,
    TaskContractRef,
)
from .storage import AstraStore
from .tool_catalog import CatalogIntegrityError, preflight_contract

class CommandIdentityConflict(ValueError):
    """A command identity was reused with different semantic input."""

    code = "identity_conflict"


class ExecutionEligibilityError(RuntimeError):
    """The next ready Run Request cannot start an Execution."""

    def __init__(self, code: str, run_request_id: str) -> None:
        self.code = code
        self.run_request_id = run_request_id
        super().__init__(f"{code}: run_request_id {run_request_id!r}")


class RunRequestExecutionConflict(RuntimeError):
    """A Run Request already owns its single allowed Execution."""

    code = "run_request_execution_conflict"


class ExecutionResultConflict(ValueError):
    """An Execution already has a different persisted result."""

    code = "execution_result_conflict"


class LeaseFencedError(PermissionError):
    """A Worker no longer owns the current Run Request fencing token."""

    code = "stale_lease_token"

    def __init__(self, run_request_id: str, reason: str) -> None:
        self.run_request_id = run_request_id
        self.reason = reason
        super().__init__(
            f"stale_lease_token: run_request_id {run_request_id!r}: {reason}"
        )


class TaskCancellationConflict(RuntimeError):
    """A cancel command lost the Task CAS or targeted a completed Task."""

    def __init__(self, code: str, task_id: str) -> None:
        self.code = code
        self.task_id = task_id
        super().__init__(f"{code}: task_id {task_id!r}")


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


class ExecutionClaimResult(FrozenContractModel):
    run_request_id: str
    run_request_state: str
    execution_id: str
    execution_status: str
    task_id: str
    attempt_id: str
    started_at: str
    lease_owner_id: str
    lease_token: str
    lease_expires_at: str
    ownership_version: int


class SingleWorkerRunResult(FrozenContractModel):
    claim: ExecutionClaimResult
    invocation: RuntimeInvocation
    execution_result: ExecutionResult
    governance_evaluation: GovernanceEvaluationResult | None = None
    governance_application: GovernanceApplicationResult | None = None
    execution_finalized: bool
    run_request_state: str


class InteractionResolutionResult(FrozenContractModel):
    command_id: str
    interaction_id: str
    interaction_state: str
    interaction_version: int
    task_id: str
    task_state: str
    task_version: int
    attempt_id: str
    attempt_state: str
    attempt_version: int
    run_request_id: str | None = None
    run_request_state: str | None = None


class TaskCancellationResult(FrozenContractModel):
    command_id: str
    task_id: str
    task_state: str
    task_version: int
    attempt_id: str
    attempt_state: str
    attempt_version: int
    cancelled_run_request_ids: tuple[str, ...] = ()
    cancelled_interaction_ids: tuple[str, ...] = ()
    active_execution_ids: tuple[str, ...] = ()


class RecoveryClassification(str, Enum):
    SAFE_CONTINUE = "safe_continue"
    RECONCILE = "reconcile"
    GOVERNANCE_RESUME = "governance_resume"
    TERMINAL = "terminal"


class CheckpointRecord(FrozenContractModel):
    checkpoint_id: str
    boundary: str
    task_id: str
    attempt_id: str | None = None
    execution_id: str | None = None
    run_request_id: str | None = None
    task_version: int
    attempt_version: int | None = None
    authority_hash: str
    envelope: Mapping[str, Any]
    created_at: str


class ExecutionRecoveryResult(FrozenContractModel):
    command_id: str
    execution_id: str
    execution_status: str
    termination_reason: str
    classification: RecoveryClassification
    task_id: str
    task_state: str
    task_version: int
    attempt_id: str
    attempt_state: str
    attempt_version: int
    run_request_id: str | None = None
    reconciliation_id: str | None = None
    checkpoint_id: str | None = None


class ReconciliationRunResult(FrozenContractModel):
    reconciliation_id: str
    task_id: str
    attempt_id: str
    status: str
    operation_outcomes: Mapping[str, str]
    run_request_id: str | None = None
    policy_decision_id: str | None = None
    checkpoint_id: str | None = None


class StartupRecoveryResult(FrozenContractModel):
    recovered_executions: tuple[ExecutionRecoveryResult, ...] = ()
    recovered_interaction_ids: tuple[str, ...] = ()
    applied_policy_decision_ids: tuple[str, ...] = ()
    reconciliation_results: tuple[ReconciliationRunResult, ...] = ()
    recovered_running_task_ids: tuple[str, ...] = ()
    pending_outbox_ids: tuple[str, ...] = ()
    unresolved_operation_ids: tuple[str, ...] = ()


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class TaskRuntime:
    """Minimal durable Task Runtime introduced by Phase 4 Milestone 1."""

    def __init__(
        self,
        store: AstraStore,
        *,
        governance_core: RuntimeGovernanceCore | None = None,
    ) -> None:
        self.store = store
        if governance_core is None:
            self.governance_store = GovernanceStore.from_astra_store(store)
            self.governance_core = RuntimeGovernanceCore(self.governance_store)
        else:
            if governance_core.store.authority_store is not store:
                raise ValueError(
                    "RuntimeGovernanceCore must share the authoritative AstraStore"
                )
            self.governance_store = governance_core.store
            self.governance_core = governance_core

    @staticmethod
    def _lease_expiry(now: datetime, lease_duration_seconds: float) -> str:
        if lease_duration_seconds <= 0:
            raise ValueError("lease_duration_seconds must be positive")
        return (now + timedelta(seconds=lease_duration_seconds)).isoformat()

    @staticmethod
    def _assert_execution_lease_row(
        connection: sqlite3.Connection,
        *,
        execution_id: str,
        lease_owner_id: str,
        lease_token: str,
        now: datetime | None = None,
        allow_completed: bool = False,
    ) -> sqlite3.Row:
        checked_at = now or datetime.now(timezone.utc)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        else:
            checked_at = checked_at.astimezone(timezone.utc)
        row = connection.execute(
            """
            SELECT execution.run_request_id, execution.status,
                   execution.ended_at, request.state AS run_request_state,
                   request.lease_owner_id, request.lease_token,
                   request.lease_expires_at, request.ownership_version
            FROM executions AS execution
            JOIN phase4_run_requests AS request
              ON request.run_request_id = execution.run_request_id
            WHERE execution.execution_id = ?
            """,
            (execution_id,),
        ).fetchone()
        if row is None or row["run_request_id"] is None:
            raise LeaseFencedError("unknown", "execution_has_no_run_request_lease")
        run_request_id = str(row["run_request_id"])
        if (
            row["lease_owner_id"] != lease_owner_id
            or row["lease_token"] != lease_token
        ):
            raise LeaseFencedError(run_request_id, "ownership_changed")
        if row["lease_expires_at"] is None or _instant(
            str(row["lease_expires_at"])
        ) <= checked_at:
            raise LeaseFencedError(run_request_id, "lease_expired")
        allowed_states = {"claimed"}
        if allow_completed:
            allowed_states.add("completed")
        if row["run_request_state"] not in allowed_states:
            raise LeaseFencedError(
                run_request_id,
                "run_request_not_owned:" + str(row["run_request_state"]),
            )
        return row

    def assert_execution_lease(
        self,
        *,
        execution_id: str,
        lease_owner_id: str,
        lease_token: str,
        allow_completed: bool = False,
    ) -> None:
        """Reject a stale Worker before it enters a protected boundary."""

        with self.store.transaction() as connection:
            self._assert_execution_lease_row(
                connection,
                execution_id=execution_id,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                allow_completed=allow_completed,
            )

    def _acquire_recovery_execution_lease(
        self,
        *,
        execution_id: str,
        lease_owner_id: str,
        lease_duration_seconds: float,
        now: datetime | None = None,
    ) -> str | None:
        """CAS-acquire expired work before recovery enters apply/finalize."""

        acquired_at = now or datetime.now(timezone.utc)
        if acquired_at.tzinfo is None:
            acquired_at = acquired_at.replace(tzinfo=timezone.utc)
        else:
            acquired_at = acquired_at.astimezone(timezone.utc)
        with self.store.transaction() as connection:
            owned = connection.execute(
                """
                SELECT request.lease_token
                FROM executions AS execution
                JOIN phase4_run_requests AS request
                  ON request.run_request_id = execution.run_request_id
                WHERE execution.execution_id = ?
                  AND request.state IN ('claimed', 'completed')
                  AND request.lease_owner_id = ?
                  AND request.lease_token IS NOT NULL
                  AND request.lease_expires_at > ?
                """,
                (
                    execution_id,
                    lease_owner_id,
                    acquired_at.isoformat(),
                ),
            ).fetchone()
            if owned is not None:
                return str(owned["lease_token"])
            row = connection.execute(
                """
                SELECT execution.run_request_id, request.state,
                       request.lease_token
                FROM executions AS execution
                JOIN phase4_run_requests AS request
                  ON request.run_request_id = execution.run_request_id
                WHERE execution.execution_id = ?
                  AND request.state IN ('claimed', 'completed')
                  AND (
                    request.state = 'completed'
                    OR request.lease_token IS NULL
                    OR request.lease_expires_at IS NULL
                    OR request.lease_expires_at <= ?
                  )
                """,
                (execution_id, acquired_at.isoformat()),
            ).fetchone()
            if row is None:
                return None
            lease_token = "lease:" + str(uuid4())
            expires_at = self._lease_expiry(
                acquired_at, lease_duration_seconds
            )
            if row["lease_token"] is None:
                token_predicate = "lease_token IS NULL"
                parameters: tuple[Any, ...] = (
                    lease_owner_id,
                    lease_token,
                    expires_at,
                    acquired_at.isoformat(),
                    row["run_request_id"],
                    acquired_at.isoformat(),
                )
            else:
                token_predicate = "lease_token = ?"
                parameters = (
                    lease_owner_id,
                    lease_token,
                    expires_at,
                    acquired_at.isoformat(),
                    row["run_request_id"],
                    acquired_at.isoformat(),
                    row["lease_token"],
                )
            cursor = connection.execute(
                f"""
                UPDATE phase4_run_requests
                SET lease_owner_id = ?, lease_token = ?,
                    lease_expires_at = ?, heartbeat_at = ?,
                    ownership_version = ownership_version + 1
                WHERE run_request_id = ?
                  AND state IN ('claimed', 'completed')
                  AND (
                    state = 'completed'
                    OR lease_expires_at IS NULL
                    OR lease_expires_at <= ?
                  )
                  AND {token_predicate}
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                return None
            return lease_token

    def heartbeat_lease(
        self,
        *,
        execution_id: str,
        lease_owner_id: str,
        lease_token: str,
        lease_duration_seconds: float,
        now: datetime | None = None,
    ) -> str | None:
        """Renew an active lease or stop cleanly after owned completion."""

        heartbeat_at = now or datetime.now(timezone.utc)
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        else:
            heartbeat_at = heartbeat_at.astimezone(timezone.utc)
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT execution.run_request_id, request.state,
                       request.lease_owner_id, request.lease_token,
                       request.lease_expires_at
                FROM executions AS execution
                JOIN phase4_run_requests AS request
                  ON request.run_request_id = execution.run_request_id
                WHERE execution.execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise LeaseFencedError("unknown", "execution_not_found")
            run_request_id = str(row["run_request_id"])
            if (
                row["lease_owner_id"] != lease_owner_id
                or row["lease_token"] != lease_token
            ):
                raise LeaseFencedError(run_request_id, "ownership_changed")
            if row["state"] == "completed":
                return None
            if row["state"] != "claimed":
                raise LeaseFencedError(
                    run_request_id,
                    "run_request_not_claimed:" + str(row["state"]),
                )
            if row["lease_expires_at"] is None or _instant(
                str(row["lease_expires_at"])
            ) <= heartbeat_at:
                raise LeaseFencedError(run_request_id, "lease_expired")
            expires_at = self._lease_expiry(
                heartbeat_at, lease_duration_seconds
            )
            cursor = connection.execute(
                """
                UPDATE phase4_run_requests
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE run_request_id = ? AND state = 'claimed'
                  AND lease_owner_id = ? AND lease_token = ?
                  AND lease_expires_at > ?
                """,
                (
                    heartbeat_at.isoformat(),
                    expires_at,
                    run_request_id,
                    lease_owner_id,
                    lease_token,
                    heartbeat_at.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseFencedError(run_request_id, "heartbeat_cas_failed")
            return expires_at

    @staticmethod
    def _write_checkpoint(
        connection: sqlite3.Connection,
        *,
        boundary: str,
        task_id: str,
        attempt_id: str | None = None,
        execution_id: str | None = None,
        run_request_id: str | None = None,
        references: Mapping[str, Any] | None = None,
    ) -> CheckpointRecord:
        """Append one minimal recovery envelope at a safe lifecycle boundary."""

        task = connection.execute(
            "SELECT * FROM phase3_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise RuntimeError("Checkpoint references a missing Task")
        actual_attempt_id = attempt_id or task["current_attempt_id"]
        attempt = (
            connection.execute(
                "SELECT * FROM phase3_attempts WHERE attempt_id = ?",
                (actual_attempt_id,),
            ).fetchone()
            if actual_attempt_id is not None
            else None
        )
        execution = (
            connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if execution_id is not None
            else None
        )
        request = (
            connection.execute(
                "SELECT * FROM phase4_run_requests WHERE run_request_id = ?",
                (run_request_id,),
            ).fetchone()
            if run_request_id is not None
            else None
        )
        envelope: dict[str, Any] = {
            "boundary": boundary,
            "task": {
                "task_id": task_id,
                "version": int(task["version"]),
                "state": str(task["state"]),
                "contract_id": str(task["contract_id"]),
                "contract_version": str(task["contract_version"]),
                "contract_hash": str(task["contract_hash"]),
                "current_attempt_id": str(task["current_attempt_id"]),
            },
            "attempt": (
                {
                    "attempt_id": str(attempt["attempt_id"]),
                    "version": int(attempt["version"]),
                    "state": str(attempt["state"]),
                }
                if attempt is not None
                else None
            ),
            "execution": (
                {
                    "execution_id": str(execution["execution_id"]),
                    "status": str(execution["status"]),
                    "termination_reason": execution["termination_reason"],
                    "session_handle": execution["session_handle"],
                }
                if execution is not None
                else None
            ),
            "run_request": (
                {
                    "run_request_id": str(request["run_request_id"]),
                    "state": str(request["state"]),
                    "reason": str(request["reason"]),
                    "lease_owner_id": request["lease_owner_id"],
                    "lease_token": request["lease_token"],
                    "lease_expires_at": request["lease_expires_at"],
                    "heartbeat_at": request["heartbeat_at"],
                    "ownership_version": int(request["ownership_version"]),
                }
                if request is not None
                else None
            ),
            "references": dict(references or {}),
        }
        authority_hash = sha256_digest(envelope)
        checkpoint_id = "checkpoint:" + authority_hash
        created_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO phase4_checkpoints(
                checkpoint_id, task_id, attempt_id, execution_id,
                run_request_id, boundary, task_version, attempt_version,
                authority_hash, envelope_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, boundary, authority_hash) DO NOTHING
            """,
            (
                checkpoint_id,
                task_id,
                str(attempt["attempt_id"]) if attempt is not None else None,
                execution_id,
                run_request_id,
                boundary,
                int(task["version"]),
                int(attempt["version"]) if attempt is not None else None,
                authority_hash,
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                created_at,
            ),
        )
        persisted = connection.execute(
            """
            SELECT * FROM phase4_checkpoints
            WHERE task_id = ? AND boundary = ? AND authority_hash = ?
            """,
            (task_id, boundary, authority_hash),
        ).fetchone()
        if persisted is None:
            raise RuntimeError("Checkpoint persistence failed")
        return CheckpointRecord(
            checkpoint_id=str(persisted["checkpoint_id"]),
            boundary=str(persisted["boundary"]),
            task_id=str(persisted["task_id"]),
            attempt_id=persisted["attempt_id"],
            execution_id=persisted["execution_id"],
            run_request_id=persisted["run_request_id"],
            task_version=int(persisted["task_version"]),
            attempt_version=(
                int(persisted["attempt_version"])
                if persisted["attempt_version"] is not None
                else None
            ),
            authority_hash=str(persisted["authority_hash"]),
            envelope=json.loads(str(persisted["envelope_json"])),
            created_at=str(persisted["created_at"]),
        )

    def submit_task(
        self,
        *,
        command_id: str,
        task_id: str,
        contract: TaskContract,
        completion_contract: CompletionContract | None = None,
        priority: int = 0,
        ready_at: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        message_id: str | None = None,
        message_hash: str | None = None,
    ) -> TaskSubmissionResult:
        """Atomically persist a Task, initial Attempt, Run Request and result."""

        ingress_values = (conversation_id, turn_id, message_id, message_hash)
        if any(value is not None for value in ingress_values) and not all(
            isinstance(value, str) and value for value in ingress_values
        ):
            raise ValueError("Ingress identity fields must be supplied together")
        contract = preflight_contract(contract)

        payload: dict[str, Any] = {
            "command_type": "submit_task",
            "task_id": task_id,
            "contract": contract,
            "priority": priority,
            "ready_at": ready_at,
        }
        if message_id is not None:
            payload["ingress_identity"] = {
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "message_id": message_id,
                "message_hash": message_hash,
            }
        if completion_contract is not None:
            payload["completion_contract"] = completion_contract
        payload_hash = sha256_digest(payload)
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

            if message_id is not None:
                existing_message = connection.execute(
                    """
                    SELECT ingress.*, command.result_json
                    FROM phase4_ingress_messages AS ingress
                    JOIN phase4_runtime_commands AS command
                      ON command.command_id = ingress.command_id
                    WHERE ingress.message_id = ?
                    """,
                    (message_id,),
                ).fetchone()
                if existing_message is not None:
                    if (
                        existing_message["conversation_id"] != conversation_id
                        or existing_message["turn_id"] != turn_id
                        or existing_message["message_hash"] != message_hash
                        or existing_message["task_id"] != task_id
                    ):
                        raise CommandIdentityConflict(
                            f"message_identity_conflict: message_id {message_id!r}"
                        )
                    return TaskSubmissionResult.model_validate_json(
                        existing_message["result_json"]
                    )
                occupied_turn = connection.execute(
                    """
                    SELECT message_id, message_hash
                    FROM phase4_ingress_messages
                    WHERE conversation_id = ? AND turn_id = ?
                    """,
                    (conversation_id, turn_id),
                ).fetchone()
                if occupied_turn is not None:
                    raise CommandIdentityConflict(
                        "message_identity_conflict: conversation turn reused"
                    )

            if connection.execute(
                "SELECT 1 FROM phase3_tasks WHERE task_id = ?", (task_id,)
            ).fetchone():
                raise CommandIdentityConflict(
                    f"identity_conflict: task_id {task_id!r}"
                )

            if completion_contract is not None:
                if (
                    completion_contract.contract_id
                    != contract.completion_contract_ref.contract_id
                    or completion_contract.contract_version
                    != contract.completion_contract_ref.contract_version
                    or completion_contract.task_type != contract.task_type
                ):
                    raise ValueError(
                        "CompletionContract does not match TaskContract reference"
                    )
                self.governance_core.register_completion_contract(
                    completion_contract
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
            if message_id is not None:
                connection.execute(
                    """
                    INSERT INTO phase4_ingress_messages(
                        message_id, conversation_id, turn_id, message_hash,
                        task_id, command_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        conversation_id,
                        turn_id,
                        message_hash,
                        task_id,
                        command_id,
                        now,
                    ),
                )
            self._write_checkpoint(
                connection,
                boundary="task_attempt_run_request_created",
                task_id=task_id,
                attempt_id=attempt_id,
                run_request_id=run_request_id,
                references={"command_id": command_id},
            )
            return result

    def _terminate_contract_runtime_incompatible(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        attempt_id: str,
        now: datetime,
    ) -> None:
        """Fail closed while preserving the immutable historical Contract JSON."""

        now_text = now.isoformat()
        connection.execute(
            """
            UPDATE phase3_tasks
            SET state = 'failed', version = version + 1,
                termination_reason = 'contract_runtime_incompatible',
                updated_at = ?
            WHERE task_id = ?
              AND state NOT IN ('succeeded', 'failed', 'cancelled')
            """,
            (now_text, task_id),
        )
        connection.execute(
            """
            UPDATE phase3_attempts
            SET state = 'failed', version = version + 1, ended_at = ?,
                termination_reason = 'contract_runtime_incompatible'
            WHERE attempt_id = ?
              AND state NOT IN (
                'completed', 'failed', 'exhausted', 'superseded', 'cancelled'
              )
            """,
            (now_text, attempt_id),
        )
        connection.execute(
            """
            UPDATE phase4_run_requests
            SET state = 'cancelled',
                termination_reason = 'contract_runtime_incompatible',
                terminated_at = COALESCE(terminated_at, ?)
            WHERE task_id = ? AND state IN ('pending', 'claimed')
            """,
            (now_text, task_id),
        )
        connection.execute(
            """
            UPDATE interactions
            SET status = 'cancelled', version = version + 1,
                resolved_at = COALESCE(resolved_at, ?)
            WHERE task_id = ? AND status = 'pending'
            """,
            (now_text, task_id),
        )

    def request_interaction(
        self,
        *,
        execution_id: str,
        lease_owner_id: str,
        lease_token: str,
        kind: InteractionKind,
        prompt: str,
        payload: Mapping[str, Any] | None = None,
    ) -> InteractionRequest:
        """Persist an Adapter-requested wait through the Runtime authority."""

        requested_payload = dict(payload or {})
        actual_kind = kind
        if kind == InteractionKind.APPROVAL:
            actual_kind = InteractionKind.USER_INPUT
            requested_payload = {
                **requested_payload,
                "requested_interaction_kind": InteractionKind.APPROVAL.value,
                "reason_code": "exact_effect_request_required",
                "required_information": ["canonical_effect_request"],
                "purpose": InteractionPurpose.CLARIFICATION.value,
                "missing_fields": ["canonical_effect_request"],
                "resume_condition": "Provide the exact canonical effect request.",
            }
        elif not str(prompt).strip():
            raise ValueError("clarification InteractionRequest requires a prompt")
        else:
            # Existing waits remain clarification waits; this is explicit data,
            # not a second conversation state machine.
            requested_payload = {
                **requested_payload,
                "purpose": InteractionPurpose.CLARIFICATION.value,
                "missing_fields": list(requested_payload.get("missing_fields") or ["user_response"]),
                "resume_condition": str(requested_payload.get("resume_condition") or "Provide the requested information."),
            }
        return self._request_interaction(
            execution_id=execution_id,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            kind=actual_kind,
            purpose=InteractionPurpose.CLARIFICATION,
            prompt=prompt,
            payload=requested_payload,
            exact_effect=None,
        )

    def request_approval(
        self,
        *,
        execution_id: str,
        lease_owner_id: str,
        lease_token: str,
        effect: CanonicalEffectRequest,
        permission_scope: str = "execute_effect",
    ) -> InteractionRequest:
        """Persist an exact-effect ApprovalRequest and approval Interaction."""

        return self._request_interaction(
            execution_id=execution_id,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
            kind=InteractionKind.APPROVAL,
            purpose=InteractionPurpose.APPROVAL,
            prompt="Approval is required for the exact canonical effect request.",
            payload={},
            exact_effect=effect,
            permission_scope=permission_scope,
        )

    def _request_interaction(
        self,
        *,
        execution_id: str,
        lease_owner_id: str,
        lease_token: str,
        kind: InteractionKind,
        purpose: InteractionPurpose,
        prompt: str,
        payload: Mapping[str, Any],
        exact_effect: CanonicalEffectRequest | None,
        permission_scope: str = "execute_effect",
    ) -> InteractionRequest:
        interaction_id = "interaction:execution:" + execution_id
        now = datetime.now(timezone.utc)
        with self.store.transaction() as connection:
            self._assert_execution_lease_row(
                connection,
                execution_id=execution_id,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
            )
            existing = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if existing is not None:
                existing_payload = json.loads(existing["payload_json"])
                exact_replay_matches = exact_effect is None
                if exact_effect is not None:
                    approval_row = connection.execute(
                        """
                        SELECT effect_identity, effect_request_hash,
                               permission_scope
                        FROM phase3_approval_requests
                        WHERE interaction_id = ?
                        """,
                        (interaction_id,),
                    ).fetchone()
                    exact_replay_matches = bool(
                        approval_row is not None
                        and approval_row["effect_identity"]
                        == exact_effect.effect_identity
                        and approval_row["effect_request_hash"]
                        == exact_effect.effect_request_hash
                        and approval_row["permission_scope"] == permission_scope
                    )
                if (
                    existing["execution_id"] != execution_id
                    or existing["kind"] != kind.value
                    or existing["purpose"] != purpose.value
                    or existing["prompt"] != prompt
                    or not exact_replay_matches
                    or (exact_effect is None and existing_payload != dict(payload))
                ):
                    raise CommandIdentityConflict(
                        f"identity_conflict: interaction_id {interaction_id!r}"
                    )
                return InteractionRequest(
                    interaction_id=interaction_id,
                    execution_id=execution_id,
                    task_id=str(existing["task_id"]),
                    attempt_id=str(existing["attempt_id"]),
                    kind=kind,
                    purpose=InteractionPurpose(str(existing["purpose"])),
                    prompt=prompt,
                    status=str(existing["status"]),
                    payload=existing_payload,
                )

            row = connection.execute(
                """
                SELECT execution.task_id, execution.attempt_id,
                       execution.run_request_id, execution.status,
                       execution.ended_at, request.state AS request_state,
                       task.contract_json, task.state AS task_state,
                       task.version AS task_version,
                       task.current_attempt_id,
                       attempt.state AS attempt_state,
                       attempt.version AS attempt_version
                FROM executions AS execution
                JOIN phase4_run_requests AS request
                  ON request.run_request_id = execution.run_request_id
                JOIN phase3_tasks AS task ON task.task_id = execution.task_id
                JOIN phase3_attempts AS attempt
                  ON attempt.attempt_id = execution.attempt_id
                WHERE execution.execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            if (
                row["ended_at"] is not None
                or row["status"] != "running"
                or row["request_state"] != "claimed"
                or row["task_state"] != "running"
                or row["attempt_state"] != "active"
                or row["current_attempt_id"] != row["attempt_id"]
            ):
                raise RuntimeError("Interaction request is not lifecycle-eligible")

            contract = effective_contract(
                self.store,
                str(row["task_id"]),
                preflight_contract(row["contract_json"]),
            )
            approval_request: ApprovalRequest | None = None
            persisted_payload = dict(payload)
            if kind == InteractionKind.APPROVAL:
                if exact_effect is None or exact_effect.task_contract_ref != contract.ref:
                    raise PermissionError("approval_stale_contract")
                intent = contract.effect_intent(exact_effect.effect_intent_ref)
                if (
                    exact_effect.authority_domain != intent.authority_domain
                    or exact_effect.effect_type != intent.effect_type
                    or exact_effect.effect_type_version != intent.effect_type_version
                    or exact_effect.subject_ref != intent.subject_ref
                ):
                    raise PermissionError("approval_effect_mismatch")
                requirement_ref = intent.approval_requirement_ref
                if requirement_ref is None:
                    raise PermissionError("approval_not_required")
                requirement = contract.approval_requirement(requirement_ref)
                approval_request = ApprovalRequest(
                    approval_request_id="approval-request:execution:" + execution_id,
                    interaction_id=interaction_id,
                    task_id=str(row["task_id"]),
                    attempt_id=str(row["attempt_id"]),
                    execution_id=execution_id,
                    task_contract_ref=contract.ref,
                    approval_requirement_ref=ApprovalRequirementRef.parse(
                        requirement.ref
                    ),
                    effect_identity=exact_effect.effect_identity,
                    effect_request_hash=exact_effect.effect_request_hash,
                    effect_summary={
                        "subject_ref": exact_effect.subject_ref.model_dump(
                            mode="json", exclude_none=True
                        ),
                        "normalized_parameters": dict(
                            exact_effect.normalized_parameters
                        ),
                        "authority_domain": exact_effect.authority_domain,
                        "effect_type": exact_effect.effect_type,
                        "effect_type_version": exact_effect.effect_type_version,
                    },
                    permission_scope=permission_scope,
                    risk_class=requirement.risk_class,
                    approver_policy_ref=requirement.approver_policy_ref,
                    requested_at=now,
                )
                persisted_payload = {
                    "purpose": InteractionPurpose.APPROVAL.value,
                    "approval_request_id": approval_request.approval_request_id,
                    "effect_identity": approval_request.effect_identity,
                    "effect_request_hash": approval_request.effect_request_hash,
                    "permission_scope": approval_request.permission_scope,
                    "effect_summary": dict(approval_request.effect_summary),
                }

            interaction = InteractionRequest(
                interaction_id=interaction_id,
                execution_id=execution_id,
                task_id=str(row["task_id"]),
                attempt_id=str(row["attempt_id"]),
                kind=kind,
                purpose=purpose,
                prompt=prompt,
                payload=persisted_payload,
            )
            connection.execute(
                """
                INSERT INTO interactions(
                    interaction_id, execution_id, task_id, attempt_id, kind,
                    purpose, prompt, status, version, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)
                """,
                (
                    interaction.interaction_id,
                    execution_id,
                    interaction.task_id,
                    interaction.attempt_id,
                    interaction.kind.value,
                    interaction.purpose.value,
                    interaction.prompt,
                    json.dumps(
                        persisted_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                    now.isoformat(),
                ),
            )
            if approval_request is not None:
                connection.execute(
                    """
                    INSERT INTO phase3_approval_requests(
                        approval_request_id, interaction_id, task_id, attempt_id,
                        execution_id, effect_identity, effect_request_hash,
                        permission_scope, risk_class, approver_policy_ref,
                        request_json, status, version, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)
                    """,
                    (
                        approval_request.approval_request_id,
                        approval_request.interaction_id,
                        approval_request.task_id,
                        approval_request.attempt_id,
                        approval_request.execution_id,
                        approval_request.effect_identity,
                        approval_request.effect_request_hash,
                        approval_request.permission_scope,
                        approval_request.risk_class,
                        approval_request.approver_policy_ref,
                        approval_request.model_dump_json(),
                        approval_request.requested_at.isoformat(),
                        (
                            approval_request.expires_at.isoformat()
                            if approval_request.expires_at
                            else None
                        ),
                    ),
                )

            terminal_status = (
                "waiting_approval"
                if kind == InteractionKind.APPROVAL
                else "waiting_input"
            )
            execution_cursor = connection.execute(
                """
                UPDATE executions
                SET status = ?, suspension_requested = 1, ended_at = ?,
                    termination_reason = ?
                WHERE execution_id = ? AND ended_at IS NULL AND status = 'running'
                """,
                (
                    terminal_status,
                    now.isoformat(),
                    "interaction_requested:" + kind.value,
                    execution_id,
                ),
            )
            if execution_cursor.rowcount != 1:
                raise RuntimeError("Execution waiting transition CAS failed")
            connection.execute(
                """
                INSERT INTO execution_events(
                    event_id, execution_id, event_type, payload_json, created_at
                ) VALUES (?, ?, 'AstraExecutionEnded', ?, ?)
                """,
                (
                    "event:" + interaction_id,
                    execution_id,
                    json.dumps(
                        {
                            "interaction_id": interaction_id,
                            "status": terminal_status,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now.isoformat(),
                ),
            )
            request_cursor = connection.execute(
                """
                UPDATE phase4_run_requests SET state = 'completed'
                WHERE run_request_id = ? AND state = 'claimed'
                """,
                (row["run_request_id"],),
            )
            if request_cursor.rowcount != 1:
                raise RuntimeError("Run Request waiting transition CAS failed")
            task_cursor = connection.execute(
                """
                UPDATE phase3_tasks
                SET state = ?, version = version + 1, updated_at = ?
                WHERE task_id = ? AND version = ? AND state = 'running'
                """,
                (
                    terminal_status,
                    now.isoformat(),
                    row["task_id"],
                    row["task_version"],
                ),
            )
            attempt_cursor = connection.execute(
                """
                UPDATE phase3_attempts
                SET state = 'waiting', version = version + 1
                WHERE attempt_id = ? AND version = ? AND state = 'active'
                """,
                (row["attempt_id"], row["attempt_version"]),
            )
            if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                raise RuntimeError("Task/Attempt waiting transition CAS failed")
            self._publish_runtime_fact(
                connection,
                fact_id="fact:" + interaction_id,
                fact_type="interaction_requested",
                task_id=interaction.task_id,
                attempt_id=interaction.attempt_id,
                execution_id=execution_id,
                source_event_id=interaction_id,
                subject_type="interaction",
                subject_id=interaction_id,
                subject_version=1,
                evidence_refs=(interaction_id,),
                attributes={
                    "kind": kind.value,
                    "task_state": terminal_status,
                },
            )
            self._write_checkpoint(
                connection,
                boundary="task_waiting",
                task_id=interaction.task_id,
                attempt_id=interaction.attempt_id,
                execution_id=execution_id,
                run_request_id=str(row["run_request_id"]),
                references={"interaction_id": interaction_id},
            )
            return interaction

    def resolve_interaction(
        self,
        *,
        command_id: str,
        interaction_id: str,
        expected_version: int,
        resolution: Mapping[str, Any],
        priority: int = 0,
        ready_at: str | None = None,
    ) -> InteractionResolutionResult:
        """Resolve one Interaction and atomically authorize the next work."""

        payload_hash = sha256_digest(
            {
                "command_type": "resolve_interaction",
                "interaction_id": interaction_id,
                "expected_version": expected_version,
                "resolution": dict(resolution),
                "priority": priority,
                "ready_at": ready_at,
            }
        )
        now = datetime.now(timezone.utc)
        with self.store.transaction() as connection:
            existing_command = connection.execute(
                """
                SELECT command_type, payload_hash, result_json
                FROM phase4_runtime_commands WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
            if existing_command is not None:
                if (
                    existing_command["command_type"] != "resolve_interaction"
                    or existing_command["payload_hash"] != payload_hash
                ):
                    raise CommandIdentityConflict(
                        f"identity_conflict: command_id {command_id!r}"
                    )
                return InteractionResolutionResult.model_validate_json(
                    existing_command["result_json"]
                )

            row = connection.execute(
                """
                SELECT interaction.*, task.contract_json,
                       task.state AS task_state, task.version AS task_version,
                       task.current_attempt_id,
                       attempt.state AS attempt_state,
                       attempt.version AS attempt_version,
                       execution.session_handle
                FROM interactions AS interaction
                JOIN phase3_tasks AS task ON task.task_id = interaction.task_id
                JOIN phase3_attempts AS attempt
                  ON attempt.attempt_id = interaction.attempt_id
                LEFT JOIN executions AS execution
                  ON execution.execution_id = interaction.execution_id
                WHERE interaction.interaction_id = ?
                """,
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise KeyError(interaction_id)
            if int(row["version"]) != expected_version:
                raise CommandIdentityConflict(
                    f"version_conflict: interaction_id {interaction_id!r}"
                )
            if row["status"] != "pending":
                raise RuntimeError("Interaction is not pending")
            if row["task_state"] in {"succeeded", "failed", "cancelled"}:
                raise RuntimeError("Terminal Task cannot resolve into executable work")
            expected_task_state = (
                "waiting_approval"
                if row["kind"] == InteractionKind.APPROVAL.value
                else "waiting_input"
            )
            if (
                row["task_state"] != expected_task_state
                or row["attempt_state"] != "waiting"
                or row["current_attempt_id"] != row["attempt_id"]
            ):
                raise RuntimeError("Interaction is not the authoritative current wait")

            try:
                contract = effective_contract(
                    self.store,
                    str(row["task_id"]),
                    preflight_contract(row["contract_json"]),
                )
            except CatalogIntegrityError:
                self._terminate_contract_runtime_incompatible(
                    connection,
                    task_id=str(row["task_id"]),
                    attempt_id=str(row["attempt_id"]),
                    now=now,
                )
                failed_task = connection.execute(
                    "SELECT state, version FROM phase3_tasks WHERE task_id = ?",
                    (row["task_id"],),
                ).fetchone()
                failed_attempt = connection.execute(
                    "SELECT state, version FROM phase3_attempts WHERE attempt_id = ?",
                    (row["attempt_id"],),
                ).fetchone()
                result = InteractionResolutionResult(
                    command_id=command_id,
                    interaction_id=interaction_id,
                    interaction_state="cancelled",
                    interaction_version=expected_version + 1,
                    task_id=str(row["task_id"]),
                    task_state=str(failed_task["state"]),
                    task_version=int(failed_task["version"]),
                    attempt_id=str(row["attempt_id"]),
                    attempt_state=str(failed_attempt["state"]),
                    attempt_version=int(failed_attempt["version"]),
                    run_request_id=None,
                    run_request_state=None,
                )
                connection.execute(
                    """
                    INSERT INTO phase4_runtime_commands(
                        command_id, command_type, payload_hash, result_json,
                        created_at
                    ) VALUES (?, 'resolve_interaction', ?, ?, ?)
                    """,
                    (
                        command_id,
                        payload_hash,
                        result.model_dump_json(),
                        now.isoformat(),
                    ),
                )
                self._write_checkpoint(
                    connection,
                    boundary="task_terminal",
                    task_id=result.task_id,
                    attempt_id=result.attempt_id,
                    references={
                        "interaction_id": interaction_id,
                        "command_id": command_id,
                        "termination_reason": "contract_runtime_incompatible",
                    },
                )
                return result
            approval_resolution: ApprovalResolution | None = None
            approval_request: ApprovalRequest | None = None
            if row["kind"] == InteractionKind.APPROVAL.value:
                approval_resolution = ApprovalResolution.model_validate(resolution)
                request_row = connection.execute(
                    """
                    SELECT request_json, status, version
                    FROM phase3_approval_requests WHERE interaction_id = ?
                    """,
                    (interaction_id,),
                ).fetchone()
                if request_row is None:
                    raise PermissionError("approval_required")
                approval_request = ApprovalRequest.model_validate_json(
                    request_row["request_json"]
                )
                requirement = contract.approval_requirement(
                    approval_request.approval_requirement_ref.ref
                )
                if (
                    request_row["status"] != "pending"
                    or approval_resolution.approval_request_id
                    != approval_request.approval_request_id
                    or approval_resolution.interaction_id != interaction_id
                    or approval_resolution.task_contract_ref != contract.ref
                    or approval_resolution.approval_requirement_ref
                    != approval_request.approval_requirement_ref
                    or approval_resolution.effect_identity
                    != approval_request.effect_identity
                    or approval_resolution.effect_request_hash
                    != approval_request.effect_request_hash
                    or approval_resolution.permission_scope
                    != approval_request.permission_scope
                    or approval_resolution.approver_policy_ref
                    != requirement.approver_policy_ref
                    or approval_resolution.usage_semantics
                    != requirement.usage_semantics
                    or approval_resolution.revoked
                    or approval_resolution.usage_count != 0
                    or (
                        approval_resolution.decision.value == "approved"
                        and (
                            now < approval_resolution.valid_from
                            or (
                                approval_resolution.expires_at is not None
                                and now >= approval_resolution.expires_at
                            )
                        )
                    )
                    or (
                        approval_request.expires_at is not None
                        and now >= approval_request.expires_at
                    )
                ):
                    raise PermissionError("approval_effect_mismatch")
            else:
                response = resolution.get("response")
                candidate = (
                    response
                    if isinstance(response, str)
                    else json.dumps(
                        dict(resolution),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                )
                validation = validate_interaction_response(
                    candidate,
                    json.loads(row["payload_json"] or "{}"),
                )
                if isinstance(response, str):
                    resolution = {
                        **dict(resolution),
                        "response": validation.normalized_response,
                    }
                resolution = {
                    **dict(resolution),
                    "matched_identifiers": list(validation.matched_identifiers),
                }

            resolution_json = json.dumps(
                dict(resolution),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            interaction_cursor = connection.execute(
                """
                UPDATE interactions
                SET status = 'resolved', version = version + 1,
                    resolution_json = ?, resolved_at = ?
                WHERE interaction_id = ? AND status = 'pending' AND version = ?
                """,
                (
                    resolution_json,
                    now.isoformat(),
                    interaction_id,
                    expected_version,
                ),
            )
            if interaction_cursor.rowcount != 1:
                raise RuntimeError("Interaction resolution CAS failed")
            if approval_resolution is not None and approval_request is not None:
                self.governance_core._record_approval_resolution_from_runtime(
                    connection,
                    str(row["task_id"]),
                    approval_resolution,
                )
                resolved_request = approval_request.model_copy(
                    update={
                        "status": "resolved",
                        "version": int(request_row["version"]) + 1,
                    }
                )
                request_cursor = connection.execute(
                    """
                    UPDATE phase3_approval_requests
                    SET status = 'resolved', version = version + 1,
                        request_json = ?
                    WHERE approval_request_id = ? AND status = 'pending'
                    """,
                    (
                        resolved_request.model_dump_json(),
                        approval_request.approval_request_id,
                    ),
                )
                if request_cursor.rowcount != 1:
                    raise RuntimeError("ApprovalRequest resolution CAS failed")

            execution_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM executions WHERE attempt_id = ?",
                    (row["attempt_id"],),
                ).fetchone()[0]
            )
            termination_reason: str | None = None
            if (
                approval_resolution is not None
                and approval_resolution.decision == ApprovalDecision.DENIED
            ):
                termination_reason = "approval_denied_effect_not_executed"
            elif now >= _instant(contract.limits.task_deadline):
                termination_reason = "task_deadline_exceeded"
            elif (
                contract.limits.attempt_deadline is not None
                and now >= _instant(contract.limits.attempt_deadline)
            ):
                termination_reason = "attempt_deadline_exceeded"
            elif execution_count >= contract.limits.max_executions_per_attempt:
                termination_reason = "attempt_budget_exhausted"

            run_request_id: str | None = None
            run_request_state: str | None = None
            actual_ready_at = ready_at or now.isoformat()
            if termination_reason is None:
                task_cursor = connection.execute(
                    """
                    UPDATE phase3_tasks
                    SET state = 'running', version = version + 1, updated_at = ?
                    WHERE task_id = ? AND version = ? AND state = ?
                    """,
                    (
                        now.isoformat(),
                        row["task_id"],
                        row["task_version"],
                        expected_task_state,
                    ),
                )
                attempt_cursor = connection.execute(
                    """
                    UPDATE phase3_attempts
                    SET state = 'active', version = version + 1
                    WHERE attempt_id = ? AND version = ? AND state = 'waiting'
                    """,
                    (row["attempt_id"], row["attempt_version"]),
                )
                if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                    raise RuntimeError("Task/Attempt resume CAS failed")

                run_request_id = "run-request:" + command_id
                feedback_item: dict[str, Any] = {
                    "type": "InteractionResolution",
                    "interaction_id": interaction_id,
                    "kind": row["kind"],
                    "resolution": dict(resolution),
                }
                if approval_request is not None and approval_resolution is not None:
                    feedback_item["approved_exact_effect"] = {
                        "decision": approval_resolution.decision.value,
                        "effect_identity": approval_request.effect_identity,
                        "effect_request_hash": approval_request.effect_request_hash,
                        "permission_scope": approval_request.permission_scope,
                        "effect_summary": dict(approval_request.effect_summary),
                        "required_action": (
                            "execute_approved_exact_effect"
                            if approval_resolution.decision.value == "approved"
                            else "do_not_execute_denied_effect"
                        ),
                    }
                feedback = (feedback_item,)
                connection.execute(
                    """
                    INSERT INTO phase4_run_requests(
                        run_request_id, task_id, attempt_id, reason, state,
                        priority, ready_at, created_by_command_id,
                        created_by_decision_id, source_interaction_id,
                        session_handle, feedback_json, created_at
                    ) VALUES (?, ?, ?, 'interaction_resolved', 'pending', ?, ?, ?,
                              NULL, ?, ?, ?, ?)
                    """,
                    (
                        run_request_id,
                        row["task_id"],
                        row["attempt_id"],
                        priority,
                        actual_ready_at,
                        command_id,
                        interaction_id,
                        row["session_handle"],
                        json.dumps(
                            feedback,
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ),
                        now.isoformat(),
                    ),
                )
                run_request_state = "pending"
            else:
                task_cursor = connection.execute(
                    """
                    UPDATE phase3_tasks
                    SET state = 'failed', version = version + 1,
                        termination_reason = ?, updated_at = ?
                    WHERE task_id = ? AND version = ? AND state = ?
                    """,
                    (
                        termination_reason,
                        now.isoformat(),
                        row["task_id"],
                        row["task_version"],
                        expected_task_state,
                    ),
                )
                attempt_terminal_state = (
                    "exhausted"
                    if termination_reason
                    in {"attempt_deadline_exceeded", "attempt_budget_exhausted"}
                    else "failed"
                )
                attempt_cursor = connection.execute(
                    """
                    UPDATE phase3_attempts
                    SET state = ?, version = version + 1, ended_at = ?,
                        termination_reason = ?
                    WHERE attempt_id = ? AND version = ? AND state = 'waiting'
                    """,
                    (
                        attempt_terminal_state,
                        now.isoformat(),
                        termination_reason,
                        row["attempt_id"],
                        row["attempt_version"],
                    ),
                )
                if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                    raise RuntimeError("Interaction limit transition CAS failed")

            refreshed_task = connection.execute(
                "SELECT state, version FROM phase3_tasks WHERE task_id = ?",
                (row["task_id"],),
            ).fetchone()
            refreshed_attempt = connection.execute(
                "SELECT state, version FROM phase3_attempts WHERE attempt_id = ?",
                (row["attempt_id"],),
            ).fetchone()
            result = InteractionResolutionResult(
                command_id=command_id,
                interaction_id=interaction_id,
                interaction_state="resolved",
                interaction_version=expected_version + 1,
                task_id=str(row["task_id"]),
                task_state=str(refreshed_task["state"]),
                task_version=int(refreshed_task["version"]),
                attempt_id=str(row["attempt_id"]),
                attempt_state=str(refreshed_attempt["state"]),
                attempt_version=int(refreshed_attempt["version"]),
                run_request_id=run_request_id,
                run_request_state=run_request_state,
            )
            connection.execute(
                """
                INSERT INTO phase4_runtime_commands(
                    command_id, command_type, payload_hash, result_json, created_at
                ) VALUES (?, 'resolve_interaction', ?, ?, ?)
                """,
                (
                    command_id,
                    payload_hash,
                    result.model_dump_json(),
                    now.isoformat(),
                ),
            )
            self._publish_runtime_fact(
                connection,
                fact_id="fact:resolve:" + command_id,
                fact_type="interaction_resolved",
                task_id=result.task_id,
                attempt_id=result.attempt_id,
                execution_id=row["execution_id"],
                source_event_id=command_id,
                subject_type="interaction",
                subject_id=interaction_id,
                subject_version=result.interaction_version,
                evidence_refs=(interaction_id,),
                attributes={
                    "kind": row["kind"],
                    "task_state": result.task_state,
                    "run_request_id": run_request_id,
                    "termination_reason": termination_reason,
                },
            )
            self._write_checkpoint(
                connection,
                boundary=(
                    "task_terminal"
                    if termination_reason is not None
                    else "interaction_resolved"
                ),
                task_id=result.task_id,
                attempt_id=result.attempt_id,
                run_request_id=run_request_id,
                references={
                    "interaction_id": interaction_id,
                    "command_id": command_id,
                },
            )
            return result

    def cancel_task(
        self,
        *,
        command_id: str,
        task_id: str,
        expected_task_version: int,
        reason: str = "operator_cancelled",
    ) -> TaskCancellationResult:
        """Persist cancel intent and the complete authoritative cancel CAS."""

        payload_hash = sha256_digest(
            {
                "command_type": "cancel_task",
                "task_id": task_id,
                "expected_task_version": expected_task_version,
                "reason": reason,
            }
        )
        now = datetime.now(timezone.utc)
        with self.store.transaction() as connection:
            existing_command = connection.execute(
                """
                SELECT command_type, payload_hash, result_json
                FROM phase4_runtime_commands WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
            if existing_command is not None:
                if (
                    existing_command["command_type"] != "cancel_task"
                    or existing_command["payload_hash"] != payload_hash
                ):
                    raise CommandIdentityConflict(
                        f"identity_conflict: command_id {command_id!r}"
                    )
                return TaskCancellationResult.model_validate_json(
                    existing_command["result_json"]
                )

            task = connection.execute(
                "SELECT * FROM phase3_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            attempt = connection.execute(
                "SELECT * FROM phase3_attempts WHERE attempt_id = ?",
                (task["current_attempt_id"],),
            ).fetchone()
            if attempt is None:
                raise RuntimeError("Task current Attempt is missing")
            if task["state"] in {"succeeded", "failed"}:
                raise TaskCancellationConflict("terminal_conflict", task_id)

            pending_requests = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT run_request_id FROM phase4_run_requests
                    WHERE task_id = ? AND state = 'pending'
                    ORDER BY run_request_id
                    """,
                    (task_id,),
                ).fetchall()
            )
            pending_interactions = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT interaction_id FROM interactions
                    WHERE task_id = ? AND status = 'pending'
                    ORDER BY interaction_id
                    """,
                    (task_id,),
                ).fetchall()
            )
            active_executions = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT execution_id FROM executions
                    WHERE task_id = ? AND ended_at IS NULL
                    ORDER BY execution_id
                    """,
                    (task_id,),
                ).fetchall()
            )

            if task["state"] != "cancelled":
                if int(task["version"]) != expected_task_version:
                    raise TaskCancellationConflict("version_conflict", task_id)
                task_cursor = connection.execute(
                    """
                    UPDATE phase3_tasks
                    SET state = 'cancelled', version = version + 1,
                        termination_reason = ?, updated_at = ?
                    WHERE task_id = ? AND version = ?
                      AND state NOT IN ('succeeded', 'failed', 'cancelled')
                    """,
                    (
                        reason,
                        now.isoformat(),
                        task_id,
                        expected_task_version,
                    ),
                )
                if task_cursor.rowcount != 1:
                    raise TaskCancellationConflict("version_conflict", task_id)
                if attempt["state"] not in {
                    "completed",
                    "failed",
                    "exhausted",
                    "superseded",
                    "cancelled",
                }:
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = 'cancelled', version = version + 1,
                            ended_at = ?, termination_reason = ?
                        WHERE attempt_id = ? AND version = ?
                        """,
                        (
                            now.isoformat(),
                            reason,
                            attempt["attempt_id"],
                            attempt["version"],
                        ),
                    )
                    if attempt_cursor.rowcount != 1:
                        raise TaskCancellationConflict("version_conflict", task_id)
                connection.execute(
                    """
                    UPDATE phase4_run_requests SET state = 'cancelled'
                    WHERE task_id = ? AND state = 'pending'
                    """,
                    (task_id,),
                )
                connection.execute(
                    """
                    UPDATE interactions
                    SET status = 'cancelled', version = version + 1,
                        resolved_at = ?
                    WHERE task_id = ? AND status = 'pending'
                    """,
                    (now.isoformat(), task_id),
                )
                connection.execute(
                    """
                    UPDATE phase3_approval_requests
                    SET status = 'cancelled', version = version + 1
                    WHERE task_id = ? AND status = 'pending'
                    """,
                    (task_id,),
                )
                connection.execute(
                    """
                    UPDATE executions SET suspension_requested = 1
                    WHERE task_id = ? AND ended_at IS NULL
                    """,
                    (task_id,),
                )
                for operation_row in connection.execute(
                    """
                    SELECT operation_id, operation_json
                    FROM phase3_external_operations
                    WHERE task_id = ? AND status IN ('in_flight', 'acknowledged')
                    """,
                    (task_id,),
                ).fetchall():
                    operation = ExternalOperation.model_validate_json(
                        operation_row["operation_json"]
                    )
                    indeterminate = operation.model_copy(
                        update={
                            "status": ExternalOperationStatus.INDETERMINATE,
                            "version": operation.version + 1,
                        }
                    )
                    connection.execute(
                        """
                        UPDATE phase3_external_operations
                        SET status = 'indeterminate', version = ?, operation_json = ?
                        WHERE operation_id = ? AND version = ?
                        """,
                        (
                            indeterminate.version,
                            indeterminate.model_dump_json(),
                            operation.operation_id,
                            operation.version,
                        ),
                    )

            refreshed_task = connection.execute(
                "SELECT state, version FROM phase3_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            refreshed_attempt = connection.execute(
                "SELECT state, version FROM phase3_attempts WHERE attempt_id = ?",
                (attempt["attempt_id"],),
            ).fetchone()
            result = TaskCancellationResult(
                command_id=command_id,
                task_id=task_id,
                task_state=str(refreshed_task["state"]),
                task_version=int(refreshed_task["version"]),
                attempt_id=str(attempt["attempt_id"]),
                attempt_state=str(refreshed_attempt["state"]),
                attempt_version=int(refreshed_attempt["version"]),
                cancelled_run_request_ids=pending_requests,
                cancelled_interaction_ids=pending_interactions,
                active_execution_ids=active_executions,
            )
            connection.execute(
                """
                INSERT INTO phase4_runtime_commands(
                    command_id, command_type, payload_hash, result_json, created_at
                ) VALUES (?, 'cancel_task', ?, ?, ?)
                """,
                (
                    command_id,
                    payload_hash,
                    result.model_dump_json(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO phase4_cancel_intents(
                    command_id, task_id, expected_task_version, reason,
                    payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    task_id,
                    expected_task_version,
                    reason,
                    payload_hash,
                    now.isoformat(),
                ),
            )
            self._publish_runtime_fact(
                connection,
                fact_id="fact:cancel:" + command_id,
                fact_type="task_cancelled",
                task_id=task_id,
                attempt_id=str(attempt["attempt_id"]),
                execution_id=None,
                source_event_id=command_id,
                subject_type="task",
                subject_id=task_id,
                subject_version=result.task_version,
                evidence_refs=(command_id,),
                attributes={"state": "cancelled", "reason": reason},
            )
            self._write_checkpoint(
                connection,
                boundary="task_terminal",
                task_id=task_id,
                attempt_id=str(attempt["attempt_id"]),
                references={"command_id": command_id, "reason": reason},
            )
            return result

    def begin_execution(
        self,
        *,
        execution_id: str,
        task_id: str,
        attempt_id: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> None:
        """Move a claimed initial Task into running under Runtime authority."""

        with self.store.transaction() as connection:
            self._assert_execution_lease_row(
                connection,
                execution_id=execution_id,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
            )
            task = connection.execute(
                "SELECT * FROM phase3_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            attempt = connection.execute(
                "SELECT * FROM phase3_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if task is None or attempt is None:
                raise RuntimeError("Execution references missing authoritative records")
            if task["current_attempt_id"] != attempt_id:
                raise RuntimeError("Execution attempt is not current")
            if attempt["task_id"] != task_id or attempt["state"] != "active":
                raise RuntimeError("Execution attempt is not active")
            if task["state"] == TaskState.RUNNING.value:
                return
            if task["state"] != TaskState.PENDING.value:
                raise RuntimeError("Task cannot begin execution from current state")
            cursor = connection.execute(
                """
                UPDATE phase3_tasks
                SET state = 'running', version = version + 1, updated_at = ?
                WHERE task_id = ? AND version = ? AND state = 'pending'
                """,
                (datetime.now(timezone.utc).isoformat(), task_id, task["version"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Task begin-execution CAS failed")

    def save_session_handle(
        self,
        execution_id: str,
        handle: str | None,
        *,
        lease_owner_id: str,
        lease_token: str,
    ) -> None:
        """Persist the opaque Hermes session on the authoritative Execution."""

        with self.store.transaction() as connection:
            self._assert_execution_lease_row(
                connection,
                execution_id=execution_id,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
            )
            execution = connection.execute(
                """
                SELECT session_handle, ended_at FROM executions
                WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if execution is None:
                raise KeyError(execution_id)
            if execution["ended_at"] is not None:
                if execution["session_handle"] == handle:
                    return
                raise RuntimeError("Ended Execution session handle is immutable")
            connection.execute(
                """
                UPDATE executions SET session_handle = ?
                WHERE execution_id = ? AND ended_at IS NULL
                """,
                (handle, execution_id),
            )

    def expire_deadline(
        self,
        *,
        task_id: str,
        attempt_id: str,
        reason: str,
    ) -> None:
        """Persist a frozen deadline outcome under Runtime authority."""

        now = datetime.now(timezone.utc).isoformat()
        with self.store.transaction() as connection:
            task = connection.execute(
                "SELECT * FROM phase3_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            attempt = connection.execute(
                "SELECT * FROM phase3_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if task is None or attempt is None:
                raise RuntimeError("Deadline references missing authoritative records")
            if task["current_attempt_id"] != attempt_id:
                raise RuntimeError("Deadline attempt is not current")
            if reason == "task_deadline_exceeded":
                if task["state"] not in {
                    TaskState.SUCCEEDED.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELLED.value,
                }:
                    task_cursor = connection.execute(
                        """
                        UPDATE phase3_tasks
                        SET state = 'failed', version = version + 1,
                            termination_reason = ?, updated_at = ?
                        WHERE task_id = ? AND version = ?
                        """,
                        (reason, now, task_id, task["version"]),
                    )
                    if task_cursor.rowcount != 1:
                        raise RuntimeError("Task deadline CAS failed")
                if attempt["state"] == AttemptState.ACTIVE.value:
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = 'failed', version = version + 1,
                            ended_at = ?, termination_reason = ?
                        WHERE attempt_id = ? AND version = ?
                        """,
                        (now, reason, attempt_id, attempt["version"]),
                    )
                    if attempt_cursor.rowcount != 1:
                        raise RuntimeError("Attempt deadline CAS failed")
                self._write_checkpoint(
                    connection,
                    boundary="task_terminal",
                    task_id=task_id,
                    attempt_id=attempt_id,
                    references={"termination_reason": reason},
                )
                return
            if reason == "attempt_deadline_exceeded":
                if attempt["state"] == AttemptState.ACTIVE.value:
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = 'exhausted', version = version + 1,
                            ended_at = ?, termination_reason = ?
                        WHERE attempt_id = ? AND version = ?
                        """,
                        (now, reason, attempt_id, attempt["version"]),
                    )
                    if attempt_cursor.rowcount != 1:
                        raise RuntimeError("Attempt deadline CAS failed")
                self._write_checkpoint(
                    connection,
                    boundary="attempt_terminal",
                    task_id=task_id,
                    attempt_id=attempt_id,
                    references={"termination_reason": reason},
                )
                return
            raise ValueError(f"Unsupported deadline reason: {reason!r}")

    def apply_policy_decision(
        self,
        decision_id: str,
        *,
        execution_id: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> GovernanceApplicationResult:
        """Apply one immutable PolicyDecision through the Runtime transaction."""

        with self.store.transaction() as connection:
            self._assert_execution_lease_row(
                connection,
                execution_id=execution_id,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                allow_completed=True,
            )
            decision_row = connection.execute(
                "SELECT * FROM phase3_policy_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if decision_row is None:
                raise KeyError(decision_id)
            decision = PolicyDecision.model_validate_json(
                decision_row["decision_json"]
            )
            task = connection.execute(
                "SELECT * FROM phase3_tasks WHERE task_id = ?",
                (decision.task_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT * FROM phase3_attempts WHERE attempt_id = ?",
                (decision.attempt_id,),
            ).fetchone()
            if task is None or attempt is None:
                raise RuntimeError("Decision references missing authoritative records")
            if decision_row["application_status"] != "pending":
                result_attempt = attempt
                if decision_row["application_status"] == "applied":
                    current_attempt = connection.execute(
                        "SELECT * FROM phase3_attempts WHERE attempt_id = ?",
                        (task["current_attempt_id"],),
                    ).fetchone()
                    if current_attempt is not None:
                        result_attempt = current_attempt
                return self._policy_result_from_rows(
                    decision,
                    DecisionApplication(decision_row["application_status"]),
                    task,
                    result_attempt,
                    derived_record_id=decision_row["derived_record_id"],
                )

            completion = None
            if decision.expected_completion_validation_id is not None:
                completion_row = connection.execute(
                    """
                    SELECT validation_json FROM phase3_completion_validations
                    WHERE completion_validation_id = ? AND task_id = ?
                    """,
                    (
                        decision.expected_completion_validation_id,
                        decision.task_id,
                    ),
                ).fetchone()
                if completion_row is not None:
                    completion = CompletionValidationResult.model_validate_json(
                        completion_row["validation_json"]
                    )
            pending_interaction = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM interactions
                    WHERE task_id = ? AND status = 'pending'
                    """,
                    (decision.task_id,),
                ).fetchone()[0]
            )
            reconciliation = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM phase3_reconciliations
                    WHERE task_id = ? AND status IN ('pending', 'running')
                    """,
                    (decision.task_id,),
                ).fetchone()[0]
            )
            operation_statuses = tuple(
                row[0]
                for row in connection.execute(
                    """
                    SELECT status FROM phase3_external_operations
                    WHERE task_id = ? AND required_for_completion = 1
                    ORDER BY operation_id
                    """,
                    (decision.task_id,),
                ).fetchall()
            )
            current = DecisionApplicationSnapshot(
                task_id=task["task_id"],
                task_version=task["version"],
                task_state=task["state"],
                attempt_id=attempt["attempt_id"],
                attempt_version=attempt["version"],
                task_contract_ref=TaskContractRef(
                    contract_id=str(task["contract_id"]),
                    contract_version=str(task["contract_version"]),
                    contract_hash=str(task["contract_hash"]),
                ),
                interaction_snapshot_version=self._interaction_watermark(
                    connection, decision.task_id
                ),
                completion_validation_id=(
                    completion.completion_validation_id if completion else None
                ),
                pending_blocking_interaction=bool(pending_interaction),
                unresolved_reconciliation=bool(reconciliation),
                required_external_operation_statuses=operation_statuses,
            )
            application = apply_policy_decision(
                decision, current, completion=completion
            )
            if application != DecisionApplication.APPLIED:
                cursor = connection.execute(
                    """
                    UPDATE phase3_policy_decisions
                    SET application_status = ?, applied_at = ?,
                        derived_record_id = NULL
                    WHERE decision_id = ? AND application_status = 'pending'
                    """,
                    (
                        application.value,
                        datetime.now(timezone.utc).isoformat(),
                        decision_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("PolicyDecision verdict CAS failed")
                return self._policy_result_from_rows(
                    decision, application, task, attempt
                )

            derived_record_id = self._apply_policy_action(
                connection,
                decision,
                task,
                attempt,
                authority_execution_id=execution_id,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
            )
            applied_cursor = connection.execute(
                """
                UPDATE phase3_policy_decisions
                SET application_status = 'applied', applied_at = ?,
                    derived_record_id = ?
                WHERE decision_id = ? AND application_status = 'pending'
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    derived_record_id,
                    decision_id,
                ),
            )
            if applied_cursor.rowcount != 1:
                raise RuntimeError("PolicyDecision application CAS failed")
            refreshed_task = connection.execute(
                "SELECT * FROM phase3_tasks WHERE task_id = ?",
                (decision.task_id,),
            ).fetchone()
            refreshed_attempt = connection.execute(
                "SELECT * FROM phase3_attempts WHERE attempt_id = ?",
                (refreshed_task["current_attempt_id"],),
            ).fetchone()
            current_work = connection.execute(
                """
                SELECT execution_id, run_request_id
                FROM executions
                WHERE execution_id = ? AND task_id = ? AND attempt_id = ?
                """,
                (execution_id, decision.task_id, decision.attempt_id),
            ).fetchone()
            boundary = "policy_decision_applied"
            if refreshed_task["state"] in {
                TaskState.WAITING_INPUT.value,
                TaskState.WAITING_APPROVAL.value,
            }:
                boundary = "task_waiting"
            elif refreshed_task["state"] in {
                TaskState.SUCCEEDED.value,
                TaskState.FAILED.value,
                TaskState.CANCELLED.value,
            }:
                boundary = "task_terminal"
            self._write_checkpoint(
                connection,
                boundary=boundary,
                task_id=decision.task_id,
                attempt_id=str(refreshed_attempt["attempt_id"]),
                execution_id=(
                    str(current_work["execution_id"])
                    if current_work is not None
                    else None
                ),
                run_request_id=(
                    str(current_work["run_request_id"])
                    if current_work is not None
                    and current_work["run_request_id"] is not None
                    else None
                ),
                references={
                    "policy_decision_id": decision.decision_id,
                    "derived_record_id": derived_record_id,
                },
            )
            return self._policy_result_from_rows(
                decision,
                DecisionApplication.APPLIED,
                refreshed_task,
                refreshed_attempt,
                derived_record_id=derived_record_id,
            )

    def recover_pending_policy_decisions(
        self,
        *,
        lease_owner_id: str,
        lease_duration_seconds: float,
    ) -> tuple[GovernanceApplicationResult, ...]:
        """Apply durable pending decisions during Production startup recovery."""

        rows = self.store.query_all(
            """
            SELECT decision_id, task_id, attempt_id
            FROM phase3_policy_decisions AS decision
            WHERE decision.application_status = 'pending'
              AND NOT EXISTS (
                SELECT 1
                FROM phase4_run_requests AS request
                WHERE request.task_id = decision.task_id
                  AND request.state = 'claimed'
                  AND request.lease_token IS NOT NULL
                  AND request.lease_expires_at > ?
              )
            ORDER BY decision.created_at, decision.decision_id
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        recovered: list[GovernanceApplicationResult] = []
        for row in rows:
            authority = self.store.query_one(
                """
                SELECT task.contract_json, task.state AS task_state,
                       task.version AS task_version,
                       task.current_attempt_id,
                       attempt.state AS attempt_state,
                       attempt.version AS attempt_version,
                       decision.decision_json
                FROM phase3_policy_decisions AS decision
                JOIN phase3_tasks AS task ON task.task_id = decision.task_id
                JOIN phase3_attempts AS attempt
                  ON attempt.attempt_id = decision.attempt_id
                WHERE decision.decision_id = ?
                  AND decision.application_status = 'pending'
                """,
                (row["decision_id"],),
            )
            if authority is None:
                continue
            execution = self.store.query_one(
                """
                SELECT execution.execution_id
                FROM executions AS execution
                JOIN phase4_execution_results AS result
                  ON result.execution_id = execution.execution_id
                WHERE execution.task_id = ? AND execution.attempt_id = ?
                  AND execution.run_request_id IS NOT NULL
                ORDER BY result.created_at DESC, execution.execution_id DESC
                LIMIT 1
                """,
                (row["task_id"], row["attempt_id"]),
            )
            if execution is None:
                continue
            authority_execution_id = str(execution["execution_id"])
            lease_token = self._acquire_recovery_execution_lease(
                execution_id=authority_execution_id,
                lease_owner_id=lease_owner_id,
                lease_duration_seconds=lease_duration_seconds,
            )
            if lease_token is None:
                continue
            contract = preflight_contract(authority["contract_json"])
            decision = PolicyDecision.model_validate_json(
                authority["decision_json"]
            )
            now = datetime.now(timezone.utc)
            terminal_reason: str | None = None
            if now >= _instant(contract.limits.task_deadline):
                terminal_reason = "task_deadline_exceeded"
            elif (
                contract.limits.attempt_deadline is not None
                and now >= _instant(contract.limits.attempt_deadline)
            ):
                terminal_reason = "attempt_deadline_exceeded"
            elif decision.action == PolicyAction.CONTINUE_WITH_FEEDBACK:
                execution_count_row = self.store.query_one(
                    "SELECT COUNT(*) AS value FROM executions WHERE attempt_id = ?",
                    (row["attempt_id"],),
                )
                if execution_count_row is None:
                    raise RuntimeError("Execution count query failed")
                execution_count = int(execution_count_row["value"])
                if execution_count >= contract.limits.max_executions_per_attempt:
                    terminal_reason = "execution_budget_exhausted"
            elif decision.action == PolicyAction.START_NEW_ATTEMPT:
                attempt_count_row = self.store.query_one(
                    "SELECT COUNT(*) AS value FROM phase3_attempts WHERE task_id = ?",
                    (row["task_id"],),
                )
                if attempt_count_row is None:
                    raise RuntimeError("Attempt count query failed")
                attempt_count = int(attempt_count_row["value"])
                if attempt_count >= contract.limits.max_attempts:
                    terminal_reason = "attempt_budget_exhausted"
            if (
                terminal_reason is not None
                and authority["task_state"]
                not in {
                    TaskState.SUCCEEDED.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELLED.value,
                }
            ):
                with self.store.transaction() as connection:
                    self._assert_execution_lease_row(
                        connection,
                        execution_id=authority_execution_id,
                        lease_owner_id=lease_owner_id,
                        lease_token=lease_token,
                        allow_completed=True,
                    )
                    attempt_target = (
                        AttemptState.FAILED.value
                        if terminal_reason == "task_deadline_exceeded"
                        else AttemptState.EXHAUSTED.value
                    )
                    task_cursor = connection.execute(
                        """
                        UPDATE phase3_tasks
                        SET state = 'failed', version = version + 1,
                            termination_reason = ?, updated_at = ?
                        WHERE task_id = ? AND version = ?
                          AND state NOT IN ('succeeded', 'failed', 'cancelled')
                        """,
                        (
                            terminal_reason,
                            now.isoformat(),
                            row["task_id"],
                            authority["task_version"],
                        ),
                    )
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = ?, version = version + 1, ended_at = ?,
                            termination_reason = ?
                        WHERE attempt_id = ? AND version = ?
                          AND state NOT IN (
                            'completed', 'failed', 'exhausted',
                            'superseded', 'cancelled'
                          )
                        """,
                        (
                            attempt_target,
                            now.isoformat(),
                            terminal_reason,
                            row["attempt_id"],
                            authority["attempt_version"],
                        ),
                    )
                    if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                        raise RuntimeError("Pending decision recovery limit CAS failed")
                    self._write_checkpoint(
                        connection,
                        boundary="task_terminal",
                        task_id=str(row["task_id"]),
                        attempt_id=str(row["attempt_id"]),
                        references={
                            "policy_decision_id": str(row["decision_id"]),
                            "termination_reason": terminal_reason,
                        },
                    )
            if execution is not None and terminal_reason is None:
                replay = self.governance_core.evaluate(authority_execution_id)
                if replay.policy_decision.decision_id != row["decision_id"]:
                    raise RuntimeError("PolicyDecision recovery identity mismatch")
            application = self.apply_policy_decision(
                str(row["decision_id"]),
                execution_id=authority_execution_id,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
            )
            if application.application != DecisionApplication.APPLIED:
                self.finalize_late_execution_result(
                    authority_execution_id,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                )
            recovered.append(application)
        return tuple(recovered)

    @staticmethod
    def _operation_requires_reconciliation(
        connection: sqlite3.Connection,
        operation_id: str,
        status: str,
    ) -> bool:
        if status in {
            ExternalOperationStatus.IN_FLIGHT.value,
            ExternalOperationStatus.ACKNOWLEDGED.value,
            ExternalOperationStatus.INDETERMINATE.value,
        }:
            return True
        if status != ExternalOperationStatus.PREPARED.value:
            return False
        dispatch_fact = connection.execute(
            """
            SELECT fact_type FROM phase3_reliability_facts
            WHERE source_event_id LIKE ?
              AND fact_type IN (
                'external_operation_dispatched',
                'external_operation_confirmed_not_dispatched'
              )
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (operation_id + ":%",),
        ).fetchone()
        return bool(
            dispatch_fact is not None
            and dispatch_fact["fact_type"] == "external_operation_dispatched"
        )

    @classmethod
    def _reconciliation_operation_ids(
        cls,
        connection: sqlite3.Connection,
        task_id: str,
    ) -> tuple[str, ...]:
        return tuple(
            str(row["operation_id"])
            for row in connection.execute(
                """
                SELECT operation_id, status
                FROM phase3_external_operations
                WHERE task_id = ?
                  AND status IN ('prepared', 'in_flight', 'acknowledged',
                                 'indeterminate')
                ORDER BY created_at, operation_id
                """,
                (task_id,),
            ).fetchall()
            if cls._operation_requires_reconciliation(
                connection,
                str(row["operation_id"]),
                str(row["status"]),
            )
        )

    @staticmethod
    def _insert_recovery_run_request(
        connection: sqlite3.Connection,
        *,
        command_id: str,
        task_id: str,
        attempt_id: str,
        source_execution_id: str | None,
        session_handle: str | None,
        priority: int,
        reason: str,
        references: Mapping[str, Any],
        source_interaction_id: str | None = None,
    ) -> str:
        run_request_id = "run-request:" + command_id
        existing = connection.execute(
            """
            SELECT run_request_id FROM phase4_run_requests
            WHERE created_by_command_id = ?
            """,
            (command_id,),
        ).fetchone()
        if existing is not None:
            return str(existing["run_request_id"])
        now = datetime.now(timezone.utc).isoformat()
        feedback = (
            {
                "type": "RuntimeRecovery",
                "source_execution_id": source_execution_id,
                **dict(references),
            },
        )
        connection.execute(
            """
            INSERT INTO phase4_run_requests(
                run_request_id, task_id, attempt_id, reason, state,
                priority, ready_at, created_by_command_id,
                created_by_decision_id, source_interaction_id,
                session_handle, feedback_json, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                run_request_id,
                task_id,
                attempt_id,
                reason,
                priority,
                now,
                command_id,
                source_interaction_id,
                session_handle,
                json.dumps(
                    feedback,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                now,
            ),
        )
        return run_request_id

    @staticmethod
    def _ensure_recovery_reconciliation(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        attempt_id: str,
        source_identity: str,
    ) -> str:
        existing = connection.execute(
            """
            SELECT reconciliation_id FROM phase3_reconciliations
            WHERE task_id = ? AND status IN ('pending', 'running')
            ORDER BY created_at, reconciliation_id
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if existing is not None:
            return str(existing["reconciliation_id"])
        reconciliation_id = "reconciliation:recovery:" + sha256_digest(
            {"task_id": task_id, "source_identity": source_identity}
        )
        connection.execute(
            """
            INSERT INTO phase3_reconciliations(
                reconciliation_id, task_id, attempt_id, status,
                created_by_decision_id, created_at, version
            ) VALUES (?, ?, ?, 'pending', NULL, ?, 1)
            """,
            (
                reconciliation_id,
                task_id,
                attempt_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return reconciliation_id

    def recover_execution(
        self,
        *,
        command_id: str,
        execution_id: str,
        expected_task_version: int,
        expected_attempt_version: int,
        now: datetime | None = None,
    ) -> ExecutionRecoveryResult:
        """CAS-close one orphan and authorize only its frozen recovery class."""

        recovered_at = now or datetime.now(timezone.utc)
        if recovered_at.tzinfo is None:
            recovered_at = recovered_at.replace(tzinfo=timezone.utc)
        else:
            recovered_at = recovered_at.astimezone(timezone.utc)
        payload_hash = sha256_digest(
            {
                "command_type": "recover_execution",
                "execution_id": execution_id,
                "expected_task_version": expected_task_version,
                "expected_attempt_version": expected_attempt_version,
            }
        )
        with self.store.transaction() as connection:
            existing_command = connection.execute(
                """
                SELECT command_type, payload_hash, result_json
                FROM phase4_runtime_commands WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
            if existing_command is not None:
                if (
                    existing_command["command_type"] != "recover_execution"
                    or existing_command["payload_hash"] != payload_hash
                ):
                    raise CommandIdentityConflict(
                        f"identity_conflict: command_id {command_id!r}"
                    )
                return ExecutionRecoveryResult.model_validate_json(
                    existing_command["result_json"]
                )

            row = connection.execute(
                """
                SELECT execution.*, request.priority,
                       task.contract_json, task.state AS task_state,
                       task.version AS task_version,
                       task.current_attempt_id,
                       attempt.state AS attempt_state,
                       attempt.version AS attempt_version
                FROM executions AS execution
                JOIN phase3_tasks AS task ON task.task_id = execution.task_id
                JOIN phase3_attempts AS attempt
                  ON attempt.attempt_id = execution.attempt_id
                LEFT JOIN phase4_run_requests AS request
                  ON request.run_request_id = execution.run_request_id
                WHERE execution.execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            if row["ended_at"] is not None or row["status"] != "running":
                raise RuntimeError("Execution is not an orphaned running Execution")
            if (
                int(row["task_version"]) != expected_task_version
                or int(row["attempt_version"]) != expected_attempt_version
            ):
                raise CommandIdentityConflict(
                    f"version_conflict: execution_id {execution_id!r}"
                )
            if row["current_attempt_id"] != row["attempt_id"]:
                raise RuntimeError("Orphaned Execution attempt is not current")
            latest_checkpoint_row = connection.execute(
                """
                SELECT checkpoint_id, task_version, envelope_json
                FROM phase4_checkpoints
                WHERE task_id = ?
                ORDER BY created_at DESC, checkpoint_id DESC
                LIMIT 1
                """,
                (row["task_id"],),
            ).fetchone()
            checkpoint_envelope: Mapping[str, Any] = {}
            source_checkpoint_id: str | None = None
            if (
                latest_checkpoint_row is not None
                and int(latest_checkpoint_row["task_version"])
                <= int(row["task_version"])
            ):
                checkpoint_envelope = json.loads(
                    latest_checkpoint_row["envelope_json"]
                )
                source_checkpoint_id = str(
                    latest_checkpoint_row["checkpoint_id"]
                )
            checkpoint_execution = checkpoint_envelope.get("execution")
            checkpoint_session = (
                checkpoint_execution.get("session_handle")
                if isinstance(checkpoint_execution, Mapping)
                else None
            )

            ended_at = recovered_at.isoformat()
            execution_cursor = connection.execute(
                """
                UPDATE executions
                SET status = 'interrupted', ended_at = ?,
                    termination_reason = 'process_lost'
                WHERE execution_id = ? AND status = 'running' AND ended_at IS NULL
                """,
                (ended_at, execution_id),
            )
            if execution_cursor.rowcount != 1:
                raise RuntimeError("Execution recovery CAS failed")
            connection.execute(
                """
                INSERT INTO execution_events(
                    event_id, execution_id, event_type, payload_json, created_at
                ) VALUES (?, ?, 'AstraExecutionEnded', ?, ?)
                """,
                (
                    "event:recovery:" + execution_id,
                    execution_id,
                    json.dumps(
                        {
                            "status": "interrupted",
                            "termination_reason": "process_lost",
                            "recovery_command_id": command_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    ended_at,
                ),
            )
            if row["run_request_id"] is not None:
                connection.execute(
                    """
                    UPDATE phase4_run_requests SET state = ?
                    WHERE run_request_id = ? AND state = 'claimed'
                    """,
                    (
                        (
                            "cancelled"
                            if row["task_state"] == TaskState.CANCELLED.value
                            else "completed"
                        ),
                        row["run_request_id"],
                    ),
                )
            self._publish_runtime_fact(
                connection,
                fact_id="fact:recovery:" + execution_id,
                fact_type="execution_interrupted",
                task_id=str(row["task_id"]),
                attempt_id=str(row["attempt_id"]),
                execution_id=execution_id,
                source_event_id=execution_id + ":process_lost",
                subject_type="execution",
                subject_id=execution_id,
                subject_version=1,
                evidence_refs=(execution_id,),
                attributes={
                    "status": "interrupted",
                    "termination_reason": "process_lost",
                },
            )

            task_id = str(row["task_id"])
            attempt_id = str(row["attempt_id"])
            task_state = str(row["task_state"])
            attempt_state = str(row["attempt_state"])
            classification = RecoveryClassification.SAFE_CONTINUE
            run_request_id: str | None = None
            reconciliation_id: str | None = None
            terminal_reason: str | None = None
            incompatible_contract = False
            try:
                contract = preflight_contract(row["contract_json"])
            except CatalogIntegrityError:
                contract = None
                incompatible_contract = True
                classification = RecoveryClassification.TERMINAL
                self._terminate_contract_runtime_incompatible(
                    connection,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    now=recovered_at,
                )
            result_exists = connection.execute(
                """
                SELECT 1 FROM phase4_execution_results WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone() is not None

            if incompatible_contract:
                classification = RecoveryClassification.TERMINAL
            elif task_state in {
                TaskState.SUCCEEDED.value,
                TaskState.FAILED.value,
                TaskState.CANCELLED.value,
            } or attempt_state in {
                AttemptState.COMPLETED.value,
                AttemptState.FAILED.value,
                AttemptState.EXHAUSTED.value,
                AttemptState.SUPERSEDED.value,
                AttemptState.CANCELLED.value,
            }:
                classification = RecoveryClassification.TERMINAL
            elif recovered_at >= _instant(contract.limits.task_deadline):
                terminal_reason = "task_deadline_exceeded"
            elif (
                contract.limits.attempt_deadline is not None
                and recovered_at >= _instant(contract.limits.attempt_deadline)
            ):
                terminal_reason = "attempt_deadline_exceeded"
            elif not result_exists:
                execution_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM executions WHERE attempt_id = ?",
                        (attempt_id,),
                    ).fetchone()[0]
                )
                if execution_count >= contract.limits.max_executions_per_attempt:
                    terminal_reason = "execution_budget_exhausted"

            if terminal_reason is not None:
                classification = RecoveryClassification.TERMINAL
                attempt_target = (
                    AttemptState.FAILED.value
                    if terminal_reason == "task_deadline_exceeded"
                    else AttemptState.EXHAUSTED.value
                )
                task_cursor = connection.execute(
                    """
                    UPDATE phase3_tasks
                    SET state = 'failed', version = version + 1,
                        termination_reason = ?, updated_at = ?
                    WHERE task_id = ? AND version = ?
                      AND state NOT IN ('succeeded', 'failed', 'cancelled')
                    """,
                    (
                        terminal_reason,
                        ended_at,
                        task_id,
                        expected_task_version,
                    ),
                )
                attempt_cursor = connection.execute(
                    """
                    UPDATE phase3_attempts
                    SET state = ?, version = version + 1, ended_at = ?,
                        termination_reason = ?
                    WHERE attempt_id = ? AND version = ?
                      AND state NOT IN (
                        'completed', 'failed', 'exhausted',
                        'superseded', 'cancelled'
                      )
                    """,
                    (
                        attempt_target,
                        ended_at,
                        terminal_reason,
                        attempt_id,
                        expected_attempt_version,
                    ),
                )
                if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                    raise RuntimeError("Recovery terminal CAS failed")
            elif classification != RecoveryClassification.TERMINAL:
                operation_ids = self._reconciliation_operation_ids(
                    connection, task_id
                )
                if operation_ids:
                    classification = RecoveryClassification.RECONCILE
                    task_cursor = connection.execute(
                        """
                        UPDATE phase3_tasks
                        SET state = 'reconciling', version = version + 1,
                            updated_at = ?
                        WHERE task_id = ? AND version = ?
                          AND state NOT IN ('succeeded', 'failed', 'cancelled')
                        """,
                        (ended_at, task_id, expected_task_version),
                    )
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = 'reconciling', version = version + 1
                        WHERE attempt_id = ? AND version = ?
                          AND state NOT IN (
                            'completed', 'failed', 'exhausted',
                            'superseded', 'cancelled'
                          )
                        """,
                        (attempt_id, expected_attempt_version),
                    )
                    if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                        raise RuntimeError("Recovery reconciliation CAS failed")
                    reconciliation_id = self._ensure_recovery_reconciliation(
                        connection,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        source_identity=execution_id,
                    )
                elif result_exists:
                    classification = RecoveryClassification.GOVERNANCE_RESUME
                else:
                    run_request_id = self._insert_recovery_run_request(
                        connection,
                        command_id=command_id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        source_execution_id=execution_id,
                        session_handle=row["session_handle"] or checkpoint_session,
                        priority=int(row["priority"] or 0),
                        reason="restart_recovery",
                        references={"termination_reason": "process_lost"},
                    )

            refreshed_task = connection.execute(
                "SELECT state, version FROM phase3_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            refreshed_attempt = connection.execute(
                "SELECT state, version FROM phase3_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            checkpoint = self._write_checkpoint(
                connection,
                boundary=(
                    "task_terminal"
                    if classification == RecoveryClassification.TERMINAL
                    else "execution_recovered"
                ),
                task_id=task_id,
                attempt_id=attempt_id,
                execution_id=execution_id,
                run_request_id=run_request_id,
                references={
                    "command_id": command_id,
                    "classification": classification.value,
                    "reconciliation_id": reconciliation_id,
                    "source_checkpoint_id": source_checkpoint_id,
                },
            )
            result = ExecutionRecoveryResult(
                command_id=command_id,
                execution_id=execution_id,
                execution_status="interrupted",
                termination_reason="process_lost",
                classification=classification,
                task_id=task_id,
                task_state=str(refreshed_task["state"]),
                task_version=int(refreshed_task["version"]),
                attempt_id=attempt_id,
                attempt_state=str(refreshed_attempt["state"]),
                attempt_version=int(refreshed_attempt["version"]),
                run_request_id=run_request_id,
                reconciliation_id=reconciliation_id,
                checkpoint_id=checkpoint.checkpoint_id,
            )
            connection.execute(
                """
                INSERT INTO phase4_runtime_commands(
                    command_id, command_type, payload_hash, result_json, created_at
                ) VALUES (?, 'recover_execution', ?, ?, ?)
                """,
                (
                    command_id,
                    payload_hash,
                    result.model_dump_json(),
                    ended_at,
                ),
            )
            return result

    def _reconcile_operation_state(
        self,
        connection: sqlite3.Connection,
        operation: ExternalOperation,
        *,
        status: ExternalOperationStatus,
        external_operation_id: str | None = None,
        fact_type: str,
    ) -> ExternalOperation:
        updates: dict[str, Any] = {
            "status": status,
            "version": operation.version + 1,
        }
        if status == ExternalOperationStatus.PREPARED:
            updates.update(
                {
                    "external_operation_id": None,
                    "acknowledged_at": None,
                    "confirmed_at": None,
                }
            )
        if external_operation_id is not None:
            updates["external_operation_id"] = external_operation_id
        if status == ExternalOperationStatus.CONFIRMED:
            updates["confirmed_at"] = datetime.now(timezone.utc)
        changed = operation.model_copy(update=updates)
        cursor = connection.execute(
            """
            UPDATE phase3_external_operations
            SET status = ?, version = ?, operation_json = ?
            WHERE operation_id = ? AND version = ?
            """,
            (
                status.value,
                changed.version,
                changed.model_dump_json(),
                operation.operation_id,
                operation.version,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("ExternalOperation reconciliation CAS failed")
        self.governance_core._publish_operation_fact(
            connection,
            changed,
            fact_type=fact_type,
        )
        return changed

    def run_reconciliation(
        self,
        reconciliation_id: str,
        operation_observer: Callable[
            [ExternalOperation, CanonicalEffectRequest], ExternalOperationConfirmation
        ],
        *,
        lease_owner_id: str | None = None,
        lease_duration_seconds: float = 30.0,
    ) -> ReconciliationRunResult:
        """Query the business authority and close one durable reconciliation."""

        runner_version = "astra.reconciliation_runner@1"
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT reconciliation.*, task.contract_json,
                       task.state AS task_state, task.version AS task_version,
                       task.current_attempt_id,
                       attempt.state AS attempt_state,
                       attempt.version AS attempt_version
                FROM phase3_reconciliations AS reconciliation
                JOIN phase3_tasks AS task
                  ON task.task_id = reconciliation.task_id
                JOIN phase3_attempts AS attempt
                  ON attempt.attempt_id = reconciliation.attempt_id
                WHERE reconciliation.reconciliation_id = ?
                """,
                (reconciliation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(reconciliation_id)
            if row["status"] == "completed":
                result_payload = json.loads(row["result_json"] or "{}")
                return ReconciliationRunResult(
                    reconciliation_id=reconciliation_id,
                    task_id=str(row["task_id"]),
                    attempt_id=str(row["attempt_id"]),
                    status="completed",
                    operation_outcomes=dict(
                        result_payload.get("operation_outcomes", {})
                    ),
                    run_request_id=result_payload.get("run_request_id"),
                    policy_decision_id=result_payload.get("policy_decision_id"),
                    checkpoint_id=result_payload.get("checkpoint_id"),
                )
            if row["status"] not in {"pending", "running"}:
                raise RuntimeError("Reconciliation is not runnable")
            if row["current_attempt_id"] != row["attempt_id"]:
                raise RuntimeError("Reconciliation attempt is not current")
            started_at = datetime.now(timezone.utc).isoformat()
            cursor = connection.execute(
                """
                UPDATE phase3_reconciliations
                SET status = 'running', version = version + 1,
                    runner_version = ?, started_at = COALESCE(started_at, ?),
                    last_error_json = NULL
                WHERE reconciliation_id = ? AND version = ?
                  AND status IN ('pending', 'running')
                """,
                (
                    runner_version,
                    started_at,
                    reconciliation_id,
                    row["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Reconciliation claim CAS failed")

            operation_rows = connection.execute(
                """
                SELECT operation.operation_json, request.request_json
                FROM phase3_external_operations AS operation
                JOIN phase3_canonical_effect_requests AS request
                  ON request.effect_identity = operation.effect_identity
                WHERE operation.task_id = ?
                  AND operation.status IN (
                    'prepared', 'in_flight', 'acknowledged', 'indeterminate'
                  )
                ORDER BY operation.created_at, operation.operation_id
                """,
                (row["task_id"],),
            ).fetchall()
            outcomes: dict[str, str] = {}
            unresolved = False
            last_error: Mapping[str, Any] | None = None
            for operation_row in operation_rows:
                operation = ExternalOperation.model_validate_json(
                    operation_row["operation_json"]
                )
                canonical = CanonicalEffectRequest.model_validate_json(
                    operation_row["request_json"]
                )
                if not self._operation_requires_reconciliation(
                    connection,
                    operation.operation_id,
                    operation.status.value,
                ):
                    outcomes[operation.operation_id] = "confirmed_not_dispatched"
                    continue
                try:
                    confirmation = operation_observer(operation, canonical)
                except BaseException as exc:
                    unresolved = True
                    outcomes[operation.operation_id] = "authority_query_failed"
                    last_error = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "operation_id": operation.operation_id,
                    }
                    continue
                if not isinstance(confirmation, ExternalOperationConfirmation):
                    unresolved = True
                    outcomes[operation.operation_id] = "invalid_confirmation_result"
                    continue
                observation = confirmation.observation
                if observation is not None:
                    self.governance_core.record_business_observation(
                        operation.operation_id,
                        observation.payload,
                        external_object_id=observation.external_object_id,
                    )
                if confirmation.status == ConfirmationStatus.FAILED:
                    self._reconcile_operation_state(
                        connection,
                        operation,
                        status=ExternalOperationStatus.FAILED,
                        external_operation_id=(
                            observation.external_object_id
                            if observation is not None
                            else operation.external_operation_id
                        ),
                        fact_type="external_operation_failed",
                    )
                    outcomes[operation.operation_id] = (
                        confirmation.reason or "failed"
                    )
                    continue
                if confirmation.status != ConfirmationStatus.CONFIRMED:
                    if operation.status != ExternalOperationStatus.INDETERMINATE:
                        self._reconcile_operation_state(
                            connection,
                            operation,
                            status=ExternalOperationStatus.INDETERMINATE,
                            external_operation_id=(
                                observation.external_object_id
                                if observation is not None
                                else operation.external_operation_id
                            ),
                            fact_type="external_operation_indeterminate",
                        )
                    unresolved = True
                    outcomes[operation.operation_id] = (
                        confirmation.reason or confirmation.status.value
                    )
                    continue
                if observation is None:
                    unresolved = True
                    outcomes[operation.operation_id] = "observation_identity_missing"
                    continue
                self._reconcile_operation_state(
                    connection,
                    operation,
                    status=ExternalOperationStatus.CONFIRMED,
                    external_operation_id=observation.external_object_id,
                    fact_type="external_operation_confirmed",
                )
                outcomes[operation.operation_id] = "confirmed"

            if unresolved:
                payload = {
                    "operation_outcomes": outcomes,
                    "runner_version": runner_version,
                }
                connection.execute(
                    """
                    UPDATE phase3_reconciliations
                    SET status = 'pending', version = version + 1,
                        result_json = ?, last_error_json = ?
                    WHERE reconciliation_id = ? AND status = 'running'
                    """,
                    (
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        (
                            json.dumps(
                                last_error,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            if last_error is not None
                            else None
                        ),
                        reconciliation_id,
                    ),
                )
                checkpoint = self._write_checkpoint(
                    connection,
                    boundary="reconciliation_pending",
                    task_id=str(row["task_id"]),
                    attempt_id=str(row["attempt_id"]),
                    references={
                        "reconciliation_id": reconciliation_id,
                        "operation_outcomes": outcomes,
                    },
                )
                return ReconciliationRunResult(
                    reconciliation_id=reconciliation_id,
                    task_id=str(row["task_id"]),
                    attempt_id=str(row["attempt_id"]),
                    status="pending",
                    operation_outcomes=outcomes,
                    checkpoint_id=checkpoint.checkpoint_id,
                )

            completed_at = datetime.now(timezone.utc)
            contract = preflight_contract(row["contract_json"])
            result_execution = connection.execute(
                """
                SELECT execution.execution_id, execution.session_handle,
                       request.priority
                FROM executions AS execution
                JOIN phase4_execution_results AS result
                  ON result.execution_id = execution.execution_id
                LEFT JOIN phase4_run_requests AS request
                  ON request.run_request_id = execution.run_request_id
                WHERE execution.task_id = ? AND execution.attempt_id = ?
                ORDER BY result.created_at DESC, execution.execution_id DESC
                LIMIT 1
                """,
                (row["task_id"], row["attempt_id"]),
            ).fetchone()
            preserve_terminal = row["task_state"] in {
                TaskState.SUCCEEDED.value,
                TaskState.FAILED.value,
                TaskState.CANCELLED.value,
            } or row["attempt_state"] in {
                AttemptState.COMPLETED.value,
                AttemptState.FAILED.value,
                AttemptState.EXHAUSTED.value,
                AttemptState.SUPERSEDED.value,
                AttemptState.CANCELLED.value,
            }
            terminal_reason: str | None = None
            if preserve_terminal:
                result_execution = None
            elif completed_at >= _instant(contract.limits.task_deadline):
                terminal_reason = "task_deadline_exceeded"
            elif (
                contract.limits.attempt_deadline is not None
                and completed_at >= _instant(contract.limits.attempt_deadline)
            ):
                terminal_reason = "attempt_deadline_exceeded"
            elif result_execution is None:
                execution_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM executions WHERE attempt_id = ?",
                        (row["attempt_id"],),
                    ).fetchone()[0]
                )
                if execution_count >= contract.limits.max_executions_per_attempt:
                    terminal_reason = "execution_budget_exhausted"

            run_request_id: str | None = None
            lifecycle_cas_ok = True
            if preserve_terminal:
                pass
            elif terminal_reason is not None:
                attempt_target = (
                    AttemptState.FAILED.value
                    if terminal_reason == "task_deadline_exceeded"
                    else AttemptState.EXHAUSTED.value
                )
                task_cursor = connection.execute(
                    """
                    UPDATE phase3_tasks
                    SET state = 'failed', version = version + 1,
                        termination_reason = ?, updated_at = ?
                    WHERE task_id = ? AND version = ?
                      AND state NOT IN ('succeeded', 'failed', 'cancelled')
                    """,
                    (
                        terminal_reason,
                        completed_at.isoformat(),
                        row["task_id"],
                        row["task_version"],
                    ),
                )
                attempt_cursor = connection.execute(
                    """
                    UPDATE phase3_attempts
                    SET state = ?, version = version + 1, ended_at = ?,
                        termination_reason = ?
                    WHERE attempt_id = ? AND version = ?
                      AND state NOT IN (
                        'completed', 'failed', 'exhausted',
                        'superseded', 'cancelled'
                      )
                    """,
                    (
                        attempt_target,
                        completed_at.isoformat(),
                        terminal_reason,
                        row["attempt_id"],
                        row["attempt_version"],
                    ),
                )
                lifecycle_cas_ok = (
                    task_cursor.rowcount == 1 and attempt_cursor.rowcount == 1
                )
            else:
                task_cursor = connection.execute(
                    """
                    UPDATE phase3_tasks
                    SET state = 'running', version = version + 1, updated_at = ?
                    WHERE task_id = ? AND version = ?
                      AND state = 'reconciling'
                    """,
                    (
                        completed_at.isoformat(),
                        row["task_id"],
                        row["task_version"],
                    ),
                )
                attempt_cursor = connection.execute(
                    """
                    UPDATE phase3_attempts
                    SET state = 'active', version = version + 1
                    WHERE attempt_id = ? AND version = ?
                      AND state = 'reconciling'
                    """,
                    (row["attempt_id"], row["attempt_version"]),
                )
                if result_execution is None:
                    command_id = "reconciliation-complete:" + reconciliation_id
                    payload_hash = sha256_digest(
                        {
                            "command_type": "reconciliation_recovery",
                            "reconciliation_id": reconciliation_id,
                            "operation_outcomes": outcomes,
                        }
                    )
                    connection.execute(
                        """
                        INSERT INTO phase4_runtime_commands(
                            command_id, command_type, payload_hash,
                            result_json, created_at
                        ) VALUES (?, 'reconciliation_recovery', ?, ?, ?)
                        ON CONFLICT(command_id) DO NOTHING
                        """,
                        (
                            command_id,
                            payload_hash,
                            json.dumps(
                                {"reconciliation_id": reconciliation_id},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            completed_at.isoformat(),
                        ),
                    )
                    run_request_id = self._insert_recovery_run_request(
                        connection,
                        command_id=command_id,
                        task_id=str(row["task_id"]),
                        attempt_id=str(row["attempt_id"]),
                        source_execution_id=None,
                        session_handle=None,
                        priority=0,
                        reason="reconciliation_completed",
                        references={
                            "reconciliation_id": reconciliation_id,
                            "operation_outcomes": outcomes,
                        },
                    )
                lifecycle_cas_ok = (
                    task_cursor.rowcount == 1 and attempt_cursor.rowcount == 1
                )
            if not lifecycle_cas_ok:
                raise RuntimeError("Reconciliation lifecycle CAS failed")

            checkpoint = self._write_checkpoint(
                connection,
                boundary=(
                    "task_terminal"
                    if terminal_reason is not None or preserve_terminal
                    else "reconciliation_completed"
                ),
                task_id=str(row["task_id"]),
                attempt_id=str(row["attempt_id"]),
                run_request_id=run_request_id,
                references={
                    "reconciliation_id": reconciliation_id,
                    "operation_outcomes": outcomes,
                },
            )
            reconciliation_payload: dict[str, Any] = {
                "operation_outcomes": outcomes,
                "runner_version": runner_version,
                "run_request_id": run_request_id,
                "checkpoint_id": checkpoint.checkpoint_id,
            }
            reconciliation_cursor = connection.execute(
                """
                UPDATE phase3_reconciliations
                SET status = 'completed', version = version + 1,
                    completed_at = ?, result_json = ?, last_error_json = NULL
                WHERE reconciliation_id = ? AND status = 'running'
                """,
                (
                    completed_at.isoformat(),
                    json.dumps(
                        reconciliation_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    reconciliation_id,
                ),
            )
            if reconciliation_cursor.rowcount != 1:
                raise RuntimeError("Reconciliation completion CAS failed")
            result_execution_id = (
                str(result_execution["execution_id"])
                if result_execution is not None
                and terminal_reason is None
                and not preserve_terminal
                else None
            )

        policy_decision_id: str | None = None
        if result_execution_id is not None:
            evaluation = self.governance_core.evaluate(result_execution_id)
            policy_decision_id = evaluation.policy_decision.decision_id
            recovery_owner = (
                lease_owner_id
                or "worker:reconciliation:" + reconciliation_id
            )
            lease_token = self._acquire_recovery_execution_lease(
                execution_id=result_execution_id,
                lease_owner_id=recovery_owner,
                lease_duration_seconds=lease_duration_seconds,
            )
            if lease_token is not None:
                application = self.apply_policy_decision(
                    policy_decision_id,
                    execution_id=result_execution_id,
                    lease_owner_id=recovery_owner,
                    lease_token=lease_token,
                )
                if application.application != DecisionApplication.APPLIED:
                    self.finalize_late_execution_result(
                        result_execution_id,
                        lease_owner_id=recovery_owner,
                        lease_token=lease_token,
                    )
            with self.store.transaction() as connection:
                stored = connection.execute(
                    """
                    SELECT result_json FROM phase3_reconciliations
                    WHERE reconciliation_id = ?
                    """,
                    (reconciliation_id,),
                ).fetchone()
                persisted_payload = json.loads(stored["result_json"] or "{}")
                persisted_payload["policy_decision_id"] = policy_decision_id
                connection.execute(
                    """
                    UPDATE phase3_reconciliations SET result_json = ?
                    WHERE reconciliation_id = ? AND status = 'completed'
                    """,
                    (
                        json.dumps(
                            persisted_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        reconciliation_id,
                    ),
                )
        return ReconciliationRunResult(
            reconciliation_id=reconciliation_id,
            task_id=str(row["task_id"]),
            attempt_id=str(row["attempt_id"]),
            status="completed",
            operation_outcomes=outcomes,
            run_request_id=run_request_id,
            policy_decision_id=policy_decision_id,
            checkpoint_id=checkpoint.checkpoint_id,
        )

    def _recover_resolved_interactions(self) -> tuple[str, ...]:
        rows = self.store.query_all(
            """
            SELECT interaction.interaction_id
            FROM interactions AS interaction
            JOIN phase3_tasks AS task ON task.task_id = interaction.task_id
            WHERE interaction.status = 'resolved'
              AND task.state NOT IN ('succeeded', 'failed', 'cancelled')
              AND NOT EXISTS (
                SELECT 1 FROM phase4_run_requests AS request
                WHERE request.source_interaction_id = interaction.interaction_id
              )
            ORDER BY interaction.resolved_at, interaction.interaction_id
            """
        )
        recovered: list[str] = []
        for scan_row in rows:
            interaction_id = str(scan_row["interaction_id"])
            command_id = "recover-interaction:" + interaction_id
            with self.store.transaction() as connection:
                row = connection.execute(
                    """
                    SELECT interaction.*, task.contract_json,
                           task.state AS task_state,
                           task.version AS task_version,
                           task.current_attempt_id,
                           attempt.state AS attempt_state,
                           attempt.version AS attempt_version,
                           execution.session_handle
                    FROM interactions AS interaction
                    JOIN phase3_tasks AS task
                      ON task.task_id = interaction.task_id
                    JOIN phase3_attempts AS attempt
                      ON attempt.attempt_id = interaction.attempt_id
                    LEFT JOIN executions AS execution
                      ON execution.execution_id = interaction.execution_id
                    WHERE interaction.interaction_id = ?
                    """,
                    (interaction_id,),
                ).fetchone()
                if (
                    row is None
                    or row["status"] != "resolved"
                    or row["current_attempt_id"] != row["attempt_id"]
                    or row["task_state"] in {"succeeded", "failed", "cancelled"}
                    or connection.execute(
                        """
                        SELECT 1 FROM phase4_run_requests
                        WHERE source_interaction_id = ?
                        """,
                        (interaction_id,),
                    ).fetchone()
                    is not None
                ):
                    continue
                now = datetime.now(timezone.utc)
                try:
                    contract = preflight_contract(row["contract_json"])
                except CatalogIntegrityError:
                    self._terminate_contract_runtime_incompatible(
                        connection,
                        task_id=str(row["task_id"]),
                        attempt_id=str(row["attempt_id"]),
                        now=now,
                    )
                    self._write_checkpoint(
                        connection,
                        boundary="task_terminal",
                        task_id=str(row["task_id"]),
                        attempt_id=str(row["attempt_id"]),
                        references={
                            "interaction_id": interaction_id,
                            "termination_reason": "contract_runtime_incompatible",
                        },
                    )
                    recovered.append(interaction_id)
                    continue
                execution_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM executions WHERE attempt_id = ?",
                        (row["attempt_id"],),
                    ).fetchone()[0]
                )
                terminal_reason: str | None = None
                if now >= _instant(contract.limits.task_deadline):
                    terminal_reason = "task_deadline_exceeded"
                elif (
                    contract.limits.attempt_deadline is not None
                    and now >= _instant(contract.limits.attempt_deadline)
                ):
                    terminal_reason = "attempt_deadline_exceeded"
                elif execution_count >= contract.limits.max_executions_per_attempt:
                    terminal_reason = "execution_budget_exhausted"
                if terminal_reason is not None:
                    attempt_target = (
                        AttemptState.FAILED.value
                        if terminal_reason == "task_deadline_exceeded"
                        else AttemptState.EXHAUSTED.value
                    )
                    task_cursor = connection.execute(
                        """
                        UPDATE phase3_tasks
                        SET state = 'failed', version = version + 1,
                            termination_reason = ?, updated_at = ?
                        WHERE task_id = ? AND version = ?
                          AND state NOT IN ('succeeded', 'failed', 'cancelled')
                        """,
                        (
                            terminal_reason,
                            now.isoformat(),
                            row["task_id"],
                            row["task_version"],
                        ),
                    )
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = ?, version = version + 1, ended_at = ?,
                            termination_reason = ?
                        WHERE attempt_id = ? AND version = ?
                          AND state NOT IN (
                            'completed', 'failed', 'exhausted',
                            'superseded', 'cancelled'
                          )
                        """,
                        (
                            attempt_target,
                            now.isoformat(),
                            terminal_reason,
                            row["attempt_id"],
                            row["attempt_version"],
                        ),
                    )
                    if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                        raise RuntimeError("Interaction recovery terminal CAS failed")
                    self._write_checkpoint(
                        connection,
                        boundary="task_terminal",
                        task_id=str(row["task_id"]),
                        attempt_id=str(row["attempt_id"]),
                        references={
                            "interaction_id": interaction_id,
                            "termination_reason": terminal_reason,
                        },
                    )
                    recovered.append(interaction_id)
                    continue
                if row["task_state"] in {
                    TaskState.WAITING_INPUT.value,
                    TaskState.WAITING_APPROVAL.value,
                }:
                    task_cursor = connection.execute(
                        """
                        UPDATE phase3_tasks
                        SET state = 'running', version = version + 1,
                            updated_at = ?
                        WHERE task_id = ? AND version = ?
                          AND state IN ('waiting_input', 'waiting_approval')
                        """,
                        (
                            now.isoformat(),
                            row["task_id"],
                            row["task_version"],
                        ),
                    )
                    if task_cursor.rowcount != 1:
                        raise RuntimeError("Interaction recovery Task CAS failed")
                if row["attempt_state"] == AttemptState.WAITING.value:
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = 'active', version = version + 1
                        WHERE attempt_id = ? AND version = ? AND state = 'waiting'
                        """,
                        (row["attempt_id"], row["attempt_version"]),
                    )
                    if attempt_cursor.rowcount != 1:
                        raise RuntimeError("Interaction recovery Attempt CAS failed")
                payload_hash = sha256_digest(
                    {
                        "command_type": "recover_interaction",
                        "interaction_id": interaction_id,
                        "interaction_version": int(row["version"]),
                    }
                )
                connection.execute(
                    """
                    INSERT INTO phase4_runtime_commands(
                        command_id, command_type, payload_hash,
                        result_json, created_at
                    ) VALUES (?, 'recover_interaction', ?, ?, ?)
                    ON CONFLICT(command_id) DO NOTHING
                    """,
                    (
                        command_id,
                        payload_hash,
                        json.dumps(
                            {"interaction_id": interaction_id},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now.isoformat(),
                    ),
                )
                run_request_id = self._insert_recovery_run_request(
                    connection,
                    command_id=command_id,
                    task_id=str(row["task_id"]),
                    attempt_id=str(row["attempt_id"]),
                    source_execution_id=row["execution_id"],
                    session_handle=row["session_handle"],
                    priority=0,
                    reason="interaction_resolved_recovery",
                    references={
                        "interaction_id": interaction_id,
                        "resolution": json.loads(row["resolution_json"] or "{}"),
                    },
                    source_interaction_id=interaction_id,
                )
                self._write_checkpoint(
                    connection,
                    boundary="interaction_resolved",
                    task_id=str(row["task_id"]),
                    attempt_id=str(row["attempt_id"]),
                    run_request_id=run_request_id,
                    references={
                        "interaction_id": interaction_id,
                        "recovery_command_id": command_id,
                    },
                )
                recovered.append(interaction_id)
        return tuple(recovered)

    def _recover_running_tasks_without_work(self) -> tuple[str, ...]:
        rows = self.store.query_all(
            """
            SELECT task.task_id
            FROM phase3_tasks AS task
            JOIN phase3_attempts AS attempt
              ON attempt.attempt_id = task.current_attempt_id
            WHERE task.state = 'running' AND attempt.state = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM phase4_run_requests AS request
                WHERE request.task_id = task.task_id
                  AND request.state IN ('pending', 'claimed')
              )
              AND NOT EXISTS (
                SELECT 1 FROM interactions AS interaction
                WHERE interaction.task_id = task.task_id
                  AND interaction.status = 'pending'
              )
              AND NOT EXISTS (
                SELECT 1 FROM phase3_reconciliations AS reconciliation
                WHERE reconciliation.task_id = task.task_id
                  AND reconciliation.status IN ('pending', 'running')
              )
              AND NOT EXISTS (
                SELECT 1 FROM phase3_policy_decisions AS decision
                WHERE decision.task_id = task.task_id
                  AND decision.application_status = 'pending'
              )
            ORDER BY task.created_at, task.task_id
            """
        )
        recovered: list[str] = []
        for scan_row in rows:
            task_id = str(scan_row["task_id"])
            eligibility = self.store.query_one(
                """
                SELECT task.contract_json, task.version AS task_version,
                       task.current_attempt_id,
                       attempt.version AS attempt_version
                FROM phase3_tasks AS task
                JOIN phase3_attempts AS attempt
                  ON attempt.attempt_id = task.current_attempt_id
                WHERE task.task_id = ? AND task.state = 'running'
                  AND attempt.state = 'active'
                """,
                (task_id,),
            )
            if eligibility is None:
                continue
            current_time = datetime.now(timezone.utc)
            try:
                contract = preflight_contract(eligibility["contract_json"])
            except CatalogIntegrityError:
                with self.store.transaction() as connection:
                    self._terminate_contract_runtime_incompatible(
                        connection,
                        task_id=task_id,
                        attempt_id=str(eligibility["current_attempt_id"]),
                        now=current_time,
                    )
                    self._write_checkpoint(
                        connection,
                        boundary="task_terminal",
                        task_id=task_id,
                        attempt_id=str(eligibility["current_attempt_id"]),
                        references={
                            "termination_reason": "contract_runtime_incompatible"
                        },
                    )
                recovered.append(task_id)
                continue
            deadline_reason: str | None = None
            if current_time >= _instant(contract.limits.task_deadline):
                deadline_reason = "task_deadline_exceeded"
            elif (
                contract.limits.attempt_deadline is not None
                and current_time >= _instant(contract.limits.attempt_deadline)
            ):
                deadline_reason = "attempt_deadline_exceeded"
            if deadline_reason is not None:
                with self.store.transaction() as connection:
                    attempt_target = (
                        AttemptState.FAILED.value
                        if deadline_reason == "task_deadline_exceeded"
                        else AttemptState.EXHAUSTED.value
                    )
                    task_cursor = connection.execute(
                        """
                        UPDATE phase3_tasks
                        SET state = 'failed', version = version + 1,
                            termination_reason = ?, updated_at = ?
                        WHERE task_id = ? AND version = ? AND state = 'running'
                        """,
                        (
                            deadline_reason,
                            current_time.isoformat(),
                            task_id,
                            eligibility["task_version"],
                        ),
                    )
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = ?, version = version + 1, ended_at = ?,
                            termination_reason = ?
                        WHERE attempt_id = ? AND version = ? AND state = 'active'
                        """,
                        (
                            attempt_target,
                            current_time.isoformat(),
                            deadline_reason,
                            eligibility["current_attempt_id"],
                            eligibility["attempt_version"],
                        ),
                    )
                    if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                        raise RuntimeError("Missing-work deadline CAS failed")
                    self._write_checkpoint(
                        connection,
                        boundary="task_terminal",
                        task_id=task_id,
                        attempt_id=str(eligibility["current_attempt_id"]),
                        references={"termination_reason": deadline_reason},
                    )
                recovered.append(task_id)
                continue
            result_row = self.store.query_one(
                """
                SELECT execution.execution_id
                FROM executions AS execution
                JOIN phase4_execution_results AS result
                  ON result.execution_id = execution.execution_id
                JOIN phase3_tasks AS task
                  ON task.current_attempt_id = execution.attempt_id
                 AND task.task_id = execution.task_id
                WHERE execution.task_id = ?
                ORDER BY result.created_at DESC, execution.execution_id DESC
                LIMIT 1
                """,
                (task_id,),
            )
            if result_row is not None:
                result_execution_id = str(result_row["execution_id"])
                recovery_owner = "worker:missing-work:" + task_id
                lease_token = self._acquire_recovery_execution_lease(
                    execution_id=result_execution_id,
                    lease_owner_id=recovery_owner,
                    lease_duration_seconds=30.0,
                )
                if lease_token is not None:
                    evaluation = self.governance_core.evaluate(
                        result_execution_id
                    )
                    application = self.apply_policy_decision(
                        evaluation.policy_decision.decision_id,
                        execution_id=result_execution_id,
                        lease_owner_id=recovery_owner,
                        lease_token=lease_token,
                    )
                    if application.application != DecisionApplication.APPLIED:
                        self.finalize_late_execution_result(
                            result_execution_id,
                            lease_owner_id=recovery_owner,
                            lease_token=lease_token,
                        )
                    recovered.append(task_id)
                continue
            with self.store.transaction() as connection:
                row = connection.execute(
                    """
                    SELECT task.*, attempt.state AS attempt_state,
                           attempt.version AS attempt_version
                    FROM phase3_tasks AS task
                    JOIN phase3_attempts AS attempt
                      ON attempt.attempt_id = task.current_attempt_id
                    WHERE task.task_id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if (
                    row is None
                    or row["state"] != TaskState.RUNNING.value
                    or row["attempt_state"] != AttemptState.ACTIVE.value
                    or connection.execute(
                        """
                        SELECT 1 FROM phase4_run_requests
                        WHERE task_id = ? AND state IN ('pending', 'claimed')
                        """,
                        (task_id,),
                    ).fetchone()
                    is not None
                ):
                    continue
                unsafe_operations = self._reconciliation_operation_ids(
                    connection, task_id
                )
                if unsafe_operations:
                    task_cursor = connection.execute(
                        """
                        UPDATE phase3_tasks
                        SET state = 'reconciling', version = version + 1,
                            updated_at = ?
                        WHERE task_id = ? AND version = ? AND state = 'running'
                        """,
                        (
                            datetime.now(timezone.utc).isoformat(),
                            task_id,
                            row["version"],
                        ),
                    )
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = 'reconciling', version = version + 1
                        WHERE attempt_id = ? AND version = ? AND state = 'active'
                        """,
                        (row["current_attempt_id"], row["attempt_version"]),
                    )
                    if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                        raise RuntimeError("Missing-work reconciliation CAS failed")
                    self._ensure_recovery_reconciliation(
                        connection,
                        task_id=task_id,
                        attempt_id=str(row["current_attempt_id"]),
                        source_identity="running-task-missing-work",
                    )
                    recovered.append(task_id)
                    continue
                contract = preflight_contract(row["contract_json"])
                now = datetime.now(timezone.utc)
                execution_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM executions WHERE attempt_id = ?",
                        (row["current_attempt_id"],),
                    ).fetchone()[0]
                )
                terminal_reason: str | None = None
                if now >= _instant(contract.limits.task_deadline):
                    terminal_reason = "task_deadline_exceeded"
                elif (
                    contract.limits.attempt_deadline is not None
                    and now >= _instant(contract.limits.attempt_deadline)
                ):
                    terminal_reason = "attempt_deadline_exceeded"
                elif execution_count >= contract.limits.max_executions_per_attempt:
                    terminal_reason = "execution_budget_exhausted"
                if terminal_reason is not None:
                    attempt_target = (
                        AttemptState.FAILED.value
                        if terminal_reason == "task_deadline_exceeded"
                        else AttemptState.EXHAUSTED.value
                    )
                    task_cursor = connection.execute(
                        """
                        UPDATE phase3_tasks
                        SET state = 'failed', version = version + 1,
                            termination_reason = ?, updated_at = ?
                        WHERE task_id = ? AND version = ? AND state = 'running'
                        """,
                        (
                            terminal_reason,
                            now.isoformat(),
                            task_id,
                            row["version"],
                        ),
                    )
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = ?, version = version + 1, ended_at = ?,
                            termination_reason = ?
                        WHERE attempt_id = ? AND version = ? AND state = 'active'
                        """,
                        (
                            attempt_target,
                            now.isoformat(),
                            terminal_reason,
                            row["current_attempt_id"],
                            row["attempt_version"],
                        ),
                    )
                    if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                        raise RuntimeError("Missing-work terminal CAS failed")
                    self._write_checkpoint(
                        connection,
                        boundary="task_terminal",
                        task_id=task_id,
                        attempt_id=str(row["current_attempt_id"]),
                        references={"termination_reason": terminal_reason},
                    )
                    recovered.append(task_id)
                    continue
                last_execution = connection.execute(
                    """
                    SELECT execution_id, session_handle
                    FROM executions
                    WHERE task_id = ? AND attempt_id = ?
                    ORDER BY started_at DESC, execution_id DESC
                    LIMIT 1
                    """,
                    (task_id, row["current_attempt_id"]),
                ).fetchone()
                command_id = (
                    "recover-running-task:"
                    + sha256_digest(
                        {
                            "task_id": task_id,
                            "task_version": int(row["version"]),
                            "attempt_version": int(row["attempt_version"]),
                        }
                    )
                )
                payload_hash = sha256_digest(
                    {
                        "command_type": "recover_running_task",
                        "task_id": task_id,
                        "task_version": int(row["version"]),
                        "attempt_version": int(row["attempt_version"]),
                    }
                )
                connection.execute(
                    """
                    INSERT INTO phase4_runtime_commands(
                        command_id, command_type, payload_hash,
                        result_json, created_at
                    ) VALUES (?, 'recover_running_task', ?, ?, ?)
                    ON CONFLICT(command_id) DO NOTHING
                    """,
                    (
                        command_id,
                        payload_hash,
                        json.dumps({"task_id": task_id}, sort_keys=True),
                        now.isoformat(),
                    ),
                )
                run_request_id = self._insert_recovery_run_request(
                    connection,
                    command_id=command_id,
                    task_id=task_id,
                    attempt_id=str(row["current_attempt_id"]),
                    source_execution_id=(
                        str(last_execution["execution_id"])
                        if last_execution is not None
                        else None
                    ),
                    session_handle=(
                        last_execution["session_handle"]
                        if last_execution is not None
                        else None
                    ),
                    priority=0,
                    reason="running_task_missing_work",
                    references={"scanner": "startup"},
                )
                self._write_checkpoint(
                    connection,
                    boundary="running_task_recovered",
                    task_id=task_id,
                    attempt_id=str(row["current_attempt_id"]),
                    run_request_id=run_request_id,
                    references={"command_id": command_id},
                )
                recovered.append(task_id)
        return tuple(recovered)

    def startup_recover(
        self,
        operation_observer: Callable[
            [ExternalOperation, CanonicalEffectRequest], ExternalOperationConfirmation
        ],
        *,
        lease_owner_id: str | None = None,
        lease_duration_seconds: float = 30.0,
    ) -> StartupRecoveryResult:
        """Scan and repair every S-12 restart category at Production startup."""

        recovery_owner = lease_owner_id or "worker:startup:" + str(uuid4())
        # A persisted Decision is the authoritative next lifecycle transition.
        # Once its former lease has expired, apply it before generic orphan
        # recovery changes the Task/Attempt state and invalidates its fixed
        # DecisionContext.  The recovery helper still acquires a fresh fencing
        # token, so an active owner is never preempted.
        applied = self.recover_pending_policy_decisions(
            lease_owner_id=recovery_owner,
            lease_duration_seconds=lease_duration_seconds,
        )
        recovered_executions = list(
            self.recover_expired_leases(
                lease_owner_id=recovery_owner,
                lease_duration_seconds=lease_duration_seconds,
                operation_observer=operation_observer,
            )
        )
        orphan_ids = tuple(
            str(row["execution_id"])
            for row in self.store.query_all(
                """
                SELECT execution.execution_id
                FROM executions AS execution
                WHERE execution.status = 'running'
                  AND execution.ended_at IS NULL
                  AND execution.run_request_id IS NULL
                ORDER BY execution.started_at, execution.execution_id
                """
            )
        )
        for execution_id in orphan_ids:
            versions = self.store.query_one(
                """
                SELECT task.version AS task_version,
                       attempt.version AS attempt_version
                FROM executions AS execution
                JOIN phase3_tasks AS task ON task.task_id = execution.task_id
                JOIN phase3_attempts AS attempt
                  ON attempt.attempt_id = execution.attempt_id
                WHERE execution.execution_id = ?
                  AND execution.status = 'running'
                  AND execution.ended_at IS NULL
                """,
                (execution_id,),
            )
            if versions is None:
                continue
            recovered_executions.append(
                self.recover_execution(
                    command_id="recover-execution:" + execution_id,
                    execution_id=execution_id,
                    expected_task_version=int(versions["task_version"]),
                    expected_attempt_version=int(versions["attempt_version"]),
                )
            )

        recovered_interactions = self._recover_resolved_interactions()

        reconciliation_results: list[ReconciliationRunResult] = []
        reconciliation_ids = tuple(
            str(row["reconciliation_id"])
            for row in self.store.query_all(
                """
                SELECT reconciliation_id FROM phase3_reconciliations
                WHERE status IN ('pending', 'running')
                ORDER BY created_at, reconciliation_id
                """
            )
        )
        for reconciliation_id in reconciliation_ids:
            reconciliation_results.append(
                self.run_reconciliation(
                    reconciliation_id,
                    operation_observer,
                    lease_owner_id=recovery_owner,
                    lease_duration_seconds=lease_duration_seconds,
                )
            )

        recovered_tasks = self._recover_running_tasks_without_work()
        pending_outbox_ids = tuple(
            str(row["outbox_id"])
            for row in self.store.query_all(
                """
                SELECT outbox_id FROM phase3_outbox
                WHERE delivery_status = 'pending'
                ORDER BY created_at, outbox_id
                """
            )
        )
        unresolved_operation_ids = tuple(
            str(row["operation_id"])
            for row in self.store.query_all(
                """
                SELECT operation_id FROM phase3_external_operations
                WHERE status IN (
                    'prepared', 'in_flight', 'acknowledged', 'indeterminate'
                )
                ORDER BY created_at, operation_id
                """
            )
        )
        return StartupRecoveryResult(
            recovered_executions=tuple(recovered_executions),
            recovered_interaction_ids=recovered_interactions,
            applied_policy_decision_ids=tuple(
                result.decision_id for result in applied
            ),
            reconciliation_results=tuple(reconciliation_results),
            recovered_running_task_ids=recovered_tasks,
            pending_outbox_ids=pending_outbox_ids,
            unresolved_operation_ids=unresolved_operation_ids,
        )

    @staticmethod
    def _interaction_watermark(
        connection: sqlite3.Connection, task_id: str
    ) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) FROM interactions
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        return int(row[0])

    def _apply_policy_action(
        self,
        connection: sqlite3.Connection,
        decision: PolicyDecision,
        task: sqlite3.Row,
        attempt: sqlite3.Row,
        *,
        authority_execution_id: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> str | None:
        self._assert_policy_work_limits(connection, decision, task)
        action = decision.action
        task_state = TaskState(task["state"])
        attempt_state = AttemptState(attempt["state"])
        current_work = self._finish_current_runtime_work(
            connection,
            decision,
            authority_execution_id=authority_execution_id,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
        )
        derived_record_id: str | None = None
        escalation = int(task["escalation_required"])
        ended_at = None
        termination_reason: str | None = None
        if action == PolicyAction.COMPLETE:
            task_state = TaskState.SUCCEEDED
            attempt_state = AttemptState.COMPLETED
            ended_at = datetime.now(timezone.utc).isoformat()
        elif action == PolicyAction.REQUEST_INPUT:
            task_state = TaskState.WAITING_INPUT
            attempt_state = AttemptState.WAITING
            derived_record_id = self._create_policy_interaction(
                connection,
                decision,
                task,
                kind="user_input",
                execution_id=(current_work or {}).get("execution_id"),
            )
        elif action == PolicyAction.REQUEST_APPROVAL:
            task_state = TaskState.WAITING_APPROVAL
            attempt_state = AttemptState.WAITING
            derived_record_id = self._create_policy_interaction(
                connection,
                decision,
                task,
                kind="approval",
                execution_id=(current_work or {}).get("execution_id"),
            )
        elif action == PolicyAction.RECONCILE:
            task_state = TaskState.RECONCILING
            attempt_state = AttemptState.RECONCILING
            derived_record_id = self._create_reconciliation(connection, decision)
        elif action in {PolicyAction.FAIL, PolicyAction.ESCALATE}:
            task_state = TaskState.FAILED
            attempt_state = AttemptState.FAILED
            escalation = int(action == PolicyAction.ESCALATE)
            ended_at = datetime.now(timezone.utc).isoformat()
            termination_reason = decision.reason_code
        elif action == PolicyAction.CONTINUE_WITH_FEEDBACK:
            task_state = TaskState.RUNNING
            attempt_state = AttemptState.ACTIVE
            derived_record_id = self._create_policy_run_request(
                connection,
                decision,
                attempt_id=decision.attempt_id,
                reason="continue_with_feedback",
                session_handle=(current_work or {}).get("session_handle"),
                priority=int((current_work or {}).get("priority", 0)),
            )
        elif action == PolicyAction.START_NEW_ATTEMPT:
            now = datetime.now(timezone.utc).isoformat()
            old_attempt_cursor = connection.execute(
                """
                UPDATE phase3_attempts
                SET state = 'superseded', version = version + 1, ended_at = ?,
                    termination_reason = ?
                WHERE attempt_id = ? AND version = ? AND state = 'active'
                """,
                (
                    now,
                    decision.reason_code,
                    attempt["attempt_id"],
                    attempt["version"],
                ),
            )
            if old_attempt_cursor.rowcount != 1:
                raise RuntimeError("Attempt supersede CAS failed")
            new_attempt_id = "attempt:" + decision.decision_id
            ordinal = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(ordinal), 0) + 1
                    FROM phase3_attempts WHERE task_id = ?
                    """,
                    (decision.task_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO phase3_attempts(
                    attempt_id, task_id, ordinal, state, version,
                    created_by_decision_id, created_at
                ) VALUES (?, ?, ?, 'active', 1, ?, ?)
                """,
                (
                    new_attempt_id,
                    decision.task_id,
                    ordinal,
                    decision.decision_id,
                    now,
                ),
            )
            task_cursor = connection.execute(
                """
                UPDATE phase3_tasks
                SET state = 'running', version = version + 1,
                    current_attempt_id = ?, updated_at = ?
                WHERE task_id = ? AND version = ?
                """,
                (
                    new_attempt_id,
                    now,
                    decision.task_id,
                    task["version"],
                ),
            )
            if task_cursor.rowcount != 1:
                raise RuntimeError("Task new-attempt CAS failed")
            derived_record_id = self._create_policy_run_request(
                connection,
                decision,
                attempt_id=new_attempt_id,
                reason="new_attempt",
                session_handle=None,
                priority=int((current_work or {}).get("priority", 0)),
            )
            self._publish_policy_state_fact(
                connection,
                decision,
                task_state=TaskState.RUNNING,
                attempt_id=new_attempt_id,
                task_version=int(task["version"]) + 1,
            )
            return derived_record_id

        task_cursor = connection.execute(
            """
            UPDATE phase3_tasks
            SET state = ?, version = version + 1,
                escalation_required = ?, termination_reason = ?, updated_at = ?
            WHERE task_id = ? AND version = ?
            """,
            (
                task_state.value,
                escalation,
                termination_reason,
                datetime.now(timezone.utc).isoformat(),
                decision.task_id,
                task["version"],
            ),
        )
        attempt_cursor = connection.execute(
            """
            UPDATE phase3_attempts
            SET state = ?, version = version + 1,
                ended_at = COALESCE(?, ended_at),
                termination_reason = COALESCE(?, termination_reason)
            WHERE attempt_id = ? AND version = ?
            """,
            (
                attempt_state.value,
                ended_at,
                termination_reason,
                attempt["attempt_id"],
                attempt["version"],
            ),
        )
        if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
            raise RuntimeError("Policy lifecycle CAS failed")
        self._publish_policy_state_fact(
            connection,
            decision,
            task_state=task_state,
            attempt_id=str(attempt["attempt_id"]),
            task_version=int(task["version"]) + 1,
        )
        return derived_record_id

    @staticmethod
    def _assert_policy_work_limits(
        connection: sqlite3.Connection,
        decision: PolicyDecision,
        task: sqlite3.Row,
    ) -> None:
        """Defensively recheck frozen limits at the authoritative create point."""

        action = decision.action
        if action not in {
            PolicyAction.CONTINUE_WITH_FEEDBACK,
            PolicyAction.START_NEW_ATTEMPT,
        }:
            return
        contract = preflight_contract(task["contract_json"])
        limits = contract.limits

        if action == PolicyAction.START_NEW_ATTEMPT:
            attempt_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM phase3_attempts WHERE task_id = ?",
                    (decision.task_id,),
                ).fetchone()[0]
            )
            if attempt_count >= limits.max_attempts:
                raise RuntimeError("runtime_limit_violation:max_attempts")
            return

        if action == PolicyAction.CONTINUE_WITH_FEEDBACK:
            execution_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM executions WHERE attempt_id = ?",
                    (decision.attempt_id,),
                ).fetchone()[0]
            )
            feedback_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM phase4_run_requests
                    WHERE attempt_id = ? AND reason = 'continue_with_feedback'
                    """,
                    (decision.attempt_id,),
                ).fetchone()[0]
            )
            if execution_count >= limits.max_executions_per_attempt:
                raise RuntimeError(
                    "runtime_limit_violation:max_executions_per_attempt"
                )
            if feedback_count >= limits.max_feedback_cycles:
                raise RuntimeError("runtime_limit_violation:max_feedback_cycles")

    @staticmethod
    def _finish_current_runtime_work(
        connection: sqlite3.Connection,
        decision: PolicyDecision,
        *,
        authority_execution_id: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> Mapping[str, Any] | None:
        rows = connection.execute(
            """
            SELECT execution.execution_id, execution.run_request_id,
                   execution.session_handle, request.priority,
                   result.result_json
            FROM executions AS execution
            JOIN phase4_execution_results AS result
              ON result.execution_id = execution.execution_id
            LEFT JOIN phase4_run_requests AS request
              ON request.run_request_id = execution.run_request_id
            WHERE execution.task_id = ? AND execution.attempt_id = ?
              AND execution.execution_id = ?
              AND execution.ended_at IS NULL
            ORDER BY execution.started_at DESC, execution.execution_id DESC
            """,
            (decision.task_id, decision.attempt_id, authority_execution_id),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("Task has multiple active authoritative Executions")
        if not rows:
            return None
        row = rows[0]
        result = ExecutionResult.model_validate_json(row["result_json"])
        ended_at = datetime.now(timezone.utc).isoformat()
        cursor = connection.execute(
            """
            UPDATE executions
            SET status = ?, ended_at = ?, termination_reason = ?
            WHERE execution_id = ? AND ended_at IS NULL
              AND EXISTS (
                SELECT 1 FROM phase4_run_requests AS request
                WHERE request.run_request_id = executions.run_request_id
                  AND request.lease_owner_id = ?
                  AND request.lease_token = ?
                  AND request.lease_expires_at > ?
                  AND request.state IN ('claimed', 'completed')
              )
            """,
            (
                result.status.value,
                ended_at,
                result.termination_reason,
                row["execution_id"],
                lease_owner_id,
                lease_token,
                ended_at,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Execution finalization CAS failed")
        connection.execute(
            """
            INSERT INTO execution_events(
                event_id, execution_id, event_type, payload_json, created_at
            ) VALUES (?, ?, 'AstraExecutionEnded', ?, ?)
            """,
            (
                "event:" + decision.decision_id,
                row["execution_id"],
                json.dumps(
                    {
                        "governance_decision_id": decision.decision_id,
                        "policy_action": decision.action.value,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                ended_at,
            ),
        )
        if row["run_request_id"] is not None:
            request_cursor = connection.execute(
                """
                UPDATE phase4_run_requests SET state = 'completed'
                WHERE run_request_id = ? AND state = 'claimed'
                  AND lease_owner_id = ? AND lease_token = ?
                  AND lease_expires_at > ?
                """,
                (
                    row["run_request_id"],
                    lease_owner_id,
                    lease_token,
                    ended_at,
                ),
            )
            if request_cursor.rowcount != 1:
                raise RuntimeError("Run Request completion CAS failed")
        return dict(row)

    @staticmethod
    def _create_policy_interaction(
        connection: sqlite3.Connection,
        decision: PolicyDecision,
        task: sqlite3.Row,
        *,
        kind: str,
        execution_id: str | None,
    ) -> str:
        interaction_id = "interaction:" + decision.decision_id
        spec = decision.interaction_spec
        if spec is None or spec.kind != kind:
            raise RuntimeError("Policy interaction is missing its frozen spec")
        purpose = (
            InteractionPurpose.APPROVAL.value
            if kind == InteractionKind.APPROVAL.value
            else InteractionPurpose.CLARIFICATION.value
        )
        connection.execute(
            """
            INSERT INTO interactions(
                interaction_id, execution_id, task_id, attempt_id, kind,
                purpose, prompt, status, version, payload_json,
                created_by_decision_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?)
            """,
            (
                interaction_id,
                execution_id,
                decision.task_id,
                decision.attempt_id,
                kind,
                purpose,
                spec.reason_code,
                spec.model_dump_json(),
                decision.decision_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        if kind == "approval":
            if execution_id is None:
                raise RuntimeError("ApprovalRequest requires an authoritative Execution")
            contract = preflight_contract(task["contract_json"])
            assert spec.approval_requirement_ref is not None
            assert spec.effect_identity is not None
            assert spec.effect_request_hash is not None
            assert spec.permission_scope is not None
            requirement = contract.approval_requirement(
                spec.approval_requirement_ref
            )
            request = ApprovalRequest(
                approval_request_id="approval-request:" + decision.decision_id,
                interaction_id=interaction_id,
                task_id=decision.task_id,
                attempt_id=decision.attempt_id,
                execution_id=execution_id,
                task_contract_ref=contract.ref,
                approval_requirement_ref=ApprovalRequirementRef.parse(
                    spec.approval_requirement_ref
                ),
                effect_identity=spec.effect_identity,
                effect_request_hash=spec.effect_request_hash,
                effect_summary={
                    "effect_identity": spec.effect_identity,
                    "effect_request_hash": spec.effect_request_hash,
                    "evidence_refs": list(spec.evidence_refs),
                },
                permission_scope=spec.permission_scope,
                risk_class=requirement.risk_class,
                approver_policy_ref=requirement.approver_policy_ref,
                requested_at=datetime.now(timezone.utc),
            )
            connection.execute(
                """
                INSERT INTO phase3_approval_requests(
                    approval_request_id, interaction_id, task_id, attempt_id,
                    execution_id, effect_identity, effect_request_hash,
                    permission_scope, risk_class, approver_policy_ref,
                    request_json, status, version, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)
                """,
                (
                    request.approval_request_id,
                    request.interaction_id,
                    request.task_id,
                    request.attempt_id,
                    request.execution_id,
                    request.effect_identity,
                    request.effect_request_hash,
                    request.permission_scope,
                    request.risk_class,
                    request.approver_policy_ref,
                    request.model_dump_json(),
                    request.requested_at.isoformat(),
                    request.expires_at.isoformat() if request.expires_at else None,
                ),
            )
        return interaction_id

    @staticmethod
    def _create_reconciliation(
        connection: sqlite3.Connection, decision: PolicyDecision
    ) -> str:
        reconciliation_id = "reconciliation:" + decision.decision_id
        connection.execute(
            """
            INSERT INTO phase3_reconciliations(
                reconciliation_id, task_id, attempt_id, status,
                created_by_decision_id, created_at
            ) VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (
                reconciliation_id,
                decision.task_id,
                decision.attempt_id,
                decision.decision_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return reconciliation_id

    @staticmethod
    def _create_policy_run_request(
        connection: sqlite3.Connection,
        decision: PolicyDecision,
        *,
        attempt_id: str,
        reason: str,
        session_handle: str | None,
        priority: int,
    ) -> str:
        command_id = "policy-decision:" + decision.decision_id
        command_payload_hash = sha256_digest(
            {
                "command_type": "apply_policy_decision",
                "decision_id": decision.decision_id,
                "decision_key": decision.decision_key,
            }
        )
        connection.execute(
            """
            INSERT INTO phase4_runtime_commands(
                command_id, command_type, payload_hash, result_json, created_at
            ) VALUES (?, 'apply_policy_decision', ?, ?, ?)
            ON CONFLICT(command_id) DO NOTHING
            """,
            (
                command_id,
                command_payload_hash,
                json.dumps(
                    {"decision_id": decision.decision_id},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        existing_command = connection.execute(
            """
            SELECT command_type, payload_hash FROM phase4_runtime_commands
            WHERE command_id = ?
            """,
            (command_id,),
        ).fetchone()
        if (
            existing_command is None
            or existing_command["command_type"] != "apply_policy_decision"
            or existing_command["payload_hash"] != command_payload_hash
        ):
            raise ValueError("Policy Runtime command identity conflict")
        run_request_id = "run-request:" + decision.decision_id
        feedback: tuple[Mapping[str, Any], ...] = ()
        if decision.feedback is not None:
            feedback = (
                {
                    "type": "PolicyFeedback",
                    **decision.feedback.model_dump(mode="json"),
                },
            )
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO phase4_run_requests(
                run_request_id, task_id, attempt_id, reason, state,
                priority, ready_at, created_by_command_id,
                created_by_decision_id, source_interaction_id,
                session_handle, feedback_json, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                run_request_id,
                decision.task_id,
                attempt_id,
                reason,
                priority,
                now,
                command_id,
                decision.decision_id,
                session_handle,
                json.dumps(
                    feedback,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                now,
            ),
        )
        return run_request_id

    def _publish_policy_state_fact(
        self,
        connection: sqlite3.Connection,
        decision: PolicyDecision,
        *,
        task_state: TaskState,
        attempt_id: str,
        task_version: int,
    ) -> None:
        self._publish_runtime_fact(
            connection,
            fact_id="fact:" + decision.decision_id,
            fact_type="task_state_changed",
            task_id=decision.task_id,
            attempt_id=attempt_id,
            execution_id=None,
            source_event_id=decision.decision_id + ":applied",
            subject_type="task",
            subject_id=decision.task_id,
            subject_version=task_version,
            evidence_refs=(decision.decision_id,),
            attributes={
                "state": task_state.value,
                "policy_action": decision.action.value,
            },
        )

    @staticmethod
    def _policy_result_from_rows(
        decision: PolicyDecision,
        application: DecisionApplication,
        task: sqlite3.Row,
        attempt: sqlite3.Row,
        *,
        derived_record_id: str | None = None,
    ) -> GovernanceApplicationResult:
        return GovernanceApplicationResult(
            decision_id=decision.decision_id,
            application=application,
            task_id=task["task_id"],
            task_state=task["state"],
            task_version=task["version"],
            attempt_id=attempt["attempt_id"],
            attempt_state=attempt["state"],
            attempt_version=attempt["version"],
            derived_record_id=derived_record_id,
        )

    @staticmethod
    def _publish_runtime_fact(
        connection: sqlite3.Connection,
        *,
        fact_id: str,
        fact_type: str,
        task_id: str,
        attempt_id: str,
        execution_id: str | None,
        source_event_id: str,
        subject_type: str,
        subject_id: str,
        subject_version: int,
        evidence_refs: tuple[str, ...],
        attributes: Mapping[str, Any],
    ) -> None:
        occurred = datetime.now(timezone.utc)
        fact = ReliabilityFact(
            fact_id=fact_id,
            fact_type=fact_type,
            task_id=task_id,
            attempt_id=attempt_id,
            execution_id=execution_id,
            source=FactSource(kind="task_runtime", name="astra", version="1"),
            authority_scope=subject_type,
            source_event_id=source_event_id,
            subject_ref=SubjectRef(
                type=subject_type,
                id=subject_id,
                version=subject_version,
                authority_domain="astra",
            ),
            occurred_at=occurred,
            evidence_refs=evidence_refs,
            attributes=dict(attributes),
        )
        RuntimeGovernanceCore._insert_fact_outbox(connection, fact)

    def takeover_expired_execution(
        self,
        *,
        lease_owner_id: str,
        lease_duration_seconds: float,
        now: datetime | None = None,
    ) -> ExecutionRecoveryResult | None:
        """Fence one expired owner and feed its orphan into S-12 recovery."""

        takeover_at = now or datetime.now(timezone.utc)
        if takeover_at.tzinfo is None:
            takeover_at = takeover_at.replace(tzinfo=timezone.utc)
        else:
            takeover_at = takeover_at.astimezone(timezone.utc)
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT execution.execution_id, execution.task_id,
                       execution.attempt_id, execution.run_request_id,
                       request.lease_token AS previous_lease_token,
                       task.version AS task_version,
                       attempt.version AS attempt_version
                FROM executions AS execution
                JOIN phase4_run_requests AS request
                  ON request.run_request_id = execution.run_request_id
                JOIN phase3_tasks AS task ON task.task_id = execution.task_id
                JOIN phase3_attempts AS attempt
                  ON attempt.attempt_id = execution.attempt_id
                WHERE execution.status = 'running'
                  AND execution.ended_at IS NULL
                  AND request.state = 'claimed'
                  AND (
                    request.lease_token IS NULL
                    OR request.lease_expires_at IS NULL
                    OR request.lease_expires_at <= ?
                  )
                ORDER BY request.lease_expires_at,
                         execution.started_at, execution.execution_id
                LIMIT 1
                """,
                (takeover_at.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            run_request_id = str(row["run_request_id"])
            lease_token = "lease:" + str(uuid4())
            lease_expires_at = self._lease_expiry(
                takeover_at, lease_duration_seconds
            )
            if row["previous_lease_token"] is None:
                token_predicate = "lease_token IS NULL"
                parameters: tuple[Any, ...] = (
                    lease_owner_id,
                    lease_token,
                    lease_expires_at,
                    takeover_at.isoformat(),
                    run_request_id,
                    takeover_at.isoformat(),
                )
            else:
                token_predicate = "lease_token = ?"
                parameters = (
                    lease_owner_id,
                    lease_token,
                    lease_expires_at,
                    takeover_at.isoformat(),
                    run_request_id,
                    takeover_at.isoformat(),
                    row["previous_lease_token"],
                )
            cursor = connection.execute(
                f"""
                UPDATE phase4_run_requests
                SET lease_owner_id = ?, lease_token = ?,
                    lease_expires_at = ?, heartbeat_at = ?,
                    ownership_version = ownership_version + 1
                WHERE run_request_id = ? AND state = 'claimed'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                  AND {token_predicate}
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                return None
            return self.recover_execution(
                command_id="recover-execution:" + str(row["execution_id"]),
                execution_id=str(row["execution_id"]),
                expected_task_version=int(row["task_version"]),
                expected_attempt_version=int(row["attempt_version"]),
                now=takeover_at,
            )

    def recover_expired_leases(
        self,
        *,
        lease_owner_id: str,
        lease_duration_seconds: float,
        operation_observer: Callable[
            [ExternalOperation, CanonicalEffectRequest], ExternalOperationConfirmation
        ]
        | None = None,
    ) -> tuple[ExecutionRecoveryResult, ...]:
        """Drain expired owned work through the established Recovery classes."""

        recovered: list[ExecutionRecoveryResult] = []
        while True:
            result = self.takeover_expired_execution(
                lease_owner_id=lease_owner_id,
                lease_duration_seconds=lease_duration_seconds,
            )
            if result is None:
                break
            recovered.append(result)
            if result.classification == RecoveryClassification.GOVERNANCE_RESUME:
                lease = self.store.query_one(
                    """
                    SELECT request.lease_token
                    FROM executions AS execution
                    JOIN phase4_run_requests AS request
                      ON request.run_request_id = execution.run_request_id
                    WHERE execution.execution_id = ?
                      AND request.lease_owner_id = ?
                    """,
                    (result.execution_id, lease_owner_id),
                )
                if lease is None or lease["lease_token"] is None:
                    raise LeaseFencedError(
                        result.run_request_id or "unknown",
                        "recovery_ownership_missing",
                    )
                evaluation = self.governance_core.evaluate(result.execution_id)
                application = self.apply_policy_decision(
                    evaluation.policy_decision.decision_id,
                    execution_id=result.execution_id,
                    lease_owner_id=lease_owner_id,
                    lease_token=str(lease["lease_token"]),
                )
                if application.application != DecisionApplication.APPLIED:
                    self.finalize_late_execution_result(
                        result.execution_id,
                        lease_owner_id=lease_owner_id,
                        lease_token=str(lease["lease_token"]),
                    )
            elif (
                result.classification == RecoveryClassification.RECONCILE
                and result.reconciliation_id is not None
                and operation_observer is not None
            ):
                self.run_reconciliation(
                    result.reconciliation_id,
                    operation_observer,
                    lease_owner_id=lease_owner_id,
                    lease_duration_seconds=lease_duration_seconds,
                )
        return tuple(recovered)

    def has_ready_run_request(self, *, now: datetime | None = None) -> bool:
        """Read whether durable Task work is ready.

        This is deliberately conservative and side-effect free: it does not
        claim a lease, recover work, or alter a Run Request.  A pending request
        that may later prove ineligible is still reported as ready work.
        """

        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        else:
            instant = instant.astimezone(timezone.utc)
        rows = self.store.query_all(
            "SELECT ready_at FROM phase4_run_requests WHERE state = 'pending'"
        )
        return any(_instant(str(row["ready_at"])) <= instant for row in rows)

    def claim_and_start_execution(
        self,
        *,
        lease_owner_id: str,
        lease_duration_seconds: float,
        execution_id: str | None = None,
        run_request_id: str | None = None,
        now: datetime | None = None,
    ) -> ExecutionClaimResult | None:
        """Atomically claim the next ready request and create its Execution."""

        claimed_at = now or datetime.now(timezone.utc)
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
        else:
            claimed_at = claimed_at.astimezone(timezone.utc)
        started_at = claimed_at.isoformat()
        lease_token = "lease:" + str(uuid4())
        lease_expires_at = self._lease_expiry(
            claimed_at, lease_duration_seconds
        )
        eligibility_error: ExecutionEligibilityError | None = None

        with self.store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT
                    request.*,
                    task.contract_json,
                    task.state AS task_state,
                    task.version AS task_version,
                    task.termination_reason AS task_termination_reason,
                    task.current_attempt_id,
                    attempt.state AS attempt_state,
                    attempt.version AS attempt_version,
                    attempt.termination_reason AS attempt_termination_reason
                FROM phase4_run_requests AS request
                JOIN phase3_tasks AS task ON task.task_id = request.task_id
                JOIN phase3_attempts AS attempt
                    ON attempt.attempt_id = request.attempt_id
                    AND attempt.task_id = request.task_id
                WHERE request.state = 'pending'
                ORDER BY
                    request.priority DESC,
                    request.ready_at ASC,
                    request.created_at ASC,
                    request.run_request_id ASC
                """
            ).fetchall()
            ready_rows = (
                row
                for row in rows
                if _instant(row["ready_at"]) <= claimed_at
                and (
                    run_request_id is None
                    or row["run_request_id"] == run_request_id
                )
            )
            for request in ready_rows:
                candidate_id = str(request["run_request_id"])
                ineligible_reason: str | None = None
                if request["task_state"] in {"succeeded", "failed", "cancelled"}:
                    ineligible_reason = str(
                        request["task_termination_reason"] or "task_not_executable"
                    )
                elif request["current_attempt_id"] != request["attempt_id"]:
                    ineligible_reason = "attempt_not_current"
                elif request["attempt_state"] != "active":
                    ineligible_reason = str(
                        request["attempt_termination_reason"] or "attempt_not_active"
                    )

                try:
                    contract = preflight_contract(request["contract_json"])
                except CatalogIntegrityError:
                    contract = None
                    ineligible_reason = "contract_runtime_incompatible"
                    now_text = claimed_at.isoformat()
                    task_cursor = connection.execute(
                        """
                        UPDATE phase3_tasks
                        SET state = 'failed', version = version + 1,
                            termination_reason = ?, updated_at = ?
                        WHERE task_id = ? AND version = ?
                          AND state NOT IN ('succeeded', 'failed', 'cancelled')
                        """,
                        (
                            ineligible_reason,
                            now_text,
                            request["task_id"],
                            request["task_version"],
                        ),
                    )
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = 'failed', version = version + 1,
                            ended_at = ?, termination_reason = ?
                        WHERE attempt_id = ? AND version = ?
                          AND state NOT IN (
                            'completed', 'failed', 'exhausted',
                            'superseded', 'cancelled'
                          )
                        """,
                        (
                            now_text,
                            ineligible_reason,
                            request["attempt_id"],
                            request["attempt_version"],
                        ),
                    )
                    if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                        raise RuntimeError("Contract incompatibility CAS failed")
                if ineligible_reason is None and claimed_at >= _instant(
                    contract.limits.task_deadline
                ):
                    ineligible_reason = "task_deadline_exceeded"
                    self.expire_deadline(
                        task_id=str(request["task_id"]),
                        attempt_id=str(request["attempt_id"]),
                        reason=ineligible_reason,
                    )
                elif (
                    ineligible_reason is None
                    and contract.limits.attempt_deadline is not None
                    and claimed_at >= _instant(contract.limits.attempt_deadline)
                ):
                    ineligible_reason = "attempt_deadline_exceeded"
                    self.expire_deadline(
                        task_id=str(request["task_id"]),
                        attempt_id=str(request["attempt_id"]),
                        reason=ineligible_reason,
                    )

                execution_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM executions WHERE attempt_id = ?",
                        (request["attempt_id"],),
                    ).fetchone()[0]
                )
                if (
                    ineligible_reason is None
                    and execution_count >= contract.limits.max_executions_per_attempt
                ):
                    ineligible_reason = "execution_budget_exhausted"
                    now_text = claimed_at.isoformat()
                    task_cursor = connection.execute(
                        """
                        UPDATE phase3_tasks
                        SET state = 'failed', version = version + 1,
                            termination_reason = ?, updated_at = ?
                        WHERE task_id = ? AND version = ?
                          AND state NOT IN ('succeeded', 'failed', 'cancelled')
                        """,
                        (
                            ineligible_reason,
                            now_text,
                            request["task_id"],
                            request["task_version"],
                        ),
                    )
                    attempt_cursor = connection.execute(
                        """
                        UPDATE phase3_attempts
                        SET state = 'exhausted', version = version + 1,
                            ended_at = ?, termination_reason = ?
                        WHERE attempt_id = ? AND version = ? AND state = 'active'
                        """,
                        (
                            now_text,
                            ineligible_reason,
                            request["attempt_id"],
                            request["attempt_version"],
                        ),
                    )
                    if task_cursor.rowcount != 1 or attempt_cursor.rowcount != 1:
                        raise RuntimeError("Execution budget transition CAS failed")
                    self._write_checkpoint(
                        connection,
                        boundary="task_terminal",
                        task_id=str(request["task_id"]),
                        attempt_id=str(request["attempt_id"]),
                        references={"termination_reason": ineligible_reason},
                    )

                if ineligible_reason is not None:
                    connection.execute(
                        """
                        UPDATE phase4_run_requests
                        SET state = 'cancelled', termination_reason = ?,
                            terminated_at = COALESCE(terminated_at, ?)
                        WHERE run_request_id = ? AND state = 'pending'
                        """,
                        (
                            ineligible_reason,
                            claimed_at.isoformat(),
                            candidate_id,
                        ),
                    )
                    self._write_checkpoint(
                        connection,
                        boundary="run_request_cancelled",
                        task_id=str(request["task_id"]),
                        attempt_id=str(request["attempt_id"]),
                        run_request_id=candidate_id,
                        references={"termination_reason": ineligible_reason},
                    )
                    if run_request_id is not None:
                        eligibility_error = ExecutionEligibilityError(
                            ineligible_reason, candidate_id
                        )
                        break
                    continue

                actual_execution_id = execution_id or "execution:" + str(uuid4())
                cursor = connection.execute(
                    """
                    UPDATE phase4_run_requests
                    SET state = 'claimed', lease_owner_id = ?, lease_token = ?,
                        lease_expires_at = ?, heartbeat_at = ?,
                        ownership_version = ownership_version + 1
                    WHERE run_request_id = ? AND state = 'pending'
                    """,
                    (
                        lease_owner_id,
                        lease_token,
                        lease_expires_at,
                        started_at,
                        candidate_id,
                    ),
                )
                if cursor.rowcount != 1:
                    continue

                try:
                    connection.execute(
                        """
                        INSERT INTO executions(
                            execution_id, task_id, attempt_id, status,
                            session_handle, started_at, run_request_id
                        ) VALUES (?, ?, ?, 'running', ?, ?, ?)
                        """,
                        (
                            actual_execution_id,
                            request["task_id"],
                            request["attempt_id"],
                            request["session_handle"],
                            started_at,
                            candidate_id,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    if "executions.run_request_id" in str(error):
                        raise RunRequestExecutionConflict(
                            f"run_request_execution_conflict: "
                            f"run_request_id {candidate_id!r}"
                        ) from error
                    raise

                connection.execute(
                    """
                    INSERT INTO budget_ledger(execution_id)
                    VALUES (?)
                    """,
                    (actual_execution_id,),
                )
                self._write_checkpoint(
                    connection,
                    boundary="execution_starting",
                    task_id=str(request["task_id"]),
                    attempt_id=str(request["attempt_id"]),
                    execution_id=actual_execution_id,
                    run_request_id=candidate_id,
                )
                return ExecutionClaimResult(
                    run_request_id=candidate_id,
                    run_request_state="claimed",
                    execution_id=actual_execution_id,
                    execution_status="running",
                    task_id=str(request["task_id"]),
                    attempt_id=str(request["attempt_id"]),
                    started_at=started_at,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    ownership_version=int(request["ownership_version"]) + 1,
                )
        if eligibility_error is not None:
            raise eligibility_error
        return None

    def build_runtime_invocation(
        self,
        claim: ExecutionClaimResult,
        *,
        provider_config: Mapping[str, Any] | None = None,
        execution_profile: str = "astra_controlled",
        execution_profile_version: str = "1",
        execution_profile_hash: str | None = None,
        hermes_home: str | None = None,
    ) -> RuntimeInvocation:
        """Build the stable executor port from persisted authoritative data."""

        row = self.store.query_one(
            """
            SELECT task.contract_json, task.contract_hash,
                   task.current_attempt_id,
                   execution.task_id AS execution_task_id,
                   execution.attempt_id AS execution_attempt_id,
                   execution.run_request_id AS execution_run_request_id,
                   execution.session_handle,
                   request.task_id AS request_task_id,
                   request.attempt_id AS request_attempt_id,
                   request.feedback_json, request.lease_owner_id,
                   request.lease_token, request.lease_expires_at,
                   request.state AS run_request_state,
                   request.reason AS run_request_reason,
                   request.source_interaction_id,
                   interaction.purpose AS interaction_purpose,
                   interaction.prompt AS interaction_prompt,
                   interaction.payload_json AS interaction_payload_json,
                   interaction.status AS interaction_status,
                   interaction.resolution_json
            FROM phase3_tasks AS task
            JOIN executions AS execution ON execution.task_id = task.task_id
            JOIN phase4_run_requests AS request
              ON request.run_request_id = execution.run_request_id
            LEFT JOIN interactions AS interaction
              ON interaction.interaction_id = request.source_interaction_id
             AND interaction.task_id = request.task_id
             AND interaction.attempt_id = request.attempt_id
            WHERE task.task_id = ?
                AND task.current_attempt_id = ?
                AND execution.execution_id = ?
                AND execution.task_id = ?
                AND execution.attempt_id = ?
                AND execution.run_request_id = ?
                AND request.run_request_id = ?
                AND request.task_id = ?
                AND request.attempt_id = ?
                AND execution.ended_at IS NULL
                AND request.state = 'claimed'
                AND request.lease_owner_id = ?
                AND request.lease_token = ?
                AND request.lease_expires_at > ?
            """,
            (
                claim.task_id,
                claim.attempt_id,
                claim.execution_id,
                claim.task_id,
                claim.attempt_id,
                claim.run_request_id,
                claim.run_request_id,
                claim.task_id,
                claim.attempt_id,
                claim.lease_owner_id,
                claim.lease_token,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        if row is None:
            raise ExecutionEligibilityError(
                "execution_not_invocable", claim.run_request_id
            )
        contract = preflight_contract(row["contract_json"])
        if contract.contract_hash != row["contract_hash"]:
            raise ExecutionEligibilityError(
                "task_contract_hash_mismatch", claim.run_request_id
            )
        if any(
            str(row[field]) != expected
            for field, expected in (
                ("current_attempt_id", claim.attempt_id),
                ("execution_task_id", claim.task_id),
                ("execution_attempt_id", claim.attempt_id),
                ("execution_run_request_id", claim.run_request_id),
                ("request_task_id", claim.task_id),
                ("request_attempt_id", claim.attempt_id),
            )
        ):
            raise ExecutionEligibilityError(
                "runtime_invocation_identity_mismatch", claim.run_request_id
            )
        feedback_payload = json.loads(row["feedback_json"] or "[]")
        if not isinstance(feedback_payload, list):
            raise RuntimeError("Persisted Runtime feedback must be a JSON array")
        evidence_receipt_ids = tuple(
            str(receipt["receipt_id"])
            for receipt in self.store.query_all(
                """
                SELECT receipt_id
                FROM execution_receipts
                WHERE task_id = ? AND attempt_id = ?
                ORDER BY created_at, receipt_id
                """,
                (claim.task_id, claim.attempt_id),
            )
        )
        user_request = contract.objective.description
        allowed_tools = tuple(tool.tool_name for tool in contract.resolved_tools)
        if row["run_request_reason"] in {
            "interaction_resolved",
            "interaction_resolved_recovery",
        }:
            if (
                row["source_interaction_id"] is None
                or row["interaction_status"] != "resolved"
                or row["resolution_json"] is None
            ):
                raise RuntimeError("Resolved Interaction input is not authoritative")
            if row["interaction_purpose"] == InteractionPurpose.CLARIFICATION.value:
                resolution = json.loads(str(row["resolution_json"]))
                response = resolution.get("response")
                candidate_answer: Any = (
                    response
                    if isinstance(response, str) and response
                    else resolution
                )
                user_request = json.dumps(
                    {
                        "message_type": "clarification_candidate",
                        "original_task_objective": contract.objective.description,
                        "pending_clarification": {
                            "prompt": row["interaction_prompt"],
                            "requirements": json.loads(
                                row["interaction_payload_json"] or "{}"
                            ),
                        },
                        "candidate_answer": candidate_answer,
                        "instructions": [
                            "Treat candidate_answer only as a response to pending_clarification.",
                            "First determine whether it supplies the requested information.",
                            "If it is unrelated, malformed, or insufficient, do not restart original_task_objective and do not call business tools; request the missing information again.",
                            "Only continue original_task_objective when candidate_answer satisfies pending_clarification.",
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            elif row["interaction_purpose"] == InteractionPurpose.APPROVAL.value:
                resolution = ApprovalResolution.model_validate_json(
                    str(row["resolution_json"])
                )
                authoritative_resolution = self.store.query_one(
                    """
                    SELECT resolution_json
                    FROM phase3_approval_resolutions
                    WHERE approval_resolution_id = ? AND task_id = ?
                    """,
                    (resolution.approval_resolution_id, claim.task_id),
                )
                if authoritative_resolution is None or (
                    ApprovalResolution.model_validate_json(
                        authoritative_resolution["resolution_json"]
                    )
                    != resolution
                ):
                    raise RuntimeError(
                        "Resolved approval is not the authoritative resolution"
                    )
                if resolution.decision == ApprovalDecision.DENIED:
                    allowed_tools = ()
                    user_request = json.dumps(
                        {
                            "message_type": "denied_exact_effect_outcome",
                            "approval_resolution_id": resolution.approval_resolution_id,
                            "decision": resolution.decision.value,
                            "effect_identity": resolution.effect_identity,
                            "instructions": [
                                "The exact governed effect was declined and was not executed.",
                                "Do not resume or reinterpret the original task objective.",
                                "Do not call business tools or repeat business reads.",
                                "Only submit an unexecuted outcome with submit_task_result, then give a concise user-facing explanation.",
                            ],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                else:
                    user_request = (
                        "Continue the existing task from the resolved exact-effect "
                        "approval in ASTRA_RUNTIME_FEEDBACK. Reissue the same "
                        "effect tool call with exactly the approved normalized "
                        "parameters before doing anything else. Do not repeat "
                        "successful read calls unless their evidence is missing "
                        "or stale."
                    )
        return RuntimeInvocation(
            execution_id=claim.execution_id,
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            user_request=user_request,
            task_contract=contract.model_dump(mode="json"),
            allowed_tools=allowed_tools,
            provider_config=dict(provider_config or {}),
            limits=contract.limits.model_dump(mode="json"),
            session_handle=row["session_handle"],
            feedback=tuple(dict(item) for item in feedback_payload),
            evidence_receipt_ids=evidence_receipt_ids,
            run_request_id=claim.run_request_id,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
            lease_expires_at=row["lease_expires_at"],
            execution_profile=execution_profile,
            execution_profile_version=execution_profile_version,
            execution_profile_hash=execution_profile_hash,
            hermes_home=hermes_home,
        )

    def record_execution_result(
        self,
        *,
        command_id: str,
        execution_id: str,
        lease_owner_id: str,
        lease_token: str,
        result: ExecutionResult,
    ) -> ExecutionResult:
        """Persist exactly one immutable result through an idempotent command."""

        if result.execution_id != execution_id:
            raise ExecutionResultConflict(
                "execution_result_conflict: result execution_id mismatch"
            )
        payload_hash = sha256_digest(
            {
                "command_type": "record_execution_result",
                "execution_id": execution_id,
                "result": result,
            }
        )
        result_hash = sha256_digest(result)
        with self.store.transaction() as connection:
            self._assert_execution_lease_row(
                connection,
                execution_id=execution_id,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                allow_completed=True,
            )
            existing_command = connection.execute(
                """
                SELECT command_type, payload_hash, result_json
                FROM phase4_runtime_commands WHERE command_id = ?
                """,
                (command_id,),
            ).fetchone()
            if existing_command is not None:
                if (
                    existing_command["command_type"]
                    != "record_execution_result"
                    or existing_command["payload_hash"] != payload_hash
                ):
                    raise CommandIdentityConflict(
                        f"identity_conflict: command_id {command_id!r}"
                    )
                return ExecutionResult.model_validate_json(
                    existing_command["result_json"]
                )

            execution = connection.execute(
                """
                SELECT execution.*, task.state AS task_state,
                       task.current_attempt_id,
                       attempt.state AS attempt_state
                FROM executions AS execution
                JOIN phase3_tasks AS task ON task.task_id = execution.task_id
                JOIN phase3_attempts AS attempt
                  ON attempt.attempt_id = execution.attempt_id
                WHERE execution.execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if execution is None:
                raise KeyError(execution_id)
            existing_result = connection.execute(
                """
                SELECT result_hash, result_json
                FROM phase4_execution_results WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if existing_result is not None:
                if existing_result["result_hash"] != result_hash:
                    raise ExecutionResultConflict(
                        "execution_result_conflict: execution already has "
                        "a different result"
                    )
                persisted = ExecutionResult.model_validate_json(
                    existing_result["result_json"]
                )
                connection.execute(
                    """
                    INSERT INTO phase4_runtime_commands(
                        command_id, command_type, payload_hash, result_json,
                        created_at
                    ) VALUES (?, 'record_execution_result', ?, ?, ?)
                    """,
                    (
                        command_id,
                        payload_hash,
                        persisted.model_dump_json(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                if (
                    persisted.session_handle is not None
                    and execution["task_state"]
                    not in {"succeeded", "failed", "cancelled"}
                ):
                    connection.execute(
                        "UPDATE executions SET session_handle = ? WHERE execution_id = ?",
                        (persisted.session_handle, execution_id),
                    )
                return persisted

            now = datetime.now(timezone.utc).isoformat()
            serialized = result.model_dump_json()
            persisted = ExecutionResult.model_validate_json(serialized)
            connection.execute(
                """
                INSERT INTO phase4_execution_results(
                    execution_id, command_id, result_hash, result_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (execution_id, command_id, result_hash, serialized, now),
            )
            connection.execute(
                """
                INSERT INTO phase4_runtime_commands(
                    command_id, command_type, payload_hash, result_json,
                    created_at
                ) VALUES (?, 'record_execution_result', ?, ?, ?)
                """,
                (command_id, payload_hash, serialized, now),
            )
            if (
                persisted.session_handle is not None
                and execution["task_state"]
                not in {"succeeded", "failed", "cancelled"}
            ):
                connection.execute(
                    "UPDATE executions SET session_handle = ? WHERE execution_id = ?",
                    (persisted.session_handle, execution_id),
                )
            self._write_checkpoint(
                connection,
                boundary="execution_result_persisted",
                task_id=str(execution["task_id"]),
                attempt_id=str(execution["attempt_id"]),
                execution_id=execution_id,
                run_request_id=execution["run_request_id"],
                references={"result_hash": result_hash, "command_id": command_id},
            )
            return persisted

    def finalize_late_execution_result(
        self,
        execution_id: str,
        *,
        lease_owner_id: str,
        lease_token: str,
    ) -> tuple[str, str] | None:
        """Close old Worker work without allowing a terminal Task overwrite."""

        with self.store.transaction() as connection:
            self._assert_execution_lease_row(
                connection,
                execution_id=execution_id,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                allow_completed=True,
            )
            row = connection.execute(
                """
                SELECT execution.run_request_id, execution.ended_at,
                       task.state AS task_state,
                       request.state AS run_request_state
                FROM executions AS execution
                JOIN phase3_tasks AS task ON task.task_id = execution.task_id
                LEFT JOIN phase4_run_requests AS request
                  ON request.run_request_id = execution.run_request_id
                WHERE execution.execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            if row["task_state"] not in {"succeeded", "failed", "cancelled"}:
                return None
            ended_at = datetime.now(timezone.utc).isoformat()
            if row["ended_at"] is None:
                execution_cursor = connection.execute(
                    """
                    UPDATE executions
                    SET status = ?, ended_at = ?, termination_reason = ?
                    WHERE execution_id = ? AND ended_at IS NULL
                      AND EXISTS (
                        SELECT 1 FROM phase4_run_requests AS request
                        WHERE request.run_request_id = executions.run_request_id
                          AND request.lease_owner_id = ?
                          AND request.lease_token = ?
                          AND request.lease_expires_at > ?
                          AND request.state IN ('claimed', 'completed')
                      )
                    """,
                    (
                        "cancelled"
                        if row["task_state"] == "cancelled"
                        else "interrupted",
                        ended_at,
                        "late_result_after_terminal_task:" + row["task_state"],
                        execution_id,
                        lease_owner_id,
                        lease_token,
                        ended_at,
                    ),
                )
                if execution_cursor.rowcount != 1:
                    raise LeaseFencedError(
                        str(row["run_request_id"] or "unknown"),
                        "late_execution_finalize_cas_failed",
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO execution_events(
                        event_id, execution_id, event_type,
                        payload_json, created_at
                    ) VALUES (?, ?, 'LateExecutionResultRecorded', ?, ?)
                    """,
                    (
                        "event:late-result:" + execution_id,
                        execution_id,
                        json.dumps(
                            {"task_state": row["task_state"]},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        ended_at,
                    ),
                )
            request_state = "completed"
            if row["run_request_id"] is not None:
                target_state = (
                    "cancelled"
                    if row["task_state"] == "cancelled"
                    else "completed"
                )
                if row["run_request_state"] == "claimed":
                    request_cursor = connection.execute(
                        """
                        UPDATE phase4_run_requests SET state = ?
                        WHERE run_request_id = ? AND state = 'claimed'
                          AND lease_owner_id = ? AND lease_token = ?
                          AND lease_expires_at > ?
                        """,
                        (
                            target_state,
                            row["run_request_id"],
                            lease_owner_id,
                            lease_token,
                            ended_at,
                        ),
                    )
                    if request_cursor.rowcount != 1:
                        raise LeaseFencedError(
                            str(row["run_request_id"]),
                            "late_run_request_finalize_cas_failed",
                        )
                request = connection.execute(
                    """
                    SELECT state FROM phase4_run_requests
                    WHERE run_request_id = ?
                    """,
                    (row["run_request_id"],),
                ).fetchone()
                request_state = str(request["state"])
            return str(row["task_state"]), request_state

    def get_execution_result(self, execution_id: str) -> ExecutionResult | None:
        row = self.store.query_one(
            """
            SELECT result_json FROM phase4_execution_results
            WHERE execution_id = ?
            """,
            (execution_id,),
        )
        return ExecutionResult.model_validate_json(row[0]) if row else None

    def complete_run_request(self, run_request_id: str) -> str:
        raise RuntimeError(
            "Run Request completion is part of the TaskRuntime lifecycle transaction"
        )


class SingleWorker:
    """Execute at most one ready Run Request through durable governance."""

    def __init__(
        self,
        runtime: TaskRuntime,
        executor: AgentExecutor,
        *,
        governance_core: RuntimeGovernanceCore | None = None,
        provider_config: Mapping[str, Any] | None = None,
        worker_id: str | None = None,
        lease_duration_seconds: float = 30.0,
        heartbeat_interval_seconds: float = 10.0,
        operation_observer: Callable[
            [ExternalOperation, CanonicalEffectRequest], ExternalOperationConfirmation
        ]
        | None = None,
        fault_injector: Callable[[str, Mapping[str, Any]], None] | None = None,
        execution_profile: str = "astra_controlled",
        execution_profile_version: str = "1",
        execution_profile_hash: str | None = None,
        hermes_home: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.executor = executor
        executor_runtime = getattr(executor, "runtime", None)
        if executor_runtime is not None and executor_runtime is not runtime:
            raise ValueError("SingleWorker and AgentExecutor must share TaskRuntime")
        if governance_core is not None and governance_core is not runtime.governance_core:
            raise ValueError("SingleWorker must use TaskRuntime governance authority")
        self.governance_core = governance_core or runtime.governance_core
        self.provider_config = dict(provider_config or {})
        if lease_duration_seconds <= 0:
            raise ValueError("lease_duration_seconds must be positive")
        if (
            heartbeat_interval_seconds <= 0
            or heartbeat_interval_seconds >= lease_duration_seconds
        ):
            raise ValueError(
                "heartbeat_interval_seconds must be positive and shorter than lease"
            )
        self.worker_id = worker_id or "worker:" + str(uuid4())
        self.lease_duration_seconds = lease_duration_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.operation_observer = operation_observer
        self.fault_injector = fault_injector
        self.execution_profile = execution_profile
        self.execution_profile_version = execution_profile_version
        self.execution_profile_hash = execution_profile_hash
        self.hermes_home = hermes_home

    async def run_once(
        self,
        *,
        execution_id: str | None = None,
        run_request_id: str | None = None,
        now: datetime | None = None,
        event_sink: ExecutionEventSink | None = None,
    ) -> SingleWorkerRunResult | None:
        self.runtime.recover_expired_leases(
            lease_owner_id=self.worker_id,
            lease_duration_seconds=self.lease_duration_seconds,
            operation_observer=self.operation_observer,
        )
        claim = self.runtime.claim_and_start_execution(
            lease_owner_id=self.worker_id,
            lease_duration_seconds=self.lease_duration_seconds,
            execution_id=execution_id,
            run_request_id=run_request_id,
            now=now,
        )
        if claim is None:
            return None

        self.runtime.begin_execution(
            execution_id=claim.execution_id,
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
        )
        invocation = self.runtime.build_runtime_invocation(
            claim,
            provider_config=self.provider_config,
            execution_profile=self.execution_profile,
            execution_profile_version=self.execution_profile_version,
            execution_profile_hash=self.execution_profile_hash,
            hermes_home=self.hermes_home,
        )

        async def discard_event(event: ExecutionEvent) -> None:
            return None

        setattr(discard_event, "_astra_discard", True)

        actual_event_sink = event_sink or discard_event

        lease_failure: LeaseFencedError | None = None

        async def heartbeat() -> None:
            nonlocal lease_failure
            while True:
                await asyncio.sleep(self.heartbeat_interval_seconds)
                try:
                    renewed = self.runtime.heartbeat_lease(
                        execution_id=claim.execution_id,
                        lease_owner_id=claim.lease_owner_id,
                        lease_token=claim.lease_token,
                        lease_duration_seconds=self.lease_duration_seconds,
                    )
                except LeaseFencedError as error:
                    lease_failure = error
                    try:
                        await self.executor.cancel(
                            claim.execution_id, "astra_lease_fenced"
                        )
                    except BaseException:
                        pass
                    return
                if renewed is None:
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            result = await self.executor.execute(invocation, actual_event_sink)
            if lease_failure is not None:
                raise lease_failure
            persisted = self.runtime.record_execution_result(
                command_id="record-execution-result:" + claim.execution_id,
                execution_id=claim.execution_id,
                lease_owner_id=claim.lease_owner_id,
                lease_token=claim.lease_token,
                result=result,
            )
            late = self.runtime.finalize_late_execution_result(
                claim.execution_id,
                lease_owner_id=claim.lease_owner_id,
                lease_token=claim.lease_token,
            )
            if late is not None:
                execution = self.runtime.store.get_execution(claim.execution_id)
                return SingleWorkerRunResult(
                    claim=claim,
                    invocation=invocation,
                    execution_result=persisted,
                    governance_evaluation=None,
                    governance_application=None,
                    execution_finalized=bool(execution and execution["ended_at"]),
                    run_request_state=late[1],
                )
            pending_interaction = self.runtime.store.pending_interaction(
                claim.execution_id
            )
            if pending_interaction is not None:
                execution = self.runtime.store.get_execution(claim.execution_id)
                request = self.runtime.store.query_one(
                    "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
                    (claim.run_request_id,),
                )
                if request is None:
                    raise RuntimeError("Run Request disappeared during interaction wait")
                return SingleWorkerRunResult(
                    claim=claim,
                    invocation=invocation,
                    execution_result=persisted,
                    governance_evaluation=None,
                    governance_application=None,
                    execution_finalized=bool(execution and execution["ended_at"]),
                    run_request_state=str(request["state"]),
                )

            self.runtime.assert_execution_lease(
                execution_id=claim.execution_id,
                lease_owner_id=claim.lease_owner_id,
                lease_token=claim.lease_token,
            )
            try:
                evaluation = self.governance_core.evaluate(claim.execution_id)
            except RuntimeError:
                late = self.runtime.finalize_late_execution_result(
                    claim.execution_id,
                    lease_owner_id=claim.lease_owner_id,
                    lease_token=claim.lease_token,
                )
                if late is None:
                    raise
                execution = self.runtime.store.get_execution(claim.execution_id)
                return SingleWorkerRunResult(
                    claim=claim,
                    invocation=invocation,
                    execution_result=persisted,
                    governance_evaluation=None,
                    governance_application=None,
                    execution_finalized=bool(execution and execution["ended_at"]),
                    run_request_state=late[1],
                )
            if self.fault_injector is not None:
                self.fault_injector(
                    "after_policy_decision_persisted",
                    {
                        "execution_id": claim.execution_id,
                        "decision_id": evaluation.policy_decision.decision_id,
                    },
                )
            application = self.runtime.apply_policy_decision(
                evaluation.policy_decision.decision_id,
                execution_id=claim.execution_id,
                lease_owner_id=claim.lease_owner_id,
                lease_token=claim.lease_token,
            )
            if application.application != DecisionApplication.APPLIED:
                self.runtime.finalize_late_execution_result(
                    claim.execution_id,
                    lease_owner_id=claim.lease_owner_id,
                    lease_token=claim.lease_token,
                )
            execution = self.runtime.store.get_execution(claim.execution_id)
            request = self.runtime.store.query_one(
                "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
                (claim.run_request_id,),
            )
            if execution is None or request is None:
                raise RuntimeError("Applied PolicyDecision lost Runtime work records")
            finalized = execution["ended_at"] is not None
            run_request_state = str(request["state"])
            if application.application.value == "applied" and (
                not finalized or run_request_state != "completed"
            ):
                raise RuntimeError(
                    "PolicyDecision applied without closing current work"
                )
            return SingleWorkerRunResult(
                claim=claim,
                invocation=invocation,
                execution_result=persisted,
                governance_evaluation=evaluation,
                governance_application=application,
                execution_finalized=finalized,
                run_request_state=run_request_state,
            )
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass


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
        self.runtime = TaskRuntime(store, governance_core=governance_core)
        self.governance_core = self.runtime.governance_core

    def apply_governance_decision(
        self,
        decision_id: str,
        *,
        execution_id: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> "GovernanceApplicationResult":
        """Delegate an immutable decision to the composed governance component."""

        return self.runtime.apply_policy_decision(
            decision_id,
            execution_id=execution_id,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
        )

    def resolve_and_resume(
        self,
        previous: RuntimeInvocation,
        *,
        interaction_id: str,
        resolution: Mapping[str, Any],
        new_execution_id: str,
    ) -> RuntimeInvocation:
        raise RuntimeError(
            "Use TaskRuntime.resolve_interaction and let SingleWorker consume "
            "the persisted Run Request"
        )
