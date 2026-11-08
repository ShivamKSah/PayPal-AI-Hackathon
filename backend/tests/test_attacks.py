"""Unit tests for Break-the-Mandate attack simulation and challenge tracking.

All tests in this module verify that every adversarial attack scenario is deterministically
blocked by policy engine checks or cart hash binding, and that NO outbound PayPal charge calls
are EVER executed.
[LABEL: MOCKED UNIT TESTS]
"""

import uuid
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from backend.app.main import app
from backend.app.db import init_db, engine
from backend.app.models import ChallengeAttempt, Mandate, Payment

client = TestClient(app)

ALL_ATTACK_IDS = [
    "over_budget",
    "over_cap",
    "off_list_merchant",
    "lookalike_merchant",
    "mislabeled_category",
    "inflated_price",
    "hidden_line_item",
    "quantity_inflation",
    "currency_swap",
    "expired_mandate",
    "revoked_mandate",
    "velocity_burst",
    "duplicate_replay",
    "subscription_upsell",
    "cart_swap_after_approval",
]


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


# ── Individual Unit Tests for All 15 Attacks ──────────────────────────

@pytest.mark.parametrize("attack_id", ALL_ATTACK_IDS)
def test_each_attack_is_blocked_and_not_executed_mocked(attack_id: str):
    """
    [MOCKED] Non-negotiable security invariant:
    Every simulated attack must be blocked (breached=False) and produce ZERO outbound PayPal charges.
    """
    session_id = f"test-attack-{attack_id}"

    with patch("backend.app.main.payment_gateway.charge") as mock_charge:
        resp = client.post("/attacks/run", json={"attack_id": attack_id, "session_id": session_id})
        assert resp.status_code == 200
        data = resp.json()

        # Invariant 1: Breached must be False
        assert data["breached"] is False
        assert data["attack_id"] == attack_id
        assert data["rule_held"] != "none"

        # Invariant 2: Outbound PayPal charge must NEVER be called
        mock_charge.assert_not_called()

        # Invariant 3: Challenge attempt recorded with breached=False
        with Session(engine) as db:
            attempt = db.exec(
                select(ChallengeAttempt)
                .where(ChallengeAttempt.session_id == session_id, ChallengeAttempt.attack_type == attack_id)
            ).first()
            assert attempt is not None
            assert attempt.breached is False


# ── Challenge Stats Verification ──────────────────────────────────────

def test_challenge_stats_aggregation_mocked():
    """
    [MOCKED] Verify GET /challenge/stats correctly returns total attempts,
    0 breaches, and per-rule breakdown counts.
    """
    session_id = f"test-session-stats-{uuid.uuid4().hex[:8]}"

    # Run 3 attacks
    client.post("/attacks/run", json={"attack_id": "over_budget", "session_id": session_id})
    client.post("/attacks/run", json={"attack_id": "over_cap", "session_id": session_id})
    client.post("/attacks/run", json={"attack_id": "off_list_merchant", "session_id": session_id})

    resp = client.get(f"/challenge/stats?session_id={session_id}")
    assert resp.status_code == 200
    stats = resp.json()

    assert stats["attempts"] == 3
    assert stats["breaches"] == 0
    assert "remaining_budget" in stats["per_rule_counts"]
    assert "per_txn_cap" in stats["per_rule_counts"]
    assert "merchant_allowlist" in stats["per_rule_counts"]


# ── System Reset Endpoint ─────────────────────────────────────────────

def test_reset_endpoint_mocked():
    """
    [MOCKED] Verify POST /reset cleans session data and re-seeds clean demo mandate.
    """
    session_id = f"test-session-reset-{uuid.uuid4().hex[:8]}"

    # Seed an attack
    client.post("/attacks/run", json={"attack_id": "over_budget", "session_id": session_id})

    # Reset
    resp = client.post("/reset", json={"session_id": session_id})
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["status"] == "RESET_SUCCESSFUL"
    assert res_data["session_id"] == session_id
    assert res_data["budget_cents"] == 50000

    # Verify challenge attempts wiped for this session
    stats_resp = client.get(f"/challenge/stats?session_id={session_id}")
    assert stats_resp.json()["attempts"] == 0


# ── Ledger Endpoint ───────────────────────────────────────────────────

def test_ledger_endpoint_mocked():
    """
    [MOCKED] Verify GET /ledger returns chronological proposals with details.
    """
    session_id = f"test-session-ledger-{uuid.uuid4().hex[:8]}"
    client.post("/attacks/run", json={"attack_id": "over_budget", "session_id": session_id})

    resp = client.get(f"/ledger?session_id={session_id}")
    assert resp.status_code == 200
    ledger = resp.json()
    assert len(ledger) >= 1
    assert ledger[0]["verdict"] == "DENY"
    assert "amount_cents" in ledger[0]
    assert "rule_hits" in ledger[0]

