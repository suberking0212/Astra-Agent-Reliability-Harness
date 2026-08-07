from astra.phase3.completion import CompletionStatus
from astra.phase3.effects import ExternalOperationStatus
from astra.phase3.policy import PolicyAction, choose_policy_action


def test_denied_approval_is_a_terminal_policy_outcome():
    action, _ = choose_policy_action(
        input_complete=False,
        approval_required=True,
        approval_matched=False,
        approval_denied=True,
        external_operation_status=None,
        completion_status=CompletionStatus.UNSATISFIED,
    )
    assert action == PolicyAction.FAIL


def test_failed_governed_effect_uses_retry_policy_not_request_input():
    action, _ = choose_policy_action(
        input_complete=False,
        approval_required=False,
        approval_matched=True,
        governed_effect_failed=True,
        external_operation_status=None,
        completion_status=CompletionStatus.UNSATISFIED,
    )
    assert action == PolicyAction.CONTINUE_WITH_FEEDBACK


def test_indeterminate_external_operation_requires_reconciliation_before_retry():
    action, reason = choose_policy_action(
        input_complete=False,
        approval_required=False,
        approval_matched=True,
        governed_effect_failed=True,
        external_operation_status=ExternalOperationStatus.INDETERMINATE,
        completion_status=CompletionStatus.UNSATISFIED,
    )
    assert action == PolicyAction.RECONCILE
    assert reason == "external_operation_indeterminate"


def test_request_input_is_reserved_for_actual_missing_input():
    missing_action, _ = choose_policy_action(
        input_complete=False,
        approval_required=False,
        approval_matched=True,
        external_operation_status=None,
        completion_status=CompletionStatus.UNSATISFIED,
    )
    evaluator_action, _ = choose_policy_action(
        input_complete=True,
        approval_required=False,
        approval_matched=True,
        external_operation_status=None,
        completion_status=CompletionStatus.EVALUATOR_ERROR,
    )
    assert missing_action == PolicyAction.REQUEST_INPUT
    assert evaluator_action == PolicyAction.ESCALATE
