"""Attack simulator for Mandate Break-the-Mandate testing.

Generates scripted adversarial attacks against mandates without calling PayPal or LLMs.
Verifies all 15 threat scenarios are deterministically blocked by policy checks or hash binding.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Tuple, Optional
import uuid

from backend.app.policy.engine import compute_cart_hash, PolicyEngine
from backend.app.catalog import get_catalog_adapter


ATTACK_DESCRIPTIONS = {
    "over_budget": "Cart total exceeds the mandate's remaining budget (Check 2)",
    "over_cap": "Cart total exceeds the per-transaction cap (Check 3)",
    "off_list_merchant": "Cart merchant is not present on buyer's allowlist (Check 4)",
    "lookalike_merchant": "Cart merchant is a typosquatted lookalike of an approved vendor (Check 4)",
    "mislabeled_category": "Product belongs to an unapproved category such as gift cards (Check 5)",
    "inflated_price": "Cart unit price exceeds verified catalog listing price (Check 6)",
    "hidden_line_item": "Cart contains an uncataloged phantom fee or hidden product (Check 6)",
    "quantity_inflation": "Cart total is mathematically inconsistent with unit price and quantity (Check 6)",
    "currency_swap": "Cart currency differs from approved mandate currency (Check 6)",
    "expired_mandate": "Transaction attempted against an expired mandate window (Check 1)",
    "revoked_mandate": "Transaction attempted against a revoked mandate (Check 1)",
    "velocity_burst": "Transaction rate exceeds the permitted velocity per hour (Check 7)",
    "duplicate_replay": "Attacker replays identical cart submission within idempotency window (Check 8)",
    "subscription_upsell": "Recurring subscription injected when mandate forbids recurring billing (Check 10)",
    "cart_swap_after_approval": "Cart contents tampered after policy approval before payment execution",
}


def build_attack_proposal(
    attack_id: str,
    mandate: Any,
) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    """
    Build cart, instruction text, and evaluation context for a specified attack.
    Returns (cart_json, instruction_text, eval_kwargs).
    """
    catalog = get_catalog_adapter()
    eval_kwargs: Dict[str, Any] = {}
    base_currency = getattr(mandate, "currency", "USD") or "USD"

    if attack_id == "over_budget":
        # Total exceeds remaining budget
        overflow_cents = (mandate.budget_cents - mandate.spent_cents) + 10000
        cart = {
            "merchant_id": mandate.merchant_allowlist[0] if mandate.merchant_allowlist else "MERCHANT-TECH-STORE",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Bulk Mouse Order",
                    "quantity": overflow_cents // 4500,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": overflow_cents,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": overflow_cents,
        }
        return cart, "buy excessive bulk mice exceeding budget", eval_kwargs

    elif attack_id == "over_cap":
        # Total exceeds per-transaction cap
        overflow_cap = mandate.per_txn_cap_cents + 5000
        cart = {
            "merchant_id": mandate.merchant_allowlist[0] if mandate.merchant_allowlist else "MERCHANT-TECH-STORE",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Expensive Custom Mouse",
                    "quantity": 1,
                    "unit_price_cents": overflow_cap,
                }
            ],
            "subtotal_cents": overflow_cap,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": overflow_cap,
        }
        return cart, "buy item exceeding transaction cap", eval_kwargs

    elif attack_id == "off_list_merchant":
        cart = {
            "merchant_id": "ROGUE-MERCHANT-999",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 4500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 4500,
        }
        return cart, "buy from unapproved rogue vendor", eval_kwargs

    elif attack_id == "lookalike_merchant":
        # Lookalike/typosquatted merchant
        approved = mandate.merchant_allowlist[0] if mandate.merchant_allowlist else "MERCHANT-TECH-STORE"
        lookalike = approved + "-OFFICIAL"
        cart = {
            "merchant_id": lookalike,
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 4500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 4500,
        }
        return cart, "buy from lookalike spoofed merchant", eval_kwargs

    elif attack_id == "mislabeled_category":
        # Unapproved category: gift cards
        cart = {
            "merchant_id": "MERCHANT-GIFT-CARDS",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "gift-card-50",
                    "title": "Store Gift Card $50",
                    "quantity": 1,
                    "unit_price_cents": 5000,
                }
            ],
            "subtotal_cents": 5000,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 5000,
        }
        return cart, "buy unauthorized digital gift card", eval_kwargs

    elif attack_id == "inflated_price":
        # Catalog has mouse at 4500 cents; cart claims 8500 cents
        cart = {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Vertical Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 8500,  # Inflated!
                }
            ],
            "subtotal_cents": 8500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 8500,
        }
        return cart, "buy mouse with markup price injection", eval_kwargs

    elif attack_id == "hidden_line_item":
        # Phantom item not in catalog
        cart = {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Vertical Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                },
                {
                    "product_id": "phantom-vip-insurance-fee",
                    "title": "Hidden VIP Service Fee",
                    "quantity": 1,
                    "unit_price_cents": 2500,
                },
            ],
            "subtotal_cents": 7000,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 7000,
        }
        return cart, "buy mouse with hidden rogue fee line item", eval_kwargs

    elif attack_id == "quantity_inflation":
        # Subtotal does not match 1 * 4500
        cart = {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Vertical Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 9000,  # Math mismatch
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 9000,
        }
        return cart, "buy mouse with inflated line-item math", eval_kwargs

    elif attack_id == "currency_swap":
        cart = {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": "EUR",  # Mandate expects USD
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Vertical Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 4500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 4500,
        }
        return cart, "buy mouse with currency swap from USD to EUR", eval_kwargs

    elif attack_id == "expired_mandate":
        cart = {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Vertical Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 4500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 4500,
        }
        # Simulate time in future beyond mandate expiry
        future_now = (mandate.expires_at or datetime.now(timezone.utc)) + timedelta(hours=2)
        eval_kwargs["now"] = future_now
        return cart, "buy item after mandate window has expired", eval_kwargs

    elif attack_id == "revoked_mandate":
        cart = {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Vertical Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 4500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 4500,
        }
        return cart, "buy item against revoked mandate", eval_kwargs

    elif attack_id == "velocity_burst":
        cart = {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Vertical Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 4500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 4500,
        }
        eval_kwargs["purchases_in_last_hour"] = mandate.velocity_per_hour + 2
        return cart, "burst spam purchases exceeding hourly velocity limit", eval_kwargs

    elif attack_id == "duplicate_replay":
        cart = {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Vertical Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 4500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 4500,
        }
        cart_hash = compute_cart_hash(cart)
        eval_kwargs["recent_cart_hashes"] = {cart_hash}
        return cart, "replay identical transaction cart hash", eval_kwargs

    elif attack_id == "subscription_upsell":
        # Injects subscription item when allow_recurring is False
        cart = {
            "merchant_id": "MERCHANT-CLOUD-INC",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "sub-cloud-storage",
                    "title": "Cloud Storage Pro Monthly",
                    "quantity": 1,
                    "unit_price_cents": 2000,
                }
            ],
            "subtotal_cents": 2000,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 2000,
        }
        return cart, "inject unauthorized recurring monthly subscription", eval_kwargs

    elif attack_id == "cart_swap_after_approval":
        # Normal clean cart that APPROVES, but tampered before execution
        cart = {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": base_currency,
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Vertical Ergonomic Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 4500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 4500,
        }
        return cart, "tamper cart items after policy engine approval", eval_kwargs

    raise ValueError(f"Unknown attack_id '{attack_id}'")

