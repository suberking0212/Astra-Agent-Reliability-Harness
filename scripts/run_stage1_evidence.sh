#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[1/4] Full-profile Hermes adversarial authority boundary"
pytest -q tests/test_astra_full_hermes_adversarial_e2e.py \
  tests/test_stage1_controlled_launch_boundary.py \
  tests/test_hermes_explicit_protocol.py \
  tests/test_hermes_plugin_invocation.py

echo "[2/4] approval cannot bypass exact effect binding"
pytest -q tests/test_phase3_domain_round2.py::test_approval_is_bound_to_exact_effect_identity \
  tests/test_policy_outcomes.py::test_denied_approval_is_a_terminal_policy_outcome

echo "[3/4] response-loss reconciliation does not duplicate side effects"
pytest -q tests/test_cross_domain_production.py::test_crm_adapter_reconciles_response_loss_after_runtime_reopen \
  tests/test_policy_outcomes.py::test_indeterminate_external_operation_requires_reconciliation_before_retry

echo "[4/4] completion evidence is authoritative"
pytest -q tests/test_stage1_controlled_launch_boundary.py::test_completion_contract_rejects_hermes_claim_without_authoritative_evidence

echo "FULL_PROFILE_AUTHORITY_EVIDENCE=PASS (deterministic/local evidence only)"
