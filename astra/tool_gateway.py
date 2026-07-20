"""Minimal controlled tool gateway for the Phase 2 complaint scenario."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from .domain import ToolInvocationContext, ToolResult
from .mock_business import MockBusinessService
from .storage import AstraStore


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetCustomerArgs(ToolArgs):
    customer_id: str


class GetOrderArgs(ToolArgs):
    order_id: str


class SearchPolicyArgs(ToolArgs):
    query: str


class CreateComplaintTicketArgs(ToolArgs):
    customer_id: str
    order_id: str
    reason: str
    resolution: str


class GetComplaintTicketArgs(ToolArgs):
    ticket_id: str


class ToolSpec:
    def __init__(
        self,
        args_model: type[ToolArgs],
        handler: Callable[[ToolArgs, ToolInvocationContext], Mapping[str, Any]],
        *,
        side_effect: bool = False,
    ) -> None:
        self.args_model = args_model
        self.handler = handler
        self.side_effect = side_effect


class AstraToolGateway:
    def __init__(self, store: AstraStore, business: MockBusinessService) -> None:
        self.store = store
        self.business = business
        self._specs = {
            "get_customer": ToolSpec(GetCustomerArgs, self._get_customer),
            "get_order": ToolSpec(GetOrderArgs, self._get_order),
            "search_policy": ToolSpec(SearchPolicyArgs, self._search_policy),
            "create_complaint_ticket": ToolSpec(
                CreateComplaintTicketArgs,
                self._create_complaint_ticket,
                side_effect=True,
            ),
            "get_complaint_ticket": ToolSpec(
                GetComplaintTicketArgs, self._get_complaint_ticket
            ),
        }

    @property
    def business_tool_names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def invoke(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        spec = self._specs.get(tool_name)
        if spec is None:
            return self._error(tool_name, "unknown_tool", "Tool is not registered")
        if tool_name not in context.allowed_tools:
            return self._error(
                tool_name,
                "permission_denied",
                "Tool is not allowed by the persisted Task Contract",
            )
        try:
            parsed = spec.args_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                ok=False,
                tool_name=tool_name,
                error={
                    "type": "invalid_tool_arguments",
                    "message": "Tool arguments failed schema validation",
                    "details": exc.errors(include_url=False),
                },
            )

        if spec.side_effect and not context.idempotency_key:
            return self._error(
                tool_name,
                "idempotency_key_required",
                "Side-effect tools require an idempotency key",
            )

        if spec.side_effect:
            # The suspension check, business commit, and receipt commit share
            # one BEGIN IMMEDIATE transaction. request_interaction() competes
            # for the same database write boundary, so either the side effect
            # commits first with its receipt or suspension commits first and
            # the side effect is rejected.
            with self.store.transaction():
                if self.store.is_suspended(context.execution_id):
                    return self._error(
                        tool_name,
                        "execution_suspended",
                        "New business side effects are denied after suspension_requested",
                    )
                data = dict(spec.handler(parsed, context))
                receipt_id, created = self.store.add_execution_receipt(
                    execution_id=context.execution_id,
                    task_id=context.task_id,
                    attempt_id=context.attempt_id,
                    tool_name=tool_name,
                    tool_call_id=context.tool_call_id,
                    request=parsed.model_dump(),
                    result=data,
                    side_effect=True,
                    idempotency_key=context.idempotency_key,
                )
        else:
            data = dict(spec.handler(parsed, context))
            receipt_id, created = self.store.add_execution_receipt(
                execution_id=context.execution_id,
                task_id=context.task_id,
                attempt_id=context.attempt_id,
                tool_name=tool_name,
                tool_call_id=context.tool_call_id,
                request=parsed.model_dump(),
                result=data,
                side_effect=False,
                idempotency_key=context.idempotency_key,
            )
        data["receipt_created"] = created
        return ToolResult(
            ok=True,
            tool_name=tool_name,
            data=data,
            receipt_id=receipt_id,
            side_effect=spec.side_effect,
        )

    def _get_customer(
        self, args: ToolArgs, context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        assert isinstance(args, GetCustomerArgs)
        customer = self.business.get_customer(args.customer_id)
        return {"found": customer is not None, "customer": customer}

    def _get_order(
        self, args: ToolArgs, context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        assert isinstance(args, GetOrderArgs)
        order = self.business.get_order(args.order_id)
        return {"found": order is not None, "order": order}

    def _search_policy(
        self, args: ToolArgs, context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        assert isinstance(args, SearchPolicyArgs)
        policies = self.business.search_policy(args.query)
        return {"count": len(policies), "policies": policies}

    def _create_complaint_ticket(
        self, args: ToolArgs, context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        assert isinstance(args, CreateComplaintTicketArgs)
        assert context.idempotency_key is not None
        order = self.business.get_order(args.order_id)
        if order is None or order["customer_id"] != args.customer_id:
            return {
                "created": False,
                "business_error": "order_customer_mismatch",
            }
        ticket, created = self.business.create_complaint_ticket(
            customer_id=args.customer_id,
            order_id=args.order_id,
            reason=args.reason,
            resolution=args.resolution,
            idempotency_key=context.idempotency_key,
        )
        return {"created": created, "ticket": ticket}

    def _get_complaint_ticket(
        self, args: ToolArgs, context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        assert isinstance(args, GetComplaintTicketArgs)
        ticket = self.business.get_complaint_ticket(args.ticket_id)
        return {"found": ticket is not None, "ticket": ticket}

    @staticmethod
    def _error(tool_name: str, error_type: str, message: str) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error={"type": error_type, "message": message},
        )
