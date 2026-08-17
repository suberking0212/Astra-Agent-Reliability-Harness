"""Commerce order capability registration.

This module is an optional domain extension; the Runtime and Broker do not
import it.  It binds business fields, policy scope, and result projection to
generic Broker primitives.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..capability_broker import CapabilityDefinition


def latest_customer_order_definition(*, authority_domain: str) -> CapabilityDefinition:
    def subjects(_runtime: Any, values: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        customer_id = str(values.get("customer_id", "")).strip()
        if not customer_id:
            raise ValueError("customer_id is required")
        return ({"authority_domain": authority_domain, "type": "customer", "id": customer_id},)

    def project(values: Mapping[str, Any], outcome: Any) -> dict[str, Any]:
        result = outcome.get("result") if isinstance(outcome, dict) else None
        data = result.get("data") if isinstance(result, dict) else None
        orders = data.get("orders") if isinstance(data, dict) else None
        if not outcome.get("ok") or not isinstance(orders, list):
            raise RuntimeError("broker_governed_read_failed")
        candidates = [item for item in orders if isinstance(item, dict)]
        receipt_id = result.get("receipt_id")
        base = {"ok": True, "customer_id": str(values["customer_id"]),
                "receipt_ref": receipt_id,
                "evidence_ref": {"type": "execution_receipt", "id": receipt_id}}
        if not candidates:
            return {**base, "order": None}
        latest = max(candidates, key=lambda item: (str(item.get("delivered_at") or ""), str(item.get("order_id") or "")))
        return {**base, "order_id": latest.get("order_id"), "status": latest.get("status")}

    return CapabilityDefinition(
        name="astra_get_latest_customer_order",
        description="Get a customer's latest authoritative order from Astra.",
        input_schema={"type": "object", "properties": {"customer_id": {"type": "string", "description": "Customer identifier."}}, "required": ["customer_id"], "additionalProperties": False},
        requested_tools=("list_customer_orders",),
        subject_builder=subjects,
        objective_builder=lambda values: "Read the latest authoritative order for customer " + str(values.get("customer_id", "")).strip(),
        result_projector=project,
    )
