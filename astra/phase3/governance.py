"""Runtime Governance Core required by the frozen Phase 3 contracts.

This callable component is composed into the existing Phase 2 Runtime. It
applies immutable PolicyDecisions but does not form a parallel Runtime, select
tools, schedule business steps, retry Hermes calls, or encode a workflow.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from .approval import ApprovalResolution, verify_approval_binding
from .canonical import sha256_digest
from .completion import CompletionValidationResult
from .effects import (
    CanonicalEffectRequest,
    ExternalOperation,
    ExternalOperationStatus,
)
from .facts import FactSource, OutboxMessage, ReliabilityFact
from .policy import (
    DecisionApplication,
    DecisionApplicationSnapshot,
    PolicyAction,
    PolicyDecision,
    TaskState,
    apply_policy_decision,
)
from .task_contract import FrozenContractModel, SubjectRef, TaskContract

if TYPE_CHECKING:
    from ..storage import AstraStore
    from .round2 import Round2ValidationReport


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    if isinstance(value, FrozenContractModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


class AttemptState(str, Enum):
    ACTIVE = "active"
    WAITING = "waiting"
    RECONCILING = "reconciling"
    COMPLETED = "completed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


class GovernanceApplicationResult(FrozenContractModel):
    decision_id: str
    application: DecisionApplication
    task_id: str
    task_state: TaskState
    task_version: int = Field(ge=0)
    attempt_id: str
    attempt_state: AttemptState
    attempt_version: int = Field(ge=0)
    derived_record_id: str | None = None


class GovernanceStore:
    """SQLite authoritative records owned by the Runtime Governance Core."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._transaction_factory: (
            Callable[[], Iterator[sqlite3.Connection]] | None
        ) = None
        self._owns_connection = True
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    @classmethod
    def from_astra_store(cls, store: "AstraStore") -> "GovernanceStore":
        """Bind governance tables and transactions to an existing Phase 2 store."""

        instance = cls.__new__(cls)
        instance.path = store.path
        instance._lock = threading.RLock()
        instance._connection = store.connection
        instance._transaction_factory = store.transaction
        instance._owns_connection = False
        instance._initialize()
        return instance

    def close(self) -> None:
        if self._owns_connection:
            with self._lock:
                self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._transaction_factory is not None:
            with self._transaction_factory() as connection:
                yield connection
            return
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS phase3_tasks (
                task_id TEXT PRIMARY KEY,
                contract_json TEXT NOT NULL,
                contract_id TEXT NOT NULL,
                contract_version TEXT NOT NULL,
                contract_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                current_attempt_id TEXT NOT NULL,
                escalation_required INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS phase3_attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_by_decision_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                ended_at TEXT,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                UNIQUE(task_id, ordinal)
            );

            CREATE TABLE IF NOT EXISTS phase3_executions (
                execution_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_by_decision_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(attempt_id) REFERENCES phase3_attempts(attempt_id)
            );

            CREATE TABLE IF NOT EXISTS phase3_interactions (
                interaction_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_by_decision_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(attempt_id) REFERENCES phase3_attempts(attempt_id)
            );

            CREATE TABLE IF NOT EXISTS phase3_reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_by_decision_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(attempt_id) REFERENCES phase3_attempts(attempt_id)
            );

            CREATE TABLE IF NOT EXISTS phase3_external_operations (
                operation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                authority_domain TEXT NOT NULL,
                effect_identity TEXT NOT NULL,
                effect_request_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL,
                required_for_completion INTEGER NOT NULL,
                operation_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                UNIQUE(authority_domain, effect_identity)
            );

            CREATE TABLE IF NOT EXISTS phase3_approval_resolutions (
                approval_resolution_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                effect_identity TEXT NOT NULL,
                effect_request_hash TEXT NOT NULL,
                decision TEXT NOT NULL,
                usage_semantics TEXT NOT NULL,
                usage_count INTEGER NOT NULL,
                revoked INTEGER NOT NULL,
                resolution_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id)
            );

            CREATE TABLE IF NOT EXISTS phase3_completion_validations (
                completion_validation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id)
            );

            CREATE TABLE IF NOT EXISTS phase3_policy_decisions (
                decision_id TEXT PRIMARY KEY,
                decision_key TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                application_status TEXT NOT NULL DEFAULT 'pending',
                decision_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                applied_at TEXT,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id)
            );

            CREATE TABLE IF NOT EXISTS phase3_reliability_facts (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_id TEXT NOT NULL UNIQUE,
                fact_type TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_id TEXT,
                source_kind TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                fact_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(source_kind, source_name, source_event_id)
            );

            CREATE TABLE IF NOT EXISTS phase3_outbox (
                outbox_id TEXT PRIMARY KEY,
                fact_id TEXT NOT NULL UNIQUE,
                topic TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                delivery_status TEXT NOT NULL,
                delivery_attempts INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(fact_id) REFERENCES phase3_reliability_facts(fact_id)
            );
            """
        )

    def query_one(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchone()

    def query_all(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, parameters).fetchall()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection


class RuntimeGovernanceCore:
    """Callable governance component used by the existing Phase 2 Runtime."""

    def __init__(self, store: GovernanceStore) -> None:
        self.store = store

    def create_task(
        self,
        contract: TaskContract,
        *,
        task_id: str,
        attempt_id: str,
        task_version: int = 1,
        attempt_version: int = 1,
    ) -> None:
        now = _now().isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO phase3_tasks(
                    task_id, contract_json, contract_id, contract_version,
                    contract_hash, state, version, current_attempt_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    task_id,
                    contract.model_dump_json(),
                    contract.contract_id,
                    contract.contract_version,
                    contract.contract_hash,
                    task_version,
                    attempt_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO phase3_attempts(
                    attempt_id, task_id, ordinal, state, version, created_at
                ) VALUES (?, ?, 1, 'active', ?, ?)
                """,
                (attempt_id, task_id, attempt_version, now),
            )

    def record_approval_resolution(
        self, task_id: str, resolution: ApprovalResolution
    ) -> None:
        """Persist an immutable exact-effect ApprovalResolution."""

        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO phase3_approval_resolutions(
                    approval_resolution_id, task_id, effect_identity,
                    effect_request_hash, decision, usage_semantics, usage_count,
                    revoked, resolution_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_resolution_id) DO NOTHING
                """,
                (
                    resolution.approval_resolution_id,
                    task_id,
                    resolution.effect_identity,
                    resolution.effect_request_hash,
                    resolution.decision.value,
                    resolution.usage_semantics.value,
                    resolution.usage_count,
                    int(resolution.revoked),
                    resolution.model_dump_json(),
                    _now().isoformat(),
                ),
            )
            existing = connection.execute(
                """
                SELECT task_id, resolution_json
                FROM phase3_approval_resolutions
                WHERE approval_resolution_id = ?
                """,
                (resolution.approval_resolution_id,),
            ).fetchone()
            if (
                existing is None
                or existing["task_id"] != task_id
                or ApprovalResolution.model_validate_json(
                    existing["resolution_json"]
                )
                != resolution
            ):
                raise ValueError("ApprovalResolution identity conflict")

    def prepare_external_operation(
        self,
        effect: CanonicalEffectRequest,
        *,
        task_id: str,
        attempt_id: str,
        execution_id: str,
        approval_resolution_id: str | None = None,
        operation_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ExternalOperation, bool]:
        """Atomically verify/consume approval and prepare one effect operation."""

        current_time = now or _now()
        with self.store.transaction() as connection:
            task = connection.execute(
                "SELECT contract_json FROM phase3_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            contract = TaskContract.model_validate_json(task[0])
            if effect.task_contract_ref != contract.ref:
                raise PermissionError("effect_stale_contract")
            existing_row = connection.execute(
                """
                SELECT operation_json FROM phase3_external_operations
                WHERE authority_domain = ? AND effect_identity = ?
                """,
                (effect.authority_domain, effect.effect_identity),
            ).fetchone()
            existing = (
                ExternalOperation.model_validate_json(existing_row[0])
                if existing_row is not None
                else None
            )

            resolution = None
            if approval_resolution_id is not None:
                approval_row = connection.execute(
                    """
                    SELECT * FROM phase3_approval_resolutions
                    WHERE approval_resolution_id = ? AND task_id = ?
                    """,
                    (approval_resolution_id, task_id),
                ).fetchone()
                if approval_row is None:
                    raise PermissionError("approval_required")
                stored = ApprovalResolution.model_validate_json(
                    approval_row["resolution_json"]
                )
                resolution = stored.model_copy(
                    update={
                        "usage_count": approval_row["usage_count"],
                        "revoked": bool(approval_row["revoked"]),
                    }
                )
            binding = verify_approval_binding(
                contract,
                effect,
                resolution,
                now=current_time,
                existing_operation_effect_identity=(
                    existing.effect_identity if existing is not None else None
                ),
            )
            if not binding.matched:
                raise PermissionError(binding.code.value)
            if existing is not None:
                if existing.effect_request_hash != effect.effect_request_hash:
                    raise RuntimeError("effect_identity_request_hash_conflict")
                return existing, False

            operation = ExternalOperation.from_effect(
                effect,
                operation_id=operation_id
                or "operation:" + sha256_digest(effect.effect_identity),
                task_id=task_id,
                attempt_id=attempt_id,
                execution_id=execution_id,
                approval_ref=(
                    resolution.approval_resolution_id if resolution else None
                ),
                created_at=current_time,
            )
            connection.execute(
                """
                INSERT INTO phase3_external_operations(
                    operation_id, task_id, attempt_id, authority_domain,
                    effect_identity, effect_request_hash, status, version,
                    required_for_completion, operation_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', 1, 1, ?, ?)
                """,
                (
                    operation.operation_id,
                    task_id,
                    attempt_id,
                    operation.authority_domain,
                    operation.effect_identity,
                    operation.effect_request_hash,
                    operation.model_dump_json(),
                    current_time.isoformat(),
                ),
            )
            if resolution is not None:
                connection.execute(
                    """
                    UPDATE phase3_approval_resolutions
                    SET usage_count = usage_count + 1
                    WHERE approval_resolution_id = ?
                    """,
                    (resolution.approval_resolution_id,),
                )
            self._publish_operation_fact(
                connection,
                operation,
                fact_type="side_effect_requested",
            )
            return operation, True

    def transition_external_operation(
        self,
        operation_id: str,
        status: ExternalOperationStatus,
        *,
        external_operation_id: str | None = None,
    ) -> ExternalOperation:
        """Persist an authoritative operation status/version transition."""

        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT operation_json FROM phase3_external_operations
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            operation = ExternalOperation.model_validate_json(row[0])
            if status == operation.status:
                return operation
            allowed_transitions = {
                ExternalOperationStatus.PREPARED: {
                    ExternalOperationStatus.IN_FLIGHT,
                    ExternalOperationStatus.ACKNOWLEDGED,
                    ExternalOperationStatus.CONFIRMED,
                    ExternalOperationStatus.INDETERMINATE,
                    ExternalOperationStatus.FAILED,
                },
                ExternalOperationStatus.IN_FLIGHT: {
                    ExternalOperationStatus.ACKNOWLEDGED,
                    ExternalOperationStatus.CONFIRMED,
                    ExternalOperationStatus.INDETERMINATE,
                    ExternalOperationStatus.FAILED,
                },
                ExternalOperationStatus.ACKNOWLEDGED: {
                    ExternalOperationStatus.CONFIRMED,
                    ExternalOperationStatus.INDETERMINATE,
                    ExternalOperationStatus.FAILED,
                },
                ExternalOperationStatus.INDETERMINATE: {
                    ExternalOperationStatus.CONFIRMED,
                    ExternalOperationStatus.FAILED,
                },
                ExternalOperationStatus.CONFIRMED: set(),
                ExternalOperationStatus.FAILED: set(),
            }
            if status not in allowed_transitions[operation.status]:
                raise ValueError(
                    f"Invalid ExternalOperation transition: "
                    f"{operation.status.value} -> {status.value}"
                )
            updates: dict[str, Any] = {
                "status": status,
                "version": operation.version + 1,
            }
            if external_operation_id is not None:
                updates["external_operation_id"] = external_operation_id
            if status == ExternalOperationStatus.ACKNOWLEDGED:
                updates["acknowledged_at"] = _now()
            if status == ExternalOperationStatus.CONFIRMED:
                updates["confirmed_at"] = _now()
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
                    operation_id,
                    operation.version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("ExternalOperation CAS failed")
            if status == ExternalOperationStatus.CONFIRMED:
                self._publish_operation_fact(
                    connection,
                    changed,
                    fact_type="external_operation_confirmed",
                )
            return changed

    def record_completion_validation(
        self, task_id: str, validation: CompletionValidationResult
    ) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO phase3_completion_validations(
                    completion_validation_id, task_id, status,
                    validation_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(completion_validation_id) DO NOTHING
                """,
                (
                    validation.completion_validation_id,
                    task_id,
                    validation.status.value,
                    validation.model_dump_json(),
                    _now().isoformat(),
                ),
            )
            existing = connection.execute(
                """
                SELECT task_id, validation_json
                FROM phase3_completion_validations
                WHERE completion_validation_id = ?
                """,
                (validation.completion_validation_id,),
            ).fetchone()
            if (
                existing is None
                or existing["task_id"] != task_id
                or CompletionValidationResult.model_validate_json(
                    existing["validation_json"]
                )
                != validation
            ):
                raise ValueError("CompletionValidation identity conflict")

    def record_policy_decision(self, decision: PolicyDecision) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO phase3_policy_decisions(
                    decision_id, decision_key, task_id, attempt_id,
                    decision_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_key) DO NOTHING
                """,
                (
                    decision.decision_id,
                    decision.decision_key,
                    decision.task_id,
                    decision.attempt_id,
                    decision.model_dump_json(),
                    _now().isoformat(),
                ),
            )
            existing = connection.execute(
                """
                SELECT decision_json FROM phase3_policy_decisions
                WHERE decision_key = ?
                """,
                (decision.decision_key,),
            ).fetchone()
            if (
                existing is None
                or PolicyDecision.model_validate_json(existing[0]) != decision
            ):
                raise ValueError("PolicyDecision identity conflict")

    def apply_decision(self, decision_id: str) -> GovernanceApplicationResult:
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
                return self._result_from_rows(
                    decision,
                    DecisionApplication(decision_row["application_status"]),
                    task,
                    attempt,
                )

            completion = None
            if decision.expected_completion_validation_id is not None:
                row = connection.execute(
                    """
                    SELECT validation_json FROM phase3_completion_validations
                    WHERE completion_validation_id = ? AND task_id = ?
                    """,
                    (
                        decision.expected_completion_validation_id,
                        decision.task_id,
                    ),
                ).fetchone()
                if row is not None:
                    completion = CompletionValidationResult.model_validate_json(row[0])
            pending_interaction = connection.execute(
                """
                SELECT COUNT(*) FROM phase3_interactions
                WHERE task_id = ? AND status = 'pending'
                """,
                (decision.task_id,),
            ).fetchone()[0]
            reconciliation = connection.execute(
                """
                SELECT COUNT(*) FROM phase3_reconciliations
                WHERE task_id = ? AND status IN ('pending', 'running')
                """,
                (decision.task_id,),
            ).fetchone()[0]
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
                connection.execute(
                    """
                    UPDATE phase3_policy_decisions
                    SET application_status = ?, applied_at = ?
                    WHERE decision_id = ? AND application_status = 'pending'
                    """,
                    (application.value, _now().isoformat(), decision_id),
                )
                return self._result_from_rows(
                    decision, application, task, attempt
                )

            derived_record_id = self._apply_action(
                connection, decision, task, attempt
            )
            connection.execute(
                """
                UPDATE phase3_policy_decisions
                SET application_status = 'applied', applied_at = ?
                WHERE decision_id = ? AND application_status = 'pending'
                """,
                (_now().isoformat(), decision_id),
            )
            refreshed_task = connection.execute(
                "SELECT * FROM phase3_tasks WHERE task_id = ?",
                (decision.task_id,),
            ).fetchone()
            refreshed_attempt_id = refreshed_task["current_attempt_id"]
            refreshed_attempt = connection.execute(
                "SELECT * FROM phase3_attempts WHERE attempt_id = ?",
                (refreshed_attempt_id,),
            ).fetchone()
            result = self._result_from_rows(
                decision,
                DecisionApplication.APPLIED,
                refreshed_task,
                refreshed_attempt,
                derived_record_id=derived_record_id,
            )
            return result

    @staticmethod
    def _interaction_watermark(
        connection: sqlite3.Connection, task_id: str
    ) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) FROM phase3_interactions
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        return int(row[0])

    def _apply_action(
        self,
        connection: sqlite3.Connection,
        decision: PolicyDecision,
        task: sqlite3.Row,
        attempt: sqlite3.Row,
    ) -> str | None:
        action = decision.action
        task_state = TaskState(task["state"])
        attempt_state = AttemptState(attempt["state"])
        derived_record_id = None
        escalation = int(task["escalation_required"])
        ended_at = None
        if action == PolicyAction.COMPLETE:
            task_state = TaskState.SUCCEEDED
            attempt_state = AttemptState.COMPLETED
            ended_at = _now().isoformat()
        elif action == PolicyAction.REQUEST_INPUT:
            task_state = TaskState.WAITING_INPUT
            attempt_state = AttemptState.WAITING
            derived_record_id = self._create_interaction(
                connection, decision, "user_input"
            )
        elif action == PolicyAction.REQUEST_APPROVAL:
            task_state = TaskState.WAITING_APPROVAL
            attempt_state = AttemptState.WAITING
            derived_record_id = self._create_interaction(
                connection, decision, "approval"
            )
        elif action == PolicyAction.RECONCILE:
            task_state = TaskState.RECONCILING
            attempt_state = AttemptState.RECONCILING
            derived_record_id = self._create_reconciliation(connection, decision)
        elif action in {PolicyAction.FAIL, PolicyAction.ESCALATE}:
            task_state = TaskState.FAILED
            attempt_state = AttemptState.FAILED
            escalation = int(action == PolicyAction.ESCALATE)
            ended_at = _now().isoformat()
        elif action == PolicyAction.CONTINUE_WITH_FEEDBACK:
            task_state = TaskState.RUNNING
            attempt_state = AttemptState.ACTIVE
            derived_record_id = self._create_execution(connection, decision)
        elif action == PolicyAction.START_NEW_ATTEMPT:
            connection.execute(
                """
                UPDATE phase3_attempts
                SET state = 'superseded', version = version + 1, ended_at = ?
                WHERE attempt_id = ?
                """,
                (_now().isoformat(), attempt["attempt_id"]),
            )
            derived_record_id = "attempt:" + decision.decision_id
            ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM phase3_attempts WHERE task_id = ?",
                (decision.task_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO phase3_attempts(
                    attempt_id, task_id, ordinal, state, version,
                    created_by_decision_id, created_at
                ) VALUES (?, ?, ?, 'active', 1, ?, ?)
                """,
                (
                    derived_record_id,
                    decision.task_id,
                    ordinal,
                    decision.decision_id,
                    _now().isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE phase3_tasks
                SET state = 'running', version = version + 1,
                    current_attempt_id = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (derived_record_id, _now().isoformat(), decision.task_id),
            )
            self._publish_state_fact(
                connection,
                decision,
                TaskState.RUNNING,
                derived_record_id,
            )
            return derived_record_id

        connection.execute(
            """
            UPDATE phase3_tasks
            SET state = ?, version = version + 1,
                escalation_required = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                task_state.value,
                escalation,
                _now().isoformat(),
                decision.task_id,
            ),
        )
        connection.execute(
            """
            UPDATE phase3_attempts
            SET state = ?, version = version + 1, ended_at = COALESCE(?, ended_at)
            WHERE attempt_id = ?
            """,
            (attempt_state.value, ended_at, attempt["attempt_id"]),
        )
        self._publish_state_fact(
            connection, decision, task_state, attempt["attempt_id"]
        )
        return derived_record_id

    @staticmethod
    def _create_interaction(
        connection: sqlite3.Connection,
        decision: PolicyDecision,
        kind: str,
    ) -> str:
        interaction_id = "interaction:" + decision.decision_id
        connection.execute(
            """
            INSERT INTO phase3_interactions(
                interaction_id, task_id, attempt_id, kind, status, version,
                payload_json, created_by_decision_id, created_at
            ) VALUES (?, ?, ?, ?, 'pending', 1, ?, ?, ?)
            """,
            (
                interaction_id,
                decision.task_id,
                decision.attempt_id,
                kind,
                _json(decision.interaction_spec or {}),
                decision.decision_id,
                _now().isoformat(),
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
                _now().isoformat(),
            ),
        )
        return reconciliation_id

    @staticmethod
    def _create_execution(
        connection: sqlite3.Connection, decision: PolicyDecision
    ) -> str:
        execution_id = "execution:" + decision.decision_id
        connection.execute(
            """
            INSERT INTO phase3_executions(
                execution_id, task_id, attempt_id, state,
                created_by_decision_id, created_at
            ) VALUES (?, ?, ?, 'running', ?, ?)
            """,
            (
                execution_id,
                decision.task_id,
                decision.attempt_id,
                decision.decision_id,
                _now().isoformat(),
            ),
        )
        return execution_id

    @staticmethod
    def _publish_operation_fact(
        connection: sqlite3.Connection,
        operation: ExternalOperation,
        *,
        fact_type: str,
    ) -> None:
        occurred = _now()
        fact_id = (
            f"fact:{operation.operation_id}:{fact_type}:v{operation.version}"
        )
        fact = ReliabilityFact(
            fact_id=fact_id,
            fact_type=fact_type,
            task_id=operation.task_id,
            attempt_id=operation.attempt_id,
            execution_id=operation.execution_id,
            source=FactSource(
                kind="external_operation_store", name="astra", version="1"
            ),
            authority_scope="external_operation",
            source_event_id=f"{operation.operation_id}:{fact_type}:v{operation.version}",
            subject_ref=SubjectRef(
                type="external_operation",
                id=operation.operation_id,
                version=operation.version,
                authority_domain="astra",
            ),
            occurred_at=occurred,
            evidence_refs=(operation.operation_id,),
            attributes={
                "effect_identity": operation.effect_identity,
                "effect_request_hash": operation.effect_request_hash,
                "status": operation.status.value,
            },
        )
        RuntimeGovernanceCore._insert_fact_outbox(connection, fact)

    @staticmethod
    def _publish_state_fact(
        connection: sqlite3.Connection,
        decision: PolicyDecision,
        task_state: TaskState,
        attempt_id: str,
    ) -> None:
        occurred = _now()
        fact_id = "fact:" + decision.decision_id
        fact = ReliabilityFact(
            fact_id=fact_id,
            fact_type="task_state_changed",
            task_id=decision.task_id,
            attempt_id=attempt_id,
            source=FactSource(kind="task_runtime", name="astra", version="1"),
            authority_scope="task",
            source_event_id=decision.decision_id + ":applied",
            subject_ref=SubjectRef(
                type="task", id=decision.task_id, authority_domain="astra"
            ),
            occurred_at=occurred,
            evidence_refs=(decision.decision_id,),
            attributes={
                "state": task_state.value,
                "policy_action": decision.action.value,
            },
        )
        RuntimeGovernanceCore._insert_fact_outbox(connection, fact)

    @staticmethod
    def _insert_fact_outbox(
        connection: sqlite3.Connection, fact: ReliabilityFact
    ) -> None:
        outbox = OutboxMessage(
            outbox_id="outbox:" + fact.fact_id,
            fact_id=fact.fact_id,
            topic="astra.phase3.reliability_fact",
            payload=fact.model_dump(mode="json", exclude_none=True),
            created_at=fact.recorded_at,
        )
        connection.execute(
            """
            INSERT INTO phase3_reliability_facts(
                fact_id, fact_type, task_id, attempt_id, source_kind,
                source_name, source_event_id, fact_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.fact_id,
                fact.fact_type,
                fact.task_id,
                fact.attempt_id,
                fact.source.kind,
                fact.source.name,
                fact.source_event_id,
                fact.model_dump_json(),
                fact.recorded_at.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO phase3_outbox(
                outbox_id, fact_id, topic, payload_json, delivery_status,
                delivery_attempts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outbox.outbox_id,
                outbox.fact_id,
                outbox.topic,
                _json(outbox.payload),
                outbox.delivery_status,
                outbox.delivery_attempts,
                outbox.created_at.isoformat(),
            ),
        )

    @staticmethod
    def _result_from_rows(
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


def validate_governance_round2_fixture(
    fixture: Mapping[str, Any],
) -> "Round2ValidationReport":
    """Exercise Round 2 decisions through SQLite CAS and atomic Fact/Outbox."""

    from .round2 import (
        Round2Path,
        Round2ValidationFailure,
        Round2ValidationReport,
        evaluate_round2_contract_chain,
    )
    from ..runtime import Phase2Runtime
    from ..storage import AstraStore

    scenarios = tuple(fixture.get("scenarios", ()))
    paths = dict(fixture.get("paths", {}))
    failures: list[Round2ValidationFailure] = []
    passed = 0
    expected_states = {
        PolicyAction.COMPLETE: TaskState.SUCCEEDED,
        PolicyAction.REQUEST_INPUT: TaskState.WAITING_INPUT,
        PolicyAction.REQUEST_APPROVAL: TaskState.WAITING_APPROVAL,
        PolicyAction.RECONCILE: TaskState.RECONCILING,
        PolicyAction.FAIL: TaskState.FAILED,
        PolicyAction.ESCALATE: TaskState.FAILED,
        PolicyAction.CONTINUE_WITH_FEEDBACK: TaskState.RUNNING,
        PolicyAction.START_NEW_ATTEMPT: TaskState.RUNNING,
    }
    for scenario in scenarios:
        scenario_id = str(scenario["scenario_id"])
        for path_name, raw_path in paths.items():
            task_id = f"task-{scenario_id}-{path_name}"
            attempt_id = f"attempt-{scenario_id}-{path_name}"
            execution_id = f"execution-{scenario_id}-{path_name}"
            path = Round2Path.model_validate(raw_path)
            chain = evaluate_round2_contract_chain(
                task_contract=scenario["task_contract"],
                normalized_parameters=scenario["normalized_parameters"],
                normalizer_id=scenario["normalizer"]["normalizer_id"],
                normalizer_version=scenario["normalizer"]["normalizer_version"],
                path=path,
                task_id=task_id,
                attempt_id=attempt_id,
                execution_id=execution_id,
            )
            phase2_store = AstraStore()
            store = GovernanceStore.from_astra_store(phase2_store)
            governance = RuntimeGovernanceCore(store)
            runtime = Phase2Runtime(
                phase2_store, governance_core=governance
            )
            try:
                governance.create_task(
                    chain.task_contract,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    task_version=12 + path.current_task_version_offset,
                    attempt_version=5,
                )
                if chain.approval_resolution is not None:
                    governance.record_approval_resolution(
                        task_id, chain.approval_resolution
                    )
                if chain.external_operation is not None:
                    if chain.canonical_effect_request is None:
                        raise RuntimeError("Operation is missing its canonical effect")
                    prepared, created = governance.prepare_external_operation(
                        chain.canonical_effect_request,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        execution_id=execution_id,
                        approval_resolution_id=(
                            chain.approval_resolution.approval_resolution_id
                            if chain.approval_resolution
                            else None
                        ),
                        operation_id=chain.external_operation.operation_id,
                        now=chain.external_operation.created_at,
                    )
                    if not created:
                        raise RuntimeError("First operation prepare must create")
                    governance.transition_external_operation(
                        prepared.operation_id,
                        chain.external_operation.status,
                    )
                governance.record_completion_validation(
                    task_id, chain.completion_validation
                )
                governance.record_policy_decision(chain.policy_decision)
                result = runtime.apply_governance_decision(
                    chain.policy_decision.decision_id
                )
                expected_application = path.expected_application or ""
                case_failures: list[tuple[str, str, str]] = []
                if result.application.value != expected_application:
                    case_failures.append(
                        (
                            "runtime_application",
                            expected_application,
                            result.application.value,
                        )
                    )
                if result.application == DecisionApplication.APPLIED:
                    expected_state = expected_states[chain.policy_action]
                    if result.task_state != expected_state:
                        case_failures.append(
                            (
                                "runtime_task_state",
                                expected_state.value,
                                result.task_state.value,
                            )
                        )
                    fact_count = store.query_one(
                        "SELECT COUNT(*) FROM phase3_reliability_facts"
                    )[0]
                    outbox_count = store.query_one(
                        "SELECT COUNT(*) FROM phase3_outbox"
                    )[0]
                    operation_fact_count = 0
                    if chain.external_operation is not None:
                        operation_fact_count = 1 + int(
                            chain.external_operation.status
                            == ExternalOperationStatus.CONFIRMED
                        )
                    expected_fact_count = operation_fact_count + 1
                    if (
                        fact_count != expected_fact_count
                        or outbox_count != expected_fact_count
                    ):
                        case_failures.append(
                            (
                                "atomic_fact_outbox",
                                f"{expected_fact_count}/{expected_fact_count}",
                                f"{fact_count}/{outbox_count}",
                            )
                        )
                for field, expected, actual in case_failures:
                    failures.append(
                        Round2ValidationFailure(
                            scenario_id=scenario_id,
                            path_name=str(path_name),
                            field=field,
                            expected=expected,
                            actual=actual,
                        )
                    )
                if not case_failures:
                    passed += 1
            finally:
                store.close()
                phase2_store.close()
    return Round2ValidationReport(
        fixture_schema_version=str(fixture.get("fixture_schema_version", "")),
        scenario_count=len(scenarios),
        path_count=len(paths),
        case_count=len(scenarios) * len(paths),
        passed_count=passed,
        failures=tuple(failures),
    )
