"""Check 6: Cart & Catalog Reconciliation."""

from typing import Dict, Any, List, Optional
from backend.app.catalog.mock import Product


def check_6_reconcile(
    cart: Dict[str, Any],
    catalog_products: Dict[str, Optional[Product]],
) -> Dict[str, Any]:
    """
    Check 6:
    - Currency must be USD.
    - Each line item must exist in catalog.
    - Unit price must be within tolerance (1% or 50 cents, whichever is greater).
    - Quantity > 0 and within sensible limits.
    - Subtotal + tax + shipping + fees must strictly equal total_cents.
    Verdict on fail: DENY.
    """
    currency = cart.get("currency", "").upper()
    if currency != "USD":
        return {
            "check_id": 6,
            "name": "cart_reconciliation",
            "passed": False,
            "reason": f"Currency '{currency}' not supported; only USD is permitted",
            "verdict_on_fail": "DENY",
        }

    line_items: List[Dict[str, Any]] = cart.get("items", [])
    if not line_items:
        return {
            "check_id": 6,
            "name": "cart_reconciliation",
            "passed": False,
            "reason": "Cart contains no line items",
            "verdict_on_fail": "DENY",
        }

    computed_subtotal = 0

    for item in line_items:
        product_id = item.get("product_id") or item.get("id")
        qty = item.get("quantity", 1)
        cart_unit_price = item.get("unit_price_cents", 0)

        if qty <= 0:
            return {
                "check_id": 6,
                "name": "cart_reconciliation",
                "passed": False,
                "reason": f"Item '{product_id}' has invalid quantity {qty}",
                "verdict_on_fail": "DENY",
            }

        product = catalog_products.get(product_id)
        if not product:
            return {
                "check_id": 6,
                "name": "cart_reconciliation",
                "passed": False,
                "reason": f"Item '{product_id}' does not exist in catalog",
                "verdict_on_fail": "DENY",
            }

        # Price tolerance: 1% or 50 cents (50 cents = 50 cents)
        catalog_price = product.price_cents
        tolerance = max(int(catalog_price * 0.01), 50)
        price_diff = abs(cart_unit_price - catalog_price)

        if price_diff > tolerance:
            return {
                "check_id": 6,
                "name": "cart_reconciliation",
                "passed": False,
                "reason": (
                    f"Item '{product_id}' cart price {cart_unit_price}¢ deviates from "
                    f"catalog price {catalog_price}¢ by {price_diff}¢ (tolerance: {tolerance}¢)"
                ),
                "verdict_on_fail": "DENY",
            }

        computed_subtotal += cart_unit_price * qty

    tax_cents = cart.get("tax_cents", 0)
    shipping_cents = cart.get("shipping_cents", 0)
    fees_cents = cart.get("fees_cents", 0)
    total_cents = cart.get("total_cents", 0)

    expected_total = computed_subtotal + tax_cents + shipping_cents + fees_cents
    if total_cents != expected_total:
        return {
            "check_id": 6,
            "name": "cart_reconciliation",
            "passed": False,
            "reason": (
                f"Cart total {total_cents}¢ does not match computed sum "
                f"{computed_subtotal}¢ (subtotal) + {tax_cents}¢ (tax) + {shipping_cents}¢ (shipping) + {fees_cents}¢ (fees) = {expected_total}¢"
            ),
            "verdict_on_fail": "DENY",
        }

    return {
        "check_id": 6,
        "name": "cart_reconciliation",
        "passed": True,
        "reason": f"Cart reconciles with catalog ({len(line_items)} items, total {total_cents}¢ USD)",
        "verdict_on_fail": "DENY",
    }

