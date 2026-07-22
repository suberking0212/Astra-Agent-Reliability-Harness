"""Astra Task Runtime entry points and Phase 2 compatibility helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .domain import (
    AgentExecutor,
    ExecutionEvent,
    ExecutionResult,
    InteractionKind,
    InteractionRequest,
    RuntimeInvocation,
)
from .phase3.approval import (
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
from .phase3.task_contract import FrozenContractModel, SubjectRef, TaskContract
from .storage import AstraStore

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

    def submit_task(
        self,
        *,
        command_id: str,
        task_id: str,
        contract: TaskContract,
        completion_contract: CompletionContract | None = None,
        priority: int = 0,
        ready_at: str | None = None,
    ) -> TaskSubmissionResult:
        """Atomically persist a Task, initial Attempt, Run Request and result."""

        payload: dict[str, Any] = {
            "command_type": "submit_task",
            "task_id": task_id,
            "contract": contract,
            "priority": priority,
            "ready_at": ready_at,
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
            return result

    def request_interaction(
        self,
        *,
        execution_id: str,
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
            }
        return self._request_interaction(
            execution_id=execution_id,
            kind=actual_kind,
            prompt=prompt,
            payload=requested_payload,
            exact_effect=None,
        )

    def request_approval(
        self,
        *,
        execution_id: str,
        effect: CanonicalEffectRequest,
        permission_scope: str = "execute_effect",
    ) -> InteractionRequest:
        """Persist an exact-effect ApprovalRequest and approval Interaction."""

        return self._request_interaction(
            execution_id=execution_id,
            kind=InteractionKind.APPROVAL,
            prompt="Approval is required for the exact canonical effect request.",
            payload={},
            exact_effect=effect,
            permission_scope=permission_scope,
        )

    def _request_interaction(
        self,
        *,
        execution_id: str,
        kind: InteractionKind,
        prompt: str,
        payload: Mapping[str, Any],
        exact_effect: CanonicalEffectRequest | None,
        permission_scope: str = "execute_effect",
    ) -> InteractionRequest:
        interaction_id = "interaction:execution:" + execution_id
        now = datetime.now(timezone.utc)
        with self.store.transaction() as connection:
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

            contract = TaskContract.model_validate_json(row["contract_json"])
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
                prompt=prompt,
                payload=persisted_payload,
            )
            connection.execute(
                """
                INSERT INTO interactions(
                    interaction_id, execution_id, task_id, attempt_id, kind,
                    prompt, status, version, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)
                """,
                (
                    interaction.interaction_id,
                    execution_id,
                    interaction.task_id,
                    interaction.attempt_id,
                    interaction.kind.value,
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

            contract = TaskContract.model_validate_json(row["contract_json"])
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
            if now >= _instant(contract.limits.task_deadline):
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
                feedback = (
                    {
                        "type": "InteractionResolution",
                        "interaction_id": interaction_id,
                        "kind": row["kind"],
                        "resolution": dict(resolution),
                    },
                )
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
                    if termination_reason != "task_deadline_exceeded"
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
            return result

    def begin_execution(self, *, task_id: str, attempt_id: str) -> None:
        """Move a claimed initial Task into running under Runtime authority."""

        with self.store.transaction() as connection:
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

    def save_session_handle(self, execution_id: str, handle: str | None) -> None:
        """Persist the opaque Hermes session on the authoritative Execution."""

        with self.store.transaction() as connection:
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
                return
            raise ValueError(f"Unsupported deadline reason: {reason!r}")

    def apply_policy_decision(
        self, decision_id: str
    ) -> GovernanceApplicationResult:
        """Apply one immutable PolicyDecision through the Runtime transaction."""

        with self.store.transaction() as connection:
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
                task_contract_ref={
                    "contract_id": task["contract_id"],
                    "contract_version": task["contract_version"],
                    "contract_hash": task["contract_hash"],
                },
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
                connection, decision, task, attempt
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
            return self._policy_result_from_rows(
                decision,
                DecisionApplication.APPLIED,
                refreshed_task,
                refreshed_attempt,
                derived_record_id=derived_record_id,
            )

    def recover_pending_policy_decisions(
        self,
    ) -> tuple[GovernanceApplicationResult, ...]:
        """Apply durable pending decisions during Production startup recovery."""

        rows = self.store.query_all(
            """
            SELECT decision_id, task_id, attempt_id
            FROM phase3_policy_decisions
            WHERE application_status = 'pending'
            ORDER BY created_at, decision_id
            """
        )
        recovered: list[GovernanceApplicationResult] = []
        for row in rows:
            execution = self.store.query_one(
                """
                SELECT execution.execution_id
                FROM executions AS execution
                JOIN phase4_execution_results AS result
                  ON result.execution_id = execution.execution_id
                WHERE execution.task_id = ? AND execution.attempt_id = ?
                  AND execution.ended_at IS NULL
                ORDER BY result.created_at DESC, execution.execution_id DESC
                LIMIT 1
                """,
                (row["task_id"], row["attempt_id"]),
            )
            if execution is not None:
                replay = self.governance_core.evaluate(str(execution["execution_id"]))
                if replay.policy_decision.decision_id != row["decision_id"]:
                    raise RuntimeError("PolicyDecision recovery identity mismatch")
            recovered.append(self.apply_policy_decision(str(row["decision_id"])))
        return tuple(recovered)

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
    ) -> str | None:
        self._assert_policy_work_limits(connection, decision, task)
        action = decision.action
        task_state = TaskState(task["state"])
        attempt_state = AttemptState(attempt["state"])
        current_work = self._finish_current_runtime_work(connection, decision)
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
        contract = TaskContract.model_validate_json(task["contract_json"])
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
              AND execution.ended_at IS NULL
            ORDER BY execution.started_at DESC, execution.execution_id DESC
            """,
            (decision.task_id, decision.attempt_id),
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
            """,
            (
                result.status.value,
                ended_at,
                result.termination_reason,
                row["execution_id"],
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
                """,
                (row["run_request_id"],),
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
        connection.execute(
            """
            INSERT INTO interactions(
                interaction_id, execution_id, task_id, attempt_id, kind,
                prompt, status, version, payload_json,
                created_by_decision_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?)
            """,
            (
                interaction_id,
                execution_id,
                decision.task_id,
                decision.attempt_id,
                kind,
                spec.reason_code,
                spec.model_dump_json(),
                decision.decision_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        if kind == "approval":
            if execution_id is None:
                raise RuntimeError("ApprovalRequest requires an authoritative Execution")
            contract = TaskContract.model_validate_json(task["contract_json"])
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

    def claim_and_start_execution(
        self,
        *,
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
        deadline_error: ExecutionEligibilityError | None = None

        with self.store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT
                    request.*,
                    task.contract_json,
                    task.state AS task_state,
                    task.current_attempt_id,
                    attempt.state AS attempt_state
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
            request = next(
                (
                    row
                    for row in rows
                    if _instant(row["ready_at"]) <= claimed_at
                    and (
                        run_request_id is None
                        or row["run_request_id"] == run_request_id
                    )
                ),
                None,
            )
            if request is None:
                return None

            run_request_id = str(request["run_request_id"])
            if request["task_state"] in {"succeeded", "failed", "cancelled"}:
                raise ExecutionEligibilityError(
                    "task_not_executable", run_request_id
                )
            if request["current_attempt_id"] != request["attempt_id"]:
                raise ExecutionEligibilityError(
                    "attempt_not_current", run_request_id
                )
            if request["attempt_state"] != "active":
                raise ExecutionEligibilityError(
                    "attempt_not_active", run_request_id
                )

            contract = TaskContract.model_validate_json(request["contract_json"])
            if claimed_at >= _instant(contract.limits.task_deadline):
                self.expire_deadline(
                    task_id=str(request["task_id"]),
                    attempt_id=str(request["attempt_id"]),
                    reason="task_deadline_exceeded",
                )
                deadline_error = ExecutionEligibilityError(
                    "task_deadline_exceeded", run_request_id
                )
            elif (
                contract.limits.attempt_deadline is not None
                and claimed_at >= _instant(contract.limits.attempt_deadline)
            ):
                self.expire_deadline(
                    task_id=str(request["task_id"]),
                    attempt_id=str(request["attempt_id"]),
                    reason="attempt_deadline_exceeded",
                )
                deadline_error = ExecutionEligibilityError(
                    "attempt_deadline_exceeded", run_request_id
                )
            else:
                execution_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM executions WHERE attempt_id = ?",
                        (request["attempt_id"],),
                    ).fetchone()[0]
                )
                if execution_count >= contract.limits.max_executions_per_attempt:
                    raise ExecutionEligibilityError(
                        "execution_budget_exhausted", run_request_id
                    )

                actual_execution_id = execution_id or "execution:" + str(uuid4())
                cursor = connection.execute(
                    """
                    UPDATE phase4_run_requests
                    SET state = 'claimed'
                    WHERE run_request_id = ? AND state = 'pending'
                    """,
                    (run_request_id,),
                )
                if cursor.rowcount != 1:
                    return None

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
                            run_request_id,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    if "executions.run_request_id" in str(error):
                        raise RunRequestExecutionConflict(
                            f"run_request_execution_conflict: "
                            f"run_request_id {run_request_id!r}"
                        ) from error
                    raise

                connection.execute(
                    """
                    INSERT INTO budget_ledger(execution_id)
                    VALUES (?)
                    """,
                    (actual_execution_id,),
                )
                return ExecutionClaimResult(
                    run_request_id=run_request_id,
                    run_request_state="claimed",
                    execution_id=actual_execution_id,
                    execution_status="running",
                    task_id=str(request["task_id"]),
                    attempt_id=str(request["attempt_id"]),
                    started_at=started_at,
                )
        assert deadline_error is not None
        raise deadline_error

    def build_runtime_invocation(
        self,
        claim: ExecutionClaimResult,
        *,
        provider_config: Mapping[str, Any] | None = None,
    ) -> RuntimeInvocation:
        """Build the stable executor port from persisted authoritative data."""

        row = self.store.query_one(
            """
            SELECT task.contract_json, execution.session_handle,
                   request.feedback_json
            FROM phase3_tasks AS task
            JOIN executions AS execution ON execution.task_id = task.task_id
            JOIN phase4_run_requests AS request
              ON request.run_request_id = execution.run_request_id
            WHERE task.task_id = ?
                AND task.current_attempt_id = ?
                AND execution.execution_id = ?
                AND execution.attempt_id = ?
                AND execution.ended_at IS NULL
            """,
            (
                claim.task_id,
                claim.attempt_id,
                claim.execution_id,
                claim.attempt_id,
            ),
        )
        if row is None:
            raise ExecutionEligibilityError(
                "execution_not_invocable", claim.run_request_id
            )
        contract = TaskContract.model_validate_json(row["contract_json"])
        feedback_payload = json.loads(row["feedback_json"] or "[]")
        if not isinstance(feedback_payload, list):
            raise RuntimeError("Persisted Runtime feedback must be a JSON array")
        return RuntimeInvocation(
            execution_id=claim.execution_id,
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
            user_request=contract.objective.description,
            task_contract=contract.model_dump(mode="json"),
            allowed_tools=tuple(
                tool.tool_name for tool in contract.resolved_tools
            ),
            provider_config=dict(provider_config or {}),
            limits=contract.limits.model_dump(mode="json"),
            session_handle=row["session_handle"],
            feedback=tuple(dict(item) for item in feedback_payload),
        )

    def record_execution_result(
        self,
        *,
        command_id: str,
        execution_id: str,
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
            return persisted

    def finalize_late_execution_result(
        self,
        execution_id: str,
    ) -> tuple[str, str] | None:
        """Close old Worker work without allowing a terminal Task overwrite."""

        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT execution.run_request_id, execution.ended_at,
                       task.state AS task_state
                FROM executions AS execution
                JOIN phase3_tasks AS task ON task.task_id = execution.task_id
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
                connection.execute(
                    """
                    UPDATE executions
                    SET status = ?, ended_at = ?, termination_reason = ?
                    WHERE execution_id = ? AND ended_at IS NULL
                    """,
                    (
                        "cancelled"
                        if row["task_state"] == "cancelled"
                        else "interrupted",
                        ended_at,
                        "late_result_after_terminal_task:" + row["task_state"],
                        execution_id,
                    ),
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
                connection.execute(
                    """
                    UPDATE phase4_run_requests SET state = ?
                    WHERE run_request_id = ? AND state = 'claimed'
                    """,
                    (target_state, row["run_request_id"]),
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
        fault_injector: Callable[[str, Mapping[str, Any]], None] | None = None,
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
        self.fault_injector = fault_injector

    async def run_once(
        self,
        *,
        execution_id: str | None = None,
        now: datetime | None = None,
    ) -> SingleWorkerRunResult | None:
        claim = self.runtime.claim_and_start_execution(
            execution_id=execution_id,
            now=now,
        )
        if claim is None:
            return None

        self.runtime.begin_execution(
            task_id=claim.task_id,
            attempt_id=claim.attempt_id,
        )
        invocation = self.runtime.build_runtime_invocation(
            claim,
            provider_config=self.provider_config,
        )

        async def event_sink(event: ExecutionEvent) -> None:
            return None

        result = await self.executor.execute(invocation, event_sink)
        persisted = self.runtime.record_execution_result(
            command_id="record-execution-result:" + claim.execution_id,
            execution_id=claim.execution_id,
            result=result,
        )
        late = self.runtime.finalize_late_execution_result(claim.execution_id)
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
            return SingleWorkerRunResult(
                claim=claim,
                invocation=invocation,
                execution_result=persisted,
                governance_evaluation=None,
                governance_application=None,
                execution_finalized=bool(execution and execution["ended_at"]),
                run_request_state=str(request["state"]),
            )

        try:
            evaluation = self.governance_core.evaluate(claim.execution_id)
        except RuntimeError:
            late = self.runtime.finalize_late_execution_result(claim.execution_id)
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
            evaluation.policy_decision.decision_id
        )
        if application.application != DecisionApplication.APPLIED:
            self.runtime.finalize_late_execution_result(claim.execution_id)
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
            raise RuntimeError("PolicyDecision applied without closing current work")
        return SingleWorkerRunResult(
            claim=claim,
            invocation=invocation,
            execution_result=persisted,
            governance_evaluation=evaluation,
            governance_application=application,
            execution_finalized=finalized,
            run_request_state=run_request_state,
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
        self.runtime = TaskRuntime(store, governance_core=governance_core)
        self.governance_core = self.runtime.governance_core

    def apply_governance_decision(
        self, decision_id: str
    ) -> "GovernanceApplicationResult":
        """Delegate an immutable decision to the composed governance component."""

        return self.runtime.apply_policy_decision(decision_id)

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
