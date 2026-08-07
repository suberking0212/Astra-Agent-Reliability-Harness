"""Production business authority port and HTTP sandbox adapter."""

from __future__ import annotations

import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


class BusinessService(Protocol):
    """Business operations required by the production Tool Gateway."""

    commerce_authority_domain: str
    support_authority_domain: str

    def get_customer(self, customer_id: str) -> Mapping[str, Any] | None: ...

    def get_order(self, order_id: str) -> Mapping[str, Any] | None: ...

    def list_customer_orders(self, customer_id: str) -> list[Mapping[str, Any]]: ...

    def search_policy(self, query: str) -> list[Mapping[str, Any]]: ...

    def create_complaint_ticket(
        self,
        *,
        customer_id: str,
        order_id: str,
        reason: str,
        resolution: str,
        idempotency_key: str,
    ) -> tuple[Mapping[str, Any], bool]: ...

    def get_complaint_ticket(self, ticket_id: str) -> Mapping[str, Any] | None: ...

    def find_complaint_ticket_by_idempotency_key(
        self, idempotency_key: str
    ) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class BusinessSandboxConfig:
    endpoint: str
    token: str
    commerce_authority_domain: str
    support_authority_domain: str
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        endpoint = self.endpoint.strip().rstrip("/")
        token = self.token.strip()
        commerce = self.commerce_authority_domain.strip()
        support = self.support_authority_domain.strip()
        if not endpoint:
            raise ValueError("business sandbox endpoint is required")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("business sandbox endpoint must be an absolute HTTP URL")
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not loopback:
            raise ValueError(
                "business sandbox endpoint must use HTTPS unless it is loopback"
            )
        if not token:
            raise ValueError("business sandbox token is required")
        if not commerce or not support:
            raise ValueError("business authority domains are required")
        if self.timeout_seconds <= 0:
            raise ValueError("business sandbox timeout must be positive")
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "commerce_authority_domain", commerce)
        object.__setattr__(self, "support_authority_domain", support)


class HttpBusinessSandboxAdapter:
    """Minimal REST adapter for the complaint-domain business sandbox.

    The endpoint must expose:

    - ``GET /customers/{customer_id}``
    - ``GET /orders/{order_id}``
    - ``GET /policies?query=...``
    - ``POST /complaint-tickets`` with an ``Idempotency-Key`` header
    - ``GET /complaint-tickets/{ticket_id}``
    - ``GET /complaint-tickets/by-idempotency-key/{key}``
    """

    def __init__(self, config: BusinessSandboxConfig) -> None:
        self.config = config
        self.commerce_authority_domain = config.commerce_authority_domain
        self.support_authority_domain = config.support_authority_domain

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        not_found_none: bool = False,
    ) -> tuple[int, Any] | None:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.token}",
        }
        if payload is not None:
            body = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(
            self.config.endpoint + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(
                request,
                timeout=self.config.timeout_seconds,
                context=ssl.create_default_context(),
            ) as response:
                status = int(response.status)
                raw = response.read()
        except HTTPError as exc:
            if exc.code == 404 and not_found_none:
                return None
            raise RuntimeError(
                f"business_sandbox_http_error:{exc.code}:{method}:{path}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(
                f"business_sandbox_unavailable:{method}:{path}:{type(exc).__name__}"
            ) from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"business_sandbox_invalid_json:{method}:{path}"
            ) from exc
        return status, decoded

    @staticmethod
    def _object(payload: Any, *, operation: str) -> Mapping[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError(f"business_sandbox_invalid_response:{operation}")
        return dict(payload)

    def get_customer(self, customer_id: str) -> Mapping[str, Any] | None:
        result = self._request(
            "GET", f"/customers/{quote(customer_id, safe='')}", not_found_none=True
        )
        return None if result is None else self._object(result[1], operation="customer")

    def get_order(self, order_id: str) -> Mapping[str, Any] | None:
        result = self._request(
            "GET", f"/orders/{quote(order_id, safe='')}", not_found_none=True
        )
        return None if result is None else self._object(result[1], operation="order")

    def list_customer_orders(self, customer_id: str) -> list[Mapping[str, Any]]:
        result = self._request(
            "GET", "/orders?" + urlencode({"customer_id": customer_id})
        )
        assert result is not None
        payload = result[1]
        if isinstance(payload, dict):
            payload = payload.get("orders")
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise RuntimeError("business_sandbox_invalid_response:customer_orders")
        orders = [dict(item) for item in payload]
        if any(str(order.get("customer_id", "")) != customer_id for order in orders):
            raise RuntimeError("business_sandbox_response_mismatch:customer_orders")
        return orders

    def search_policy(self, query: str) -> list[Mapping[str, Any]]:
        result = self._request("GET", "/policies?" + urlencode({"query": query}))
        assert result is not None
        payload = result[1]
        if isinstance(payload, dict):
            payload = payload.get("policies")
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise RuntimeError("business_sandbox_invalid_response:policies")
        return [dict(item) for item in payload]

    def create_complaint_ticket(
        self,
        *,
        customer_id: str,
        order_id: str,
        reason: str,
        resolution: str,
        idempotency_key: str,
    ) -> tuple[Mapping[str, Any], bool]:
        result = self._request(
            "POST",
            "/complaint-tickets",
            payload={
                "customer_id": customer_id,
                "order_id": order_id,
                "reason": reason,
                "resolution": resolution,
            },
            idempotency_key=idempotency_key,
        )
        assert result is not None
        status, payload = result
        created = status == 201
        if isinstance(payload, dict) and isinstance(payload.get("ticket"), dict):
            created_value = payload.get("created")
            if isinstance(created_value, bool):
                created = created_value
            payload = payload["ticket"]
        ticket = self._object(payload, operation="complaint_ticket_create")
        for field, expected in (
            ("customer_id", customer_id),
            ("order_id", order_id),
        ):
            if str(ticket.get(field, "")) != expected:
                raise RuntimeError(
                    f"business_sandbox_response_mismatch:complaint_ticket:{field}"
                )
        if not str(ticket.get("ticket_id", "")):
            raise RuntimeError(
                "business_sandbox_invalid_response:complaint_ticket:ticket_id"
            )
        return ticket, created

    def get_complaint_ticket(self, ticket_id: str) -> Mapping[str, Any] | None:
        result = self._request(
            "GET",
            f"/complaint-tickets/{quote(ticket_id, safe='')}",
            not_found_none=True,
        )
        return (
            None
            if result is None
            else self._object(result[1], operation="complaint_ticket")
        )

    def find_complaint_ticket_by_idempotency_key(
        self, idempotency_key: str
    ) -> Mapping[str, Any] | None:
        result = self._request(
            "GET",
            "/complaint-tickets/by-idempotency-key/" + quote(idempotency_key, safe=""),
            not_found_none=True,
        )
        return (
            None
            if result is None
            else self._object(result[1], operation="complaint_ticket")
        )
