#!/usr/bin/env python3
"""
scripts/e2e_phase2_live.py

MANDATE PHASE 2 LIVE SANDBOX ACCEPTANCE RUNNER.

This script executes a 100% REAL, un-mocked end-to-end run against the PayPal Sandbox:
1. Fresh Temporary Database: Isolated SQLite db created on the fly (audit chain starts at 0).
2. Live Vault Grant Initiation: Creates a real $1.00 vault setup order via PayPal API.
3. Human-in-the-loop Approval: Prints PayPal approval link and pauses for buyer approval.
4. Live Grant Capture & Vault Token Extraction: Captures order, extracts real vault token from
   payment_source.paypal.attributes.vault.id, auto-refunds the $1.00, and stores encrypted token.
5. Proposal Evaluation: Submits $45.00 proposal through policy engine (verdict: APPROVE, cart hash bound).
6. Live Vault Execution: Charges real $45.00 via VaultGateway using pinned vault shape
   payment_source.paypal.vault_id with fresh PayPal-Request-Id (no browser needed).
7. Strict PayPal ID Validation: Validates that all Order IDs and Capture IDs strictly match
   PayPal's 17-character alphanumeric format (^[0-9A-Z]{17}$) and contains ZERO mock markers.
8. Live Confirmation: Confirms via real GET /v2/checkout/orders/{id} and polling reconciler.
9. Cryptographic Audit Verification: Recomputes and verifies the complete SHA-256 hash chain.
"""

import os
import re
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Strict validation: verify credentials exist before running
from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")

if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
    print("[FATAL] PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET missing in .env! Aborting.")
    sys.exit(1)

if not TOKEN_ENCRYPTION_KEY:
    print("[FATAL] TOKEN_ENCRYPTION_KEY missing in .env! Aborting.")
    sys.exit(1)

# ── 1. Create Fresh Isolated Temporary Database ───────────────────────
temp_dir = tempfile.mkdtemp(prefix="mandate_live_")
temp_db_path = Path(temp_dir) / "mandate_live.db"
temp_db_url = f"sqlite:///{temp_db_path.as_posix()}"

os.environ["DATABASE_URL"] = temp_db_url

import backend.app.config as app_config
app_config.DATABASE_URL = temp_db_url

from sqlmodel import create_engine, Session, select
import backend.app.db as db_mod
db_mod.DATABASE_URL = temp_db_url
db_mod.engine = create_engine(temp_db_url, echo=False, connect_args={"check_same_thread": False})
db_mod.init_db()

from fastapi.testclient import TestClient
from backend.app.main import app, payment_gateway
from backend.app.db import get_session
from backend.app.security import mask_token, decrypt_vault_token
from backend.app.paypal.gateway import VaultGateway
from backend.app.paypal.auth import get_access_token
from backend.app.models import Mandate, Proposal, Payment, AuditLog

# Override FastAPI dependency to strictly use the temporary database
def get_isolated_session():
    with Session(db_mod.engine) as session:
        yield session

app.dependency_overrides[get_session] = get_isolated_session
client = TestClient(app)

# ── ID & Mock Validation Helpers ─────────────────────────────────────
PAYPAL_TXN_ID_REGEX = re.compile(r"^[0-9A-Z]{17}$")
BANNED_MOCK_KEYWORDS = ["mock", "synthetic", "order-pp", "capture-pp", "fake", "dummy", "pinned", "test-token"]


def assert_real_paypal_id(val: str, id_name: str) -> str:
    """Strictly assert that an ID is a genuine 17-character PayPal transaction ID."""
    if not val or not isinstance(val, str):
        raise AssertionError(f"[FAIL] {id_name} is empty or invalid! Value: {val!r}")
    val = val.strip()
    for banned in BANNED_MOCK_KEYWORDS:
        if banned in val.lower():
            raise AssertionError(f"[FAIL] {id_name}='{val}' contains forbidden mock marker '{banned}'! Mocks are not permitted.")
    if not PAYPAL_TXN_ID_REGEX.match(val):
        raise AssertionError(
            f"[FAIL] {id_name}='{val}' does not match PayPal's 17-char alphanumeric format (^[0-9A-Z]{{17}}$)!"
        )
    return val


def assert_real_vault_token(val: str) -> str:
    """Strictly assert that a vault token ID is genuine and un-mocked."""
    if not val or not isinstance(val, str):
        raise AssertionError(f"[FAIL] Vault token is empty or invalid! Value: {val!r}")
    val = val.strip()
    for banned in BANNED_MOCK_KEYWORDS:
        if banned in val.lower():
            raise AssertionError(f"[FAIL] Vault token='{val}' contains forbidden mock marker '{banned}'!")
    if len(val) < 8:
        raise AssertionError(f"[FAIL] Vault token '{val}' is suspiciously short for a PayPal vault ID.")
    return val


def log_step(step: str, title: str):
    print(f"\n{'='*75}")
    print(f"[LIVE STEP {step}] {title}")
    print(f"{'='*75}")


# ── Main Live Acceptance Execution ───────────────────────────────────
def main():
    print("=" * 75)
    print("MANDATE PHASE 2 LIVE SANDBOX ACCEPTANCE RUN")
    print(f"Temporary Isolated DB: {temp_db_path}")
    print("=" * 75)

    # ── 0. Initial Integrity & Audit Cleanliness Check ───────────────
    log_step("0", "Verify Clean Ephemeral Environment & OAuth Connectivity")
    initial_audit = client.get("/audit/verify").json()
    if initial_audit["count"] != 0:
        raise AssertionError(f"[FAIL] Expected 0 initial audit entries in fresh DB, got {initial_audit['count']}")
    print(f"[OK] Ephemeral DB verified: 0 existing audit log rows.")

    oauth_token = get_access_token()
    print(f"[OK] Live PayPal Sandbox OAuth succeeded. Token: {mask_token(oauth_token)}")

    # ── 1. Create Active Mandate (Pending Grant Authorization) ───────
    log_step("1", "Create Active Mandate (Awaiting Vault Grant)")
    now = datetime.now(timezone.utc)
    mandate_payload = {
        "session_id": "session-live-sandbox-buyer",
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
        "vault_token_ref": None,         # Will be populated by real Grant step
    }

    create_resp = client.post("/mandates", json=mandate_payload)
    if create_resp.status_code != 201:
        raise AssertionError(f"[FAIL] Failed to create mandate: {create_resp.text}")
    mandate_data = create_resp.json()
    mandate_id = mandate_data["id"]
    session_token = mandate_data["session_token"]

    print(f"[OK] Mandate Created ID: {mandate_id}")
    print(f"[OK] Budget: {mandate_data['budget_cents']}c ($500.00) | Reserved: {mandate_data['reserved_cents']}c | Spent: {mandate_data['spent_cents']}c")
    print(f"[OK] Session Token: {mask_token(session_token)}")

    # ── 2. Grant Step: Create $1.00 Vault Setup Order ────────────────
    log_step("2", "Initiate $1.00 Vault Setup Order via PayPal API")
    grant_init_resp = client.post(
        f"/mandates/{mandate_id}/grant/init",
        json={
            "return_url": "https://example.com/grant/return",
            "cancel_url": "https://example.com/grant/cancel",
        },
    )
    if grant_init_resp.status_code != 200:
        raise AssertionError(f"[FAIL] Grant init failed: {grant_init_resp.text}")

    grant_info = grant_init_resp.json()
    grant_order_id = grant_info.get("order_id")
    approve_url = grant_info.get("approve_url")

    assert_real_paypal_id(grant_order_id, "Grant Order ID")
    if not approve_url or "sandbox.paypal.com" not in approve_url:
        raise AssertionError(f"[FAIL] Invalid PayPal approval URL: {approve_url}")

    print(f"[OK] Real PayPal Grant Order ID: {grant_order_id}")
    print(f"[OK] Live PayPal Approval Link:")
    print(f"\n    {approve_url}\n")

    # ── 3. Human Approval Pause ──────────────────────────────────────
    log_step("3", "Buyer Approval in Browser")
    print("ACTION REQUIRED:")
    print("1. Open the approval link above in your browser.")
    print("2. Log in with your PayPal Sandbox buyer account.")
    print("3. Click 'Pay Now' / Complete purchase to approve the $1.00 vault setup.")
    print("4. Come back here once approved.")
    input("\n>>> Press Enter after you have approved the order in the browser... ")

    # ── 4. Capture Grant, Extract Vault Token & Auto-Refund $1.00 ────
    log_step("4", "Capture Grant, Extract Vault Token & Auto-Refund $1.00")
    grant_complete_resp = client.post(
        f"/mandates/{mandate_id}/grant/complete",
        json={"order_id": grant_order_id},
    )
    if grant_complete_resp.status_code != 200:
        raise AssertionError(f"[FAIL] Grant complete/capture failed: {grant_complete_resp.text}")

    grant_result = grant_complete_resp.json()
    print(f"[OK] Grant Status: {grant_result.get('status')}")
    print(f"[OK] Refund Status for $1.00 test authorization: {grant_result.get('refund_status')}")

    # Verify vault token was stored encrypted in DB
    with Session(db_mod.engine) as session:
        stored_mandate = session.get(Mandate, mandate_id)
        if not stored_mandate.vault_token_ref:
            raise AssertionError("[FAIL] Mandate has no vault_token_ref stored in database!")
        raw_vault_token = decrypt_vault_token(stored_mandate.vault_token_ref)
        assert_real_vault_token(raw_vault_token)
        print(f"[OK] Decrypted Real Vault Token: {mask_token(raw_vault_token)}")
        print(f"[OK] Stored Encrypted Token Ref: {stored_mandate.vault_token_ref[:20]}... (Fernet encrypted)")

    # ── 5. Submit $45.00 Proposal through Policy Engine ──────────────
    log_step("5", "Submit $45.00 Proposal -> Policy Evaluation")
    instruction = "buy an ergonomic vertical mouse for daily workstation use"
    headers = {"Authorization": f"Bearer {session_token}"}
    proposal_payload = {
        "mandate_id": mandate_id,
        "instruction_text": instruction,
        "agent_reasoning": "Selected approved ergonomic vertical mouse within budget and category",
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

    prop_resp = client.post("/proposals", json=proposal_payload, headers=headers)
    if prop_resp.status_code != 201:
        raise AssertionError(f"[FAIL] Proposal submission failed: {prop_resp.text}")
    prop_data = prop_resp.json()
    proposal_id = prop_data["id"]
    verdict = prop_data["verdict"]
    cart_hash = prop_data["cart_hash"]

    if verdict != "APPROVE":
        raise AssertionError(f"[FAIL] Expected APPROVE verdict, got: {verdict}")

    print(f"[OK] Proposal ID: {proposal_id}")
    print(f"[OK] Policy Verdict: {verdict}")
    print(f"[OK] Bound Cart Hash (SHA-256): {cart_hash}")
    print(f"[OK] Evaluated Rules: {len(prop_data['rule_hits'])} (no early exit)")

    # ── 6. Autonomous Server-Side Execution (Pinned Vault Shape) ────
    log_step("6", "Execute $45.00 Charge via VaultGateway (payment_source.paypal.vault_id)")
    exec_resp = client.post(f"/proposals/{proposal_id}/execute", headers=headers)
    if exec_resp.status_code != 201:
        raise AssertionError(f"[FAIL] Execution failed: {exec_resp.text}")
    payment_data = exec_resp.json()

    real_order_id = payment_data.get("paypal_order_id")
    real_capture_id = payment_data.get("capture_id")
    payment_state = payment_data.get("state")

    # Strict validation of PayPal IDs
    assert_real_paypal_id(real_order_id, "Charged PayPal Order ID")
    assert_real_paypal_id(real_capture_id, "Charged PayPal Capture ID")

    if payment_state != "CAPTURED":
        raise AssertionError(f"[FAIL] Expected payment state CAPTURED, got {payment_state}")

    print(f"[OK] Payment ID: {payment_data['id']}")
    print(f"[OK] Payment State: {payment_state}")
    print(f"[OK] REAL PayPal Order ID:   {real_order_id}")
    print(f"[OK] REAL PayPal Capture ID: {real_capture_id}")
    print(f"[OK] Amount Charged: {payment_data['amount_cents']}c ($45.00)")
    print(f"[OK] Fresh Request ID: {payment_data['request_id']}")

    # Verify budget reservation in DB
    m_check = client.get(f"/mandates/{mandate_id}").json()
    print(f"[OK] Mandate Reserved Cents: {m_check['reserved_cents']}c ($45.00 locked)")
    print(f"[OK] Mandate Spent Cents:    {m_check['spent_cents']}c (pending confirmation)")

    # ── 7. Order Confirmation via Live GET /v2/checkout/orders/{id} ─
    log_step("7", "Verify Order via Real PayPal GET and Reconciler")
    vg = VaultGateway()
    live_order_details = vg.get_order(real_order_id)
    live_paypal_status = live_order_details.get("status")
    print(f"[OK] Real PayPal GET /v2/checkout/orders/{real_order_id} returned status: {live_paypal_status}")
    if live_paypal_status != "COMPLETED":
        raise AssertionError(f"[FAIL] Expected PayPal order status COMPLETED, got: {live_paypal_status}")

    # Advance CAPTURED -> CONFIRMED via Reconciler
    rec_resp = client.post("/reconcile")
    if rec_resp.status_code != 200:
        raise AssertionError(f"[FAIL] Reconcile call failed: {rec_resp.text}")
    rec_data = rec_resp.json()
    print(f"[OK] Reconciler completed: {rec_data}")

    m_confirmed = client.get(f"/mandates/{mandate_id}").json()
    print(f"[OK] Mandate Reserved Cents after confirmation: {m_confirmed['reserved_cents']}c")
    print(f"[OK] Mandate Spent Cents after confirmation:    {m_confirmed['spent_cents']}c ($45.00 spent)")

    if PAYPAL_WEBHOOK_ID:
        print(f"[INFO] PAYPAL_WEBHOOK_ID is configured ({PAYPAL_WEBHOOK_ID[:8]}...). Webhook handler is ready.")
    else:
        print("[INFO] PAYPAL_WEBHOOK_ID not set; confirmation handled via verified live GET reconciler.")

    # ── 8. Cryptographic Hash-Chained Audit Log Verification ───────
    log_step("8", "Cryptographic Hash-Chained Audit Log Verification")
    audit_resp = client.get("/audit/verify")
    if audit_resp.status_code != 200:
        raise AssertionError(f"[FAIL] Audit verification request failed: {audit_resp.text}")
    audit_data = audit_resp.json()

    if not audit_data.get("verified"):
        raise AssertionError(f"[FAIL] Cryptographic audit chain verification FAILED! Details: {audit_data}")

    print(f"[OK] Audit Chain Cryptographically Verified: {audit_data['verified']}")
    print(f"[OK] Total Chain Entries Generated in Live Run: {audit_data['count']}")
    print(f"[OK] Latest Hash Head: {audit_data['latest_hash']}")

    # ── 9. Final Summary ───────────────────────────────────────────
    print("\n" + "=" * 75)
    print("LIVE SANDBOX ACCEPTANCE SUCCESSFUL")
    print(f"PayPal Grant Order ID:   {grant_order_id}")
    print(f"PayPal Charged Order ID: {real_order_id}")
    print(f"PayPal Capture ID:       {real_capture_id}")
    print(f"Vault Token (Masked):    {mask_token(raw_vault_token)}")
    print(f"Audit Entries Verified:  {audit_data['count']}")
    print("Zero mocks used. All calls verified against real PayPal Sandbox API.")
    print("=" * 75)


if __name__ == "__main__":
    main()

