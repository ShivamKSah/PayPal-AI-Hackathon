"""Check 1: Mandate Active."""

from datetime import datetime, timezone
from typing import Dict, Any


def check_1_mandate_active(mandate: Any, now: datetime = None) -> Dict[str, Any]:
    """
    Check 1: Mandate must have status ACTIVE and current time must be before expires_at.
    Verdict on fail: DENY.
    """
    current_time = now or datetime.now(timezone.utc)
    # Ensure tz-aware comparison
    expires_at = mandate.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    if mandate.status != "ACTIVE":
        return {
            "check_id": 1,
            "name": "mandate_active",
            "passed": False,
            "reason": f"Mandate status is {mandate.status}, expected ACTIVE",
            "verdict_on_fail": "DENY",
        }

    if current_time >= expires_at:
        return {
            "check_id": 1,
            "name": "mandate_active",
            "passed": False,
            "reason": f"Mandate expired at {expires_at.isoformat()} (current time: {current_time.isoformat()})",
            "verdict_on_fail": "DENY",
        }

    return {
        "check_id": 1,
        "name": "mandate_active",
        "passed": True,
        "reason": "Mandate is ACTIVE and unexpired",
        "verdict_on_fail": "DENY",
    }

