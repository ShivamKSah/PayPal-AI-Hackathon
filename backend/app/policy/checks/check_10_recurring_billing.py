"""Check 10: Recurring Billing Gate."""

from typing import Dict, Any


def check_10_recurring_billing(mandate: Any, is_recurring: bool) -> Dict[str, Any]:
    """
    Check 10:
    If item/plan is subscription or auto-renew and mandate.allow_recurring is False, DENY.
    Verdict on fail: DENY.
    """
    allow_recurring = getattr(mandate, "allow_recurring", False)

    if is_recurring and not allow_recurring:
        return {
            "check_id": 10,
            "name": "recurring_billing",
            "passed": False,
            "reason": "Item is recurring/subscription but mandate does not permit recurring billing",
            "verdict_on_fail": "DENY",
        }

    return {
        "check_id": 10,
        "name": "recurring_billing",
        "passed": True,
        "reason": "Billing type permitted by mandate policy",
        "verdict_on_fail": "DENY",
    }

