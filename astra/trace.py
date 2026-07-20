"""Neutral, non-intervening execution trace collector."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .domain import ExecutionEvent
from .storage import AstraStore


class NeutralTraceCollector:
    def __init__(self, store: AstraStore) -> None:
        self.store = store

    def record(
        self,
        *,
        trace_id: str,
        execution_id: str,
        task_id: str,
        attempt_id: str,
        event_type: str,
        source: str,
        parent_span_id: str | None = None,
        input_summary: Mapping[str, Any] | None = None,
        output_summary: Mapping[str, Any] | None = None,
        status: str = "ok",
        error: Mapping[str, Any] | None = None,
        duration_ms: float | None = None,
        token_usage: Mapping[str, Any] | None = None,
    ) -> str:
        span_id = str(uuid4())
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO trace_spans(
                    trace_id, span_id, parent_span_id, execution_id, task_id,
                    attempt_id, event_type, timestamp, source,
                    input_summary_json, output_summary_json, status,
                    error_json, duration_ms, token_usage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    span_id,
                    parent_span_id,
                    execution_id,
                    task_id,
                    attempt_id,
                    event_type,
                    datetime.now(timezone.utc).isoformat(),
                    source,
                    json.dumps(input_summary, default=str) if input_summary else None,
                    json.dumps(output_summary, default=str) if output_summary else None,
                    status,
                    json.dumps(error, default=str) if error else None,
                    duration_ms,
                    json.dumps(token_usage, default=str) if token_usage else None,
                ),
            )
        return span_id

    def event(self, event: ExecutionEvent, trace_id: str) -> str:
        return self.record(
            trace_id=trace_id,
            execution_id=event.execution_id,
            task_id=event.task_id,
            attempt_id=event.attempt_id,
            event_type=event.event_type,
            source=event.source,
            output_summary=event.payload,
        )

    def timeline(self, execution_id: str) -> list[Mapping[str, Any]]:
        rows = self.store.query_all(
            """
            SELECT * FROM trace_spans WHERE execution_id = ?
            ORDER BY sequence
            """,
            (execution_id,),
        )
        return [dict(row) for row in rows]
