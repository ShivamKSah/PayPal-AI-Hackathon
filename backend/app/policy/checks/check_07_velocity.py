"""Check 7: Velocity Rate Limit."""

from typing import Dict, Any


def check_7_velocity(mandate: Any, purchases_in_last_hour: int) -> Dict[str, Any]:
    """
    Check 7: purchases in last hour must be strictly below velocity_per_hour limit.
    Verdict on fail: DENY.
    """
    limit = getattr(mandate, "velocity_per_hour", 5)

    if purchases_in_last_hour >= limit:
        return {
            "check_id": 7,
            "name": "velocity_limit",
            "passed": False,
            "reason": f"Purchases in last hour ({purchases_in_last_hour}) reached velocity cap ({limit}/hour)",
            "verdict_on_fail": "DENY",
        }

    return {
        "check_id": 7,
        "name": "velocity_limit",
        "passed": True,
        "reason": f"Purchases in last hour ({purchases_in_last_hour}) is below limit ({limit}/hour)",
        "verdict_on_fail": "DENY",
    }

