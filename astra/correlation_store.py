"""Minimal Astra-owned Hermes session correlation persistence."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CorrelationConflict(RuntimeError):
    pass


class HermesCorrelationStore:
    """Only stores public Hermes session id, Astra task id, and metadata."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS hermes_session_correlations (
                hermes_session_id TEXT PRIMARY KEY,
                astra_task_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT
            )"""
        )
        columns = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(hermes_session_correlations)")}
        for name in ("attempt_id", "execution_id"):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE hermes_session_correlations ADD COLUMN {name} TEXT")
        self.connection.execute("CREATE INDEX IF NOT EXISTS ix_hermes_session_correlations_task ON hermes_session_correlations(astra_task_id)")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def bind(self, hermes_session_id: str, astra_task_id: str, source: str | None = None,
             attempt_id: str | None = None, execution_id: str | None = None) -> dict[str, Any]:
        sid, tid = str(hermes_session_id).strip(), str(astra_task_id).strip()
        if not sid or not tid:
            raise ValueError("hermes_session_id and astra_task_id are required")
        now = _now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT astra_task_id FROM hermes_session_correlations WHERE hermes_session_id = ?", (sid,)
            ).fetchone()
            existing = self.connection.execute(
                "SELECT astra_task_id, status FROM hermes_session_correlations WHERE hermes_session_id = ?", (sid,)
            ).fetchone()
            if existing is not None and str(existing[0]) != tid and str(existing[1]) not in {"terminal", "released"}:
                message = f"correlation_conflict: session {sid!r} already bound to task {row[0]!r}; refusing {tid!r}"
                log.error(message)
                raise CorrelationConflict(message)
            if row is None or (existing is not None and str(existing[1]) in {"terminal", "released"}):
                if row is not None:
                    self.connection.execute("DELETE FROM hermes_session_correlations WHERE hermes_session_id = ?", (sid,))
                self.connection.execute(
                    "INSERT INTO hermes_session_correlations(hermes_session_id, astra_task_id, created_at, last_seen_at, status, source, attempt_id, execution_id) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                    (sid, tid, now, now, source, attempt_id, execution_id),
                )
            else:
                self.connection.execute(
                    "UPDATE hermes_session_correlations SET last_seen_at = ?, status = 'active', source = COALESCE(?, source), attempt_id = COALESCE(?, attempt_id), execution_id = COALESCE(?, execution_id) WHERE hermes_session_id = ?",
                    (now, source, attempt_id, execution_id, sid),
                )
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return self.lookup(sid) or {}

    def mark_status(self, hermes_session_id: str, status: str) -> dict[str, Any] | None:
        if status not in {"active", "recoverable", "terminal", "released"}:
            raise ValueError("invalid correlation status")
        self.connection.execute(
            "UPDATE hermes_session_correlations SET status = ?, last_seen_at = ? WHERE hermes_session_id = ?",
            (status, _now(), str(hermes_session_id).strip()),
        )
        self.connection.commit()
        return self.lookup(hermes_session_id)

    def release(self, hermes_session_id: str) -> dict[str, Any] | None:
        return self.mark_status(hermes_session_id, "released")

    def lookup(self, hermes_session_id: str) -> dict[str, Any] | None:
        sid = str(hermes_session_id).strip()
        if not sid:
            return None
        row = self.connection.execute("SELECT * FROM hermes_session_correlations WHERE hermes_session_id = ?", (sid,)).fetchone()
        return dict(row) if row else None

    def lookup_by_task_id(self, astra_task_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM hermes_session_correlations WHERE astra_task_id = ? ORDER BY created_at",
            (str(astra_task_id).strip(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_seen(self, hermes_session_id: str) -> dict[str, Any] | None:
        row = self.lookup(hermes_session_id)
        if row is None:
            return None
        self.connection.execute("UPDATE hermes_session_correlations SET last_seen_at = ? WHERE hermes_session_id = ?", (_now(), hermes_session_id))
        self.connection.commit()
        return self.lookup(hermes_session_id)
