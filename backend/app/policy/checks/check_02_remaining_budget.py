"""Check 2: Remaining Budget."""

from typing import Dict, Any


def check_2_remaining_budget(mandate: Any, total_cents: int) -> Dict[str, Any]:
    """
    Check 2: total + reserved + spent <= budget.
    All values in integer cents.
    Verdict on fail: DENY.
    """
    projected = total_cents + mandate.reserved_cents + mandate.spent_cents
    if projected > mandate.budget_cents:
        return {
            "check_id": 2,
            "name": "remaining_budget",
            "passed": False,
            "reason": (
                f"Proposed total {total_cents}¢ + reserved {mandate.reserved_cents}¢ + "
                f"spent {mandate.spent_cents}¢ = {projected}¢ exceeds budget {mandate.budget_cents}¢"
            ),
            "verdict_on_fail": "DENY",
        }

    return {
        "check_id": 2,
        "name": "remaining_budget",
        "passed": True,
        "reason": f"Projected spend {projected}¢ is within budget {mandate.budget_cents}¢",
        "verdict_on_fail": "DENY",
    }

