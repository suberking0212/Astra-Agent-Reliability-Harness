"""Bearer-authenticated complaint sandbox backed by an independent SQLite DB."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    item_name TEXT NOT NULL,
    status TEXT NOT NULL,
    delivered_at TEXT,
    damage_reported INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS complaint_tickets (
    ticket_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    reason TEXT NOT NULL,
    resolution TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
"""

SEED_CUSTOMER = {
    "customer_id": "cust-s12-s13-001",
    "name": "Astra Sandbox Customer",
    "email": "astra.sandbox.customer@example.test",
}
SEED_ORDER = {
    "order_id": "order-s12-s13-001",
    "customer_id": "cust-s12-s13-001",
    "item_name": "Ceramic tea set",
    "status": "delivered",
    "delivered_at": "2026-07-01T08:00:00+00:00",
    "damage_reported": 1,
}
EVALUATION_SEED_CUSTOMERS = (
    {
        "customer_id": "cust-eval-001",
        "name": "Evaluation Customer 1",
        "email": "eval-1@example.test",
    },
    {
        "customer_id": "cust-eval-002",
        "name": "Evaluation Customer 2",
        "email": "eval-2@example.test",
    },
)
EVALUATION_SEED_ORDERS = (
    {
        "order_id": "order-eval-001",
        "customer_id": "cust-eval-001",
        "item_name": "Ceramic tea set",
        "status": "delivered",
        "delivered_at": "2026-07-01T08:00:00+00:00",
        "damage_reported": 1,
    },
    {
        "order_id": "order-eval-002",
        "customer_id": "cust-eval-002",
        "item_name": "Electric kettle",
        "status": "delivered",
        "delivered_at": "2026-07-01T08:00:00+00:00",
        "damage_reported": 1,
    },
)
SEED_CUSTOMERS = (SEED_CUSTOMER, *EVALUATION_SEED_CUSTOMERS)
SEED_ORDERS = (SEED_ORDER, *EVALUATION_SEED_ORDERS)
SEED_POLICY = {
    "policy_id": "policy-damaged-goods-sandbox",
    "title": "Damaged goods complaint policy",
    "body": (
        "A delivered order with verified transit damage may receive one open "
        "complaint ticket. The ticket must identify the customer and order, "
        "describe the damage, and request replacement or manual review."
    ),
}


class SandboxStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def seed(self) -> None:
        self.initialize()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    """
                    INSERT INTO customers(customer_id, name, email)
                    VALUES (:customer_id, :name, :email)
                    ON CONFLICT(customer_id) DO UPDATE SET
                        name = excluded.name,
                        email = excluded.email
                    """,
                    SEED_CUSTOMERS,
                )
                connection.executemany(
                    """
                    INSERT INTO orders(
                        order_id, customer_id, item_name, status,
                        delivered_at, damage_reported
                    ) VALUES (
                        :order_id, :customer_id, :item_name, :status,
                        :delivered_at, :damage_reported
                    )
                    ON CONFLICT(order_id) DO UPDATE SET
                        customer_id = excluded.customer_id,
                        item_name = excluded.item_name,
                        status = excluded.status,
                        delivered_at = excluded.delivered_at,
                        damage_reported = excluded.damage_reported
                    """,
                    SEED_ORDERS,
                )
                connection.execute(
                    """
                    INSERT INTO policies(policy_id, title, body)
                    VALUES (:policy_id, :title, :body)
                    ON CONFLICT(policy_id) DO UPDATE SET
                        title = excluded.title,
                        body = excluded.body
                    """,
                    SEED_POLICY,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def get(self, table: str, key_column: str, key: str) -> dict[str, Any] | None:
        allowed = {
            ("customers", "customer_id"),
            ("orders", "order_id"),
            ("complaint_tickets", "ticket_id"),
            ("complaint_tickets", "idempotency_key"),
        }
        if (table, key_column) not in allowed:
            raise ValueError("unsupported sandbox lookup")
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {key_column} = ?", (key,)
            ).fetchone()
        return dict(row) if row is not None else None

    def search_policies(self, query: str) -> list[dict[str, Any]]:
        words = [word.casefold() for word in query.split() if word.strip()]
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM policies ORDER BY policy_id"
            ).fetchall()
        policies = [dict(row) for row in rows]
        if not words:
            return policies
        return [
            policy
            for policy in policies
            if any(
                word in f"{policy['title']} {policy['body']}".casefold()
                for word in words
            )
        ]

    def list_customer_orders(self, customer_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM orders
                WHERE customer_id = ? ORDER BY order_id
                """,
                (customer_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_complaint(
        self,
        payload: Mapping[str, str],
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM complaint_tickets WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    ticket = dict(existing)
                    if any(ticket[field] != payload[field] for field in payload):
                        raise IdempotencyConflict
                    connection.commit()
                    return ticket, False

                customer = connection.execute(
                    "SELECT customer_id FROM customers WHERE customer_id = ?",
                    (payload["customer_id"],),
                ).fetchone()
                order = connection.execute(
                    "SELECT customer_id FROM orders WHERE order_id = ?",
                    (payload["order_id"],),
                ).fetchone()
                if customer is None or order is None:
                    raise BusinessValidationError("customer_or_order_not_found")
                if order["customer_id"] != payload["customer_id"]:
                    raise BusinessValidationError("order_customer_mismatch")

                ticket_id = "ticket-" + uuid4().hex
                created_at = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    INSERT INTO complaint_tickets(
                        ticket_id, customer_id, order_id, reason, resolution,
                        status, idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
                    """,
                    (
                        ticket_id,
                        payload["customer_id"],
                        payload["order_id"],
                        payload["reason"],
                        payload["resolution"],
                        idempotency_key,
                        created_at,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM complaint_tickets WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()
                connection.commit()
                assert row is not None
                return dict(row), True
            except BaseException:
                connection.rollback()
                raise


class IdempotencyConflict(RuntimeError):
    pass


class BusinessValidationError(ValueError):
    pass


class SandboxServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: SandboxStore,
        bearer_token: str,
    ) -> None:
        self.store = store
        self.bearer_token = bearer_token
        super().__init__(server_address, SandboxHandler)


class SandboxHandler(BaseHTTPRequestHandler):
    server: SandboxServer
    protocol_version = "HTTP/1.1"

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        print(
            f"business_sandbox method={self.command} status={code} size={size}",
            file=sys.stderr,
            flush=True,
        )

    def _write_json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.bearer_token
        if secrets.compare_digest(supplied, expected):
            return True
        self._write_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": "invalid_bearer_token"},
        )
        return False

    def _path_value(self, prefix: str) -> str | None:
        path = urlparse(self.path).path
        if not path.startswith(prefix):
            return None
        value = unquote(path[len(prefix) :])
        return value if value and "/" not in value else None

    def do_GET(self) -> None:
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/policies":
            query = parse_qs(parsed.query).get("query", [""])[0]
            self._write_json(
                HTTPStatus.OK,
                {"policies": self.server.store.search_policies(query)},
            )
            return
        if parsed.path == "/orders":
            customer_id = parse_qs(parsed.query).get("customer_id", [""])[0]
            if not customer_id:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "customer_id_required"},
                )
                return
            self._write_json(
                HTTPStatus.OK,
                {"orders": self.server.store.list_customer_orders(customer_id)},
            )
            return
        routes = (
            ("/customers/", "customers", "customer_id"),
            ("/orders/", "orders", "order_id"),
            (
                "/complaint-tickets/by-idempotency-key/",
                "complaint_tickets",
                "idempotency_key",
            ),
            ("/complaint-tickets/", "complaint_tickets", "ticket_id"),
        )
        for prefix, table, key_column in routes:
            key = self._path_value(prefix)
            if key is None:
                continue
            result = self.server.store.get(table, key_column, key)
            if result is None:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            else:
                self._write_json(HTTPStatus.OK, result)
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if urlparse(self.path).path != "/complaint-tickets":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        idempotency_key = self.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "idempotency_key_required"},
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length <= 0 or content_length > 65536:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_content_length"},
            )
            return
        try:
            decoded = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return
        required = {"customer_id", "order_id", "reason", "resolution"}
        if (
            not isinstance(decoded, dict)
            or set(decoded) != required
            or any(
                not isinstance(decoded[field], str) or not decoded[field].strip()
                for field in required
            )
        ):
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_complaint_payload"},
            )
            return
        payload = {field: decoded[field].strip() for field in required}
        try:
            ticket, created = self.server.store.create_complaint(
                payload, idempotency_key
            )
        except IdempotencyConflict:
            self._write_json(
                HTTPStatus.CONFLICT,
                {"error": "idempotency_payload_conflict"},
            )
            return
        except BusinessValidationError as exc:
            self._write_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
            return
        self._write_json(
            HTTPStatus.CREATED if created else HTTPStatus.OK,
            {"created": created, "ticket": ticket},
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="astra-business-sandbox")
    parser.add_argument(
        "--database",
        default=os.environ.get(
            "ASTRA_BUSINESS_SANDBOX_DATABASE",
            "var/business-sandbox.sqlite3",
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="initialize and seed the sandbox database")
    serve = commands.add_parser("serve", help="run the HTTP sandbox")
    serve.add_argument(
        "--host",
        default=os.environ.get("ASTRA_BUSINESS_SANDBOX_HOST", "127.0.0.1"),
    )
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ASTRA_BUSINESS_SANDBOX_PORT", "8765")),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = SandboxStore(args.database)
    if args.command == "seed":
        store.seed()
        print(
            json.dumps(
                {
                    "database": str(Path(args.database).resolve()),
                    "customer_id": SEED_CUSTOMER["customer_id"],
                    "order_id": SEED_ORDER["order_id"],
                    "customer_ids": [item["customer_id"] for item in SEED_CUSTOMERS],
                    "order_ids": [item["order_id"] for item in SEED_ORDERS],
                    "policy_id": SEED_POLICY["policy_id"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    token = os.environ.get("ASTRA_BUSINESS_SANDBOX_TOKEN", "").strip()
    if not token:
        raise ValueError("ASTRA_BUSINESS_SANDBOX_TOKEN is required")
    store.initialize()
    server = SandboxServer((args.host, args.port), store, token)
    print(
        json.dumps(
            {
                "database": str(Path(args.database).resolve()),
                "endpoint": f"http://{args.host}:{args.port}",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
