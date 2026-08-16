"""Agent-facing Astra capability composition with no Runtime imports."""

from __future__ import annotations

from .capabilities.customer_order import latest_customer_order_definition


def hermes_capabilities():
    """Definitions safe to load inside the sandboxed Hermes parent process."""
    return (latest_customer_order_definition(authority_domain="commerce.orders"),)
