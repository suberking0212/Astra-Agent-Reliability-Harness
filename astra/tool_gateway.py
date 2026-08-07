"""Production Task Contract and canonical-effect enforcement gateway."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import ValidationError

from .business import BusinessService
from .dynamic_authority import (
    effective_contract,
    has_subject_grant,
    record_dynamic_effect_intent,
    record_subject_grant,
    subject_validated_in_user_resolution,
)
from .domain import ToolInvocationContext, ToolResult
from .observations import (
    ConfirmationStatus,
    ExternalObservation,
    ExternalOperationConfirmation,
)
from .tool_catalog import (
    TOOL_DISPLAY_NAMES,
    TOOL_SCHEMAS,
    tool_schema_hash,
)
from .tool_definitions import (
    ToolDefinition,
    build_business_tool_definitions,
    build_dynamic_intent,
)
from .phase3.effects import (
    CanonicalEffectRequest,
    EffectContractError,
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
)
from .storage import AstraStore


BUSINESS_TOOL_SCHEMAS = TOOL_SCHEMAS
BUSINESS_TOOL_DISPLAY_NAMES = TOOL_DISPLAY_NAMES


@dataclass
class GatewayValidationTrace:
    """Per-invocation facts emitted by the generic Gateway enforcement path."""

    checks: dict[str, str] = field(
        default_factory=lambda: {
            "capability check": "not_reached",
            "schema check": "not_reached",
            "authority check": "not_reached",
            "approval requirement": "not_reached",
        }
    )

    def mark(self, check: str, status: str) -> None:
        self.checks[check] = status


_VALIDATION_TRACE: ContextVar[GatewayValidationTrace | None] = ContextVar(
    "astra_gateway_validation_trace", default=None
)


def _mark_validation(check: str, status: str) -> None:
    trace = _VALIDATION_TRACE.get()
    if trace is not None:
        trace.mark(check, status)


def production_tool_schema_hash(tool_name: str, *, tool_version: str = "1") -> str:
    """Return the identity of the exact schema exposed to Hermes."""

    return tool_schema_hash(tool_name, tool_version=tool_version)


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


class AstraToolGateway:
    def __init__(
        self,
        store: AstraStore,
        business: BusinessService | None = None,
        *,
        governance_core: RuntimeGovernanceCore | None = None,
        normalizer_registry: EffectNormalizerRegistry | None = None,
        constraint_registry: ConstraintHandlerRegistry | None = None,
        tool_definitions: Mapping[str, ToolDefinition] | None = None,
    ) -> None:
        self.store = store
        self.governance_core = governance_core or RuntimeGovernanceCore(
            GovernanceStore.from_astra_store(store)
        )
        if self.governance_core.store.authority_store is not store:
            raise ValueError("Tool Gateway governance must share AstraStore authority")
        self.normalizer_registry = normalizer_registry or EffectNormalizerRegistry()
        self.constraint_registry = constraint_registry or ConstraintHandlerRegistry()
        if tool_definitions is None:
            if business is None:
                raise ValueError(
                    "business service is required when tool definitions are not provided"
                )
            definitions = build_business_tool_definitions(
                business, store, self.governance_core
            )
        else:
            definitions = tool_definitions
        self._specs = dict(definitions)
        for tool_name, definition in self._specs.items():
            if definition.catalog.tool_name != tool_name:
                raise ValueError("tool_definition_name_mismatch")
            if definition.side_effect != (definition.effect is not None):
                raise ValueError("tool_definition_effect_mode_mismatch")
            if (
                definition.effect is not None
                and definition.effect.adapter is not definition.adapter
            ):
                raise ValueError("tool_definition_adapter_mismatch")
            if definition.effect is not None:
                key = (
                    definition.effect.normalizer.normalizer_id,
                    definition.effect.normalizer.normalizer_version,
                )
                if key not in self.normalizer_registry.registered_refs:
                    self.normalizer_registry.register(definition.effect.normalizer)

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
        result, _ = self.invoke_with_trace(context, tool_name, arguments)
        return result

    def invoke_with_trace(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> tuple[ToolResult, GatewayValidationTrace]:
        """Invoke normally and return the checks reached by this exact call."""

        trace = GatewayValidationTrace()
        token = _VALIDATION_TRACE.set(trace)
        try:
            result = self._invoke(context, tool_name, arguments)
        finally:
            _VALIDATION_TRACE.reset(token)
        error_type = str((result.error or {}).get("type") or "")
        if trace.checks["capability check"] == "not_reached":
            if error_type in {
                "unknown_tool",
                "tool_binding_missing",
                "tool_binding_ambiguous",
                "capability_mismatch",
                "tool_version_mismatch",
                "tool_schema_hash_mismatch",
                "tool_access_mode_mismatch",
            }:
                trace.mark("capability check", "failed")
        if error_type == "invalid_tool_arguments":
            trace.mark("schema check", "failed")
        if (
            trace.checks["authority check"] == "not_reached"
            and trace.checks["schema check"] == "passed"
            and error_type
        ):
            trace.mark("authority check", "failed")
        if trace.checks["approval requirement"] == "not_reached":
            if error_type == "approval_required":
                trace.mark("approval requirement", "required")
            elif result.ok:
                trace.mark("approval requirement", "not_required")
        return result, trace

    def _invoke(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        spec = self._specs.get(tool_name)
        if spec is not None and spec.side_effect:
            if self.store.connection.in_transaction:
                return self._error(
                    tool_name,
                    "external_effect_transaction_boundary_violation",
                    "Effect dispatch cannot run inside an existing SQLite transaction",
                )
            return self._invoke_effect_entry(
                context=context,
                tool_name=tool_name,
                spec=spec,
                arguments=arguments,
            )

        with self.store.transaction():
            return self._invoke_under_lease(context, tool_name, arguments)

    def confirm_external_operation(
        self,
        operation: ExternalOperation,
        canonical: CanonicalEffectRequest,
    ) -> ExternalOperationConfirmation:
        """Use the registered adapter's observation and confirmation semantics."""

        if (
            canonical.authority_domain != operation.authority_domain
            or canonical.effect_identity != operation.effect_identity
            or canonical.effect_request_hash != operation.effect_request_hash
        ):
            raise RuntimeError("Reconciliation canonical operation mismatch")
        matches = tuple(
            definition.effect
            for definition in self._specs.values()
            if definition.effect is not None
            and definition.effect.effect_type == canonical.effect_type
            and definition.effect.effect_type_version == canonical.effect_type_version
            and definition.effect.authority_domain == canonical.authority_domain
            and definition.effect.normalizer.normalizer_id
            == canonical.normalizer_id
            and definition.effect.normalizer.normalizer_version
            == canonical.normalizer_version
        )
        if len(matches) != 1:
            raise RuntimeError("Effect adapter binding missing or ambiguous")
        adapter = matches[0].adapter
        observation = adapter.observe(operation)
        if observation is None:
            return ExternalOperationConfirmation(
                status=ConfirmationStatus.INDETERMINATE,
                reason="authoritative_observation_missing",
            )
        if not isinstance(observation, ExternalObservation):
            raise RuntimeError("Effect adapter returned an invalid Observation")
        if (
            operation.external_operation_id is not None
            and operation.external_operation_id != observation.external_object_id
        ):
            return ExternalOperationConfirmation(
                status=ConfirmationStatus.INDETERMINATE,
                reason="observation_identity_mismatch",
            )
        match_result = adapter.confirmation_matches(
            observation.payload, canonical.normalized_parameters
        )
        confirmation_status = (
            match_result
            if isinstance(match_result, ConfirmationStatus)
            else (
                ConfirmationStatus.CONFIRMED
                if match_result
                else ConfirmationStatus.INDETERMINATE
            )
        )
        if confirmation_status != ConfirmationStatus.CONFIRMED:
            return ExternalOperationConfirmation(
                status=confirmation_status,
                observation=observation,
                reason=(
                    "authoritative_failed"
                    if confirmation_status == ConfirmationStatus.FAILED
                    else "authoritative_mismatch"
                ),
            )
        return ExternalOperationConfirmation(
            status=ConfirmationStatus.CONFIRMED,
            observation=observation,
        )

    def observe_external_operation(
        self,
        operation: ExternalOperation,
        canonical: CanonicalEffectRequest,
    ) -> ExternalOperationConfirmation:
        """Compatibility name for the recovery confirmation boundary."""

        return self.confirm_external_operation(operation, canonical)

    def _invoke_effect_entry(
        self,
        *,
        context: ToolInvocationContext,
        tool_name: str,
        spec: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        """Commit effect authority before crossing the business boundary."""

        try:
            with self.store.transaction():
                contract, binding = self._load_authoritative_binding(
                    context, tool_name
                )
                self.constraint_registry.validate_contract(contract)
                parsed = spec.input_model.model_validate(arguments)
                parsed_arguments = parsed.model_dump()
                _mark_validation("schema check", "passed")
                self._enforce_subject_scope(
                    context, contract, spec, parsed_arguments
                )
                self._verify_subject_relations(spec, parsed_arguments)
                self.constraint_registry.enforce(
                    contract,
                    binding,
                    parsed_arguments,
                    point=EnforcementPoint.TOOL_GATEWAY,
                )
                _mark_validation("authority check", "passed")
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

        return self._invoke_effect(
            context=context,
            tool_name=tool_name,
            spec=spec,
            contract=contract,
            binding=binding,
            parsed_arguments=parsed_arguments,
        )

    def _invoke_under_lease(
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
            parsed = spec.input_model.model_validate(arguments)
            parsed_arguments = parsed.model_dump()
            _mark_validation("schema check", "passed")
            self._enforce_subject_scope(context, contract, spec, parsed_arguments)
            self._verify_subject_relations(spec, parsed_arguments)
            self.constraint_registry.enforce(
                contract,
                binding,
                parsed_arguments,
                point=EnforcementPoint.TOOL_GATEWAY,
            )
            _mark_validation("authority check", "passed")
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
            _mark_validation("approval requirement", "not_required")
            data = dict(spec.adapter.invoke(parsed_arguments, context))
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
            self._record_authoritative_subject_derivations(
                context=context,
                spec=spec,
                data=data,
                receipt_id=receipt_id,
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
            parsed_arguments=parsed_arguments,
        )

    def canonicalize_effect_request(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> CanonicalEffectRequest:
        """Validate and normalize one effect without preparing or dispatching it."""

        with self.store.transaction():
            return self._canonicalize_effect_request_under_lease(
                context, tool_name, arguments
            )

    def _canonicalize_effect_request_under_lease(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> CanonicalEffectRequest:

        spec = self._specs.get(tool_name)
        if spec is None:
            raise PermissionError("unknown_tool")
        if not spec.side_effect:
            raise PermissionError("approval_not_required")
        contract, binding = self._load_authoritative_binding(context, tool_name)
        self.constraint_registry.validate_contract(contract)
        parsed = spec.input_model.model_validate(arguments)
        parsed_arguments = parsed.model_dump()
        _mark_validation("schema check", "passed")
        self._enforce_subject_scope(context, contract, spec, parsed_arguments)
        self._verify_subject_relations(spec, parsed_arguments)
        self.constraint_registry.enforce(
            contract,
            binding,
            parsed_arguments,
            point=EnforcementPoint.TOOL_GATEWAY,
        )
        _mark_validation("authority check", "passed")
        if spec.effect is None:
            raise PermissionError("unknown_effect_normalizer")
        self.constraint_registry.enforce(
            contract,
            binding,
            parsed_arguments,
            point=EnforcementPoint.EFFECT_NORMALIZER,
        )
        contract = self._ensure_dynamic_effect_contract(
            context=context,
            tool_name=tool_name,
            spec=spec,
            contract=contract,
            arguments=parsed_arguments,
        )
        effect = self.normalizer_registry.normalize(
            contract,
            parsed_arguments,
            normalizer_id=spec.effect.normalizer.normalizer_id,
            normalizer_version=spec.effect.normalizer.normalizer_version,
        )
        self._enforce_effect_authority(effect, spec)
        return effect

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
                   request.state AS run_request_state,
                   request.run_request_id, request.lease_owner_id,
                   request.lease_token, request.lease_expires_at
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
        if context.run_request_id != row["run_request_id"]:
            raise PermissionError("run_request_lease_mismatch")
        if (
            context.lease_owner_id is None
            or context.lease_token is None
            or context.lease_owner_id != row["lease_owner_id"]
            or context.lease_token != row["lease_token"]
        ):
            raise PermissionError("stale_lease_token")
        if row["run_request_state"] != "claimed":
            raise PermissionError("run_request_not_claimed")
        if row["lease_expires_at"] is None:
            raise PermissionError("lease_expired")
        expires_at = datetime.fromisoformat(
            str(row["lease_expires_at"]).replace("Z", "+00:00")
        )
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise PermissionError("lease_expired")

        contract = effective_contract(
            self.store,
            context.task_id,
            TaskContract.model_validate_json(row["contract_json"]),
        )
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
        if binding.capability_ref != spec.catalog.capability_ref:
            raise PermissionError("capability_mismatch")
        if binding.tool_version != spec.catalog.tool_version:
            raise PermissionError("tool_version_mismatch")
        if binding.schema_hash != spec.catalog.schema_hash:
            raise PermissionError("tool_schema_hash_mismatch")
        if binding.access_mode != spec.catalog.access_mode:
            raise PermissionError("tool_access_mode_mismatch")
        _mark_validation("capability check", "passed")
        return contract, binding

    def _enforce_subject_scope(
        self,
        context: ToolInvocationContext,
        contract: TaskContract,
        spec: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> None:
        for subject_binding in spec.subject_bindings:
            subject_id = arguments.get(subject_binding.argument)
            if not isinstance(subject_id, str):
                raise PermissionError("subject_scope_missing")
            if any(
                subject.id == subject_id
                and subject.type == subject_binding.subject_type
                and subject.authority_domain == subject_binding.authority_domain
                for subject in contract.subject_refs
            ):
                continue
            access_scope = (
                subject_binding.effect_scope
                if spec.side_effect
                else subject_binding.read_scope
            )
            if has_subject_grant(
                self.store,
                task_id=context.task_id,
                authority_domain=subject_binding.authority_domain,
                subject_type=subject_binding.subject_type,
                subject_id=subject_id,
                access_scope=access_scope,
            ):
                continue
            interaction_id = subject_validated_in_user_resolution(
                self.store,
                task_id=context.task_id,
                subject_id=subject_id,
            )
            if interaction_id is not None:
                record_subject_grant(
                    self.store,
                    task_id=context.task_id,
                    authority_domain=subject_binding.authority_domain,
                    subject_type=subject_binding.subject_type,
                    subject_id=subject_id,
                    access_scope=access_scope,
                    source_type="user_interaction",
                    source_ref=interaction_id,
                )
                continue
            raise PermissionError("subject_scope_mismatch")

    @staticmethod
    def _verify_subject_relations(
        spec: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> None:
        if spec.relation_verifier is not None:
            spec.relation_verifier(arguments)

    def _record_authoritative_subject_derivations(
        self,
        *,
        context: ToolInvocationContext,
        spec: ToolDefinition,
        data: Mapping[str, Any],
        receipt_id: str,
    ) -> None:
        """Grant read access to subjects returned by an authoritative tool."""

        for derivation in spec.subject_derivations:
            container = data.get(derivation.container_field)
            candidates: Sequence[Any]
            if derivation.many:
                if not isinstance(container, Sequence) or isinstance(
                    container, (str, bytes)
                ):
                    continue
                candidates = container
            else:
                candidates = (container,)
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                subject_id = candidate.get(derivation.id_field)
                if not isinstance(subject_id, str) or not subject_id:
                    continue
                record_subject_grant(
                    self.store,
                    task_id=context.task_id,
                    authority_domain=derivation.authority_domain,
                    subject_type=derivation.subject_type,
                    subject_id=subject_id,
                    access_scope=derivation.access_scope,
                    source_type="authoritative_receipt",
                    source_ref=receipt_id,
                )

    def _ensure_dynamic_effect_contract(
        self,
        *,
        context: ToolInvocationContext,
        tool_name: str,
        spec: ToolDefinition,
        contract: TaskContract,
        arguments: Mapping[str, Any],
    ) -> TaskContract:
        """Turn one exact Agent effect call into an approval-bound proposal."""

        if any(
            (
                spec.effect is None
                or intent.subject_ref.id
                == arguments.get(spec.effect.subject_binding.argument)
            )
            and all(
                arguments.get(key) == value
                for key, value in intent.parameter_constraints.items()
            )
            for intent in contract.authorized_effects
        ):
            return contract
        # Explicit frozen intents are already deliberately scoped. If the
        # Agent's parameters do not match them, fail closed instead of creating
        # a broader runtime proposal.
        if contract.authorized_effects:
            return contract
        if spec.effect is None:
            return contract
        effect, requirement = build_dynamic_intent(
            task_id=context.task_id,
            tool_name=tool_name,
            arguments=arguments,
            effect=spec.effect,
        )
        record_dynamic_effect_intent(
            self.store,
            task_id=context.task_id,
            tool_name=tool_name,
            arguments=arguments,
            effect=effect,
            approval_requirement=requirement,
        )
        return effective_contract(self.store, context.task_id, contract)

    def _invoke_effect(
        self,
        *,
        context: ToolInvocationContext,
        tool_name: str,
        spec: ToolDefinition,
        contract: TaskContract,
        binding: ResolvedTool,
        parsed_arguments: Mapping[str, Any],
    ) -> ToolResult:
        if spec.effect is None:
            return self._error(
                tool_name,
                "unknown_effect_normalizer",
                "Effect Tool has no registered normalizer binding",
            )
        effect: CanonicalEffectRequest | None = None
        try:
            # Persist the exact proposal before the preparation transaction.
            # An expected approval_required result must not roll back the
            # authority record needed to validate the resumed approval.
            contract = self._ensure_dynamic_effect_contract(
                context=context,
                tool_name=tool_name,
                spec=spec,
                contract=contract,
                arguments=parsed_arguments,
            )
            with self.store.transaction():
                self.constraint_registry.enforce(
                    contract,
                    binding,
                    parsed_arguments,
                    point=EnforcementPoint.EFFECT_NORMALIZER,
                )
                effect = self.normalizer_registry.normalize(
                    contract,
                    parsed_arguments,
                    normalizer_id=spec.effect.normalizer.normalizer_id,
                    normalizer_version=spec.effect.normalizer.normalizer_version,
                )
                self._enforce_effect_authority(effect, spec)
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
                _mark_validation(
                    "approval requirement",
                    "satisfied" if approval_resolution_id is not None else "not_required",
                )
        except (EffectContractError, PermissionError, RuntimeError, ValueError) as exc:
            if (
                str(exc).split(":", 1)[0] == "approval_required"
                and effect is not None
            ):
                _mark_validation("approval requirement", "required")
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
                context, tool_name, spec, parsed_arguments, operation
            )
        if operation.status in {
            ExternalOperationStatus.IN_FLIGHT,
            ExternalOperationStatus.ACKNOWLEDGED,
            ExternalOperationStatus.INDETERMINATE,
        }:
            return self._error(
                tool_name,
                "external_operation_reconciliation_required",
                "Existing ExternalOperation may have crossed the dispatch boundary",
            )
        if operation.status == ExternalOperationStatus.FAILED:
            return self._error(
                tool_name,
                "external_operation_failed",
                "Existing ExternalOperation is terminally failed",
            )

        try:
            with self.store.transaction():
                # Re-check cancel, lifecycle and the exact fencing token at the
                # final local boundary immediately before dispatch authority is
                # committed.
                self._load_authoritative_binding(context, tool_name)
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
            dispatch = spec.effect.adapter.dispatch(
                parsed_arguments, governed_context
            )
            data = dict(dispatch.result)
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

        if not dispatch.accepted:
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
                str(dispatch.error or "Business authority rejected request"),
            )
        try:
            operation = self.governance_core.transition_external_operation(
                operation.operation_id,
                ExternalOperationStatus.ACKNOWLEDGED,
                external_operation_id=dispatch.external_object_id,
                actor_task_id=context.task_id,
                actor_attempt_id=context.attempt_id,
                actor_execution_id=context.execution_id,
            )
            confirmation = self.confirm_external_operation(operation, effect)
            observation = confirmation.observation
            if observation is not None:
                self.governance_core.record_business_observation(
                    operation.operation_id,
                    observation.payload,
                    external_object_id=observation.external_object_id,
                )
            if confirmation.status != ConfirmationStatus.CONFIRMED:
                self.governance_core.transition_external_operation(
                    operation.operation_id,
                    (
                        ExternalOperationStatus.FAILED
                        if confirmation.status == ConfirmationStatus.FAILED
                        else ExternalOperationStatus.INDETERMINATE
                    ),
                    external_operation_id=(
                        observation.external_object_id
                        if observation is not None
                        else dispatch.external_object_id
                    ),
                )
                return self._error(
                    tool_name,
                    (
                        "external_operation_failed"
                        if confirmation.status == ConfirmationStatus.FAILED
                        else "authoritative_confirmation_failed"
                    ),
                    str(
                        confirmation.reason
                        or "Business authority state does not confirm the canonical request"
                    ),
                )
            operation = self.governance_core.transition_external_operation(
                operation.operation_id,
                ExternalOperationStatus.CONFIRMED,
                external_operation_id=observation.external_object_id,
                actor_task_id=context.task_id,
                actor_attempt_id=context.attempt_id,
                actor_execution_id=context.execution_id,
            )
        except Exception as exc:
            try:
                self.governance_core.transition_external_operation(
                    operation.operation_id,
                    ExternalOperationStatus.INDETERMINATE,
                    external_operation_id=dispatch.external_object_id,
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

    @staticmethod
    def _enforce_effect_authority(
        effect: CanonicalEffectRequest,
        spec: ToolDefinition,
    ) -> None:
        definition = spec.effect
        if definition is None:
            raise PermissionError("effect_definition_missing")
        binding = definition.subject_binding
        if effect.effect_type != definition.effect_type:
            raise PermissionError("effect_type_mismatch")
        if effect.effect_type_version != definition.effect_type_version:
            raise PermissionError("effect_type_version_mismatch")
        if effect.authority_domain != definition.authority_domain:
            raise PermissionError("effect_authority_domain_mismatch")
        if (
            effect.subject_ref.authority_domain != binding.authority_domain
            or effect.subject_ref.type != binding.subject_type
            or effect.subject_ref.id
            != effect.normalized_parameters.get(binding.argument)
        ):
            raise PermissionError("effect_subject_mismatch")

    def _confirmed_replay(
        self,
        context: ToolInvocationContext,
        tool_name: str,
        spec: ToolDefinition,
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
        if spec.effect is None:
            return self._error(
                tool_name, "effect_definition_missing", "Effect definition is missing"
            )
        canonical_row = self.store.query_one(
            """
            SELECT request_json FROM phase3_canonical_effect_requests
            WHERE effect_identity = ?
            """,
            (operation.effect_identity,),
        )
        if canonical_row is None:
            return self._error(
                tool_name,
                "canonical_effect_missing",
                "Confirmed operation has no canonical effect request",
            )
        canonical = CanonicalEffectRequest.model_validate_json(
            canonical_row["request_json"]
        )
        confirmation = self.confirm_external_operation(operation, canonical)
        observation = confirmation.observation
        if observation is not None:
            self.governance_core.record_business_observation(
                operation.operation_id,
                observation.payload,
                external_object_id=observation.external_object_id,
            )
        if confirmation.status != ConfirmationStatus.CONFIRMED:
            return self._error(
                tool_name,
                "authoritative_confirmation_failed",
                "Confirmed operation no longer matches authoritative business state",
            )
        data = {
            **dict(spec.effect.adapter.replay_result(observation.payload)),
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
    def _boundary_error(tool_name: str, error: BaseException) -> ToolResult:
        message = str(error) or type(error).__name__
        error_type = message.split(":", 1)[0].replace(" ", "_").lower()
        return AstraToolGateway._error(tool_name, error_type, message)

    @staticmethod
    def _error(tool_name: str, error_type: str, message: str) -> ToolResult:
        error: dict[str, Any] = {"type": error_type, "message": message}
        if error_type in {"subject_scope_missing", "subject_scope_mismatch"}:
            error.update(
                {
                    "category": "authorization_scope",
                    "retryable": False,
                    "entity_existence": "unknown",
                    "required_action": "request_user_input",
                }
            )
        return ToolResult(
            ok=False,
            tool_name=tool_name,
            error=error,
        )
