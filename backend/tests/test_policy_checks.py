"""Unit tests for every individual policy check (Checks 1 to 11)."""

from datetime import datetime, timedelta, timezone
from typing import Optional, List
from pydantic import BaseModel
import pytest

from backend.app.catalog.mock import Product
from backend.app.policy.constraints import ExtractedConstraint
from backend.app.policy.checks import (
    check_1_mandate_active,
    check_2_remaining_budget,
    check_3_per_txn_cap,
    check_4_merchant_allowlist,
    check_5_category,
    check_6_reconcile,
    check_7_velocity,
    check_8_idempotency,
    check_9_ambiguity_gate,
    check_10_recurring_billing,
    check_11_near_limit,
)


class DummyMandate(BaseModel):
    status: str = "ACTIVE"
    expires_at: Optional[datetime] = None
    budget_cents: int = 40000
    reserved_cents: int = 0
    spent_cents: int = 0
    per_txn_cap_cents: int = 20000
    merchant_allowlist: List[str] = ["MERCHANT-OFFICE", "AMAZON-STORE"]
    allow_new_merchants: bool = False
    categories: List[str] = ["office_furniture", "electronics"]
    allow_recurring: bool = False
    velocity_per_hour: int = 5
    near_limit_pct: int = 80
    escalate_first_merchant: bool = False


# ── Check 1: Mandate Active ───────────────────────────────────

def test_check_1_active_unexpired():
    now = datetime.now(timezone.utc)
    mandate = DummyMandate(expires_at=now + timedelta(hours=1))
    res = check_1_mandate_active(mandate, now=now)
    assert res["passed"] is True
    assert res["check_id"] == 1


def test_check_1_expired():
    now = datetime.now(timezone.utc)
    # Expired 1 second ago
    mandate = DummyMandate(expires_at=now - timedelta(seconds=1))
    res = check_1_mandate_active(mandate, now=now)
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


def test_check_1_revoked():
    now = datetime.now(timezone.utc)
    mandate = DummyMandate(status="REVOKED", expires_at=now + timedelta(hours=1))
    res = check_1_mandate_active(mandate, now=now)
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


# ── Check 2: Remaining Budget ─────────────────────────────────

def test_check_2_within_budget():
    mandate = DummyMandate(budget_cents=50000, reserved_cents=10000, spent_cents=10000)
    # 20000 + 10000 + 10000 = 40000 <= 50000
    res = check_2_remaining_budget(mandate, total_cents=20000)
    assert res["passed"] is True


def test_check_2_exact_cap():
    # Exactly at budget
    mandate = DummyMandate(budget_cents=50000, reserved_cents=10000, spent_cents=10000)
    res = check_2_remaining_budget(mandate, total_cents=30000)
    assert res["passed"] is True


def test_check_2_one_cent_over():
    # 1 cent over budget
    mandate = DummyMandate(budget_cents=50000, reserved_cents=10000, spent_cents=10000)
    res = check_2_remaining_budget(mandate, total_cents=30001)
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


# ── Check 3: Per-Transaction Cap ──────────────────────────────

def test_check_3_under_cap():
    mandate = DummyMandate(per_txn_cap_cents=25000)
    res = check_3_per_txn_cap(mandate, total_cents=20000)
    assert res["passed"] is True


def test_check_3_exact_cap():
    mandate = DummyMandate(per_txn_cap_cents=25000)
    res = check_3_per_txn_cap(mandate, total_cents=25000)
    assert res["passed"] is True


def test_check_3_one_cent_over():
    mandate = DummyMandate(per_txn_cap_cents=25000)
    res = check_3_per_txn_cap(mandate, total_cents=25001)
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


# ── Check 4: Merchant Allowlist ───────────────────────────────

def test_check_4_exact_match():
    mandate = DummyMandate(merchant_allowlist=["AMAZON-STORE"])
    res = check_4_merchant_allowlist(mandate, "AMAZON-STORE")
    assert res["passed"] is True


def test_check_4_lookalike_one_char_off_denies():
    mandate = DummyMandate(merchant_allowlist=["AMAZON-STORE"], allow_new_merchants=False)
    # Lookalike: 'O' replaced with '0'
    res = check_4_merchant_allowlist(mandate, "AMAZ0N-STORE")
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


def test_check_4_allow_new_merchants_escalates():
    mandate = DummyMandate(merchant_allowlist=["AMAZON-STORE"], allow_new_merchants=True)
    res = check_4_merchant_allowlist(mandate, "UNKNOWN-MERCHANT")
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "ESCALATE"


# ── Check 5: Category Gate ────────────────────────────────────

def test_check_5_allowed_category():
    mandate = DummyMandate(categories=["office_furniture"])
    res = check_5_category(mandate, "office_furniture")
    assert res["passed"] is True


def test_check_5_unallowed_category_denies():
    mandate = DummyMandate(categories=["office_furniture"])
    res = check_5_category(mandate, "gift_cards")
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


# ── Check 6: Cart Reconciliation ──────────────────────────────

def test_check_6_reconcile_pass():
    product = Product(
        id="desk-1",
        title="Standing Desk",
        price_cents=20000,
        currency="USD",
        category="office_furniture",
    )
    cart = {
        "currency": "USD",
        "items": [{"product_id": "desk-1", "quantity": 1, "unit_price_cents": 20000}],
        "subtotal_cents": 20000,
        "tax_cents": 1000,
        "shipping_cents": 500,
        "fees_cents": 0,
        "total_cents": 21500,
    }
    res = check_6_reconcile(cart, {"desk-1": product})
    assert res["passed"] is True


def test_check_6_currency_swap_denies():
    cart = {
        "currency": "EUR",  # Currency swap attack
        "items": [{"product_id": "desk-1", "quantity": 1, "unit_price_cents": 20000}],
        "total_cents": 20000,
    }
    res = check_6_reconcile(cart, {})
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"
    assert "USD" in res["reason"]


def test_check_6_price_deviation_tolerance():
    product = Product(
        id="desk-1",
        title="Standing Desk",
        price_cents=10000,
        currency="USD",
        category="office_furniture",
    )
    # Price difference of 40 cents is within 50 cent tolerance -> pass
    cart_pass = {
        "currency": "USD",
        "items": [{"product_id": "desk-1", "quantity": 1, "unit_price_cents": 10040}],
        "total_cents": 10040,
    }
    assert check_6_reconcile(cart_pass, {"desk-1": product})["passed"] is True

    # Price difference of 60 cents exceeds 50 cent tolerance (1% is 100c, so tolerance is max(100, 50)=100c)
    # Let's test a $10 item where 1% is 10c and tolerance is 50c:
    cheap_prod = Product(
        id="mouse-1",
        title="Mouse",
        price_cents=1000,
        currency="USD",
        category="electronics",
    )
    # Price difference 51c on $10 item (tolerance is 50c) -> fail
    cart_fail = {
        "currency": "USD",
        "items": [{"product_id": "mouse-1", "quantity": 1, "unit_price_cents": 1051}],
        "total_cents": 1051,
    }
    res = check_6_reconcile(cart_fail, {"mouse-1": cheap_prod})
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


def test_check_6_total_mismatch_denies():
    product = Product(
        id="desk-1",
        title="Standing Desk",
        price_cents=20000,
        currency="USD",
        category="office_furniture",
    )
    # Subtotal 20000 + tax 1000 = 21000, but cart says total is 20000 (tax omitted)
    cart = {
        "currency": "USD",
        "items": [{"product_id": "desk-1", "quantity": 1, "unit_price_cents": 20000}],
        "subtotal_cents": 20000,
        "tax_cents": 1000,
        "shipping_cents": 0,
        "fees_cents": 0,
        "total_cents": 20000,
    }
    res = check_6_reconcile(cart, {"desk-1": product})
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


# ── Check 7: Velocity Limit ───────────────────────────────────

def test_check_7_under_velocity():
    mandate = DummyMandate(velocity_per_hour=5)
    assert check_7_velocity(mandate, purchases_in_last_hour=4)["passed"] is True


def test_check_7_at_velocity_cap_denies():
    mandate = DummyMandate(velocity_per_hour=5)
    res = check_7_velocity(mandate, purchases_in_last_hour=5)
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


# ── Check 8: Cart Idempotency ─────────────────────────────────

def test_check_8_unique_hash():
    assert check_8_idempotency("hash_abc", {"hash_xyz"})["passed"] is True


def test_check_8_duplicate_hash_denies():
    res = check_8_idempotency("hash_abc", {"hash_abc", "hash_xyz"})
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


# ── Check 9: Ambiguity Gate ───────────────────────────────────

def test_check_9_all_specs_present():
    constraints = [ExtractedConstraint(field="width", target_value=160)]
    catalog_specs = {"width": 160, "height": 75}
    res = check_9_ambiguity_gate(constraints, catalog_specs)
    assert res["passed"] is True


def test_check_9_missing_width_escalates():
    constraints = [ExtractedConstraint(field="width", target_value=160)]
    catalog_specs = {"height": 75, "depth": 80}  # No width!
    res = check_9_ambiguity_gate(constraints, catalog_specs)
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "ESCALATE"
    assert "width" in res["reason"]


def test_check_9_with_recorded_openrouter_llm_response(monkeypatch):
    """Verify constraint extraction parses a recorded OpenRouter response using JSON-schema structured output."""
    from unittest.mock import MagicMock
    import json
    import httpx
    from backend.app.policy import constraints as constraints_module

    recorded_openrouter_response = {
        "id": "gen-test-9999",
        "model": "google/gemini-2.5-flash",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps({
                        "constraints": [
                            {
                                "field": "width",
                                "target_value": 160,
                                "unit": "cm",
                                "raw_mention": "160 cm",
                            }
                        ]
                    }),
                }
            }
        ],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = recorded_openrouter_response

    monkeypatch.setattr(constraints_module, "LLM_API_KEY", "sk-or-v1-testkey123")
    monkeypatch.setattr(constraints_module, "LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(constraints_module, "LLM_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr(httpx.Client, "post", lambda self, *args, **kwargs: mock_resp)

    res = constraints_module.extract_constraints("buy a 160 cm wide standing desk")
    assert res.extraction_method == "llm_openrouter"
    assert res.model_reported == "google/gemini-2.5-flash"
    assert len(res.constraints) == 1
    assert res.constraints[0].field == "width"
    assert res.constraints[0].target_value == 160

    gate_result = check_9_ambiguity_gate(
        res.constraints,
        {"height": 75},
        extraction_method=res.extraction_method,
        model_reported=res.model_reported,
    )
    assert gate_result["passed"] is False
    assert gate_result["verdict_on_fail"] == "ESCALATE"
    assert gate_result["extraction_method"] == "llm_openrouter"
    assert gate_result["model_reported"] == "google/gemini-2.5-flash"


def test_check_9_fallback_to_regex_on_error(monkeypatch):
    """Verify check 9 falls back to regex parser when OpenRouter call fails."""
    import httpx
    from backend.app.policy import constraints as constraints_module
    from unittest.mock import MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal error"

    monkeypatch.setattr(constraints_module, "LLM_API_KEY", "sk-or-v1-testkey123")
    monkeypatch.setattr(httpx.Client, "post", lambda self, *args, **kwargs: mock_resp)

    res = constraints_module.extract_constraints("buy a 160 cm wide desk")
    assert res.extraction_method == "regex_fallback"
    assert any(c.field == "width" for c in res.constraints)

    gate_result = check_9_ambiguity_gate(res.constraints, {"width": 160}, extraction_method=res.extraction_method)
    assert gate_result["passed"] is True
    assert gate_result["extraction_method"] == "regex_fallback"


# ── Check 10: Recurring Billing ───────────────────────────────

def test_check_10_non_recurring():
    mandate = DummyMandate(allow_recurring=False)
    assert check_10_recurring_billing(mandate, is_recurring=False)["passed"] is True


def test_check_10_recurring_unallowed_denies():
    mandate = DummyMandate(allow_recurring=False)
    res = check_10_recurring_billing(mandate, is_recurring=True)
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "DENY"


def test_check_10_recurring_allowed():
    mandate = DummyMandate(allow_recurring=True)
    assert check_10_recurring_billing(mandate, is_recurring=True)["passed"] is True


# ── Check 11: Near Limit ──────────────────────────────────────

def test_check_11_under_threshold():
    # 20000 cap, 80% threshold = 16000. 15000 is under.
    mandate = DummyMandate(per_txn_cap_cents=20000, near_limit_pct=80)
    res = check_11_near_limit(mandate, total_cents=15000, is_first_purchase_from_merchant=False)
    assert res["passed"] is True


def test_check_11_at_or_above_80_pct_escalates():
    mandate = DummyMandate(per_txn_cap_cents=20000, near_limit_pct=80)
    # 16000 is exactly 80%
    res = check_11_near_limit(mandate, total_cents=16000, is_first_purchase_from_merchant=False)
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "ESCALATE"


def test_check_11_first_merchant_purchase_default_passes():
    # When escalate_first_merchant is False (default), first purchase from allowlisted merchant passes
    mandate = DummyMandate(per_txn_cap_cents=20000, near_limit_pct=80, escalate_first_merchant=False)
    res = check_11_near_limit(mandate, total_cents=5000, is_first_purchase_from_merchant=True)
    assert res["passed"] is True


def test_check_11_first_merchant_purchase_escalates_when_configured():
    # When escalate_first_merchant is True, first purchase escalates
    mandate = DummyMandate(per_txn_cap_cents=20000, near_limit_pct=80, escalate_first_merchant=True)
    res = check_11_near_limit(mandate, total_cents=5000, is_first_purchase_from_merchant=True)
    assert res["passed"] is False
    assert res["verdict_on_fail"] == "ESCALATE"
