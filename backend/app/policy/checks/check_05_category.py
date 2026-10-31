"""Check 5: Category Gate."""

from typing import Dict, Any


def check_5_category(mandate: Any, catalog_category: str) -> Dict[str, Any]:
    """
    Check 5: catalog category must be in mandate.categories.
    The agent's claimed label is strictly ignored.
    Verdict on fail: DENY.
    """
    allowed_categories = set(c.lower() for c in (mandate.categories or []))
    cat_lower = (catalog_category or "").lower()

    if cat_lower in allowed_categories:
        return {
            "check_id": 5,
            "name": "category_allowlist",
            "passed": True,
            "reason": f"Catalog category '{catalog_category}' is in allowed categories",
            "verdict_on_fail": "DENY",
        }

    return {
        "check_id": 5,
        "name": "category_allowlist",
        "passed": False,
        "reason": f"Catalog category '{catalog_category}' is not in allowed categories ({list(allowed_categories)})",
        "verdict_on_fail": "DENY",
    }

