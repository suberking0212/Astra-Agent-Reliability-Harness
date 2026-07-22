"""Production Task Contract and canonical-effect enforcement gateway."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from .domain import ToolInvocationContext, ToolResult
from .mock_business import MockBusinessService
from .phase3.canonical import sha256_digest
from .phase3.effects import (
    CanonicalEffectRequest,
    EffectContractError,
    EffectNormalizationCandidate,
    EffectNormalizerRegistry,
    ExternalOperation,
    ExternalOperationStatus,
)
from .phase3.governance import GovernanceStore, RuntimeGovernanceCore
from .phase3.task_contract import (
    Constraint,
    EnforcementPoint,
    ResolvedTool,
    TaskContract,
    ToolAccessMode,
)
from .storage import AstraStore


BUSINESS_TOOL_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    "get_customer": {
        "description": "Look up a customer by customer_id.",
        "parameters": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
            "additionalProperties": False,
        },
    },
    "get_order": {
        "description": "Look up an order by order_id.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    "search_policy": {
        "description": "Search complaint and after-sales policies.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "create_complaint_ticket": {
        "description": "Create one governed complaint ticket.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "order_id": {"type": "string"},
                "reason": {"type": "string"},
                "resolution": {"type": "string"},
            },
            "required": ["customer_id", "order_id", "reason", "resolution"],
            "additionalProperties": False,
        },
    },
    "get_complaint_ticket": {
        "description": "Read back a complaint ticket for business-state verification.",
        "parameters": {
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}},
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
    },
}


def production_tool_schema_hash(tool_name: str, *, tool_version: str = "1") -> str:
    """Return the identity of the exact schema exposed to Hermes."""

    schema = BUSINESS_TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        raise KeyError(tool_name)
    return sha256_digest(
        {"name": tool_name, "tool_version": tool_version, **dict(schema)}
    )


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


class ConstraintHandler(Protocol):
    constraint_id: str
    constraint_version: str

    def enforce(
        self,
        contract: TaskContract,
        constraint: Constraint,
        binding: ResolvedTool,
        arguments: Mapping[str, Any],
    ) -> None: ...


class ConstraintHandlerRegistry:
    """Version-keyed handlers; missing handlers always deny the Tool call."""

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], ConstraintHandler] = {}

    def register(self, handler: ConstraintHandler) -> None:
        key = (handler.constraint_id, handler.constraint_version)
        if key in self._handlers:
            raise ValueError(f"Constraint handler already registered: {key!r}")
        self._handlers[key] = handler

    @property
    def registered_refs(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._handlers))

    def validate_contract(self, contract: TaskContract) -> None:
        for constraint in contract.constraints:
            key = (constraint.constraint_id, constraint.constraint_version)
            if key not in self._handlers:
                raise PermissionError(
                    "unknown_constraint_handler:"
                    f"{constraint.constraint_id}@{constraint.constraint_version}"
                )

    def enforce(
        self,
        contract: TaskContract,
        binding: ResolvedTool,
        arguments: Mapping[str, Any],
        *,
        point: EnforcementPoint,
    ) -> None:
        self.validate_contract(contract)
        for constraint in contract.constraints:
            if constraint.enforcement_point != point:
                continue
            handler = self._handlers[
                (constraint.constraint_id, constraint.constraint_version)
            ]
            handler.enforce(contract, constraint, binding, arguments)


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
        if not any(subject.type == "order" and subject.id == expected for subject in contract.subject_refs):
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
        candidates: list[EffectNormalizationCandidate] = []
        for intent in contract.authorized_effects:
            if (
                intent.effect_type != "support.complaint_ticket"
                or intent.effect_type_version != "1"
                or intent.subject_ref.type != "order"
                or intent.subject_ref.id != normalized["order_id"]
            ):
                continue
            if any(
                normalized.get(key) != value
                for key, value in intent.parameter_constraints.items()
            ):
                continue
            candidates.append(
                EffectNormalizationCandidate(
                    effect_intent_ref=intent.effect_intent_id,
                    normalized_parameters=normalized,
                )
            )
        return tuple(candidates)


@dataclass(frozen=True)
class ToolSpec:
    args_model: type[ToolArgs]
    handler: Callable[[ToolArgs, ToolInvocationContext], Mapping[str, Any]]
    capability_ref: str
    access_mode: ToolAccessMode
    tool_version: str = "1"
    normalizer_id: str | None = None
    normalizer_version: str | None = None
    subject_argument: str | None = None
    subject_type: str | None = None
    subject_authority_domain: str | None = None

    @property
    def side_effect(self) -> bool:
        return self.access_mode == ToolAccessMode.EFFECT


class AstraToolGateway:
    def __init__(
        self,
        store: AstraStore,
        business: MockBusinessService,
        *,
        governance_core: RuntimeGovernanceCore | None = None,
        normalizer_registry: EffectNormalizerRegistry | None = None,
        constraint_registry: ConstraintHandlerRegistry | None = None,
    ) -> None:
        self.store = store
        self.business = business
        self.governance_core = governance_core or RuntimeGovernanceCore(
            GovernanceStore.from_astra_store(store)
        )
        if self.governance_core.store.authority_store is not store:
            raise ValueError("Tool Gateway governance must share AstraStore authority")
        self.normalizer_registry = normalizer_registry or EffectNormalizerRegistry()
        if normalizer_registry is None:
            self.normalizer_registry.register(ComplaintTicketNormalizer())
        self.constraint_registry = constraint_registry or ConstraintHandlerRegistry()
        if constraint_registry is None:
            self.constraint_registry.register(ComplaintOrderScopeConstraint())
            self.constraint_registry.register(ExactParameterConstraint())
        capability = "complaints.integration@1"
        self._specs = {
            "get_customer": ToolSpec(
                GetCustomerArgs,
                self._get_customer,
                capability,
                ToolAccessMode.READ,
                subject_argument="customer_id",
                subject_type="customer",
                subject_authority_domain="commerce.mock",
            ),
            "get_order": ToolSpec(
                GetOrderArgs,
                self._get_order,
                capability,
                ToolAccessMode.READ,
                subject_argument="order_id",
                subject_type="order",
                subject_authority_domain="commerce.mock",
            ),
            "search_policy": ToolSpec(
                SearchPolicyArgs,
                self._search_policy,
                capability,
                ToolAccessMode.READ,
            ),
            "create_complaint_ticket": ToolSpec(
                CreateComplaintTicketArgs,
                self._create_complaint_ticket,
                capability,
                ToolAccessMode.EFFECT,
                normalizer_id=ComplaintTicketNormalizer.normalizer_id,
                normalizer_version=ComplaintTicketNormalizer.normalizer_version,
                subject_argument="order_id",
                subject_type="order",
                subject_authority_domain="commerce.mock",
            ),
            "get_complaint_ticket": ToolSpec(
                GetComplaintTicketArgs,
                self._get_complaint_ticket,
                capability,
                ToolAccessMode.READ,
                subject_argument="ticket_id",
                subject_type="complaint_ticket",
                subject_authority_domain="support.mock",
            ),
        }

    @property
    def business_tool_names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    @classmethod
    def schema_hash(cls, tool_name: str, *, tool_version: str = "1") -> str:
        return production_tool_schema_hash(tool_name, tool_version=tool_version)

    def invoke(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        spec = self._specs.get(tool_name)
        if spec is None:
            return self._error(tool_name, "unknown_tool", "Tool is not registered")
        try:
            contract, binding = self._load_authoritative_binding(context, tool_name)
            self.constraint_registry.validate_contract(contract)
            parsed = spec.args_model.model_validate(arguments)
            parsed_arguments = parsed.model_dump()
            self._enforce_subject_scope(contract, spec, parsed_arguments)
            self.constraint_registry.enforce(
                contract,
                binding,
                parsed_arguments,
                point=EnforcementPoint.TOOL_GATEWAY,
            )
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
        except (PermissionError, RuntimeError, ValueError) as exc:
            return self._boundary_error(tool_name, exc)

        if not spec.side_effect:
            data = dict(spec.handler(parsed, context))
            receipt_id, created = self.store.add_execution_receipt(
                execution_id=context.execution_id,
                task_id=context.task_id,
                attempt_id=context.attempt_id,
                tool_name=tool_name,
                tool_call_id=context.tool_call_id,
                request=parsed_arguments,
                result=data,
                side_effect=False,
                idempotency_key=None,
            )
            data["receipt_created"] = created
            return ToolResult(
                ok=True,
                tool_name=tool_name,
                data=data,
                receipt_id=receipt_id,
                side_effect=False,
            )
        return self._invoke_effect(
            context=context,
            tool_name=tool_name,
            spec=spec,
            contract=contract,
            binding=binding,
            parsed=parsed,
            parsed_arguments=parsed_arguments,
        )

    def canonicalize_effect_request(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> CanonicalEffectRequest:
        """Validate and normalize one effect without preparing or dispatching it."""

        spec = self._specs.get(tool_name)
        if spec is None:
            raise PermissionError("unknown_tool")
        if not spec.side_effect:
            raise PermissionError("approval_not_required")
        contract, binding = self._load_authoritative_binding(context, tool_name)
        self.constraint_registry.validate_contract(contract)
        parsed = spec.args_model.model_validate(arguments)
        parsed_arguments = parsed.model_dump()
        self._enforce_subject_scope(contract, spec, parsed_arguments)
        self.constraint_registry.enforce(
            contract,
            binding,
            parsed_arguments,
            point=EnforcementPoint.TOOL_GATEWAY,
        )
        if spec.normalizer_id is None or spec.normalizer_version is None:
            raise PermissionError("unknown_effect_normalizer")
        self.constraint_registry.enforce(
            contract,
            binding,
            parsed_arguments,
            point=EnforcementPoint.EFFECT_NORMALIZER,
        )
        return self.normalizer_registry.normalize(
            contract,
            parsed_arguments,
            normalizer_id=spec.normalizer_id,
            normalizer_version=spec.normalizer_version,
        )

    def _load_authoritative_binding(
        self,
        context: ToolInvocationContext,
        tool_name: str,
    ) -> tuple[TaskContract, ResolvedTool]:
        row = self.store.query_one(
            """
            SELECT task.contract_json, task.state AS task_state,
                   task.current_attempt_id, attempt.state AS attempt_state,
                   execution.status AS execution_status, execution.ended_at,
                   request.state AS run_request_state
            FROM executions AS execution
            JOIN phase3_tasks AS task ON task.task_id = execution.task_id
            JOIN phase3_attempts AS attempt
              ON attempt.attempt_id = execution.attempt_id
            LEFT JOIN phase4_run_requests AS request
              ON request.run_request_id = execution.run_request_id
            WHERE execution.execution_id = ? AND execution.task_id = ?
              AND execution.attempt_id = ? AND attempt.task_id = task.task_id
            """,
            (context.execution_id, context.task_id, context.attempt_id),
        )
        if row is None:
            raise PermissionError("authoritative_execution_not_found")
        if row["task_state"] != "running":
            raise PermissionError("task_not_effectively_running")
        if (
            row["attempt_state"] != "active"
            or row["current_attempt_id"] != context.attempt_id
        ):
            raise PermissionError("attempt_not_active_or_current")
        if row["execution_status"] != "running" or row["ended_at"] is not None:
            raise PermissionError("execution_not_active")
        if row["run_request_state"] != "claimed":
            raise PermissionError("run_request_not_claimed")

        contract = TaskContract.model_validate_json(row["contract_json"])
        matches = tuple(
            binding
            for binding in contract.resolved_tools
            if binding.tool_name == tool_name
        )
        if len(matches) != 1:
            raise PermissionError(
                "tool_binding_missing" if not matches else "tool_binding_ambiguous"
            )
        binding = matches[0]
        spec = self._specs[tool_name]
        if binding.capability_ref != spec.capability_ref:
            raise PermissionError("capability_mismatch")
        if binding.tool_version != spec.tool_version:
            raise PermissionError("tool_version_mismatch")
        if binding.schema_hash != self.schema_hash(
            tool_name, tool_version=spec.tool_version
        ):
            raise PermissionError("tool_schema_hash_mismatch")
        if binding.access_mode != spec.access_mode:
            raise PermissionError("tool_access_mode_mismatch")
        return contract, binding

    @staticmethod
    def _enforce_subject_scope(
        contract: TaskContract,
        spec: ToolSpec,
        arguments: Mapping[str, Any],
    ) -> None:
        if spec.subject_argument is None:
            return
        subject_id = arguments.get(spec.subject_argument)
        if not isinstance(subject_id, str):
            raise PermissionError("subject_scope_missing")
        if not any(
            subject.id == subject_id
            and subject.type == spec.subject_type
            and subject.authority_domain == spec.subject_authority_domain
            for subject in contract.subject_refs
        ):
            raise PermissionError("subject_scope_mismatch")

    def _invoke_effect(
        self,
        *,
        context: ToolInvocationContext,
        tool_name: str,
        spec: ToolSpec,
        contract: TaskContract,
        binding: ResolvedTool,
        parsed: ToolArgs,
        parsed_arguments: Mapping[str, Any],
    ) -> ToolResult:
        if spec.normalizer_id is None or spec.normalizer_version is None:
            return self._error(
                tool_name,
                "unknown_effect_normalizer",
                "Effect Tool has no registered normalizer binding",
            )
        try:
            self.constraint_registry.enforce(
                contract,
                binding,
                parsed_arguments,
                point=EnforcementPoint.EFFECT_NORMALIZER,
            )
            effect = self.normalizer_registry.normalize(
                contract,
                parsed_arguments,
                normalizer_id=spec.normalizer_id,
                normalizer_version=spec.normalizer_version,
            )
            approval_resolution_id = self._exact_approval_resolution_id(
                context.task_id,
                effect.effect_identity,
                effect.effect_request_hash,
            )
            operation, _ = self.governance_core.prepare_external_operation(
                effect,
                task_id=context.task_id,
                attempt_id=context.attempt_id,
                execution_id=context.execution_id,
                approval_resolution_id=approval_resolution_id,
            )
        except (EffectContractError, PermissionError, RuntimeError, ValueError) as exc:
            if str(exc).split(":", 1)[0] == "approval_required":
                return ToolResult(
                    ok=False,
                    tool_name=tool_name,
                    error={
                        "type": "approval_required",
                        "message": str(exc),
                        "canonical_effect_request": effect.model_dump(
                            mode="json", exclude_none=True
                        ),
                    },
                )
            return self._boundary_error(tool_name, exc)

        if operation.status == ExternalOperationStatus.CONFIRMED:
            return self._confirmed_replay(
                context, tool_name, parsed_arguments, operation
            )

        try:
            operation = self.governance_core.transition_external_operation(
                operation.operation_id,
                ExternalOperationStatus.IN_FLIGHT,
                actor_task_id=context.task_id,
                actor_attempt_id=context.attempt_id,
                actor_execution_id=context.execution_id,
            )
            governed_context = context.model_copy(
                update={"idempotency_key": operation.idempotency_key}
            )
            data = dict(spec.handler(parsed, governed_context))
        except BaseException as exc:
            try:
                self.governance_core.transition_external_operation(
                    operation.operation_id,
                    ExternalOperationStatus.INDETERMINATE,
                    external_operation_id=operation.external_operation_id,
                )
            except BaseException:
                pass
            return self._error(
                tool_name,
                "external_operation_indeterminate",
                f"Business authority call did not produce a confirmable result: {exc}",
            )

        ticket = data.get("ticket")
        ticket_id = ticket.get("ticket_id") if isinstance(ticket, Mapping) else None
        if not data.get("created") and not ticket_id:
            self.governance_core.transition_external_operation(
                operation.operation_id,
                ExternalOperationStatus.FAILED,
                actor_task_id=context.task_id,
                actor_attempt_id=context.attempt_id,
                actor_execution_id=context.execution_id,
            )
            return self._error(
                tool_name,
                "external_operation_failed",
                str(data.get("business_error") or "Business authority rejected request"),
            )
        try:
            operation = self.governance_core.transition_external_operation(
                operation.operation_id,
                ExternalOperationStatus.ACKNOWLEDGED,
                external_operation_id=str(ticket_id) if ticket_id else None,
                actor_task_id=context.task_id,
                actor_attempt_id=context.attempt_id,
                actor_execution_id=context.execution_id,
            )
            observation = (
                self.business.get_complaint_ticket(str(ticket_id))
                if ticket_id is not None
                else None
            )
            if observation is not None and ticket_id is not None:
                self.governance_core.record_business_observation(
                    operation.operation_id,
                    observation,
                    external_object_id=str(ticket_id),
                )
            if not self._complaint_observation_matches(observation, parsed_arguments):
                self.governance_core.transition_external_operation(
                    operation.operation_id,
                    ExternalOperationStatus.INDETERMINATE,
                    external_operation_id=str(ticket_id) if ticket_id else None,
                )
                return self._error(
                    tool_name,
                    "authoritative_confirmation_failed",
                    "Business authority state does not confirm the canonical request",
                )
            operation = self.governance_core.transition_external_operation(
                operation.operation_id,
                ExternalOperationStatus.CONFIRMED,
                external_operation_id=str(ticket_id),
                actor_task_id=context.task_id,
                actor_attempt_id=context.attempt_id,
                actor_execution_id=context.execution_id,
            )
        except (PermissionError, RuntimeError, ValueError) as exc:
            try:
                self.governance_core.transition_external_operation(
                    operation.operation_id,
                    ExternalOperationStatus.INDETERMINATE,
                    external_operation_id=str(ticket_id) if ticket_id else None,
                )
            except BaseException:
                pass
            return self._boundary_error(tool_name, exc)

        data.update(
            {
                "external_operation_id": operation.operation_id,
                "effect_identity": operation.effect_identity,
                "authoritative_confirmation": operation.status.value,
            }
        )
        receipt_id, created = self.store.add_execution_receipt(
            execution_id=context.execution_id,
            task_id=context.task_id,
            attempt_id=context.attempt_id,
            tool_name=tool_name,
            tool_call_id=context.tool_call_id,
            request=parsed_arguments,
            result=data,
            side_effect=True,
            idempotency_key=operation.idempotency_key,
            operation_id=operation.operation_id,
        )
        data["receipt_created"] = created
        return ToolResult(
            ok=True,
            tool_name=tool_name,
            data=data,
            receipt_id=receipt_id,
            side_effect=True,
        )

    def _confirmed_replay(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: Mapping[str, Any],
        operation: ExternalOperation,
    ) -> ToolResult:
        try:
            operation = self.governance_core.transition_external_operation(
                operation.operation_id,
                ExternalOperationStatus.CONFIRMED,
                actor_task_id=context.task_id,
                actor_attempt_id=context.attempt_id,
                actor_execution_id=context.execution_id,
            )
        except (PermissionError, RuntimeError, ValueError) as exc:
            return self._boundary_error(tool_name, exc)
        ticket = (
            self.business.get_complaint_ticket(operation.external_operation_id)
            if operation.external_operation_id
            else None
        )
        if ticket is not None and operation.external_operation_id is not None:
            self.governance_core.record_business_observation(
                operation.operation_id,
                ticket,
                external_object_id=str(operation.external_operation_id),
            )
        if not self._complaint_observation_matches(ticket, arguments):
            return self._error(
                tool_name,
                "authoritative_confirmation_failed",
                "Confirmed operation no longer matches authoritative business state",
            )
        data = {
            "created": False,
            "ticket": ticket,
            "external_operation_id": operation.operation_id,
            "effect_identity": operation.effect_identity,
            "authoritative_confirmation": operation.status.value,
        }
        receipt_id, created = self.store.add_execution_receipt(
            execution_id=context.execution_id,
            task_id=context.task_id,
            attempt_id=context.attempt_id,
            tool_name=tool_name,
            tool_call_id=context.tool_call_id,
            request=arguments,
            result=data,
            side_effect=True,
            idempotency_key=operation.idempotency_key,
            operation_id=operation.operation_id,
        )
        data["receipt_created"] = created
        return ToolResult(
            ok=True,
            tool_name=tool_name,
            data=data,
            receipt_id=receipt_id,
            side_effect=True,
        )

    def _exact_approval_resolution_id(
        self,
        task_id: str,
        effect_identity: str,
        effect_request_hash: str,
    ) -> str | None:
        rows = self.store.query_all(
            """
            SELECT resolution.approval_resolution_id
            FROM phase3_approval_resolutions AS resolution
            JOIN phase3_approval_requests AS request
              ON request.approval_request_id = resolution.approval_request_id
            JOIN interactions AS interaction
              ON interaction.interaction_id = resolution.interaction_id
            WHERE resolution.task_id = ?
              AND resolution.effect_identity = ?
              AND resolution.effect_request_hash = ?
              AND resolution.decision = 'approved'
              AND resolution.revoked = 0
              AND request.status = 'resolved'
              AND interaction.status = 'resolved'
            ORDER BY resolution.created_at, resolution.approval_resolution_id
            """,
            (task_id, effect_identity, effect_request_hash),
        )
        if len(rows) > 1:
            raise PermissionError("approval_binding_ambiguous")
        return str(rows[0][0]) if rows else None

    @staticmethod
    def _complaint_observation_matches(
        observation: Mapping[str, Any] | None,
        arguments: Mapping[str, Any],
    ) -> bool:
        return observation is not None and all(
            observation.get(key) == arguments.get(key)
            for key in ("customer_id", "order_id", "reason", "resolution")
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
            return {"created": False, "business_error": "order_customer_mismatch"}
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
        if ticket is not None:
            operation_row = self.store.query_one(
                """
                SELECT operation_id FROM phase3_external_operations
                WHERE task_id = ?
                  AND json_extract(operation_json, '$.external_operation_id') = ?
                ORDER BY operation_id LIMIT 1
                """,
                (context.task_id, args.ticket_id),
            )
            if operation_row is not None:
                self.governance_core.record_business_observation(
                    str(operation_row["operation_id"]),
                    ticket,
                    external_object_id=args.ticket_id,
                )
        return {"found": ticket is not None, "ticket": ticket}

    @staticmethod
    def _boundary_error(tool_name: str, error: BaseException) -> ToolResult:
        message = str(error) or type(error).__name__
        error_type = message.split(":", 1)[0].replace(" ", "_").lower()
        return AstraToolGateway._error(tool_name, error_type, message)

    @staticmethod
    def _error(tool_name: str, error_type: str, message: str) -> ToolResult:
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error={"type": error_type, "message": message},
        )
