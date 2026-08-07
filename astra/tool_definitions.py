"""Concrete Tool Definitions and business adapters.

The Tool Gateway consumes these definitions generically.  Business-specific
schemas, subject relationships, effect normalization, and adapter behavior
belong here (or in another registration module), never in the Gateway core.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from .business import BusinessService
from .domain import ToolInvocationContext
from .observations import ConfirmationStatus, ExternalObservation
from .phase3.canonical import sha256_digest
from .phase3.effects import (
    EffectNormalizationCandidate,
    ExternalOperation,
)
from .phase3.governance import RuntimeGovernanceCore
from .phase3.task_contract import (
    ApprovalRequirement,
    ApprovalUsageSemantics,
    AuthorizedEffect,
    Constraint,
    ResolvedTool,
    SubjectRef,
    TaskContract,
    ToolAccessMode,
)
from .storage import AstraStore
from .tool_catalog import TOOL_CATALOG, CatalogTool


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class SubjectBinding:
    argument: str
    subject_type: str
    authority_domain: str
    read_scope: str = "read"
    effect_scope: str = "effect_candidate"


@dataclass(frozen=True)
class SubjectDerivation:
    container_field: str
    id_field: str
    subject_type: str
    authority_domain: str
    access_scope: str = "read"
    many: bool = True


@dataclass(frozen=True)
class ApprovalPolicy:
    risk_class: str
    approver_policy_ref: str
    usage_semantics: ApprovalUsageSemantics = (
        ApprovalUsageSemantics.SINGLE_EFFECT_SINGLE_USE
    )


class EffectNormalizer(Protocol):
    normalizer_id: str
    normalizer_version: str

    def normalize(
        self,
        contract: TaskContract,
        raw_request: Mapping[str, Any],
    ) -> Sequence[EffectNormalizationCandidate]: ...


@dataclass(frozen=True)
class EffectDispatchResult:
    result: Mapping[str, Any]
    accepted: bool
    external_object_id: str | None = None
    error: str | None = None


class ToolAdapter(Protocol):
    def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> Mapping[str, Any]: ...


class EffectAdapter(ToolAdapter, Protocol):
    def dispatch(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> EffectDispatchResult: ...

    def observe(self, operation: ExternalOperation) -> ExternalObservation | None: ...

    def confirmation_matches(
        self,
        observation: Mapping[str, Any] | None,
        arguments: Mapping[str, Any],
    ) -> bool | ConfirmationStatus: ...

    def replay_result(
        self,
        observation: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]: ...


RelationVerifier = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class EffectDefinition:
    effect_type: str
    effect_type_version: str
    authority_domain: str
    subject_binding: SubjectBinding
    normalizer: EffectNormalizer
    approval_policy: ApprovalPolicy
    adapter: EffectAdapter


@dataclass(frozen=True)
class ToolDefinition:
    catalog: CatalogTool
    input_model: type[ToolArgs]
    adapter: ToolAdapter
    subject_bindings: tuple[SubjectBinding, ...] = ()
    subject_derivations: tuple[SubjectDerivation, ...] = ()
    relation_verifier: RelationVerifier | None = None
    effect: EffectDefinition | None = None

    @property
    def side_effect(self) -> bool:
        return self.catalog.access_mode == ToolAccessMode.EFFECT


class ComplaintOrderScopeConstraint:
    constraint_id = "complaint.order_scope"
    constraint_version = "1"

    def enforce(
        self,
        contract: TaskContract,
        constraint: Constraint,
        binding: ResolvedTool,
        arguments: Mapping[str, Any],
    ) -> None:
        expected = constraint.configuration.get("order_id")
        if not isinstance(expected, str) or arguments.get("order_id") != expected:
            raise PermissionError("constraint_denied:complaint.order_scope@1")
        if not any(
            subject.type == "order" and subject.id == expected
            for subject in contract.subject_refs
        ):
            raise PermissionError("constraint_subject_mismatch:complaint.order_scope@1")


class ExactParameterConstraint:
    constraint_id = "astra.exact_parameters"
    constraint_version = "1"

    def enforce(
        self,
        contract: TaskContract,
        constraint: Constraint,
        binding: ResolvedTool,
        arguments: Mapping[str, Any],
    ) -> None:
        expected = constraint.configuration.get("parameters")
        if not isinstance(expected, Mapping):
            raise PermissionError("constraint_configuration_invalid")
        if any(arguments.get(key) != value for key, value in expected.items()):
            raise PermissionError("constraint_denied:astra.exact_parameters@1")


class GetCustomerArgs(ToolArgs):
    customer_id: str


class GetOrderArgs(ToolArgs):
    order_id: str


class ListCustomerOrdersArgs(ToolArgs):
    customer_id: str


class SearchPolicyArgs(ToolArgs):
    query: str


class CreateComplaintTicketArgs(ToolArgs):
    customer_id: str
    order_id: str
    reason: str
    resolution: str


class GetComplaintTicketArgs(ToolArgs):
    ticket_id: str


class CallableAdapter:
    def __init__(
        self,
        handler: Callable[[Mapping[str, Any], ToolInvocationContext], Mapping[str, Any]],
    ) -> None:
        self._handler = handler

    def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> Mapping[str, Any]:
        return self._handler(arguments, context)


class ComplaintTicketNormalizer:
    normalizer_id = "astra.create_complaint_ticket"
    normalizer_version = "1"

    def normalize(
        self,
        contract: TaskContract,
        raw_request: Mapping[str, Any],
    ) -> Sequence[EffectNormalizationCandidate]:
        normalized = {
            "customer_id": str(raw_request["customer_id"]),
            "order_id": str(raw_request["order_id"]),
            "reason": str(raw_request["reason"]),
            "resolution": str(raw_request["resolution"]),
        }
        return tuple(
            EffectNormalizationCandidate(
                effect_intent_ref=intent.effect_intent_id,
                normalized_parameters=normalized,
            )
            for intent in contract.authorized_effects
            if intent.effect_type == "support.complaint_ticket"
            and intent.effect_type_version == "1"
            and intent.subject_ref.type == "order"
            and intent.subject_ref.id == normalized["order_id"]
            and all(
                normalized.get(key) == value
                for key, value in intent.parameter_constraints.items()
            )
        )


class ComplaintTicketAdapter:
    def __init__(self, business: BusinessService) -> None:
        self.business = business

    def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> Mapping[str, Any]:
        return self.dispatch(arguments, context).result

    def dispatch(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> EffectDispatchResult:
        if context.idempotency_key is None:
            raise RuntimeError("missing_gateway_idempotency_key")
        ticket, created = self.business.create_complaint_ticket(
            customer_id=str(arguments["customer_id"]),
            order_id=str(arguments["order_id"]),
            reason=str(arguments["reason"]),
            resolution=str(arguments["resolution"]),
            idempotency_key=context.idempotency_key,
        )
        ticket_id = ticket.get("ticket_id")
        return EffectDispatchResult(
            result={"created": created, "ticket": ticket},
            accepted=bool(created or ticket_id),
            external_object_id=str(ticket_id) if ticket_id else None,
            error=None if created or ticket_id else "Business authority rejected request",
        )

    def observe(self, operation: ExternalOperation) -> ExternalObservation | None:
        if operation.external_operation_id is not None:
            ticket = self.business.get_complaint_ticket(
                operation.external_operation_id
            )
            return (
                ExternalObservation(
                    external_object_id=operation.external_operation_id,
                    payload=ticket,
                )
                if ticket is not None
                else None
            )
        finder = getattr(
            self.business,
            "find_complaint_ticket_by_idempotency_key",
            None,
        )
        if not callable(finder):
            raise RuntimeError(
                "Business authority does not expose idempotency-key lookup"
            )
        ticket = finder(operation.idempotency_key)
        if ticket is None:
            return None
        ticket_id = str(ticket.get("ticket_id", ""))
        return ExternalObservation(
            external_object_id=ticket_id,
            payload=ticket,
        )

    def confirmation_matches(
        self,
        observation: Mapping[str, Any] | None,
        arguments: Mapping[str, Any],
    ) -> bool:
        return observation is not None and all(
            observation.get(key) == arguments.get(key)
            for key in ("customer_id", "order_id", "reason", "resolution")
        )

    def replay_result(
        self,
        observation: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        return {"created": False, "ticket": observation}


def build_business_tool_definitions(
    business: BusinessService,
    store: AstraStore,
    governance_core: RuntimeGovernanceCore,
) -> Mapping[str, ToolDefinition]:
    """Register the concrete business tools consumed by the generic Gateway."""

    commerce = business.commerce_authority_domain
    support = business.support_authority_domain

    def customer(arguments: Mapping[str, Any], _: ToolInvocationContext) -> Mapping[str, Any]:
        value = business.get_customer(str(arguments["customer_id"]))
        return {"found": value is not None, "customer": value}

    def order(arguments: Mapping[str, Any], _: ToolInvocationContext) -> Mapping[str, Any]:
        value = business.get_order(str(arguments["order_id"]))
        return {"found": value is not None, "order": value}

    def orders(arguments: Mapping[str, Any], _: ToolInvocationContext) -> Mapping[str, Any]:
        values = business.list_customer_orders(str(arguments["customer_id"]))
        return {"count": len(values), "orders": values}

    def policies(arguments: Mapping[str, Any], _: ToolInvocationContext) -> Mapping[str, Any]:
        values = business.search_policy(str(arguments["query"]))
        return {"count": len(values), "policies": values}

    def complaint_ticket(
        arguments: Mapping[str, Any], context: ToolInvocationContext
    ) -> Mapping[str, Any]:
        ticket_id = str(arguments["ticket_id"])
        ticket = business.get_complaint_ticket(ticket_id)
        if ticket is not None:
            operation_row = store.query_one(
                """
                SELECT operation_id FROM phase3_external_operations
                WHERE task_id = ?
                  AND json_extract(operation_json, '$.external_operation_id') = ?
                ORDER BY operation_id LIMIT 1
                """,
                (context.task_id, ticket_id),
            )
            if operation_row is not None:
                governance_core.record_business_observation(
                    str(operation_row["operation_id"]),
                    ticket,
                    external_object_id=ticket_id,
                )
        return {"found": ticket is not None, "ticket": ticket}

    def verify_complaint_relation(arguments: Mapping[str, Any]) -> None:
        order_value = business.get_order(str(arguments["order_id"]))
        if (
            order_value is None
            or str(order_value.get("customer_id", ""))
            != str(arguments["customer_id"])
        ):
            raise PermissionError("subject_relation_mismatch")

    complaint_adapter = ComplaintTicketAdapter(business)
    complaint_subject = SubjectBinding("order_id", "order", commerce)
    normalizer = ComplaintTicketNormalizer()

    return {
        "get_customer": ToolDefinition(
            catalog=TOOL_CATALOG["get_customer"],
            input_model=GetCustomerArgs,
            adapter=CallableAdapter(customer),
            subject_bindings=(SubjectBinding("customer_id", "customer", commerce),),
        ),
        "get_order": ToolDefinition(
            catalog=TOOL_CATALOG["get_order"],
            input_model=GetOrderArgs,
            adapter=CallableAdapter(order),
            subject_bindings=(SubjectBinding("order_id", "order", commerce),),
            subject_derivations=(
                SubjectDerivation("order", "customer_id", "customer", commerce, many=False),
            ),
        ),
        "list_customer_orders": ToolDefinition(
            catalog=TOOL_CATALOG["list_customer_orders"],
            input_model=ListCustomerOrdersArgs,
            adapter=CallableAdapter(orders),
            subject_bindings=(SubjectBinding("customer_id", "customer", commerce),),
            subject_derivations=(
                SubjectDerivation("orders", "order_id", "order", commerce),
            ),
        ),
        "search_policy": ToolDefinition(
            catalog=TOOL_CATALOG["search_policy"],
            input_model=SearchPolicyArgs,
            adapter=CallableAdapter(policies),
        ),
        "create_complaint_ticket": ToolDefinition(
            catalog=TOOL_CATALOG["create_complaint_ticket"],
            input_model=CreateComplaintTicketArgs,
            adapter=complaint_adapter,
            subject_bindings=(complaint_subject,),
            relation_verifier=verify_complaint_relation,
            effect=EffectDefinition(
                effect_type="support.complaint_ticket",
                effect_type_version="1",
                authority_domain=support,
                subject_binding=complaint_subject,
                normalizer=normalizer,
                approval_policy=ApprovalPolicy(
                    risk_class="medium",
                    approver_policy_ref="task.requester@1",
                ),
                adapter=complaint_adapter,
            ),
        ),
        "get_complaint_ticket": ToolDefinition(
            catalog=TOOL_CATALOG["get_complaint_ticket"],
            input_model=GetComplaintTicketArgs,
            adapter=CallableAdapter(complaint_ticket),
            subject_bindings=(
                SubjectBinding("ticket_id", "complaint_ticket", support),
            ),
        ),
    }


def build_dynamic_intent(
    *,
    task_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    effect: EffectDefinition,
) -> tuple[AuthorizedEffect, ApprovalRequirement]:
    """Build one exact proposal from a Tool Definition's declared policy."""

    subject_id = str(arguments[effect.subject_binding.argument])
    fingerprint = sha256_digest(
        {"task_id": task_id, "tool_name": tool_name, "arguments": dict(arguments)}
    ).removeprefix("sha256:")
    effect_intent_id = f"runtime:{tool_name}:{fingerprint}"
    requirement_id = f"runtime-approval:{tool_name}:{fingerprint}"
    effect_intent = AuthorizedEffect(
        effect_intent_id=effect_intent_id,
        effect_type=effect.effect_type,
        effect_type_version=effect.effect_type_version,
        authority_domain=effect.authority_domain,
        subject_ref=SubjectRef(
            authority_domain=effect.subject_binding.authority_domain,
            type=effect.subject_binding.subject_type,
            id=subject_id,
        ),
        parameter_constraints=dict(arguments),
        max_confirmed_occurrences=1,
        approval_requirement_ref=f"{requirement_id}@1",
    )
    requirement = ApprovalRequirement(
        approval_requirement_id=requirement_id,
        approval_requirement_version="1",
        effect_intent_refs=(effect_intent_id,),
        risk_class=effect.approval_policy.risk_class,
        approver_policy_ref=effect.approval_policy.approver_policy_ref,
        usage_semantics=effect.approval_policy.usage_semantics,
    )
    return effect_intent, requirement
