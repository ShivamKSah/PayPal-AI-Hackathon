"""Check 11: Near-Limit & First Merchant Purchase."""

from typing import Dict, Any


def check_11_near_limit(
    mandate: Any,
    total_cents: int,
    is_first_purchase_from_merchant: bool,
) -> Dict[str, Any]:
    """
    Check 11:
    - Total above near_limit_pct (e.g. 80%) of per_txn_cap_cents
    - OR first purchase from a merchant.
    Verdict on fail: ESCALATE.
    """
    near_limit_pct = getattr(mandate, "near_limit_pct", 80)
    per_txn_cap = getattr(mandate, "per_txn_cap_cents", 0)
    threshold = (per_txn_cap * near_limit_pct) // 100

    if total_cents >= threshold:
        return {
            "check_id": 11,
            "name": "near_limit_review",
            "passed": False,
            "reason": (
                f"Total {total_cents}¢ is at or above {near_limit_pct}% threshold "
                f"({threshold}¢) of transaction cap {per_txn_cap}¢. Escalating for human review."
            ),
            "verdict_on_fail": "ESCALATE",
        }

    if is_first_purchase_from_merchant:
        return {
            "check_id": 11,
            "name": "near_limit_review",
            "passed": False,
            "reason": "First purchase from this merchant under this mandate. Escalating for human confirmation.",
            "verdict_on_fail": "ESCALATE",
        }

    return {
        "check_id": 11,
        "name": "near_limit_review",
        "passed": True,
        "reason": f"Total {total_cents}¢ is safely under {near_limit_pct}% threshold ({threshold}¢)",
        "verdict_on_fail": "ESCALATE",
    }

