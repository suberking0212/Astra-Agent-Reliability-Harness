"""Astra public contracts.

Keep package import side-effect free: the Hermes project plugin imports a
small catalog module inside the Agent's OS sandbox, while the production
composition (and its business adapter) belongs exclusively to Runtime.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

__all__ = [
    "AgentExecutor", "ExecutionEvent", "ExecutionResult", "ExecutionStatus",
    "ExecutionUsage", "RuntimeInvocation", "ProductionConfig",
    "ProductionRuntime", "ToolInvocationContext", "ToolResult",
    "TaskCancellationResult",
]

_EXPORTS = {
    "AgentExecutor": ("astra.domain", "AgentExecutor"),
    "ExecutionEvent": ("astra.domain", "ExecutionEvent"),
    "ExecutionResult": ("astra.domain", "ExecutionResult"),
    "ExecutionStatus": ("astra.domain", "ExecutionStatus"),
    "ExecutionUsage": ("astra.domain", "ExecutionUsage"),
    "RuntimeInvocation": ("astra.domain", "RuntimeInvocation"),
    "ToolInvocationContext": ("astra.domain", "ToolInvocationContext"),
    "ToolResult": ("astra.domain", "ToolResult"),
    "ProductionConfig": ("astra.production", "ProductionConfig"),
    "ProductionRuntime": ("astra.production", "ProductionRuntime"),
    "TaskCancellationResult": ("astra.runtime", "TaskCancellationResult"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


if TYPE_CHECKING:
    from .domain import AgentExecutor, ExecutionEvent, ExecutionResult, ExecutionStatus, ExecutionUsage, RuntimeInvocation, ToolInvocationContext, ToolResult
    from .production import ProductionConfig, ProductionRuntime
    from .runtime import TaskCancellationResult
