"""The single, immutable identity catalog for production business tools.

Contracts consume bindings from here; the Gateway verifies against the same
bindings.  Callers must never recreate capability/version/schema identities.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .phase3.canonical import sha256_digest
from .phase3.task_contract import CapabilityRef, ResolvedTool, TaskContract, ToolAccessMode


class CatalogIntegrityError(ValueError):
    """A persisted contract is not an exact projection of the Tool Catalog."""


@dataclass(frozen=True)
class CatalogTool:
    tool_name: str
    capability_ref: str
    access_mode: ToolAccessMode
    schema: Mapping[str, Any]
    tool_version: str = "1"
    execution_profiles: tuple[str, ...] = ("business-task",)
    governed: bool = True
    authoritative_source: str = "astra_runtime.tool_result_receipt_evidence"
    failure_policy: str = "fail_closed_runtime_only"

    @property
    def schema_hash(self) -> str:
        return sha256_digest({"name": self.tool_name, "tool_version": self.tool_version, **dict(self.schema)})

    def binding(self) -> ResolvedTool:
        return ResolvedTool(tool_name=self.tool_name, tool_version=self.tool_version,
                            schema_hash=self.schema_hash, capability_ref=self.capability_ref,
                            access_mode=self.access_mode)


_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    "get_customer": {"description": "Look up a customer by customer_id.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}},
    "list_customer_orders": {"description": "List authoritative orders for one customer_id.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"], "additionalProperties": False}},
    "get_order": {"description": "Look up an order by order_id.", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}},
    "search_policy": {"description": "Search complaint and after-sales policies.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
    "create_complaint_ticket": {"description": "Create one governed complaint ticket for support or manual review. This tool does not issue a refund or replacement by itself.", "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}, "order_id": {"type": "string"}, "reason": {"type": "string"}, "resolution": {"type": "string"}}, "required": ["customer_id", "order_id", "reason", "resolution"], "additionalProperties": False}},
    "get_complaint_ticket": {"description": "Read back a complaint ticket for business-state verification.", "parameters": {"type": "object", "properties": {"ticket_id": {"type": "string"}}, "required": ["ticket_id"], "additionalProperties": False}},
}

_READ_CAPABILITY = "complaints.integration@1"
TOOL_CATALOG: dict[str, CatalogTool] = {
    name: CatalogTool(
        name,
        _READ_CAPABILITY,
        ToolAccessMode.EFFECT if name == "create_complaint_ticket" else ToolAccessMode.READ,
        schema,
    )
    for name, schema in _SCHEMAS.items()
}

TOOL_SCHEMAS: dict[str, Mapping[str, Any]] = {
    name: tool.schema for name, tool in TOOL_CATALOG.items()
}

TOOL_DISPLAY_NAMES: dict[str, str] = {
    "get_customer": "查询客户资料",
    "list_customer_orders": "查询客户订单列表",
    "get_order": "查询订单",
    "search_policy": "查询业务规则",
    "create_complaint_ticket": "创建投诉工单",
    "get_complaint_ticket": "查询投诉工单",
}


def register_catalog_tool(
    tool: CatalogTool,
    *,
    display_name: str | None = None,
) -> None:
    """Register one Tool identity for schema exposure and Contract preflight."""

    if tool.tool_name in TOOL_CATALOG:
        raise ValueError(f"Tool already registered: {tool.tool_name}")
    TOOL_CATALOG[tool.tool_name] = tool
    TOOL_SCHEMAS[tool.tool_name] = tool.schema
    if display_name is not None:
        TOOL_DISPLAY_NAMES[tool.tool_name] = display_name


def unregister_catalog_tool(tool_name: str) -> None:
    """Remove a dynamically registered Tool (primarily for isolated runtimes/tests)."""

    TOOL_CATALOG.pop(tool_name, None)
    TOOL_SCHEMAS.pop(tool_name, None)
    TOOL_DISPLAY_NAMES.pop(tool_name, None)


def tool_schema(tool_name: str) -> Mapping[str, Any]:
    return TOOL_CATALOG[tool_name].schema


def tool_schema_hash(tool_name: str, *, tool_version: str = "1") -> str:
    tool = TOOL_CATALOG[tool_name]
    if tool_version != tool.tool_version:
        raise KeyError(f"{tool_name}@{tool_version}")
    return tool.schema_hash


def resolved_tools(names: Iterable[str]) -> tuple[ResolvedTool, ...]:
    return tuple(TOOL_CATALOG[name].binding() for name in names)


def tools_for_profile(profile: str) -> tuple[str, ...]:
    """Return capabilities, never a pre-composed workflow or ordered plan."""
    return tuple(
        name for name, tool in TOOL_CATALOG.items()
        if profile in tool.execution_profiles
    )
def allowed_capabilities(names: Iterable[str]) -> tuple[CapabilityRef, ...]:
    refs = dict.fromkeys(TOOL_CATALOG[name].capability_ref for name in names)
    return tuple(CapabilityRef(capability_id=ref.rsplit("@", 1)[0], capability_version=ref.rsplit("@", 1)[1]) for ref in refs)


def preflight_contract(
    contract: TaskContract | Mapping[str, Any] | str,
) -> TaskContract:
    """Validate one immutable Contract without reading natural-language fields.

    This is the single Contract/Catalog validation entry used for initial
    execution, Interaction resume, approval resume, and restart recovery.
    Objective and input message content are deliberately opaque to Preflight.
    """

    try:
        if isinstance(contract, TaskContract):
            validated = contract
        elif isinstance(contract, str):
            validated = TaskContract.model_validate_json(contract)
        else:
            validated = TaskContract.model_validate(contract)
    except Exception as exc:
        raise CatalogIntegrityError("contract_runtime_incompatible") from exc
    for binding in validated.resolved_tools:
        expected = TOOL_CATALOG.get(binding.tool_name)
        if expected is None:
            raise CatalogIntegrityError(
                f"contract_catalog_tool_missing:{binding.tool_name}"
            )
        if binding != expected.binding():
            raise CatalogIntegrityError(f"contract_catalog_mismatch:{binding.tool_name}")
    expected_capabilities = {tool.capability_ref for tool in (TOOL_CATALOG.get(binding.tool_name) for binding in validated.resolved_tools) if tool}
    actual_capabilities = {item.ref for item in validated.allowed_capabilities}
    if expected_capabilities and actual_capabilities != expected_capabilities:
        raise CatalogIntegrityError("contract_catalog_capability_mismatch")
    return validated
