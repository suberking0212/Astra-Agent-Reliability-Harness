"""Deterministic complaint-domain business service backed by SQLite."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .storage import AstraStore


class MockBusinessService:
    """Controlled in-process BusinessService for explicitly injected tests."""

    commerce_authority_domain = "commerce.mock"
    support_authority_domain = "support.mock"

    def __init__(self, store: AstraStore) -> None:
        self.store = store
        self.store.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS mock_customers (
                customer_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mock_orders (
                order_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                status TEXT NOT NULL,
                delivered_at TEXT,
                damage_reported INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS mock_policies (
                policy_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mock_complaint_tickets (
                ticket_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                resolution TEXT NOT NULL,
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            """
        )

    def seed_normal_complaint(self) -> Mapping[str, str]:
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO mock_customers(customer_id, name, email)
                VALUES ('cust-001', 'Lin Mei', 'lin.mei@example.test')
                """
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO mock_orders(
                    order_id, customer_id, item_name, status,
                    delivered_at, damage_reported
                ) VALUES (
                    'order-001', 'cust-001', 'Ceramic tea set', 'delivered',
                    '2026-06-01T00:00:00+00:00', 1
                )
                """
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO mock_policies(policy_id, title, body)
                VALUES (?, ?, ?)
                """,
                (
                    "policy-damaged-goods",
                    "Damaged goods exception",
                    "Verified transit damage may be handled after the normal "
                    "return window. Create a complaint ticket with order evidence.",
                ),
            )
        return {"customer_id": "cust-001", "order_id": "order-001"}

    def get_customer(self, customer_id: str) -> Mapping[str, Any] | None:
        row = self.store.query_one(
            "SELECT * FROM mock_customers WHERE customer_id = ?", (customer_id,)
        )
        return dict(row) if row else None

    def get_order(self, order_id: str) -> Mapping[str, Any] | None:
        row = self.store.query_one(
            "SELECT * FROM mock_orders WHERE order_id = ?", (order_id,)
        )
        return dict(row) if row else None

    def list_customer_orders(self, customer_id: str) -> list[Mapping[str, Any]]:
        rows = self.store.query_all(
            """
            SELECT * FROM mock_orders
            WHERE customer_id = ? ORDER BY order_id
            """,
            (customer_id,),
        )
        return [dict(row) for row in rows]

    def search_policy(self, query: str) -> list[Mapping[str, Any]]:
        words = [word.lower() for word in query.split() if word.strip()]
        rows = self.store.query_all(
            "SELECT * FROM mock_policies ORDER BY policy_id"
        )
        policies: list[Mapping[str, Any]] = [dict(row) for row in rows]
        if not words:
            return policies
        matches: list[Mapping[str, Any]] = []
        for policy in policies:
            haystack = f"{policy['title']} {policy['body']}".lower()
            if any(word in haystack for word in words):
                matches.append(policy)
        return matches

    def create_complaint_ticket(
        self,
        *,
        customer_id: str,
        order_id: str,
        reason: str,
        resolution: str,
        idempotency_key: str,
    ) -> tuple[Mapping[str, Any], bool]:
        ticket_id = f"ticket-{uuid4().hex[:12]}"
        with self.store.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM mock_complaint_tickets WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing:
                return dict(existing), False
            try:
                connection.execute(
                    """
                    INSERT INTO mock_complaint_tickets(
                        ticket_id, customer_id, order_id, reason, resolution,
                        status, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
                    """,
                    (
                        ticket_id,
                        customer_id,
                        order_id,
                        reason,
                        resolution,
                        idempotency_key,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT * FROM mock_complaint_tickets
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if existing is None:
                    raise
                return dict(existing), False
        ticket = self.get_complaint_ticket(ticket_id)
        assert ticket is not None
        return ticket, True

    def get_complaint_ticket(self, ticket_id: str) -> Mapping[str, Any] | None:
        row = self.store.query_one(
            "SELECT * FROM mock_complaint_tickets WHERE ticket_id = ?",
            (ticket_id,),
        )
        return dict(row) if row else None

    def find_complaint_ticket_by_idempotency_key(
        self, idempotency_key: str
    ) -> Mapping[str, Any] | None:
        """Read the business authority by the adapter's stable replay key."""

        row = self.store.query_one(
            """
            SELECT * FROM mock_complaint_tickets WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        )
        return dict(row) if row else None

    def snapshot(self) -> Mapping[str, Any]:
        tickets = self.store.query_all(
            "SELECT * FROM mock_complaint_tickets ORDER BY created_at"
        )
        return {"complaint_tickets": [dict(row) for row in tickets]}
