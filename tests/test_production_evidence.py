from astra.production_evidence import ProductionEvidenceReport


def test_production_evidence_separates_facts_and_claim_limits():
    report = ProductionEvidenceReport(
        provider_mode="scripted_local",
        runtime_path="hermes_cli->plugin->runtime->gateway->sandbox",
        business_boundary="commerce.orders.read",
        fault_mode="none",
        authoritative_facts=({"source": "sandbox", "order_id": "order-s12-s13-001"},),
        eligible_claims=("governed_order_read_path_observed",),
        prohibited_claims=("external_provider_autonomy", "runtime_recovery"),
    )
    rendered = report.model_dump()
    assert rendered["authoritative_facts"][0]["source"] == "sandbox"
    assert "runtime_recovery" in rendered["prohibited_claims"]
