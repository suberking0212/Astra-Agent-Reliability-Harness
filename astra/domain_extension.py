"""Minimal production injection boundary for one business domain."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from .phase3.effects import EffectNormalizer
from .phase3.governance import RuntimeGovernanceCore
from .result_validator import ResultEvaluator
from .storage import AstraStore
from .tool_definitions import ToolDefinition
from .tool_gateway import ConstraintHandler


ToolDefinitionsFactory = Callable[
    [AstraStore, RuntimeGovernanceCore], Mapping[str, ToolDefinition]
]


@dataclass(frozen=True)
class DomainExtension:
    """Static domain components merged by the production composition root."""

    extension_id: str
    tool_definitions: Mapping[str, ToolDefinition] = field(default_factory=dict)
    tool_definitions_factory: ToolDefinitionsFactory | None = None
    normalizers: Sequence[EffectNormalizer] = ()
    constraints: Sequence[ConstraintHandler] = ()
    result_evaluators: Sequence[ResultEvaluator] = ()

    def materialize_tool_definitions(
        self,
        store: AstraStore,
        governance: RuntimeGovernanceCore,
    ) -> Mapping[str, ToolDefinition]:
        generated = (
            self.tool_definitions_factory(store, governance)
            if self.tool_definitions_factory is not None
            else {}
        )
        overlap = set(self.tool_definitions).intersection(generated)
        if overlap:
            raise ValueError(f"duplicate extension tool definitions: {sorted(overlap)!r}")
        return {**self.tool_definitions, **generated}
