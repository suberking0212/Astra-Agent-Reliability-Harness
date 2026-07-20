"""Astra Task Runtime entry points and Phase 2 compatibility helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .domain import (
    AgentExecutor,
    ExecutionEvent,
    ExecutionResult,
    RuntimeInvocation,
)
from .phase3.canonical import sha256_digest
from .phase3.governance import (
    GovernanceApplicationResult,
    GovernanceEvaluationResult,
    GovernanceStore,
    RuntimeGovernanceCore,
)
from .phase3.completion import CompletionContract
from .phase3.task_contract import FrozenContractModel, TaskContract
from .storage import AstraStore

if TYPE_CHECKING:
    from .domain import ExecutionEventSink


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
    governance_evaluation: GovernanceEvaluationResult
    governance_application: GovernanceApplicationResult
    execution_finalized: bool
    run_request_state: str


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class TaskRuntime:
    """Minimal durable Task Runtime introduced by Phase 4 Milestone 1."""

    def __init__(self, store: AstraStore) -> None:
        self.store = store
        self.governance_store = GovernanceStore.from_astra_store(store)
        self.governance_core = RuntimeGovernanceCore(self.governance_store)

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

    def claim_and_start_execution(
        self,
        *,
        execution_id: str | None = None,
        now: datetime | None = None,
    ) -> ExecutionClaimResult | None:
        """Atomically claim the next ready request and create its Execution."""

        claimed_at = now or datetime.now(timezone.utc)
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
        else:
            claimed_at = claimed_at.astimezone(timezone.utc)
        started_at = claimed_at.isoformat()

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
                (row for row in rows if _instant(row["ready_at"]) <= claimed_at),
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
                raise ExecutionEligibilityError(
                    "task_deadline_exceeded", run_request_id
                )
            if (
                contract.limits.attempt_deadline is not None
                and claimed_at >= _instant(contract.limits.attempt_deadline)
            ):
                raise ExecutionEligibilityError(
                    "attempt_deadline_exceeded", run_request_id
                )

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
                        started_at, run_request_id
                    ) VALUES (?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        actual_execution_id,
                        request["task_id"],
                        request["attempt_id"],
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

    def build_runtime_invocation(
        self,
        claim: ExecutionClaimResult,
        *,
        provider_config: Mapping[str, Any] | None = None,
    ) -> RuntimeInvocation:
        """Build the stable executor port from persisted authoritative data."""

        row = self.store.query_one(
            """
            SELECT task.contract_json, execution.session_handle
            FROM phase3_tasks AS task
            JOIN executions AS execution ON execution.task_id = task.task_id
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
            feedback=(),
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
                "SELECT * FROM executions WHERE execution_id = ?",
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
            return persisted

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
        """Mark invocation work complete without assigning a Task outcome."""

        with self.store.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE phase4_run_requests SET state = 'completed'
                WHERE run_request_id = ? AND state = 'claimed'
                """,
                (run_request_id,),
            )
            if cursor.rowcount == 1:
                return "completed"
            row = connection.execute(
                """
                SELECT state FROM phase4_run_requests WHERE run_request_id = ?
                """,
                (run_request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_request_id)
            if row["state"] == "completed":
                return "completed"
            raise RuntimeError(
                f"Run Request {run_request_id!r} cannot complete from "
                f"state {row['state']!r}"
            )


class DeterministicFakeExecutor:
    """In-process executor with no provider, timing, or random dependencies."""

    def __init__(
        self,
        result: ExecutionResult | None = None,
        *,
        result_factory: Callable[[RuntimeInvocation], ExecutionResult] | None = None,
    ) -> None:
        if (result is None) == (result_factory is None):
            raise ValueError("Provide exactly one of result or result_factory")
        self._result = result
        self._result_factory = result_factory
        self.invocations: list[RuntimeInvocation] = []

    async def execute(
        self,
        invocation: RuntimeInvocation,
        event_sink: "ExecutionEventSink",
    ) -> ExecutionResult:
        self.invocations.append(invocation)
        if self._result_factory is not None:
            result = self._result_factory(invocation)
        elif self._result is not None:
            result = self._result.model_copy(
                update={"execution_id": invocation.execution_id}
            )
        else:
            raise AssertionError("Deterministic result source is missing")
        if result.execution_id != invocation.execution_id:
            raise ExecutionResultConflict(
                "execution_result_conflict: fake result execution_id mismatch"
            )
        return result

    async def cancel(self, execution_id: str, reason: str) -> None:
        return None


FakeExecutor = DeterministicFakeExecutor


class SingleWorker:
    """Execute at most one ready Run Request through durable governance."""

    def __init__(
        self,
        runtime: TaskRuntime,
        executor: AgentExecutor,
        *,
        governance_core: RuntimeGovernanceCore | None = None,
        provider_config: Mapping[str, Any] | None = None,
    ) -> None:
        self.runtime = runtime
        self.executor = executor
        self.governance_core = governance_core or runtime.governance_core
        self.provider_config = dict(provider_config or {})

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

        self.governance_core.begin_execution(
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
        evaluation = self.governance_core.evaluate(claim.execution_id)
        application = self.governance_core.apply(
            evaluation.policy_decision.decision_id
        )
        if persisted.session_handle is not None:
            self.runtime.store.save_session_handle(
                claim.execution_id, persisted.session_handle
            )
        finalized = self.runtime.store.finalize_execution(
            execution_id=claim.execution_id,
            status=persisted.status.value,
            termination_reason=persisted.termination_reason,
            payload={
                "governance_decision_id": evaluation.policy_decision.decision_id,
                "governance_application": application.application.value,
                "task_outcome_validated": persisted.task_outcome_validated,
            },
        )
        run_request_state = self.runtime.complete_run_request(
            claim.run_request_id
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
