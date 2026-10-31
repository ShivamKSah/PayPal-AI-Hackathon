"""Check 4: Merchant Allowlist."""

from typing import Dict, Any


def check_4_merchant_allowlist(mandate: Any, merchant_id: str) -> Dict[str, Any]:
    """
    Check 4: merchant_id exact match in merchant_allowlist.
    No fuzzy match allowed.
    Verdict on fail: DENY, or ESCALATE if mandate.allow_new_merchants is True.
    """
    allowlist = set(mandate.merchant_allowlist or [])

    if merchant_id in allowlist:
        return {
            "check_id": 4,
            "name": "merchant_allowlist",
            "passed": True,
            "reason": f"Merchant '{merchant_id}' is in allowlist",
            "verdict_on_fail": "DENY",
        }

    # Merchant is not in allowlist
    if getattr(mandate, "allow_new_merchants", False):
        return {
            "check_id": 4,
            "name": "merchant_allowlist",
            "passed": False,
            "reason": f"Merchant '{merchant_id}' is not in allowlist, but mandate permits new merchants with approval",
            "verdict_on_fail": "ESCALATE",
        }

    return {
        "check_id": 4,
        "name": "merchant_allowlist",
        "passed": False,
        "reason": f"Merchant '{merchant_id}' is not in allowlist and new merchants are not permitted",
        "verdict_on_fail": "DENY",
    }

