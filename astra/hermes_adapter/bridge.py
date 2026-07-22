"""Fixed Hermes plugin surface for Astra Phase 2."""

from __future__ import annotations

import json
from typing import Any

from ..domain import InteractionKind
from ..tool_gateway import BUSINESS_TOOL_SCHEMAS
from .context import registry


BUSINESS_SCHEMAS = BUSINESS_TOOL_SCHEMAS


def _active(kwargs: dict[str, Any]):
    return registry.get(kwargs.get("task_id"))


def _business_handler(name: str):
    def handler(args, **kwargs):
        context = _active(kwargs)
        if context is None:
            return json.dumps({"ok": False, "error": {"type": "missing_context"}})
        return context.invoke_tool(
            name,
            args or {},
            tool_call_id=kwargs.get("tool_call_id"),
        )

    return handler


def _request_user_input(args, **kwargs):
    context = _active(kwargs)
    if context is None:
        return json.dumps({"ok": False, "error": {"type": "missing_context"}})
    return context.request_interaction(
        InteractionKind.USER_INPUT,
        str(args["prompt"]),
        {"details": args.get("details")},
    )


def _request_approval(args, **kwargs):
    context = _active(kwargs)
    if context is None:
        return json.dumps({"ok": False, "error": {"type": "missing_context"}})
    return context.request_approval(
        str(args["prompt"]),
        args.get("tool_name"),
        args.get("arguments") or {},
    )


def _submit_task_result(args, **kwargs):
    context = _active(kwargs)
    if context is None:
        return json.dumps({"ok": False, "error": {"type": "missing_context"}})
    return context.submit_task_result(
        args.get("outcome") or {},
        args.get("evidence_refs") or [],
        args.get("receipt_refs") or [],
    )


def _pre_tool_call(**kwargs):
    context = _active(kwargs)
    if context is None:
        return {"action": "block", "message": "Astra execution context missing"}
    tool_name = str(kwargs.get("tool_name", ""))
    if tool_name in BUSINESS_SCHEMAS and tool_name not in context.invocation.allowed_tools:
        return {
            "action": "block",
            "message": "Tool denied by persisted Astra Task Contract",
        }
    if (
        tool_name == "create_complaint_ticket"
        and context.store.is_suspended(context.invocation.execution_id)
    ):
        return {
            "action": "block",
            "message": "Business side effects denied after suspension_requested",
        }
    return None


def _pre_api_request(**kwargs):
    context = _active(kwargs)
    if context is not None:
        context.emit(
            "ProviderCallStarted",
            "hermes_observer",
            {"api_call_count": kwargs.get("api_call_count")},
        )


def _post_api_request(**kwargs):
    context = _active(kwargs)
    if context is not None:
        context.observe_provider_call(kwargs.get("usage") or {})


def _post_tool_call(**kwargs):
    context = _active(kwargs)
    if context is not None:
        context.emit(
            "HermesToolCallFinished",
            "hermes_observer",
            {
                "tool_name": kwargs.get("tool_name"),
                "status": kwargs.get("status"),
                "error_type": kwargs.get("error_type"),
            },
        )


def _post_llm_call(**kwargs):
    context = _active(kwargs)
    if context is not None:
        context.emit(
            "AgentTurnOutput",
            "hermes_observer",
            {"has_output": bool(kwargs.get("assistant_response"))},
        )


def register(ctx) -> None:
    for name, spec in BUSINESS_SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset=f"astra_business_{name}",
            schema={"name": name, **spec},
            handler=_business_handler(name),
        )
    ctx.register_tool(
        name="request_user_input",
        toolset="astra_runtime",
        schema={
            "name": "request_user_input",
            "description": "Suspend the task and request required user input.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "details": {"type": "object"},
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
        handler=_request_user_input,
    )
    ctx.register_tool(
        name="request_approval",
        toolset="astra_runtime",
        schema={
            "name": "request_approval",
            "description": "Suspend the task and request runtime approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
        handler=_request_approval,
    )
    ctx.register_tool(
        name="submit_task_result",
        toolset="astra_runtime",
        schema={
            "name": "submit_task_result",
            "description": "Submit an outcome with evidence and execution receipts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "outcome": {"type": "object"},
                    "evidence_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "receipt_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["outcome", "evidence_refs", "receipt_refs"],
                "additionalProperties": False,
            },
        },
        handler=_submit_task_result,
    )
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("pre_api_request", _pre_api_request)
    ctx.register_hook("post_api_request", _post_api_request)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
