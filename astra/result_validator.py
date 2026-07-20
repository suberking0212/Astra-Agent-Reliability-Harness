"""Minimal contract-based result validation for the normal complaint path."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from .domain import ResultReceipt, RuntimeInvocation
from .mock_business import MockBusinessService
from .storage import AstraStore


class MinimalResultValidator:
    def __init__(self, store: AstraStore, business: MockBusinessService) -> None:
        self.store = store
        self.business = business

    def validate(
        self,
        invocation: RuntimeInvocation,
        outcome: Mapping[str, Any],
        evidence_refs: Sequence[str],
        receipt_refs: Sequence[str],
    ) -> ResultReceipt:
        receipts = self.store.get_execution_receipts(receipt_refs)
        receipt_ids = {str(row["receipt_id"]) for row in receipts}
        refs_complete = receipt_ids == set(receipt_refs)
        refs_owned = all(
            row["execution_id"] == invocation.execution_id
            and row["task_id"] == invocation.task_id
            for row in receipts
        )

        ticket_id = str(outcome.get("ticket_id", ""))
        ticket = self.business.get_complaint_ticket(ticket_id) if ticket_id else None
        expected_customer = str(outcome.get("customer_id", ""))
        expected_order = str(outcome.get("order_id", ""))
        ticket_exists = ticket is not None
        ticket_matches = bool(
            ticket
            and ticket["customer_id"] == expected_customer
            and ticket["order_id"] == expected_order
        )
        create_receipt_present = any(
            row["tool_name"] == "create_complaint_ticket" for row in receipts
        )
        evidence_backed = bool(evidence_refs) and set(evidence_refs).issubset(
            receipt_ids
        )
        no_unresolved_interaction = self.store.pending_interaction(
            invocation.execution_id
        ) is None

        checks = {
            "receipt_refs_complete": refs_complete,
            "receipt_refs_owned_by_execution": refs_owned,
            "complaint_ticket_exists": ticket_exists,
            "complaint_ticket_matches_outcome": ticket_matches,
            "create_ticket_receipt_present": create_receipt_present,
            "evidence_refs_backed_by_receipts": evidence_backed,
            "no_unresolved_interaction": no_unresolved_interaction,
        }
        errors = tuple(name for name, passed in checks.items() if not passed)
        receipt = ResultReceipt(
            receipt_id=str(uuid4()),
            execution_id=invocation.execution_id,
            task_id=invocation.task_id,
            valid=not errors,
            outcome=dict(outcome),
            evidence_refs=tuple(evidence_refs),
            receipt_refs=tuple(receipt_refs),
            checks=checks,
            errors=errors,
        )
        self.store.add_result_receipt(receipt)
        return receipt
