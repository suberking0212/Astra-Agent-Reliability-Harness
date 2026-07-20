"""SQLite persistence and atomic lifecycle constraints for Astra."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .domain import InteractionKind, InteractionRequest, ResultReceipt


CURRENT_SCHEMA_VERSION = 2


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
    def _validate_schema(connection: sqlite3.Connection) -> None:
        required_tables = {
            "astra_schema",
            "executions",
            "execution_events",
            "interactions",
            "execution_receipts",
            "result_receipts",
            "budget_ledger",
            "trace_spans",
            "phase4_runtime_commands",
            "phase4_run_requests",
        }
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        missing = required_tables.difference(str(row[0]) for row in rows)
        if missing:
            names = ", ".join(sorted(missing))
            raise RuntimeError(f"Database schema is incomplete: {names}")
        execution_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(executions)")
        }
        if "run_request_id" not in execution_columns:
            raise RuntimeError(
                "Database schema is incomplete: executions.run_request_id"
            )
        indexes = {
            str(row[1]): bool(row[2])
            for row in connection.execute("PRAGMA index_list(executions)")
        }
        if not indexes.get("ux_executions_run_request_id"):
            raise RuntimeError(
                "Database schema is incomplete: unique execution run request index"
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
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO executions(
                    execution_id, task_id, attempt_id, status, started_at
                ) VALUES (?, ?, ?, 'running', ?)
                ON CONFLICT(execution_id) DO NOTHING
                """,
                (execution_id, task_id, attempt_id, _now()),
            )
            connection.execute(
                """
                INSERT INTO budget_ledger(execution_id)
                VALUES (?) ON CONFLICT(execution_id) DO NOTHING
                """,
                (execution_id,),
            )

    def get_execution(self, execution_id: str) -> Mapping[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        )
        return dict(row) if row else None

    def save_session_handle(self, execution_id: str, handle: str | None) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE executions SET session_handle = ? WHERE execution_id = ?",
                (handle, execution_id),
            )

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
        interaction = InteractionRequest(
            interaction_id=str(uuid4()),
            execution_id=execution_id,
            task_id=task_id,
            attempt_id=attempt_id,
            kind=kind,
            prompt=prompt,
            payload=payload or {},
        )
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE executions
                SET suspension_requested = 1
                WHERE execution_id = ? AND ended_at IS NULL
                """,
                (execution_id,),
            )
            connection.execute(
                """
                INSERT INTO interactions(
                    interaction_id, execution_id, task_id, attempt_id, kind,
                    prompt, status, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    interaction.interaction_id,
                    execution_id,
                    task_id,
                    attempt_id,
                    kind.value,
                    prompt,
                    _json(interaction.payload),
                    _now(),
                ),
            )
        return interaction

    def resolve_interaction(
        self, interaction_id: str, resolution: Mapping[str, Any]
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT execution_id FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise KeyError(interaction_id)
            connection.execute(
                """
                UPDATE interactions
                SET status = 'resolved', resolution_json = ?, resolved_at = ?
                WHERE interaction_id = ? AND status = 'pending'
                """,
                (_json(resolution), _now(), interaction_id),
            )
            pending = connection.execute(
                """
                SELECT COUNT(*) FROM interactions
                WHERE execution_id = ? AND status = 'pending'
                """,
                (row["execution_id"],),
            ).fetchone()[0]
            if pending == 0:
                connection.execute(
                    """
                    UPDATE executions SET suspension_requested = 0
                    WHERE execution_id = ? AND ended_at IS NULL
                    """,
                    (row["execution_id"],),
                )

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
    ) -> tuple[str, bool]:
        receipt_id = str(uuid4())
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO execution_receipts(
                        receipt_id, execution_id, task_id, attempt_id,
                        tool_name, tool_call_id, idempotency_key,
                        request_json, result_json, side_effect, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    ),
                )
                return receipt_id, True
            except sqlite3.IntegrityError:
                if idempotency_key is None:
                    raise
                row = connection.execute(
                    """
                    SELECT receipt_id FROM execution_receipts
                    WHERE task_id = ? AND tool_name = ? AND idempotency_key = ?
                    """,
                    (task_id, tool_name, idempotency_key),
                ).fetchone()
                if row is None:
                    raise
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
        """Atomically win or lose the right to publish AstraExecutionEnded."""

        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE executions
                SET status = ?, ended_at = ?, termination_reason = ?
                WHERE execution_id = ? AND ended_at IS NULL
                """,
                (status, _now(), termination_reason, execution_id),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                INSERT INTO execution_events(
                    event_id, execution_id, event_type, payload_json, created_at
                ) VALUES (?, ?, 'AstraExecutionEnded', ?, ?)
                """,
                (str(uuid4()), execution_id, _json(payload or {}), _now()),
            )
            return True

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection
