"""Unit tests for dispute recovery, root-cause diagnosis, and partial refunds.

Verifies economic refund ranges, strict code clamping, and PayPal refund execution limits.
[LABEL: MOCKED UNIT TESTS]
"""

from unittest.mock import patch, MagicMock
import json
import uuid
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from backend.app.main import app
from backend.app.db import init_db, engine
from backend.app.models import Mandate, Proposal, Payment, Case, Settlement
from backend.app.recovery.economics import ROOT_CAUSE_REFUND_RANGES, clamp_refund_percentage, get_refund_range
from backend.app.recovery.labeler import diagnose_root_cause, _diagnose_via_llm

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


# ── 1. Economic Rules Clamping ────────────────────────────────────────

def test_economics_clamping_rules_mocked():
    """
    [MOCKED] Non-negotiable safety invariant:
    The model or caller can NEVER exceed the authorized range.
    Values below minimum are clamped to min; values above maximum are clamped to max.
    """
    # seller_fault: 70 - 100
    assert clamp_refund_percentage("seller_fault", 150) == 100
    assert clamp_refund_percentage("seller_fault", 20) == 70
    assert clamp_refund_percentage("seller_fault", 85) == 85

    # ambiguous_listing: 40 - 70
    assert clamp_refund_percentage("ambiguous_listing", 999) == 70
    assert clamp_refund_percentage("ambiguous_listing", 10) == 40
    assert clamp_refund_percentage("ambiguous_listing", 55) == 55

    # agent_error: 50 - 100
    assert clamp_refund_percentage("agent_error", 200) == 100
    assert clamp_refund_percentage("agent_error", 10) == 50
    assert clamp_refund_percentage("agent_error", 75) == 75

    # vague_instruction: 10 - 40
    assert clamp_refund_percentage("vague_instruction", 100) == 40  # Crucial! Even if model says 100%
    assert clamp_refund_percentage("vague_instruction", 0) == 10
    assert clamp_refund_percentage("vague_instruction", 25) == 25


# ── 2. LLM Exaggerated Output Is Clamped in Code ──────────────────────

def test_diagnose_root_cause_clamps_exaggerated_llm_output_mocked(monkeypatch):
    """
    [MOCKED] Even if an LLM returns a 100% refund for 'vague_instruction',
    code strictly clamps it to 40%.
    """
    exaggerated_response = (
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "root_cause": "vague_instruction",
                            "suggested_pct": 100,  # Exaggerated beyond 40% cap!
                            "explanation": "Buyer was vague but should get full refund anyway.",
                        })
                    }
                }
            ]
        },
        "gemini-3.5-flash",
        1,
    )

    monkeypatch.setattr("backend.app.recovery.labeler.LLM_API_KEY", "sk-mock-key")
    monkeypatch.setattr("backend.app.recovery.labeler.call_llm_with_retry", lambda *args, **kwargs: exaggerated_response)

    diagnosis = diagnose_root_cause(
        human_claim="I changed my mind",
        instruction_text="buy something nice",
        agent_reasoning="Picked nice desk",
        catalog_snapshot={},
    )

    assert diagnosis.root_cause == "vague_instruction"
    # Proves clamping: model said 100, code forced 40!
    assert diagnosis.suggested_pct == 40
    assert diagnosis.suggested_pct <= ROOT_CAUSE_REFUND_RANGES["vague_instruction"][1]


# ── 3. Case Creation & Proposal Flow ──────────────────────────────────

def test_case_open_diagnose_and_propose_mocked():
    """
    [MOCKED] Open case against confirmed payment:
    Diagnoses root cause, stores evidence snapshot, and proposes refund inside bounds.
    """
    with Session(engine) as db:
        mandate = Mandate(
            session_id="session-case-test",
            budget_cents=50000,
            spent_cents=4500,
            reserved_cents=0,
            per_txn_cap_cents=20000,
            categories=["electronics"],
            merchant_allowlist=["MERCHANT-TECH-STORE"],
            allow_new_merchants=False,
            allow_recurring=False,
            velocity_per_hour=5,
            near_limit_pct=80,
            status="ACTIVE",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db.add(mandate)
        db.commit()
        mandate_id = mandate.id

        proposal = Proposal(
            mandate_id=mandate.id,
            instruction_text="buy an ergonomic mouse",
            agent_reasoning="Picked vertical mouse",
            cart_json={"total_cents": 4500, "merchant_id": "MERCHANT-TECH-STORE"},
            cart_hash="hash-1234",
            verdict="APPROVE",
            reviewer="policy_engine",
        )
        db.add(proposal)
        db.commit()

        payment = Payment(
            proposal_id=proposal.id,
            request_id=f"req-case-{uuid.uuid4()}",
            amount_cents=4500,
            state="CONFIRMED",
            paypal_order_id=f"ORDER-{uuid.uuid4()}",
            capture_id=f"CAP-{uuid.uuid4()}",
        )
        db.add(payment)
        db.commit()
        payment_id = payment.id

    resp = client.post("/cases", json={
        "payment_id": payment_id,
        "human_claim": "Seller shipped a broken and damaged mouse with cracked casing",
    })
    assert resp.status_code == 200
    case_data = resp.json()

    assert case_data["status"] == "PROPOSED"
    assert case_data["root_cause"] == "seller_fault"
    assert case_data["allowed_min_pct"] == 70
    assert case_data["allowed_max_pct"] == 100
    assert case_data["suggested_pct"] == 100
    assert case_data["proposed_refund_cents"] == 4500


# ── 4. Accept Refund & Invariant: Never Exceeds Capture ───────────────

def test_case_accept_executes_refund_and_blocks_excess_mocked():
    """
    [MOCKED] Non-negotiable invariant:
    Accepting settlement executes partial refund via Payments v2 with fresh request_id,
    and refund total can NEVER exceed capture minus prior refunds.
    """
    with Session(engine) as db:
        mandate = Mandate(
            session_id="session-refund-test",
            budget_cents=50000,
            spent_cents=10000,
            reserved_cents=0,
            per_txn_cap_cents=20000,
            categories=["electronics"],
            merchant_allowlist=["MERCHANT-TECH-STORE"],
            status="ACTIVE",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db.add(mandate)
        db.commit()
        mandate_id = mandate.id

        proposal = Proposal(
            mandate_id=mandate.id,
            instruction_text="buy item",
            cart_json={"total_cents": 10000},
            cart_hash="hash-123",
            verdict="APPROVE",
            reviewer="policy_engine",
        )
        db.add(proposal)
        db.commit()

        payment = Payment(
            proposal_id=proposal.id,
            request_id=f"req-refund-{uuid.uuid4()}",
            amount_cents=10000,
            state="CONFIRMED",
            capture_id=f"CAP-REFUND-{uuid.uuid4()}",
        )
        db.add(payment)
        db.commit()
        payment_id = payment.id

        # Case 1: 60% refund ($60.00 / 6000 cents)
        case1 = Case(
            payment_id=payment_id,
            human_claim="Listing ambiguous",
            root_cause="ambiguous_listing",
            suggested_pct=60,
            proposed_refund_cents=6000,
            status="PROPOSED",
        )
        db.add(case1)
        db.commit()
        case1_id = case1.id

    with patch("backend.app.main.payment_gateway.refund") as mock_refund:
        mock_refund.return_value = {"refund_id": "REFUND-PP-001", "status": "COMPLETED"}

        resp1 = client.post(f"/cases/{case1_id}/accept")
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["status"] == "REFUNDED"
        assert data1["settlement"]["paypal_refund_id"] == "REFUND-PP-001"
        assert data1["settlement"]["final_amount_cents"] == 6000
        mock_refund.assert_called_once()

    # Verify mandate spent amount reduced by refund amount
    with Session(engine) as db:
        m = db.get(Mandate, mandate_id)
        assert m.spent_cents == 4000  # 10000 - 6000

    # Invariant check: Attempting second case for 5000 cents exceeds remaining 4000 cents
    with Session(engine) as db:
        case2 = Case(
            payment_id=payment_id,
            human_claim="Additional defect",
            root_cause="seller_fault",
            suggested_pct=50,
            proposed_refund_cents=5000,  # 6000 prior + 5000 = 11000 > 10000 capture!
            status="PROPOSED",
        )
        db.add(case2)
        db.commit()
        case2_id = case2.id

    with patch("backend.app.main.payment_gateway.refund") as uncalled_mock:
        resp2 = client.post(f"/cases/{case2_id}/accept")
        assert resp2.status_code == 400
        assert "exceeds remaining capturable amount" in resp2.json()["detail"]
        uncalled_mock.assert_not_called()  # Must NEVER call PayPal when invariant fails!


# ── 5. Reject Case Flow ───────────────────────────────────────────────

def test_case_reject_mocked():
    """[MOCKED] Reject case moves status to REJECTED with zero refunds."""
    with Session(engine) as db:
        payment = Payment(
            proposal_id="prop-reject-test",
            request_id=f"req-rej-{uuid.uuid4()}",
            amount_cents=5000,
            state="CONFIRMED",
        )
        db.add(payment)
        db.commit()

        case = Case(
            payment_id=payment.id,
            human_claim="Frivolous complaint",
            root_cause="vague_instruction",
            suggested_pct=20,
            proposed_refund_cents=1000,
            status="PROPOSED",
        )
        db.add(case)
        db.commit()
        case_id = case.id

    with patch("backend.app.main.payment_gateway.refund") as uncalled_mock:
        resp = client.post(f"/cases/{case_id}/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "REJECTED"
        uncalled_mock.assert_not_called()
