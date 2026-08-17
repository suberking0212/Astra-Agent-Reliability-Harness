"""Business-sandbox composition root for the Hermes deployment."""

from __future__ import annotations

import os

from .business import BusinessSandboxConfig, HttpBusinessSandboxAdapter
from .capabilities.customer_order import latest_customer_order_definition
from .complaint_extension import build_complaint_extension
from .hermes_capabilities import hermes_capabilities
from .runtime_service import RuntimeService


def build_runtime_service_from_environment() -> RuntimeService:
    endpoint = os.environ["ASTRA_BUSINESS_SANDBOX_ENDPOINT"]
    token = os.environ["ASTRA_BUSINESS_SANDBOX_TOKEN"]
    commerce = os.environ["ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN"]
    support = os.environ["ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN"]
    config = BusinessSandboxConfig(
        endpoint=endpoint, token=token,
        commerce_authority_domain=commerce,
        support_authority_domain=support,
    )
    business = HttpBusinessSandboxAdapter(config)
    return RuntimeService(
        business=business,
        domain_extensions=(build_complaint_extension(business),),
        capability_definitions=(latest_customer_order_definition(authority_domain=commerce),),
        authority_domains={"commerce": commerce, "support": support},
    )
