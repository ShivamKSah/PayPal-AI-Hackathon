"""Check 3: Per-Transaction Cap."""

from typing import Dict, Any


def check_3_per_txn_cap(mandate: Any, total_cents: int) -> Dict[str, Any]:
    """
    Check 3: total <= per_txn_cap.
    All values in integer cents.
    Verdict on fail: DENY.
    """
    if total_cents > mandate.per_txn_cap_cents:
        return {
            "check_id": 3,
            "name": "per_txn_cap",
            "passed": False,
            "reason": f"Total {total_cents}¢ exceeds per-transaction cap {mandate.per_txn_cap_cents}¢",
            "verdict_on_fail": "DENY",
        }

    return {
        "check_id": 3,
        "name": "per_txn_cap",
        "passed": True,
        "reason": f"Total {total_cents}¢ is within per-transaction cap {mandate.per_txn_cap_cents}¢",
        "verdict_on_fail": "DENY",
    }

