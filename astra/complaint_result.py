"""Complaint-specific submitted-result evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .business import BusinessService
from .domain import RuntimeInvocation


class ComplaintResultEvaluator:
    evaluator_id = "complaint.result"
    evaluator_version = "1"

    def __init__(self, business: BusinessService) -> None:
        self.business = business

    def handles(
        self,
        invocation: RuntimeInvocation,
        outcome: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
    ) -> bool:
        return "ticket_id" in outcome or any(
            row.get("tool_name") == "create_complaint_ticket" for row in receipts
        )

    def evaluate(
        self,
        invocation: RuntimeInvocation,
        outcome: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, bool]:
        create_receipt_present = any(
            row.get("tool_name") == "create_complaint_ticket" for row in receipts
        )
        ticket_id = str(outcome.get("ticket_id", ""))
        ticket = self.business.get_complaint_ticket(ticket_id) if ticket_id else None
        expected_customer = outcome.get("customer_id")
        expected_order = outcome.get("order_id")
        return {
            "complaint_ticket_exists": ticket is not None,
            "complaint_ticket_matches_outcome": bool(
                ticket
                and (
                    expected_customer is None
                    or ticket["customer_id"] == str(expected_customer)
                )
                and (
                    expected_order is None
                    or ticket["order_id"] == str(expected_order)
                )
            ),
            "create_ticket_receipt_present": create_receipt_present,
        }
