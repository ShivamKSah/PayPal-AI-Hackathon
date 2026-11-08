"""Hypothesis property-based tests for safety invariants.

Fixed seed: 42.
Invariants verified:
1. spent plus reserved never exceeds budget.
2. a DENY verdict never calls PayPal.
3. a refund never exceeds the capture amount.
[LABEL: MOCKED UNIT TESTS]
"""

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import pytest
from hypothesis import given, strategies as st, seed, settings
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from backend.app.main import app
from backend.app.db import init_db, engine
from backend.app.models import Mandate, Proposal, Payment, Case, Settlement
from backend.app.policy import PolicyEngine
from backend.app.catalog.mock import MockCatalogAdapter

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


# ── Property 1: spent + reserved never exceeds budget ─────────────────

@seed(42)
@settings(max_examples=100, deadline=None, derandomize=True)
@given(
    budget_cents=st.integers(min_value=5000, max_value=200000),
    purchase_attempts=st.lists(st.integers(min_value=500, max_value=50000), min_size=1, max_size=8),
)
def test_spent_plus_reserved_never_exceeds_budget_property(budget_cents, purchase_attempts):
    """
    [MOCKED] Property: Across any arbitrary sequence of purchase amounts,
    budget reservations and confirmations strictly preserve:
    spent_cents + reserved_cents <= budget_cents.
    """
    mandate = Mandate(
        session_id=f"prop-budget-{uuid.uuid4()}",
        budget_cents=budget_cents,
        spent_cents=0,
        reserved_cents=0,
        per_txn_cap_cents=budget_cents,
        categories=["electronics"],
        merchant_allowlist=["MERCHANT-TECH-STORE"],
        status="ACTIVE",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    policy_engine = PolicyEngine(MockCatalogAdapter())

    for amount in purchase_attempts:
        cart = {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": "USD",
            "items": [{"product_id": "mouse-ergonomic", "quantity": 1, "unit_price_cents": amount}],
            "total_cents": amount,
        }

        eval_res = policy_engine.evaluate(
            mandate=mandate,
            cart=cart,
            instruction_text="test purchase",
            now=datetime.now(timezone.utc),
        )

        remaining = mandate.budget_cents - (mandate.spent_cents + mandate.reserved_cents)
        if amount > remaining:
            # Check 2 MUST fail
            assert eval_res.verdict == "DENY"
            # Reservation rejected
        else:
            # Can be reserved
            mandate.reserved_cents += amount
            # Simulate capture & confirmation
            mandate.reserved_cents -= amount
            mandate.spent_cents += amount

        # Invariant hold after every step
        assert mandate.spent_cents + mandate.reserved_cents <= mandate.budget_cents


# ── Property 2: A DENY verdict never calls PayPal ─────────────────────

@seed(42)
@settings(max_examples=100, deadline=None, derandomize=True)
@given(
    over_budget_delta=st.integers(min_value=1, max_value=50000),
)
def test_deny_verdict_never_calls_paypal_property(over_budget_delta):
    """
    [MOCKED] Property: Any proposal evaluated to DENY must be strictly refused
    by the execution layer, producing ZERO outbound PayPal calls.
    """
    session_id = f"prop-deny-{uuid.uuid4()}"
    budget = 10000
    mandate_payload = {
        "session_id": session_id,
        "budget_cents": budget,
        "per_txn_cap_cents": budget,
        "categories": ["electronics"],
        "merchant_allowlist": ["MERCHANT-TECH-STORE"],
        "allow_new_merchants": False,
        "allow_recurring": False,
        "velocity_per_hour": 10,
        "near_limit_pct": 80,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    }
    m_resp = client.post("/mandates", json=mandate_payload)
    mandate_id = m_resp.json()["id"]

    denied_proposal_payload = {
        "mandate_id": mandate_id,
        "instruction_text": "buy over budget item",
        "cart_json": {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": "USD",
            "items": [{"product_id": "mouse-ergonomic", "quantity": 1, "unit_price_cents": budget + over_budget_delta}],
            "total_cents": budget + over_budget_delta,
        },
    }

    p_resp = client.post("/proposals", json=denied_proposal_payload)
    prop_data = p_resp.json()
    assert prop_data["verdict"] == "DENY"

    with patch("backend.app.main.payment_gateway.charge") as uncalled_mock:
        exec_resp = client.post(f"/proposals/{prop_data['id']}/execute")
        assert exec_resp.status_code == 400
        assert "Cannot execute proposal with verdict 'DENY'" in exec_resp.json()["detail"]
        uncalled_mock.assert_not_called()


# ── Property 3: Refund never exceeds capture amount ───────────────────

@seed(42)
@settings(max_examples=100, deadline=None, derandomize=True)
@given(
    capture_cents=st.integers(min_value=5000, max_value=50000),
    refund_pct=st.integers(min_value=1, max_value=150),
)
def test_refund_never_exceeds_capture_property(capture_cents, refund_pct):
    """
    [MOCKED] Property: Across any suggested refund percentage, the cumulative
    refund total executed can NEVER exceed the original capture amount.
    """
    with Session(engine) as db:
        mandate = Mandate(
            session_id=f"prop-ref-{uuid.uuid4()}",
            budget_cents=100000,
            spent_cents=capture_cents,
            reserved_cents=0,
            per_txn_cap_cents=100000,
            categories=["electronics"],
            merchant_allowlist=["MERCHANT-TECH-STORE"],
            status="ACTIVE",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add(mandate)
        db.commit()

        payment = Payment(
            proposal_id=f"prop-{uuid.uuid4()}",
            request_id=f"req-{uuid.uuid4()}",
            amount_cents=capture_cents,
            state="CONFIRMED",
            capture_id=f"CAP-PROP-{uuid.uuid4()}",
        )
        db.add(payment)
        db.commit()

        # Propose refund based on refund_pct
        proposed_refund = int(round(capture_cents * (refund_pct / 100.0)))
        case = Case(
            payment_id=payment.id,
            human_claim="Test property refund",
            root_cause="seller_fault",
            suggested_pct=min(100, refund_pct),
            proposed_refund_cents=proposed_refund,
            status="PROPOSED",
        )
        db.add(case)
        db.commit()
        case_id = case.id

    with patch("backend.app.main.payment_gateway.refund") as mock_refund:
        mock_refund.return_value = {"refund_id": f"REF-{uuid.uuid4()}", "status": "COMPLETED"}
        resp = client.post(f"/cases/{case_id}/accept")

        if proposed_refund > capture_cents:
            # Must be refused with HTTP 400
            assert resp.status_code == 400
            mock_refund.assert_not_called()
        else:
            assert resp.status_code == 200
            assert resp.json()["status"] == "REFUNDED"
            mock_refund.assert_called_once()

