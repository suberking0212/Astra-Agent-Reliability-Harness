"""Complaint demo composition kept outside ProductionRuntime core."""

from __future__ import annotations

from .business import BusinessService
from .complaint_result import ComplaintResultEvaluator
from .domain_extension import DomainExtension
from .tool_definitions import (
    ComplaintOrderScopeConstraint,
    ExactParameterConstraint,
    build_business_tool_definitions,
)


def build_complaint_extension(business: BusinessService) -> DomainExtension:
    return DomainExtension(
        extension_id="complaint.demo",
        tool_definitions_factory=lambda store, governance: (
            build_business_tool_definitions(business, store, governance)
        ),
        constraints=(
            ComplaintOrderScopeConstraint(),
            ExactParameterConstraint(),
        ),
        result_evaluators=(ComplaintResultEvaluator(business),),
    )
