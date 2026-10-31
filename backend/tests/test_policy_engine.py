"""Unit tests for the policy engine orchestrator."""

from datetime import datetime, timedelta, timezone
from backend.app.catalog.mock import MockCatalogAdapter, SEEDED_PRODUCTS
from backend.app.policy.engine import PolicyEngine
from backend.tests.test_policy_checks import DummyMandate


def test_engine_all_checks_pass_approves():
    engine = PolicyEngine(catalog_adapter=MockCatalogAdapter())
    now = datetime.now(timezone.utc)
    mandate = DummyMandate(
        expires_at=now + timedelta(hours=2),
        budget_cents=50000,
        per_txn_cap_cents=10000,
        merchant_allowlist=["MERCHANT-TECH-STORE"],
        categories=["electronics"],
        near_limit_pct=80,
    )

    # Normal $45 ergonomic mouse under $100 cap (< 80%)
    cart = {
        "merchant_id": "MERCHANT-TECH-STORE",
        "currency": "USD",
        "items": [{"product_id": "mouse-ergonomic", "quantity": 1, "unit_price_cents": 4500}],
        "subtotal_cents": 4500,
        "tax_cents": 0,
        "shipping_cents": 0,
        "fees_cents": 0,
        "total_cents": 4500,
    }

    result = engine.evaluate(
        mandate=mandate,
        cart=cart,
        instruction_text="buy an ergonomic mouse",
        now=now,
    )

    assert result.verdict == "APPROVE"
    assert len(result.rule_hits) == 11
    assert all(hit["passed"] for hit in result.rule_hits)


def test_engine_no_early_exit_on_deny():
    """Engine must evaluate all 11 checks even if Check 1 fails."""
    engine = PolicyEngine(catalog_adapter=MockCatalogAdapter())
    now = datetime.now(timezone.utc)
    # Expired mandate (fails Check 1)
    mandate = DummyMandate(
        status="REVOKED",
        expires_at=now - timedelta(hours=1),
        budget_cents=50000,
        merchant_allowlist=["UNKNOWN-MERCHANT"],
        categories=["wrong_category"],
    )

    cart = {
        "merchant_id": "WRONG-MERCHANT",
        "currency": "EUR",
        "items": [{"product_id": "gift-card-50", "quantity": 1, "unit_price_cents": 5000}],
        "total_cents": 5000,
    }

    result = engine.evaluate(mandate=mandate, cart=cart, now=now)
    assert result.verdict == "DENY"
    # Crucial: NO early exit, all 11 checks must be reported in Section 7 order
    assert len(result.rule_hits) == 11
    check_ids = [hit["check_id"] for hit in result.rule_hits]
    assert check_ids == [1, 2, 3, 4, 5, 6, 9, 10, 7, 8, 11]


def test_engine_deny_wins_over_escalate():
    """Any DENY must win over an ESCALATE."""
    engine = PolicyEngine(catalog_adapter=MockCatalogAdapter())
    now = datetime.now(timezone.utc)
    mandate = DummyMandate(
        expires_at=now + timedelta(hours=1),
        budget_cents=50000,
        per_txn_cap_cents=20000,
        merchant_allowlist=["MERCHANT-OFFICE-SUPPLY"],
        categories=["office_furniture"],
        near_limit_pct=80,
    )

    # Desk with no width triggers Check 9 (ESCALATE)
    # But currency EUR triggers Check 6 (DENY)
    cart = {
        "merchant_id": "MERCHANT-OFFICE-SUPPLY",
        "currency": "EUR",  # DENY!
        "items": [{"product_id": "desk-no-width", "quantity": 1, "unit_price_cents": 18000}],
        "total_cents": 18000,
    }

    result = engine.evaluate(
        mandate=mandate,
        cart=cart,
        instruction_text="buy a 160 cm standing desk",
        now=now,
    )
    assert result.verdict == "DENY"


def test_engine_escalate_when_check_9_ambiguity_triggers():
    """Ambiguity gate alone triggers ESCALATE."""
    engine = PolicyEngine(catalog_adapter=MockCatalogAdapter())
    now = datetime.now(timezone.utc)
    mandate = DummyMandate(
        expires_at=now + timedelta(hours=1),
        budget_cents=50000,
        per_txn_cap_cents=30000,
        merchant_allowlist=["MERCHANT-OFFICE-SUPPLY"],
        categories=["office_furniture"],
        near_limit_pct=80,
    )

    # Requesting a 160cm desk, but desk-no-width has no width spec -> ESCALATE
    cart = {
        "merchant_id": "MERCHANT-OFFICE-SUPPLY",
        "currency": "USD",
        "items": [{"product_id": "desk-no-width", "quantity": 1, "unit_price_cents": 18000}],
        "subtotal_cents": 18000,
        "tax_cents": 0,
        "shipping_cents": 0,
        "fees_cents": 0,
        "total_cents": 18000,
    }

    result = engine.evaluate(
        mandate=mandate,
        cart=cart,
        instruction_text="buy a 160 cm standing desk",
        now=now,
    )
    assert result.verdict == "ESCALATE"
    check_9_hit = next(h for h in result.rule_hits if h["check_id"] == 9)
    assert check_9_hit["passed"] is False
    assert check_9_hit["verdict_on_fail"] == "ESCALATE"
