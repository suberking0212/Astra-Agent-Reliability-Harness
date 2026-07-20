"""Transparent, conservative provider-usage ledger for Phase 2."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .domain import ExecutionUsage
from .storage import AstraStore


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str | None = None


class BudgetLedger:
    def __init__(self, store: AstraStore) -> None:
        self.store = store

    def observe_provider_call(
        self,
        execution_id: str,
        usage: Mapping[str, Any] | None,
        *,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        usage = usage or {}
        input_tokens = int(
            usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        )
        output_tokens = int(
            usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        )
        total_tokens = int(
            usage.get("total_tokens", input_tokens + output_tokens) or 0
        )
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE budget_ledger
                SET provider_request_count = provider_request_count + 1,
                    observed_input_tokens = observed_input_tokens + ?,
                    observed_output_tokens = observed_output_tokens + ?,
                    observed_total_tokens = observed_total_tokens + ?,
                    estimated_cost_usd = estimated_cost_usd + ?
                WHERE execution_id = ?
                """,
                (
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    float(estimated_cost_usd),
                    execution_id,
                ),
            )

    def reserve_next_call(self, execution_id: str, tokens: int) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE budget_ledger SET reserved_next_call_tokens = ?
                WHERE execution_id = ?
                """,
                (max(0, int(tokens)), execution_id),
            )

    def decide(
        self,
        execution_id: str,
        limits: Mapping[str, Any],
    ) -> BudgetDecision:
        mode = str(limits.get("budget_mode", "observe_only"))
        if mode == "observe_only":
            return BudgetDecision(True)
        if mode != "conservative_limit":
            return BudgetDecision(False, f"unsupported_budget_mode:{mode}")

        row = self._row(execution_id)
        token_limit = limits.get("token_budget")
        if token_limit is not None:
            projected = (
                row["observed_total_tokens"] + row["reserved_next_call_tokens"]
            )
            if projected > int(token_limit):
                return BudgetDecision(False, "token_budget_conservative_limit")

        request_limit = limits.get("provider_request_limit")
        if request_limit is not None and row["provider_request_count"] >= int(
            request_limit
        ):
            return BudgetDecision(False, "provider_request_limit")
        return BudgetDecision(True)

    def usage(self, execution_id: str) -> ExecutionUsage:
        row = self._row(execution_id)
        return ExecutionUsage(
            input_tokens=row["observed_input_tokens"],
            output_tokens=row["observed_output_tokens"],
            total_tokens=row["observed_total_tokens"],
            estimated_cost_usd=row["estimated_cost_usd"],
            cost_status="estimated",
            cost_source="phase2_budget_ledger",
        )

    def snapshot(self, execution_id: str) -> Mapping[str, Any]:
        return dict(self._row(execution_id))

    def _row(self, execution_id: str) -> Mapping[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM budget_ledger WHERE execution_id = ?",
            (execution_id,),
        )
        if row is None:
            raise KeyError(execution_id)
        return dict(row)
