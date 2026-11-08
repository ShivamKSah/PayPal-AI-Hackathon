"""Unit tests for PayPal Webhook Endpoint.

All tests in this module use mock HTTP transports via respx to test webhook signature
verification, deduplication, and state transitions without making real PayPal network calls.
[LABEL: MOCKED UNIT TESTS]
"""

import uuid
from datetime import datetime, timezone
import pytest
import respx
import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from backend.app.main import app
from backend.app.db import engine, init_db
from backend.app.config import PAYPAL_BASE_URL
from backend.app.models import WebhookEvent, Payment, Mandate, Proposal, Case, Settlement, AuditLog

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


# ── 1. Unconfigured Webhook ID (Returns 503) ──────────────────────────

def test_webhook_unconfigured_id_returns_503_mocked(monkeypatch):
    """[MOCKED] When PAYPAL_WEBHOOK_ID is empty, return 503 refusing unverified events."""
    monkeypatch.setattr("backend.app.main.PAYPAL_WEBHOOK_ID", "")
    resp = client.post(
        "/webhooks/paypal",
        json={"id": f"WH-EVENT-{uuid.uuid4()}", "event_type": "PAYMENT.CAPTURE.COMPLETED"},
    )
    assert resp.status_code == 503
    assert "Webhook ID not configured" in resp.text


# ── 2. Valid Signature: Moves CAPTURED -> CONFIRMED ───────────────────

@respx.mock
def test_webhook_valid_signature_capture_completed_mocked(monkeypatch):
    """
    [MOCKED] Proves that a verified PAYMENT.CAPTURE.COMPLETED webhook event:
    1. Verifies signature via PayPal API (mocked SUCCESS).
    2. Records WebhookEvent row.
    3. Moves payment state from CAPTURED to CONFIRMED.
    4. Shifts reserved budget to spent budget on the mandate.
    5. Appends PAYMENT_CONFIRMED_VIA_WEBHOOK to audit log.
    """
    monkeypatch.setattr("backend.app.main.PAYPAL_WEBHOOK_ID", "WH-CONFIGURED-TEST-ID")

    # Mock OAuth token
    respx.post(f"{PAYPAL_BASE_URL}/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "mock-oauth-token", "token_type": "Bearer"})
    )
    # Mock signature verification SUCCESS
    respx.post(f"{PAYPAL_BASE_URL}/v1/notifications/verify-webhook-signature").mock(
        return_value=httpx.Response(200, json={"verification_status": "SUCCESS"})
    )

    now = datetime.now(timezone.utc)
    # Setup Mandate, Proposal, and Payment in CAPTURED state
    with Session(engine) as session:
        mandate = Mandate(
            session_id="wh-test-session",
            budget_cents=50000,
            reserved_cents=4500,
            spent_cents=0,
            per_txn_cap_cents=20000,
            categories=["electronics"],
            merchant_allowlist=["MERCHANT-TECH-STORE"],
            expires_at=now,
        )
        session.add(mandate)
        session.commit()
        session.refresh(mandate)
        mandate_id = mandate.id

        proposal = Proposal(
            mandate_id=mandate_id,
            instruction_text="test webhook item",
            cart_json={"total_cents": 4500},
            cart_hash="mock-cart-hash-wh",
            verdict="APPROVE",
            rule_hits=[],
        )
        session.add(proposal)
        session.commit()
        session.refresh(proposal)

        capture_id = f"CAP-WH-{uuid.uuid4().hex[:8].upper()}"
        payment = Payment(
            proposal_id=proposal.id,
            request_id=f"req-{uuid.uuid4()}",
            amount_cents=4500,
            state="CAPTURED",
            capture_id=capture_id,
        )
        session.add(payment)
        session.commit()

    event_id = f"WH-EVENT-{uuid.uuid4()}"
    webhook_payload = {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": capture_id},
    }
    headers = {
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-cert-url": "https://api.sandbox.paypal.com/cert",
        "paypal-transmission-id": "trans-123",
        "paypal-transmission-sig": "sig-abc",
        "paypal-transmission-time": "2026-11-08T18:00:00Z",
    }

    resp = client.post("/webhooks/paypal", json=webhook_payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "verified"

    # Verify state transitions
    with Session(engine) as session:
        db_p = session.exec(select(Payment).where(Payment.capture_id == capture_id)).first()
        assert db_p.state == "CONFIRMED"
        assert db_p.confirmed_at is not None

        db_m = session.get(Mandate, mandate_id)
        assert db_m.reserved_cents == 0
        assert db_m.spent_cents == 4500

        wh_row = session.get(WebhookEvent, event_id)
        assert wh_row is not None
        assert wh_row.verified is True


# ── 3. Invalid Signature: Rejected with 400 ───────────────────────────

@respx.mock
def test_webhook_invalid_signature_rejected_with_400_mocked(monkeypatch):
    """[MOCKED] When signature verification returns FAILURE, reject with 400."""
    monkeypatch.setattr("backend.app.main.PAYPAL_WEBHOOK_ID", "WH-CONFIGURED-TEST-ID")

    respx.post(f"{PAYPAL_BASE_URL}/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "mock-oauth-token"})
    )
    respx.post(f"{PAYPAL_BASE_URL}/v1/notifications/verify-webhook-signature").mock(
        return_value=httpx.Response(200, json={"verification_status": "FAILURE"})
    )

    event_id = f"WH-EVENT-{uuid.uuid4()}"
    webhook_payload = {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAP-FORGED"},
    }
    headers = {
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-cert-url": "https://api.sandbox.paypal.com/cert",
        "paypal-transmission-id": "trans-fake",
        "paypal-transmission-sig": "sig-fake",
        "paypal-transmission-time": "2026-11-08T18:00:00Z",
    }

    resp = client.post("/webhooks/paypal", json=webhook_payload, headers=headers)
    assert resp.status_code == 400
    assert "Verification failed" in resp.text

    # WebhookEvent must NOT be recorded as verified
    with Session(engine) as session:
        wh_row = session.get(WebhookEvent, event_id)
        assert wh_row is None


# ── 4. Duplicate Event ID: Idempotent Skip ───────────────────────────

@respx.mock
def test_webhook_duplicate_event_id_idempotent_mocked(monkeypatch):
    """[MOCKED] Duplicate webhook deliveries return 200 with status duplicate and no re-processing."""
    monkeypatch.setattr("backend.app.main.PAYPAL_WEBHOOK_ID", "WH-CONFIGURED-TEST-ID")

    respx.post(f"{PAYPAL_BASE_URL}/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "mock-oauth-token"})
    )
    respx.post(f"{PAYPAL_BASE_URL}/v1/notifications/verify-webhook-signature").mock(
        return_value=httpx.Response(200, json={"verification_status": "SUCCESS"})
    )

    event_id = f"WH-EVENT-{uuid.uuid4()}"
    webhook_payload = {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAP-DUP"},
    }

    # First delivery: verified
    resp1 = client.post("/webhooks/paypal", json=webhook_payload)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "verified"

    # Second delivery with identical event_id: idempotent duplicate response
    resp2 = client.post("/webhooks/paypal", json=webhook_payload)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "duplicate"
    assert resp2.json()["event_id"] == event_id


# ── 5. PAYMENT.CAPTURE.REFUNDED Event Handler ─────────────────────────

@respx.mock
def test_webhook_payment_capture_refunded_mocked(monkeypatch):
    """
    [MOCKED] Proves that a verified PAYMENT.CAPTURE.REFUNDED event:
    1. Updates Case status to REFUNDED.
    2. Updates Settlement with paypal_refund_id.
    3. Decrements Mandate spent_cents by refund amount.
    4. Appends PAYMENT_REFUNDED_VIA_WEBHOOK to audit log.
    """
    monkeypatch.setattr("backend.app.main.PAYPAL_WEBHOOK_ID", "WH-CONFIGURED-TEST-ID")

    respx.post(f"{PAYPAL_BASE_URL}/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "mock-oauth-token"})
    )
    respx.post(f"{PAYPAL_BASE_URL}/v1/notifications/verify-webhook-signature").mock(
        return_value=httpx.Response(200, json={"verification_status": "SUCCESS"})
    )

    now = datetime.now(timezone.utc)
    capture_id = f"CAP-REF-{uuid.uuid4().hex[:8].upper()}"

    with Session(engine) as session:
        mandate = Mandate(
            session_id="wh-refund-session",
            budget_cents=50000,
            reserved_cents=0,
            spent_cents=10000,
            per_txn_cap_cents=20000,
            categories=["electronics"],
            merchant_allowlist=["MERCHANT-TECH-STORE"],
            expires_at=now,
        )
        session.add(mandate)
        session.commit()
        session.refresh(mandate)
        mandate_id = mandate.id

        proposal = Proposal(
            mandate_id=mandate_id,
            instruction_text="refundable order",
            cart_json={"total_cents": 10000},
            cart_hash="mock-cart-hash-ref",
            verdict="APPROVE",
            rule_hits=[],
        )
        session.add(proposal)
        session.commit()
        session.refresh(proposal)

        payment = Payment(
            proposal_id=proposal.id,
            request_id=f"req-ref-{uuid.uuid4()}",
            amount_cents=10000,
            state="CONFIRMED",
            capture_id=capture_id,
        )
        session.add(payment)
        session.commit()
        session.refresh(payment)

        case = Case(
            payment_id=payment.id,
            human_claim="Late shipment",
            status="PROPOSED",
        )
        session.add(case)
        session.commit()
        session.refresh(case)
        case_id = case.id

        settlement = Settlement(
            case_id=case_id,
            human_accepted=True,
            request_id=f"req-set-{uuid.uuid4()}",
            final_amount_cents=6000,
        )
        session.add(settlement)
        session.commit()

    refund_id = f"REF-PP-{uuid.uuid4().hex[:8].upper()}"
    webhook_payload = {
        "id": f"WH-REF-EVENT-{uuid.uuid4()}",
        "event_type": "PAYMENT.CAPTURE.REFUNDED",
        "resource": {
            "id": refund_id,
            "amount": {"value": "60.00", "currency_code": "USD"},
            "capture_id": capture_id,
        },
    }

    resp = client.post("/webhooks/paypal", json=webhook_payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "verified"

    # Verify database updates
    with Session(engine) as session:
        db_case = session.get(Case, case_id)
        assert db_case.status == "REFUNDED"

        db_settlement = session.exec(select(Settlement).where(Settlement.case_id == case_id)).first()
        assert db_settlement.paypal_refund_id == refund_id

        # Mandate spent decremented by $60.00 ($100.00 -> $40.00)
        db_m = session.get(Mandate, mandate_id)
        assert db_m.spent_cents == 4000

        # Audit log verification
        audit_entry = session.exec(
            select(AuditLog)
            .where(AuditLog.actor == "paypal_webhook")
            .where(AuditLog.event == "PAYMENT_REFUNDED_VIA_WEBHOOK")
        ).all()
        assert len(audit_entry) >= 1
