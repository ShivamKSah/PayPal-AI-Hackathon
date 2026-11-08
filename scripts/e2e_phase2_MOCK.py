#!/usr/bin/env python3
"""
MOCK Phase 2 Demonstration Runner.

[MOCK DEMONSTRATION ONLY - NOT A REAL SANDBOX RUN]
This runner uses local mocks and monkeypatches for simulation and unit/integration verification.
It DOES NOT contact PayPal for payment execution or vault authorization.
For real PayPal sandbox execution, run scripts/e2e_phase2_live.py.
"""

import sys
import json
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Add repo root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.db import init_db
from backend.app.security import mask_token
from backend.app.paypal.auth import get_access_token
from backend.app.paypal.gateway import VaultGateway

client = TestClient(app)


def log_step(step: str, title: str):
    print(f"\n{'='*75}")
    print(f"[MOCK STEP {step}] {title} [MOCK]")
    print(f"{'='*75}")


def main():
    init_db()
    print("=" * 75)
    print(">>> [MOCK RUNNER] MANDATE PHASE 2 DEMONSTRATION (SYNTHETIC / MOCKED) <<<")
    print(">>> NOTICE: This script uses synthetic test fixtures, NOT real PayPal execution. <<<")
    print("=" * 75)

    # ── 0. Optional OAuth token ping ──────────────────────────────
    log_step("0", "OAuth Ping Check")
    try:
        live_token = get_access_token()
        print(f"[MOCK RUNNER] PayPal OAuth reachable. Token: {mask_token(live_token)}")
    except Exception as exc:
        print(f"[MOCK RUNNER] PayPal OAuth warning: {exc}")

    # ── 1. Create Buyer Mandate with Mock Vault Token ──────────────
    log_step("1", "Create Mandate with Mock Vault Token [MOCK]")
    now = datetime.now(timezone.utc)
    mandate_payload = {
        "session_id": "session-mock-buyer-001",
        "budget_cents": 50000,           # $500.00
        "per_txn_cap_cents": 20000,       # $200.00
        "categories": ["electronics", "office_furniture"],
        "merchant_allowlist": ["MERCHANT-TECH-STORE", "MERCHANT-OFFICE-SUPPLY"],
        "allow_new_merchants": False,
        "allow_recurring": False,
        "velocity_per_hour": 5,
        "near_limit_pct": 80,
        "escalate_first_merchant": False,
        "expires_at": (now + timedelta(days=2)).isoformat(),
        "vault_token_ref": "mock-vault-token-synthetic-12345",
    }

    create_resp = client.post("/mandates", json=mandate_payload)
    assert create_resp.status_code == 201, f"Failed to create mandate: {create_resp.text}"
    mandate_data = create_resp.json()
    mandate_id = mandate_data["id"]
    session_token = mandate_data["session_token"]

    print(f"[MOCK] Mandate Created ID: {mandate_id}")
    print(f"[MOCK] Initial Budget: {mandate_data['budget_cents']}c ($500.00)")
    print(f"[MOCK] Reserved: {mandate_data['reserved_cents']}c | Spent: {mandate_data['spent_cents']}c")
    print(f"[MOCK] vault_token_ref encrypted and redacted from response: {'vault_token_ref' not in mandate_data}")
    print(f"[MOCK] Issued Session Token: {mask_token(session_token)}")

    # ── 2. Instruction -> Proposal Submission (APPROVE) ───────────
    log_step("2", "Instruction -> Proposal Evaluation [MOCK]")
    instruction = "buy an ergonomic vertical mouse for daily workstation use"
    print(f"Instruction: \"{instruction}\"")

    proposal_payload = {
        "mandate_id": mandate_id,
        "instruction_text": instruction,
        "agent_reasoning": "Found ergonomic vertical mouse within budget and allowed categories",
        "cart_json": {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": "USD",
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 4500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 4500,
        },
    }

    headers = {"Authorization": f"Bearer {session_token}"}
    prop_resp = client.post("/proposals", json=proposal_payload, headers=headers)
    assert prop_resp.status_code == 201, f"Proposal submission failed: {prop_resp.text}"
    prop_data = prop_resp.json()
    proposal_id = prop_data["id"]
    cart_hash = prop_data["cart_hash"]
    verdict = prop_data["verdict"]

    print(f"[MOCK] Proposal ID: {proposal_id}")
    print(f"[MOCK] Policy Verdict: {verdict}")
    print(f"[MOCK] SHA-256 Cart Hash Binding: {cart_hash}")
    print(f"[MOCK] Total Rule Hits Evaluated: {len(prop_data['rule_hits'])}")

    # ── 3. Execute Proposal (Monkeypatched Mock Gateway) ──────────
    log_step("3", "Execute Proposal via Mocked Gateway [MOCK]")
    print("[MOCK] Monkeypatching backend.app.main.payment_gateway.charge with synthetic IDs...")

    mock_order_id = f"MOCK-ORDER-{uuid.uuid4().hex[:8].upper()}"
    mock_capture_id = f"MOCK-CAPTURE-{uuid.uuid4().hex[:8].upper()}"
    mock_charge = {
        "order_id": mock_order_id,
        "capture_id": mock_capture_id,
        "status": "COMPLETED",
        "raw": {"status": "COMPLETED", "id": mock_order_id},
    }

    with patch("backend.app.main.payment_gateway.charge", return_value=mock_charge) as charge_mock:
        exec_resp = client.post(f"/proposals/{proposal_id}/execute", headers=headers)
        assert exec_resp.status_code == 201, f"Execution failed: {exec_resp.text}"
        payment_data = exec_resp.json()

        print(f"[MOCK] Payment ID: {payment_data['id']}")
        print(f"[MOCK] State Transition: ORDER_CREATED -> CAPTURED")
        print(f"[MOCK] Mock Order ID: {payment_data['paypal_order_id']}")
        print(f"[MOCK] Mock Capture ID: {payment_data['capture_id']}")
        print(f"[MOCK] Charged Amount: {payment_data['amount_cents']}c ($45.00)")
        print(f"[MOCK] PayPal-Request-Id generated: {payment_data['request_id']}")

    # Verify Budget Reservation
    m_check = client.get(f"/mandates/{mandate_id}").json()
    print(f"[MOCK] Mandate Reserved Cents: {m_check['reserved_cents']}c (locked)")
    print(f"[MOCK] Mandate Spent Cents: {m_check['spent_cents']}c (pending)")

    # ── 4. Webhook / Reconciler Confirmation ───────────────────────
    log_step("4", "Reconciler Confirmation [MOCK]")
    # In mock runner, patch get_order so reconciler sees COMPLETED
    with patch("backend.app.main.payment_gateway.get_order", return_value={"status": "COMPLETED"}):
        rec_resp = client.post("/reconcile")
        assert rec_resp.status_code == 200
        print(f"[MOCK] Reconciler response: {rec_resp.json()}")

    m_confirmed = client.get(f"/mandates/{mandate_id}").json()
    print(f"[MOCK] Mandate Reserved Cents after confirmation: {m_confirmed['reserved_cents']}c")
    print(f"[MOCK] Mandate Spent Cents after confirmation: {m_confirmed['spent_cents']}c ($45.00 completed)")

    # ── 5. Audit Chain Cryptographic Verification ──────────────────
    log_step("5", "Cryptographic Hash-Chained Audit Log Verification [MOCK]")
    audit_resp = client.get("/audit/verify")
    assert audit_resp.status_code == 200
    audit_data = audit_resp.json()

    print(f"[MOCK] Audit Chain Verified: {audit_data['verified']}")
    print(f"[MOCK] Total Verified Entries: {audit_data['count']}")
    print(f"[MOCK] Latest Hash Head: {audit_data['latest_hash']}")

    # ── 6. Invariant Proof: DENY Produces Zero Outbound PayPal Calls
    log_step("6", "Invariant Proof: DENY Produces Zero PayPal Calls [MOCK]")
    denied_payload = {
        "mandate_id": mandate_id,
        "instruction_text": "buy unauthorized gift card",
        "agent_reasoning": "Attempting unallowed category gift_card",
        "cart_json": {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": "USD",
            "items": [{"product_id": "gift-card-50", "quantity": 1, "unit_price_cents": 5000}],
            "total_cents": 5000,
        },
    }

    deny_resp = client.post("/proposals", json=denied_payload, headers=headers)
    assert deny_resp.status_code == 201
    deny_data = deny_resp.json()
    print(f"[MOCK] Proposal Verdict: {deny_data['verdict']}")

    with patch("backend.app.main.payment_gateway.charge") as uncalled_mock:
        exec_deny_resp = client.post(f"/proposals/{deny_data['id']}/execute", headers=headers)
        print(f"[MOCK] Execution rejection: {exec_deny_resp.json()['detail']}")
        uncalled_mock.assert_not_called()
        print(f"[MOCK] Verified zero outbound calls: {uncalled_mock.call_count}")

    # ── 7. Invariant Proof: Replay / Tamper Rejection ───────────────
    log_step("7", "Invariant Proof: Replay / Tamper Rejection [MOCK]")
    replay_resp = client.post("/proposals", json=proposal_payload, headers=headers)
    assert replay_resp.status_code == 201
    replay_data = replay_resp.json()
    print(f"[MOCK] Replay Verdict: {replay_data['verdict']}")

    from sqlmodel import Session
    from backend.app.db import engine
    from backend.app.models import Proposal

    tamper_payload = {
        "mandate_id": mandate_id,
        "instruction_text": "buy ergonomic mouse",
        "cart_json": {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": "USD",
            "items": [{"product_id": "mouse-ergonomic", "quantity": 3, "unit_price_cents": 4500}],
            "total_cents": 13500,
        },
    }
    t_prop = client.post("/proposals", json=tamper_payload, headers=headers).json()
    assert t_prop["verdict"] == "APPROVE"

    with Session(engine) as session:
        db_p = session.get(Proposal, t_prop["id"])
        tampered_dict = dict(db_p.cart_json)
        tampered_dict["total_cents"] = 25000
        db_p.cart_json = tampered_dict
        session.add(db_p)
        session.commit()

    tamper_exec = client.post(f"/proposals/{t_prop['id']}/execute", headers=headers)
    print(f"[MOCK] Tampered cart execution status: {tamper_exec.status_code}")
    print(f"[MOCK] Rejection message: {tamper_exec.json()['detail']}")

    print("\n" + "=" * 75)
    print(">>> [MOCK RUNNER COMPLETE] All assertions passed in MOCK mode. <<<")
    print(">>> Note: This was NOT a sandbox run. Use scripts/e2e_phase2_live.py for real PayPal. <<<")
    print("=" * 75)


if __name__ == "__main__":
    main()

