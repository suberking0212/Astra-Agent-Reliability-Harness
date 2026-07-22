"""SQLite persistence and atomic lifecycle constraints for Astra."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .domain import InteractionKind, InteractionRequest, ResultReceipt


CURRENT_SCHEMA_VERSION = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class AstraStore:
    """Small SQLite store with database-backed exactly-once termination."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._transaction_state = threading.local()
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
        try:
            self._initialize()
        except BaseException:
            self._connection.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            depth = getattr(self._transaction_state, "depth", 0)
            if depth:
                self._transaction_state.depth = depth + 1
                try:
                    yield self._connection
                finally:
                    self._transaction_state.depth = depth
                return
            self._connection.execute("BEGIN IMMEDIATE")
            self._transaction_state.depth = 1
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
            finally:
                self._transaction_state.depth = 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def query_one(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, tuple(parameters)).fetchone()

    def query_all(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, tuple(parameters)).fetchall()

    def _initialize(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS astra_schema (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT version FROM astra_schema WHERE singleton = 1"
            ).fetchone()
            version = int(row[0]) if row is not None else 0
            if version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    "Database schema version "
                    f"{version} is newer than supported version "
                    f"{CURRENT_SCHEMA_VERSION}"
                )
            if version == 0:
                self._migrate_legacy_to_v1(connection)
                connection.execute(
                    """
                    INSERT INTO astra_schema(singleton, version, updated_at)
                    VALUES (1, 1, ?)
                    """,
                    (_now(),),
                )
                version = 1
            if version == 1:
                self._migrate_v1_to_v2(connection)
                connection.execute(
                    """
                    UPDATE astra_schema
                    SET version = 2, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (_now(),),
                )
                version = 2
            if version == 2:
                self._migrate_v2_to_v3(connection)
                connection.execute(
                    """
                    UPDATE astra_schema
                    SET version = 3, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (_now(),),
                )
                version = 3
            if version == 3:
                self._migrate_v3_to_v4(connection)
                connection.execute(
                    """
                    UPDATE astra_schema
                    SET version = 4, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (_now(),),
                )
                version = 4
            if version == 4:
                self._migrate_v4_to_v5(connection)
                connection.execute(
                    """
                    UPDATE astra_schema
                    SET version = 5, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (_now(),),
                )
                version = 5
            if version == 5:
                self._migrate_v5_to_v6(connection)
                connection.execute(
                    """
                    UPDATE astra_schema
                    SET version = 6, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (_now(),),
                )
                version = 6
            if version == 6:
                self._migrate_v6_to_v7(connection)
                connection.execute(
                    """
                    UPDATE astra_schema
                    SET version = 7, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (_now(),),
                )
                version = 7
            if version == 7:
                self._migrate_v7_to_v8(connection)
                connection.execute(
                    """
                    UPDATE astra_schema
                    SET version = 8, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (_now(),),
                )
            self._validate_schema(connection)

    @staticmethod
    def _migrate_legacy_to_v1(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS executions (
                execution_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                status TEXT NOT NULL,
                suspension_requested INTEGER NOT NULL DEFAULT 0,
                session_handle TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                termination_reason TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS execution_events (
                event_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(execution_id) REFERENCES executions(execution_id),
                UNIQUE(execution_id, event_type)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS interactions (
                interaction_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                resolution_json TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS execution_receipts (
                receipt_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                idempotency_key TEXT,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                side_effect INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_receipt_idempotency
                ON execution_receipts(task_id, tool_name, idempotency_key)
                WHERE idempotency_key IS NOT NULL
            """,
            """
            CREATE TABLE IF NOT EXISTS result_receipts (
                receipt_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                valid INTEGER NOT NULL,
                receipt_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS budget_ledger (
                execution_id TEXT PRIMARY KEY,
                provider_request_count INTEGER NOT NULL DEFAULT 0,
                observed_input_tokens INTEGER NOT NULL DEFAULT 0,
                observed_output_tokens INTEGER NOT NULL DEFAULT 0,
                observed_total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                reserved_next_call_tokens INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS trace_spans (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL UNIQUE,
                parent_span_id TEXT,
                execution_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                input_summary_json TEXT,
                output_summary_json TEXT,
                status TEXT NOT NULL,
                error_json TEXT,
                duration_ms REAL,
                token_usage_json TEXT,
                FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS phase4_runtime_commands (
                command_id TEXT PRIMARY KEY,
                command_type TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS phase4_run_requests (
                run_request_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                state TEXT NOT NULL,
                priority INTEGER NOT NULL,
                ready_at TEXT NOT NULL,
                created_by_command_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(attempt_id) REFERENCES phase3_attempts(attempt_id),
                FOREIGN KEY(created_by_command_id)
                    REFERENCES phase4_runtime_commands(command_id)
                    DEFERRABLE INITIALLY DEFERRED
            )
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(executions)")
        }
        if "run_request_id" not in columns:
            connection.execute(
                """
                ALTER TABLE executions
                ADD COLUMN run_request_id TEXT
                    REFERENCES phase4_run_requests(run_request_id)
                """
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_executions_run_request_id
            ON executions(run_request_id)
            WHERE run_request_id IS NOT NULL
            """
        )

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS phase4_execution_results (
                execution_id TEXT PRIMARY KEY,
                command_id TEXT NOT NULL UNIQUE,
                result_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(execution_id) REFERENCES executions(execution_id),
                FOREIGN KEY(command_id)
                    REFERENCES phase4_runtime_commands(command_id)
                    DEFERRABLE INITIALLY DEFERRED
            )
            """
        )

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        for table in ("phase3_tasks", "phase3_attempts"):
            exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                (table,),
            ).fetchone()
            if exists is None:
                continue
            columns = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "termination_reason" not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN termination_reason TEXT"
                )

    @classmethod
    def _migrate_v4_to_v5(cls, connection: sqlite3.Connection) -> None:
        """Move every Runtime-owned record under the AstraStore boundary."""

        cls._create_phase3_governance_tables(connection)
        cls._rebuild_interactions_v5(connection)
        cls._extend_run_requests_v5(connection)
        cls._replace_phase3_authority_tables_with_views(connection)
        cls._create_approval_request_table(connection)
        cls._extend_phase3_governance_tables_v5(connection)
        cls._migrate_legacy_policy_work_items_v5(connection)
        cls._validate_v5_authority_data(connection)

    @staticmethod
    def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
        """Add Batch 2 canonical-effect and durable cancel authority."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS phase3_canonical_effect_requests (
                effect_identity TEXT PRIMARY KEY,
                effect_request_hash TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                authority_domain TEXT NOT NULL,
                request_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(attempt_id) REFERENCES phase3_attempts(attempt_id),
                FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS phase4_cancel_intents (
                command_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                expected_task_version INTEGER NOT NULL,
                reason TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(command_id)
                    REFERENCES phase4_runtime_commands(command_id),
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id)
            )
            """
        )
        receipt_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(execution_receipts)")
        }
        if "operation_id" not in receipt_columns:
            connection.execute(
                """
                ALTER TABLE execution_receipts
                ADD COLUMN operation_id TEXT
                    REFERENCES phase3_external_operations(operation_id)
                """
            )

    @staticmethod
    def _migrate_v6_to_v7(connection: sqlite3.Connection) -> None:
        """Add immutable authoritative observations and evidence records."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS phase3_business_observations (
                observation_id TEXT NOT NULL,
                observation_version INTEGER NOT NULL CHECK(observation_version >= 1),
                task_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                authority_domain TEXT NOT NULL,
                effect_identity TEXT NOT NULL,
                effect_request_hash TEXT NOT NULL,
                external_object_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                observation_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(observation_id, observation_version),
                UNIQUE(observation_id, content_hash),
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(operation_id)
                    REFERENCES phase3_external_operations(operation_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS phase3_evidence_snapshots (
                evidence_snapshot_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                collection_trigger_id TEXT NOT NULL,
                authoritative_versions_hash TEXT NOT NULL,
                collector_version TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(
                    task_id, collection_trigger_id,
                    authoritative_versions_hash, collector_version
                ),
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(attempt_id) REFERENCES phase3_attempts(attempt_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS phase3_requirement_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                requirement_id TEXT NOT NULL,
                evidence_snapshot_id TEXT NOT NULL,
                status TEXT NOT NULL,
                evaluation_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(evidence_snapshot_id)
                    REFERENCES phase3_evidence_snapshots(evidence_snapshot_id)
            )
            """
        )

    @staticmethod
    def _migrate_v7_to_v8(connection: sqlite3.Connection) -> None:
        """Persist replayable Task Rule outputs consumed by Task Policy."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS phase3_rule_evaluations (
                rule_evaluation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                decision_point TEXT NOT NULL,
                evidence_snapshot_id TEXT NOT NULL,
                status TEXT NOT NULL,
                evaluation_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(
                    rule_id, rule_version, decision_point,
                    evidence_snapshot_id
                ),
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(evidence_snapshot_id)
                    REFERENCES phase3_evidence_snapshots(evidence_snapshot_id)
            )
            """
        )

    @staticmethod
    def _create_phase3_governance_tables(
        connection: sqlite3.Connection,
    ) -> None:
        # phase3_executions and phase3_interactions intentionally are not
        # created here.  They become compatibility views over the Runtime's
        # authoritative executions and interactions tables below.
        statements = (
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
                termination_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS phase3_attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_by_decision_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                ended_at TEXT,
                termination_reason TEXT,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                UNIQUE(task_id, ordinal)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS phase3_reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_by_decision_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(attempt_id) REFERENCES phase3_attempts(attempt_id)
            )
            """,
            """
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
            )
            """,
            """
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
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS phase3_completion_validations (
                completion_validation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS phase3_completion_contracts (
                contract_id TEXT NOT NULL,
                contract_version TEXT NOT NULL,
                task_type TEXT NOT NULL,
                contract_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(contract_id, contract_version)
            )
            """,
            """
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
            )
            """,
            """
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
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS phase3_outbox (
                outbox_id TEXT PRIMARY KEY,
                fact_id TEXT NOT NULL UNIQUE,
                topic TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                delivery_status TEXT NOT NULL,
                delivery_attempts INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(fact_id) REFERENCES phase3_reliability_facts(fact_id)
            )
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _rebuild_interactions_v5(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(interactions)")
        }
        version_expression = "version" if "version" in columns else "1"
        decision_expression = (
            "created_by_decision_id" if "created_by_decision_id" in columns else "NULL"
        )
        connection.execute(
            """
            CREATE TABLE interactions_v5 (
                interaction_id TEXT PRIMARY KEY,
                execution_id TEXT,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                payload_json TEXT NOT NULL,
                resolution_json TEXT,
                created_by_decision_id TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
            )
            """
        )
        connection.execute(
            f"""
            INSERT INTO interactions_v5(
                interaction_id, execution_id, task_id, attempt_id, kind,
                prompt, status, version, payload_json, resolution_json,
                created_by_decision_id, created_at, resolved_at
            )
            SELECT interaction_id, execution_id, task_id, attempt_id, kind,
                   prompt, status, {version_expression}, payload_json,
                   resolution_json, {decision_expression}, created_at,
                   resolved_at
            FROM interactions
            """
        )
        connection.execute("DROP TABLE interactions")
        connection.execute("ALTER TABLE interactions_v5 RENAME TO interactions")
        connection.execute(
            """
            CREATE UNIQUE INDEX ux_interactions_created_by_decision_id
            ON interactions(created_by_decision_id)
            WHERE created_by_decision_id IS NOT NULL
            """
        )

    @staticmethod
    def _extend_run_requests_v5(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(phase4_run_requests)")
        }
        additions = {
            "created_by_decision_id": "TEXT",
            "source_interaction_id": ("TEXT REFERENCES interactions(interaction_id)"),
            "session_handle": "TEXT",
            "feedback_json": "TEXT",
        }
        for column, declaration in additions.items():
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE phase4_run_requests ADD COLUMN {column} {declaration}"
                )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                ux_phase4_run_requests_created_by_decision_id
            ON phase4_run_requests(created_by_decision_id)
            WHERE created_by_decision_id IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                ux_phase4_run_requests_source_interaction_id
            ON phase4_run_requests(source_interaction_id)
            WHERE source_interaction_id IS NOT NULL
            """
        )

    @staticmethod
    def _replace_phase3_authority_tables_with_views(
        connection: sqlite3.Connection,
    ) -> None:
        interaction_object = connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'phase3_interactions'"
        ).fetchone()
        if interaction_object is not None and interaction_object[0] == "table":
            archive = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE name = 'phase3_interactions_legacy_archive'
                """
            ).fetchone()
            if archive is not None:
                raise RuntimeError(
                    "Cannot archive phase3_interactions: archive already exists"
                )
            conflict = connection.execute(
                """
                SELECT legacy.interaction_id
                FROM phase3_interactions AS legacy
                JOIN interactions AS authoritative
                  ON authoritative.interaction_id = legacy.interaction_id
                WHERE authoritative.task_id != legacy.task_id
                   OR authoritative.attempt_id != legacy.attempt_id
                   OR authoritative.kind != legacy.kind
                   OR authoritative.status != legacy.status
                   OR authoritative.version != legacy.version
                   OR authoritative.payload_json != legacy.payload_json
                   OR authoritative.created_by_decision_id
                      IS NOT legacy.created_by_decision_id
                LIMIT 1
                """
            ).fetchone()
            if conflict is not None:
                raise RuntimeError(
                    "Legacy Interaction conflicts with authoritative record: "
                    + str(conflict["interaction_id"])
                )
            connection.execute(
                """
                INSERT INTO interactions(
                    interaction_id, execution_id, task_id, attempt_id, kind,
                    prompt, status, version, payload_json, resolution_json,
                    created_by_decision_id, created_at, resolved_at
                )
                SELECT interaction_id, NULL, task_id, attempt_id, kind, '',
                       status, version, payload_json, NULL,
                       created_by_decision_id, created_at, NULL
                FROM phase3_interactions
                WHERE NOT EXISTS (
                    SELECT 1 FROM interactions AS authoritative
                    WHERE authoritative.interaction_id =
                          phase3_interactions.interaction_id
                )
                """
            )
            connection.execute(
                """
                ALTER TABLE phase3_interactions
                RENAME TO phase3_interactions_legacy_archive
                """
            )
        elif interaction_object is not None:
            connection.execute("DROP VIEW phase3_interactions")
        connection.execute(
            """
            CREATE VIEW phase3_interactions AS
            SELECT interaction_id, task_id, attempt_id, kind, status, version,
                   payload_json, created_by_decision_id, created_at
            FROM interactions
            """
        )

        execution_object = connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'phase3_executions'"
        ).fetchone()
        if execution_object is not None and execution_object[0] == "table":
            archive = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE name = 'phase3_executions_legacy_archive'
                """
            ).fetchone()
            if archive is not None:
                raise RuntimeError(
                    "Cannot archive phase3_executions: archive already exists"
                )
            connection.execute(
                """
                ALTER TABLE phase3_executions
                RENAME TO phase3_executions_legacy_archive
                """
            )
        elif execution_object is not None:
            connection.execute("DROP VIEW phase3_executions")
        connection.execute(
            """
            CREATE VIEW phase3_executions AS
            SELECT execution.execution_id,
                   execution.task_id,
                   execution.attempt_id,
                   execution.status AS state,
                   request.created_by_decision_id,
                   execution.started_at AS created_at
            FROM executions AS execution
            LEFT JOIN phase4_run_requests AS request
              ON request.run_request_id = execution.run_request_id
            """
        )

    @staticmethod
    def _create_approval_request_table(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS phase3_approval_requests (
                approval_request_id TEXT PRIMARY KEY,
                interaction_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                effect_identity TEXT NOT NULL,
                effect_request_hash TEXT NOT NULL,
                permission_scope TEXT NOT NULL,
                risk_class TEXT NOT NULL,
                approver_policy_ref TEXT NOT NULL,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                created_at TEXT NOT NULL,
                expires_at TEXT,
                FOREIGN KEY(interaction_id)
                    REFERENCES interactions(interaction_id),
                FOREIGN KEY(task_id) REFERENCES phase3_tasks(task_id),
                FOREIGN KEY(attempt_id) REFERENCES phase3_attempts(attempt_id),
                FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
            )
            """
        )

    @staticmethod
    def _extend_phase3_governance_tables_v5(
        connection: sqlite3.Connection,
    ) -> None:
        resolution_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(phase3_approval_resolutions)"
            )
        }
        if "approval_request_id" not in resolution_columns:
            connection.execute(
                """
                ALTER TABLE phase3_approval_resolutions
                ADD COLUMN approval_request_id TEXT
                    REFERENCES phase3_approval_requests(approval_request_id)
                """
            )
        if "interaction_id" not in resolution_columns:
            connection.execute(
                """
                ALTER TABLE phase3_approval_resolutions
                ADD COLUMN interaction_id TEXT
                    REFERENCES interactions(interaction_id)
                """
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                ux_phase3_approval_resolutions_request_id
            ON phase3_approval_resolutions(approval_request_id)
            WHERE approval_request_id IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                ux_phase3_approval_resolutions_interaction_id
            ON phase3_approval_resolutions(interaction_id)
            WHERE interaction_id IS NOT NULL
            """
        )

        decision_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(phase3_policy_decisions)")
        }
        if "derived_record_id" not in decision_columns:
            connection.execute(
                """
                ALTER TABLE phase3_policy_decisions
                ADD COLUMN derived_record_id TEXT
                """
            )

    @staticmethod
    def _migrate_legacy_policy_work_items_v5(
        connection: sqlite3.Connection,
    ) -> None:
        """Convert applied legacy continuation placeholders into real work."""

        candidates: list[tuple[str, str, str, str]] = []
        archive = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'phase3_executions_legacy_archive'
            """
        ).fetchone()
        if archive is not None:
            candidates.extend(
                (
                    str(row["created_by_decision_id"]),
                    str(row["task_id"]),
                    str(row["attempt_id"]),
                    str(row["created_at"]),
                )
                for row in connection.execute(
                    """
                    SELECT created_by_decision_id, task_id, attempt_id, created_at
                    FROM phase3_executions_legacy_archive
                    WHERE created_by_decision_id IS NOT NULL
                    """
                ).fetchall()
            )
        candidates.extend(
            (
                str(row["created_by_decision_id"]),
                str(row["task_id"]),
                str(row["attempt_id"]),
                str(row["created_at"]),
            )
            for row in connection.execute(
                """
                SELECT created_by_decision_id, task_id, attempt_id, created_at
                FROM phase3_attempts
                WHERE created_by_decision_id IS NOT NULL AND state = 'active'
                """
            ).fetchall()
        )

        for decision_id, task_id, attempt_id, created_at in candidates:
            decision_row = connection.execute(
                """
                SELECT decision_key, decision_json, application_status
                FROM phase3_policy_decisions WHERE decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
            if decision_row is None or decision_row["application_status"] != "applied":
                continue
            decision_payload = json.loads(decision_row["decision_json"])
            action = str(decision_payload.get("action", ""))
            if action not in {"continue_with_feedback", "start_new_attempt"}:
                continue
            existing = connection.execute(
                """
                SELECT run_request_id FROM phase4_run_requests
                WHERE created_by_decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE phase3_policy_decisions SET derived_record_id = ?
                    WHERE decision_id = ?
                    """,
                    (existing["run_request_id"], decision_id),
                )
                continue

            task = connection.execute(
                "SELECT state FROM phase3_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT state FROM phase3_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if (
                task is None
                or attempt is None
                or task["state"] != "running"
                or attempt["state"] != "active"
            ):
                continue

            command_id = "policy-decision:" + decision_id
            command_payload = {
                "command_type": "apply_policy_decision",
                "decision_id": decision_id,
                "decision_key": str(decision_row["decision_key"]),
            }
            payload_hash = "sha256:" + hashlib.sha256(
                json.dumps(
                    command_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO phase4_runtime_commands(
                    command_id, command_type, payload_hash, result_json, created_at
                ) VALUES (?, 'apply_policy_decision', ?, ?, ?)
                ON CONFLICT(command_id) DO NOTHING
                """,
                (
                    command_id,
                    payload_hash,
                    _json({"decision_id": decision_id}),
                    created_at,
                ),
            )
            feedback = decision_payload.get("feedback")
            feedback_json = _json(
                (
                    [{"type": "PolicyFeedback", **feedback}]
                    if isinstance(feedback, Mapping)
                    else []
                )
            )
            session = connection.execute(
                """
                SELECT session_handle FROM executions
                WHERE attempt_id = ? AND session_handle IS NOT NULL
                ORDER BY started_at DESC LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
            run_request_id = "run-request:" + decision_id
            connection.execute(
                """
                INSERT INTO phase4_run_requests(
                    run_request_id, task_id, attempt_id, reason, state,
                    priority, ready_at, created_by_command_id,
                    created_by_decision_id, source_interaction_id,
                    session_handle, feedback_json, created_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    run_request_id,
                    task_id,
                    attempt_id,
                    (
                        "continue_with_feedback"
                        if action == "continue_with_feedback"
                        else "new_attempt"
                    ),
                    created_at,
                    command_id,
                    decision_id,
                    session["session_handle"] if session is not None else None,
                    feedback_json,
                    created_at,
                ),
            )
            connection.execute(
                """
                UPDATE phase3_policy_decisions SET derived_record_id = ?
                WHERE decision_id = ?
                """,
                (run_request_id, decision_id),
            )

    @staticmethod
    def _validate_v5_authority_data(connection: sqlite3.Connection) -> None:
        orphaned_running = connection.execute(
            """
            SELECT task.task_id
            FROM phase3_tasks AS task
            WHERE task.state = 'running'
              AND NOT EXISTS (
                  SELECT 1 FROM phase4_run_requests AS request
                  WHERE request.task_id = task.task_id
                    AND request.state IN ('pending', 'claimed')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM executions AS execution
                  WHERE execution.task_id = task.task_id
                    AND execution.ended_at IS NULL
              )
              AND NOT EXISTS (
                  SELECT 1 FROM phase3_reconciliations AS reconciliation
                  WHERE reconciliation.task_id = task.task_id
                    AND reconciliation.status IN ('pending', 'running')
              )
            LIMIT 1
            """
        ).fetchone()
        if orphaned_running is not None:
            raise RuntimeError(
                "Running Task has no authoritative Runtime work after migration: "
                + str(orphaned_running["task_id"])
            )

        missing_wait = connection.execute(
            """
            SELECT task.task_id
            FROM phase3_tasks AS task
            WHERE task.state IN ('waiting_input', 'waiting_approval')
              AND NOT EXISTS (
                  SELECT 1 FROM interactions AS interaction
                  WHERE interaction.task_id = task.task_id
                    AND interaction.status = 'pending'
                    AND (
                        (task.state = 'waiting_input'
                         AND interaction.kind = 'user_input')
                        OR
                        (task.state = 'waiting_approval'
                         AND interaction.kind = 'approval')
                    )
              )
            LIMIT 1
            """
        ).fetchone()
        if missing_wait is not None:
            raise RuntimeError(
                "Waiting Task has no authoritative pending Interaction after migration: "
                + str(missing_wait["task_id"])
            )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        required_columns = {
            "astra_schema": {"singleton", "version", "updated_at"},
            "executions": {
                "execution_id",
                "task_id",
                "attempt_id",
                "status",
                "suspension_requested",
                "session_handle",
                "started_at",
                "ended_at",
                "termination_reason",
                "run_request_id",
            },
            "execution_events": {
                "event_id",
                "execution_id",
                "event_type",
                "payload_json",
                "created_at",
            },
            "interactions": {
                "interaction_id",
                "execution_id",
                "task_id",
                "attempt_id",
                "kind",
                "prompt",
                "status",
                "version",
                "payload_json",
                "resolution_json",
                "created_by_decision_id",
                "created_at",
                "resolved_at",
            },
            "execution_receipts": {
                "receipt_id",
                "execution_id",
                "task_id",
                "attempt_id",
                "tool_name",
                "tool_call_id",
                "idempotency_key",
                "request_json",
                "result_json",
                "side_effect",
                "created_at",
                "operation_id",
            },
            "result_receipts": {
                "receipt_id",
                "execution_id",
                "task_id",
                "valid",
                "receipt_json",
                "created_at",
            },
            "budget_ledger": {
                "execution_id",
                "provider_request_count",
                "observed_input_tokens",
                "observed_output_tokens",
                "observed_total_tokens",
                "estimated_cost_usd",
                "reserved_next_call_tokens",
            },
            "trace_spans": {
                "sequence",
                "trace_id",
                "span_id",
                "parent_span_id",
                "execution_id",
                "task_id",
                "attempt_id",
                "event_type",
                "timestamp",
                "source",
                "input_summary_json",
                "output_summary_json",
                "status",
                "error_json",
                "duration_ms",
                "token_usage_json",
            },
            "phase3_tasks": {
                "task_id",
                "contract_json",
                "contract_id",
                "contract_version",
                "contract_hash",
                "state",
                "version",
                "current_attempt_id",
                "escalation_required",
                "termination_reason",
                "created_at",
                "updated_at",
            },
            "phase3_attempts": {
                "attempt_id",
                "task_id",
                "ordinal",
                "state",
                "version",
                "created_by_decision_id",
                "created_at",
                "ended_at",
                "termination_reason",
            },
            "phase3_reconciliations": {
                "reconciliation_id",
                "task_id",
                "attempt_id",
                "status",
                "created_by_decision_id",
                "created_at",
            },
            "phase3_external_operations": {
                "operation_id",
                "task_id",
                "attempt_id",
                "authority_domain",
                "effect_identity",
                "effect_request_hash",
                "status",
                "version",
                "required_for_completion",
                "operation_json",
                "created_at",
            },
            "phase3_canonical_effect_requests": {
                "effect_identity",
                "effect_request_hash",
                "task_id",
                "attempt_id",
                "execution_id",
                "authority_domain",
                "request_json",
                "created_at",
            },
            "phase3_approval_requests": {
                "approval_request_id",
                "interaction_id",
                "task_id",
                "attempt_id",
                "execution_id",
                "effect_identity",
                "effect_request_hash",
                "permission_scope",
                "risk_class",
                "approver_policy_ref",
                "request_json",
                "status",
                "version",
                "created_at",
                "expires_at",
            },
            "phase3_approval_resolutions": {
                "approval_resolution_id",
                "task_id",
                "effect_identity",
                "effect_request_hash",
                "decision",
                "usage_semantics",
                "usage_count",
                "revoked",
                "resolution_json",
                "created_at",
                "approval_request_id",
                "interaction_id",
            },
            "phase3_completion_validations": {
                "completion_validation_id",
                "task_id",
                "status",
                "validation_json",
                "created_at",
            },
            "phase3_business_observations": {
                "observation_id",
                "observation_version",
                "task_id",
                "operation_id",
                "authority_domain",
                "effect_identity",
                "effect_request_hash",
                "external_object_id",
                "content_hash",
                "observation_json",
                "recorded_at",
            },
            "phase3_evidence_snapshots": {
                "evidence_snapshot_id",
                "task_id",
                "attempt_id",
                "collection_trigger_id",
                "authoritative_versions_hash",
                "collector_version",
                "content_hash",
                "snapshot_json",
                "created_at",
            },
            "phase3_requirement_evaluations": {
                "evaluation_id",
                "task_id",
                "requirement_id",
                "evidence_snapshot_id",
                "status",
                "evaluation_json",
                "created_at",
            },
            "phase3_rule_evaluations": {
                "rule_evaluation_id",
                "task_id",
                "rule_id",
                "rule_version",
                "decision_point",
                "evidence_snapshot_id",
                "status",
                "evaluation_json",
                "created_at",
            },
            "phase3_completion_contracts": {
                "contract_id",
                "contract_version",
                "task_type",
                "contract_json",
                "created_at",
            },
            "phase3_policy_decisions": {
                "decision_id",
                "decision_key",
                "task_id",
                "attempt_id",
                "application_status",
                "decision_json",
                "created_at",
                "applied_at",
                "derived_record_id",
            },
            "phase3_reliability_facts": {
                "sequence",
                "fact_id",
                "fact_type",
                "task_id",
                "attempt_id",
                "source_kind",
                "source_name",
                "source_event_id",
                "fact_json",
                "recorded_at",
            },
            "phase3_outbox": {
                "outbox_id",
                "fact_id",
                "topic",
                "payload_json",
                "delivery_status",
                "delivery_attempts",
                "created_at",
            },
            "phase4_runtime_commands": {
                "command_id",
                "command_type",
                "payload_hash",
                "result_json",
                "created_at",
            },
            "phase4_run_requests": {
                "run_request_id",
                "task_id",
                "attempt_id",
                "reason",
                "state",
                "priority",
                "ready_at",
                "created_by_command_id",
                "created_at",
                "created_by_decision_id",
                "source_interaction_id",
                "session_handle",
                "feedback_json",
            },
            "phase4_execution_results": {
                "execution_id",
                "command_id",
                "result_hash",
                "result_json",
                "created_at",
            },
            "phase4_cancel_intents": {
                "command_id",
                "task_id",
                "expected_task_version",
                "reason",
                "payload_hash",
                "created_at",
            },
        }
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        tables = {str(row[0]) for row in table_rows}
        missing_tables = set(required_columns).difference(tables)
        if missing_tables:
            names = ", ".join(sorted(missing_tables))
            raise RuntimeError(f"Database schema is incomplete: {names}")

        for table, expected in required_columns.items():
            actual = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            missing_columns = expected.difference(actual)
            if missing_columns:
                names = ", ".join(sorted(missing_columns))
                raise RuntimeError(f"Database schema is incomplete: {table}.({names})")

        interaction_execution = next(
            row
            for row in connection.execute("PRAGMA table_info(interactions)")
            if str(row[1]) == "execution_id"
        )
        if bool(interaction_execution[3]):
            raise RuntimeError(
                "Database schema is incompatible: "
                "interactions.execution_id must allow NULL"
            )
        run_request_columns = {
            str(row[1]): row
            for row in connection.execute(
                "PRAGMA table_info(phase4_run_requests)"
            )
        }
        if not bool(run_request_columns["created_by_command_id"][3]):
            raise RuntimeError(
                "Database schema is incompatible: "
                "phase4_run_requests.created_by_command_id must remain required"
            )

        required_views = {
            "phase3_executions": {
                "execution_id",
                "task_id",
                "attempt_id",
                "state",
                "created_by_decision_id",
                "created_at",
            },
            "phase3_interactions": {
                "interaction_id",
                "task_id",
                "attempt_id",
                "kind",
                "status",
                "version",
                "payload_json",
                "created_by_decision_id",
                "created_at",
            },
        }
        for view, expected in required_views.items():
            object_row = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = ?", (view,)
            ).fetchone()
            if object_row is None or str(object_row[0]) != "view":
                raise RuntimeError(
                    f"Database schema is incomplete: {view} must be a view"
                )
            actual = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({view})")
            }
            missing_columns = expected.difference(actual)
            if missing_columns:
                names = ", ".join(sorted(missing_columns))
                raise RuntimeError(f"Database schema is incomplete: {view}.({names})")

        named_indexes = {
            "executions": {
                "ux_executions_run_request_id": (("run_request_id",), True),
            },
            "execution_receipts": {
                "ux_receipt_idempotency": (
                    ("task_id", "tool_name", "idempotency_key"),
                    True,
                ),
            },
            "interactions": {
                "ux_interactions_created_by_decision_id": (
                    ("created_by_decision_id",),
                    True,
                ),
            },
            "phase4_run_requests": {
                "ux_phase4_run_requests_created_by_decision_id": (
                    ("created_by_decision_id",),
                    True,
                ),
                "ux_phase4_run_requests_source_interaction_id": (
                    ("source_interaction_id",),
                    True,
                ),
            },
            "phase3_approval_resolutions": {
                "ux_phase3_approval_resolutions_request_id": (
                    ("approval_request_id",),
                    True,
                ),
                "ux_phase3_approval_resolutions_interaction_id": (
                    ("interaction_id",),
                    True,
                ),
            },
        }
        for table, expected_indexes in named_indexes.items():
            available = {
                str(row[1]): (bool(row[2]), bool(row[4]))
                for row in connection.execute(f"PRAGMA index_list({table})")
            }
            for name, (expected_columns, expected_partial) in expected_indexes.items():
                properties = available.get(name)
                if properties != (True, expected_partial):
                    raise RuntimeError(
                        f"Database schema is incomplete: unique index {name}"
                    )
                actual_columns = tuple(
                    str(row[2])
                    for row in connection.execute(f"PRAGMA index_info({name})")
                )
                if actual_columns != expected_columns:
                    raise RuntimeError(f"Database schema is incompatible: index {name}")
                index_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (name,),
                ).fetchone()
                normalized_sql = " ".join(str(index_sql[0]).lower().split())
                predicate_column = expected_columns[-1]
                if f"where {predicate_column} is not null" not in normalized_sql:
                    raise RuntimeError(
                        f"Database schema is incompatible: partial index {name}"
                    )

        required_unique_indexes = {
            "execution_events": {("execution_id", "event_type")},
            "phase3_attempts": {
                ("created_by_decision_id",),
                ("task_id", "ordinal"),
            },
            "phase3_reconciliations": {("created_by_decision_id",)},
            "phase3_external_operations": {("authority_domain", "effect_identity")},
            "phase3_canonical_effect_requests": {("effect_request_hash",)},
            "phase3_approval_requests": {("interaction_id",)},
            "phase3_completion_contracts": {("contract_id", "contract_version")},
            "phase3_policy_decisions": {("decision_key",)},
            "phase3_reliability_facts": {
                ("fact_id",),
                ("source_kind", "source_name", "source_event_id"),
            },
            "phase3_outbox": {("fact_id",)},
            "phase4_run_requests": {("created_by_command_id",)},
            "phase4_execution_results": {("command_id",)},
        }
        for table, expected_indexes in required_unique_indexes.items():
            actual_indexes: set[tuple[str, ...]] = set()
            for row in connection.execute(f"PRAGMA index_list({table})"):
                if not bool(row[2]):
                    continue
                name = str(row[1])
                actual_indexes.add(
                    tuple(
                        str(info[2])
                        for info in connection.execute(f"PRAGMA index_info({name})")
                    )
                )
            missing_indexes = expected_indexes.difference(actual_indexes)
            if missing_indexes:
                raise RuntimeError(
                    f"Database schema is incomplete: unique constraints on {table}"
                )

    @property
    def schema_version(self) -> int:
        row = self.query_one(
            "SELECT version FROM astra_schema WHERE singleton = 1"
        )
        if row is None:
            raise RuntimeError("Database schema version is not initialized")
        return int(row[0])

    def start_execution(
        self, execution_id: str, task_id: str, attempt_id: str
    ) -> None:
        raise RuntimeError("Execution must be created from a claimed Run Request")

    def get_execution(self, execution_id: str) -> Mapping[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        )
        return dict(row) if row else None

    def save_session_handle(self, execution_id: str, handle: str | None) -> None:
        raise RuntimeError("Execution session must be persisted through TaskRuntime")

    def request_interaction(
        self,
        *,
        execution_id: str,
        task_id: str,
        attempt_id: str,
        kind: InteractionKind,
        prompt: str,
        payload: Mapping[str, Any] | None = None,
    ) -> InteractionRequest:
        raise RuntimeError("Interaction must be requested through TaskRuntime")

    def resolve_interaction(
        self, interaction_id: str, resolution: Mapping[str, Any]
    ) -> None:
        raise RuntimeError("Interaction must be resolved through TaskRuntime")

    def is_suspended(self, execution_id: str) -> bool:
        row = self.query_one(
            """
            SELECT suspension_requested FROM executions
            WHERE execution_id = ?
            """,
            (execution_id,),
        )
        return bool(row and row[0])

    def pending_interaction(
        self, execution_id: str
    ) -> Mapping[str, Any] | None:
        row = self.query_one(
            """
            SELECT * FROM interactions
            WHERE execution_id = ? AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
            """,
            (execution_id,),
        )
        return dict(row) if row else None

    def get_interaction(
        self, interaction_id: str
    ) -> Mapping[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM interactions WHERE interaction_id = ?",
            (interaction_id,),
        )
        return dict(row) if row else None

    def add_execution_receipt(
        self,
        *,
        execution_id: str,
        task_id: str,
        attempt_id: str,
        tool_name: str,
        tool_call_id: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
        side_effect: bool,
        idempotency_key: str | None,
        operation_id: str | None = None,
    ) -> tuple[str, bool]:
        receipt_id = str(uuid4())
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO execution_receipts(
                        receipt_id, execution_id, task_id, attempt_id,
                        tool_name, tool_call_id, idempotency_key,
                        request_json, result_json, side_effect, created_at,
                        operation_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        execution_id,
                        task_id,
                        attempt_id,
                        tool_name,
                        tool_call_id,
                        idempotency_key,
                        _json(request),
                        _json(result),
                        int(side_effect),
                        _now(),
                        operation_id,
                    ),
                )
                return receipt_id, True
            except sqlite3.IntegrityError:
                if idempotency_key is None:
                    raise
                row = connection.execute(
                    """
                    SELECT receipt_id, operation_id FROM execution_receipts
                    WHERE task_id = ? AND tool_name = ? AND idempotency_key = ?
                    """,
                    (task_id, tool_name, idempotency_key),
                ).fetchone()
                if row is None:
                    raise
                if row["operation_id"] != operation_id:
                    raise RuntimeError("receipt_external_operation_conflict")
                return str(row[0]), False

    def get_execution_receipts(
        self, receipt_ids: Sequence[str]
    ) -> list[Mapping[str, Any]]:
        if not receipt_ids:
            return []
        placeholders = ",".join("?" for _ in receipt_ids)
        rows = self.query_all(
            f"SELECT * FROM execution_receipts WHERE receipt_id IN ({placeholders})",
            tuple(receipt_ids),
        )
        return [dict(row) for row in rows]

    def add_result_receipt(self, receipt: ResultReceipt) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO result_receipts(
                    receipt_id, execution_id, task_id, valid,
                    receipt_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    receipt.execution_id,
                    receipt.task_id,
                    int(receipt.valid),
                    receipt.model_dump_json(),
                    _now(),
                ),
            )

    def latest_result_receipt(
        self, execution_id: str
    ) -> Mapping[str, Any] | None:
        row = self.query_one(
            """
            SELECT receipt_json FROM result_receipts
            WHERE execution_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (execution_id,),
        )
        return json.loads(row[0]) if row else None

    def finalize_execution(
        self,
        *,
        execution_id: str,
        status: str,
        termination_reason: str,
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        raise RuntimeError("Execution must be finalized through TaskRuntime")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection
