# Copyright 2026 3LC Inc.
import pytest

from tlc_plugin_sdk.ai.spend import BudgetExceeded, BudgetLimitExceeded, Ledger


def test_reservation_retry_is_safe_until_settled_but_cannot_redispatch_afterward(tmp_path):
    ledger = Ledger(tmp_path)
    for _ in range(2):
        ledger.reserve("owner", "job", "request", 0.2, 1, 1)
    assert ledger.spent_today("owner") == 0.2
    for _ in range(2):
        ledger.settle("owner", "request", 0.1)
    assert ledger.spent_today("owner") == 0.1
    with pytest.raises(ValueError, match="already settled"):
        ledger.reserve("owner", "job", "request", 0.2, 1, 1)


@pytest.mark.parametrize("scope,turn_limit,day_limit", [("turn", 5, 200), ("day", 200, 5)])
def test_refusal_reports_atomic_used_requested_and_limiting_scope(tmp_path, scope, turn_limit, day_limit):
    ledger = Ledger(tmp_path)
    ledger.reserve("owner", "job", "completed", 1, turn_limit, day_limit)
    ledger.settle("owner", "completed", 0.45)
    ledger.reserve("owner", "job", "pending", 0.25, turn_limit, day_limit)
    with pytest.raises(BudgetLimitExceeded) as caught:
        ledger.reserve("owner", "job", "refused", 4.5, turn_limit, day_limit)
    error = caught.value
    assert isinstance(error, BudgetExceeded)
    assert (error.scope, error.used_usd, error.requested_usd, error.limit_usd) == (scope, 0.7, 4.5, 5)
    assert ledger.spent_today("owner") == 0.7  # refusal adds no charge or hold
