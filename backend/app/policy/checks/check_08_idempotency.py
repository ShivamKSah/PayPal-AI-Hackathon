"""Check 8: Cart Idempotency."""

from typing import Dict, Any, Set


def check_8_idempotency(cart_hash: str, recent_cart_hashes: Set[str]) -> Dict[str, Any]:
    """
    Check 8: no matching cart hash in a recent time window.
    Verdict on fail: DENY.
    """
    if cart_hash in recent_cart_hashes:
        return {
            "check_id": 8,
            "name": "cart_idempotency",
            "passed": False,
            "reason": f"Duplicate cart hash '{cart_hash}' detected in recent window",
            "verdict_on_fail": "DENY",
        }

    return {
        "check_id": 8,
        "name": "cart_idempotency",
        "passed": True,
        "reason": f"Cart hash '{cart_hash}' is unique",
        "verdict_on_fail": "DENY",
    }

