"""Agent-facing semantic capability broker.

The broker owns only projection and dispatch.  Task, authorization, execution,
receipt, evidence, and completion truth remain owned by ``RuntimeService`` and
the existing Runtime/Gateway path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping
from uuid import uuid4


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    requested_tools: tuple[str, ...]
    subject_builder: Callable[[Any, Any], tuple[dict[str, Any], ...]]
    objective_builder: Callable[[Any], str]
    result_projector: Callable[[Any, Any], dict[str, Any]]


class CapabilityBroker:
    """Dispatch semantic capabilities through an existing RuntimeService."""

    def __init__(self, runtime: Any, definitions: tuple[CapabilityDefinition, ...] = ()) -> None:
        self.runtime = runtime
        self._definitions = {item.name: item for item in definitions}

    def definitions(self) -> tuple[CapabilityDefinition, ...]:
        return tuple(self._definitions[name] for name in sorted(self._definitions))

    def register(self, definition: CapabilityDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"capability_already_registered:{definition.name}")
        self._definitions[definition.name] = definition

    def invoke(self, name: str, args: Mapping[str, Any], *, on_started: Callable[[str], None] | None = None) -> dict[str, Any]:
        definition = self._definitions.get(name)
        if definition is None:
            raise ValueError(f"unknown_semantic_capability:{name}")
        values = dict(args)
        submission = self.runtime.submit({
            "command_id": "broker-command:" + str(uuid4()),
            "task_id": "broker-task:" + str(uuid4()),
            "objective": definition.objective_builder(values),
            "subjects": definition.subject_builder(self.runtime, values),
            "requested_tools": definition.requested_tools,
            "inputs": values,
        })
        task_id = str(submission["task_id"])
        if on_started is not None:
            on_started(task_id)
        outcomes = []
        for tool_name in definition.requested_tools:
            outcomes.append(self.runtime.business(tool_name, {
                "task_id": task_id,
                "arguments": values,
            }))
        return definition.result_projector(values, outcomes[-1])
