"""Astra's Hermes public-plugin adapter.

This module uses only the documented ``PluginContext`` methods supplied to a
Hermes plugin.  It deliberately does not import Hermes internals, create an
agent, or run a conversation loop.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from astra.business import BusinessSandboxConfig
from astra.production import ProductionConfig, ProductionRuntime


class _UnavailablePublicHermesExecutor:
    """Fail closed until Hermes publishes a task-to-session execution API."""

    async def execute(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError("hermes_public_task_execution_api_unavailable")

    async def cancel(self, *_: Any, **__: Any) -> None:
        return None


_runtime: ProductionRuntime | None = None


def _sandbox_config() -> BusinessSandboxConfig:
    names = {
        "endpoint": "ASTRA_BUSINESS_SANDBOX_ENDPOINT",
        "token": "ASTRA_BUSINESS_SANDBOX_TOKEN",
        "commerce_authority_domain": "ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN",
        "support_authority_domain": "ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN",
    }
    missing = [name for name in names.values() if not os.environ.get(name)]
    if missing:
        raise RuntimeError("missing Astra Business Sandbox configuration: " + ", ".join(missing))
    return BusinessSandboxConfig(**{field: os.environ[name] for field, name in names.items()})


def runtime() -> ProductionRuntime:
    """Build one process-local Runtime without taking ownership of Hermes."""
    global _runtime
    if _runtime is None:
        _runtime = ProductionRuntime(
            ProductionConfig(
                database_path=os.environ.get("ASTRA_DATABASE", "var/astra.sqlite3"),
                business_sandbox_config=_sandbox_config(),
                hermes_home=os.environ.get("HERMES_HOME"),
            ),
            executor_factory=lambda _dependencies: _UnavailablePublicHermesExecutor(),
        )
    return _runtime


def _status(args: str) -> str:
    task_id = args.strip()
    if not task_id:
        return "用法：/status <task-id>"
    try:
        status = runtime().task_status(task_id)
    except KeyError:
        return f"未找到任务：{task_id}"
    task = status["task"]
    runtime_state = task.get("state")
    return json.dumps(
        {
            "task_id": task_id,
            "runtime_status": runtime_state,
            "runtime_status_zh": _localize_status(runtime_state),
        },
        ensure_ascii=False,
    )


def _task_status_tool(args: Mapping[str, Any], **_: Any) -> str:
    return _status(str(args.get("task_id", "")))


def _localize_status(status: object) -> str:
    return {
        "waiting_input": "等待输入",
        "waiting_approval": "等待审批",
        "succeeded": "已成功",
        "failed": "已失败",
        "cancelled": "已取消",
    }.get(str(status), "未知状态")


def register(ctx: Any) -> None:
    """Register only documented Hermes plugin extension points."""
    runtime()  # validates Runtime, Gateway, Governance, and Business Sandbox config
    ctx.register_tool(
        name="astra_runtime_status",
        toolset="astra_runtime",
        schema={
            "name": "astra_runtime_status",
            "description": "Read the authoritative Astra Runtime status for a task.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        handler=_task_status_tool,
    )
