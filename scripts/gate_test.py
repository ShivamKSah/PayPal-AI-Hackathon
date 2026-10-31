#!/usr/bin/env python3
"""
Phase 0 Gate Test — Validates the full PayPal sandbox flow:
  (a) Create a vault-enabled order, print payer-action link, wait for approval
  (b) Capture and extract the vault token id
  (c) Create a second order using the vault token (NO browser), try both shapes
  (d) Partial refund (60%) with a fresh PayPal-Request-Id
  (e) Webhook verification (if TUNNEL_URL + PAYPAL_WEBHOOK_ID are set)

Also tests:
  - Channel3 catalog API (if CATALOG_API_KEY is set)

Usage:
  1. Fill in your .env file (copy from .env.example)
  2. Start your HTTPS tunnel (ngrok/cloudflared) pointing to a local port
  3. Run:  python scripts/gate_test.py

All money values are in USD. This script uses SANDBOX only.
"""

import os
import sys
import json
import uuid
import time
import base64
import hashlib
import argparse
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

import httpx
from dotenv import load_dotenv

# ── Load .env from repo root ──────────────────────────────────
_root = Path(__file__).resolve().parents[1]
load_dotenv(_root / ".env")

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")
PAYPAL_ENV = os.environ.get("PAYPAL_ENV", "sandbox")
TUNNEL_URL = os.environ.get("TUNNEL_URL", "")
CATALOG_API_KEY = os.environ.get("CATALOG_API_KEY", "")

if PAYPAL_ENV != "sandbox":
    print("ERROR: This script MUST run against sandbox. Set PAYPAL_ENV=sandbox.")
    sys.exit(1)

BASE_URL = "https://api-m.sandbox.paypal.com"

# Return/cancel URLs for the vault approval flow
RETURN_URL = "https://example.com/return"
CANCEL_URL = "https://example.com/cancel"


# ── Helpers ────────────────────────────────────────────────────

def _log(step: str, msg: str):
    print(f"\n{'='*60}")
    print(f"[{step}] {msg}")
    print('='*60)


def _redact_dict(data: dict) -> dict:
    redacted = {}
    for k, v in data.items():
        k_lower = k.lower()
        if isinstance(v, dict):
            redacted[k] = _redact_dict(v)
        elif isinstance(v, list):
            redacted[k] = [_redact_dict(i) if isinstance(i, dict) else i for i in v]
        elif k_lower in ("access_token", "client_secret", "refresh_token", "vault_id") or "token" in k_lower:
            if isinstance(v, str) and len(v) > 8:
                redacted[k] = f"{v[:4]}...{v[-4:]} [REDACTED]"
            else:
                redacted[k] = "[REDACTED]"
        elif "email" in k_lower:
            if isinstance(v, str) and "@" in v:
                parts = v.split("@")
                redacted[k] = f"{parts[0][:2]}***@{parts[1]} [REDACTED]"
            else:
                redacted[k] = "[REDACTED]"
        elif isinstance(v, str) and "@" in v and not v.startswith("http"):
            parts = v.split("@")
            redacted[k] = f"{parts[0][:2]}***@{parts[1]} [REDACTED]"
        else:
            redacted[k] = v
    return redacted


def _log_response(label: str, resp: httpx.Response):
    print(f"\n--- {label} ---")
    print(f"Status: {resp.status_code}")
    try:
        body = resp.json()
        if isinstance(body, dict):
            body = _redact_dict(body)
        print(json.dumps(body, indent=2))
    except Exception:
        print(resp.text[:2000])
    print(f"--- /{label} ---\n")


def _request_id() -> str:
    """Fresh PayPal-Request-Id (idempotency key)."""
    return f"mandate-gate-{uuid.uuid4()}"


def _amount_str(cents: int) -> str:
    """Integer cents -> PayPal amount string like '10.00'."""
    d = Decimal(cents) / Decimal(100)
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ── Step 0: Get OAuth token ───────────────────────────────────

def get_access_token(client: httpx.Client) -> tuple[str, int]:
    """Returns (access_token, expires_in_seconds)."""
    _log("0", "Getting OAuth 2.0 access token")

    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        print("ERROR: PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set in .env")
        sys.exit(1)

    credentials = base64.b64encode(
        f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}".encode()
    ).decode()

    resp = client.post(
        f"{BASE_URL}/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        content="grant_type=client_credentials",
    )
    _log_response("OAuth Token Response", resp)

    if resp.status_code != 200:
        print(f"FATAL: OAuth failed with status {resp.status_code}")
        sys.exit(1)

    data = resp.json()
    token = data["access_token"]
    expires_in = data.get("expires_in", "UNKNOWN")
    token_preview = f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "***"
    print(f"✓ Access token obtained ({token_preview}). Expires in: {expires_in} seconds.")
    print(f"  (UNVERIFIED item: sandbox token lifetime = {expires_in}s)")
    return token, expires_in


def _inspect_order_on_error(
    client: httpx.Client,
    token: str,
    order_id: str | None,
    resp: httpx.Response,
    action_name: str,
):
    print(f"\n{'!'*60}")
    print(f"FATAL: {action_name} failed with status {resp.status_code}")
    print(f"{'!'*60}\n")

    debug_id = (
        resp.headers.get("PayPal-Debug-Id")
        or resp.headers.get("paypal-debug-id")
        or "NOT_FOUND"
    )
    try:
        err_json = resp.json()
        if isinstance(err_json, dict):
            if debug_id == "NOT_FOUND":
                debug_id = err_json.get("debug_id", "NOT_FOUND")
            err_body_redacted = _redact_dict(err_json)
        else:
            err_body_redacted = err_json
        err_str = json.dumps(err_body_redacted, indent=2)
    except Exception:
        err_str = resp.text[:2000]

    print(f"--- {action_name} Error Details ---")
    print(f"PayPal-Debug-Id: {debug_id}")
    print(f"Error Body:\n{err_str}")
    print(f"--- /{action_name} Error Details ---\n")

    if order_id:
        print(f"Fetching Order Details via GET /v2/checkout/orders/{order_id} ...")
        get_resp = client.get(
            f"{BASE_URL}/v2/checkout/orders/{order_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        if get_resp.status_code in (200, 201):
            order_data = get_resp.json()
            order_status = order_data.get("status", "UNKNOWN")
            payer_info = order_data.get("payer", {})
            payment_source = order_data.get("payment_source", {})

            paypal_info = payment_source.get("paypal", {})
            funding_source = (
                paypal_info.get("funding_source")
                or payment_source.get("funding_source")
                or (list(payment_source.keys())[0] if payment_source else "UNKNOWN")
            )

            print(f"\n--- Order Status & Inspection (Order: {order_id}) ---")
            print(f"Status: {order_status}")
            print(f"Funding Source (if returned): {funding_source}")
            print(f"Payer Info:\n{json.dumps(_redact_dict(payer_info), indent=2)}")
            print(f"Payment Source:\n{json.dumps(_redact_dict(payment_source), indent=2)}")
            print(f"--- /Order Status & Inspection ---\n")
        else:
            print(f"Failed to fetch order details ({get_resp.status_code}): {get_resp.text[:500]}")


# ── Plain Order Flow (no vault) ────────────────────────────────

def create_plain_order(client: httpx.Client, token: str) -> tuple[str, str]:
    """Create a plain $5.00 order without vault attributes. Returns (order_id, approve_link)."""
    _log("A-PLAIN", "Creating plain order ($5.00 USD, NO vault attributes)")

    order_body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": "mandate-gate-plain",
                "amount": {
                    "currency_code": "USD",
                    "value": "5.00",
                },
                "description": "Mandate gate test – plain order (no vault)",
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "return_url": RETURN_URL,
                    "cancel_url": CANCEL_URL,
                    "user_action": "PAY_NOW",
                    "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                }
            }
        },
    }

    req_id = _request_id()
    resp = client.post(
        f"{BASE_URL}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": req_id,
        },
        json=order_body,
    )
    _log_response("Create Plain Order", resp)

    if resp.status_code not in (200, 201):
        _inspect_order_on_error(client, token, None, resp, "Create Plain Order")
        sys.exit(1)

    data = resp.json()
    order_id = data["id"]

    approve_link = None
    for link in data.get("links", []):
        if link["rel"] in ("approve", "payer-action"):
            approve_link = link["href"]
            break

    if not approve_link:
        print("FATAL: No approve/payer-action link in order response.")
        print("Available links:", [l["rel"] for l in data.get("links", [])])
        _inspect_order_on_error(client, token, order_id, resp, "Locate Approve Link")
        sys.exit(1)

    print(f"✓ Plain order created: {order_id}")
    print(f"✓ PayPal-Request-Id: {req_id}")
    print(f"\n{'*'*60}")
    print(f"ACTION REQUIRED: Open this link in a browser and approve")
    print(f"with your PERSONAL sandbox buyer account:")
    print(f"\n  {approve_link}\n")
    print(f"{'*'*60}")

    return order_id, approve_link


# ── Step A: Create vault-enabled order ─────────────────────────

def create_vault_order(client: httpx.Client, token: str) -> tuple[str, str]:
    """Create an order with vault instructions. Returns (order_id, approve_link)."""
    _log("A", "Creating vault-enabled order ($5.00)")

    order_body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": "mandate-gate-test",
                "amount": {
                    "currency_code": "USD",
                    "value": "5.00",
                },
                "description": "Mandate gate test – vault setup",
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "return_url": RETURN_URL,
                    "cancel_url": CANCEL_URL,
                    "user_action": "PAY_NOW",
                    "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
                },
                "attributes": {
                    "vault": {
                        "store_in_vault": "ON_SUCCESS",
                        "usage_type": "MERCHANT",
                        "customer_type": "CONSUMER",
                    }
                },
            }
        },
    }

    req_id = _request_id()
    resp = client.post(
        f"{BASE_URL}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": req_id,
        },
        json=order_body,
    )
    _log_response("Create Vault Order", resp)

    if resp.status_code not in (200, 201):
        _inspect_order_on_error(client, token, None, resp, "Create Vault Order")
        sys.exit(1)

    data = resp.json()
    order_id = data["id"]

    # Find the payer-action / approve link
    approve_link = None
    for link in data.get("links", []):
        if link["rel"] in ("approve", "payer-action"):
            approve_link = link["href"]
            break

    if not approve_link:
        print("FATAL: No approve/payer-action link in order response.")
        print("Available links:", [l["rel"] for l in data.get("links", [])])
        sys.exit(1)

    print(f"✓ Order created: {order_id}")
    print(f"✓ PayPal-Request-Id: {req_id}")
    print(f"\n{'*'*60}")
    print(f"ACTION REQUIRED: Open this link in a browser and approve")
    print(f"with your PERSONAL sandbox buyer account:")
    print(f"\n  {approve_link}\n")
    print(f"{'*'*60}")

    return order_id, approve_link


# ── Step B: Capture the order and extract vault token ──────────

def capture_order(client: httpx.Client, token: str, order_id: str) -> tuple[str, str | None]:
    """Capture the approved order. Returns (capture_id, vault_token_id or None)."""
    _log("B", f"Capturing order {order_id}")

    req_id = _request_id()
    resp = client.post(
        f"{BASE_URL}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": req_id,
        },
    )
    _log_response("Capture Order", resp)

    if resp.status_code not in (200, 201):
        _inspect_order_on_error(client, token, order_id, resp, "Capture Order")
        sys.exit(1)

    data = resp.json()
    capture_status = data.get("status", "UNKNOWN")
    print(f"✓ Capture status: {capture_status}")

    # Funding source on successful capture
    ps = data.get("payment_source", {}).get("paypal", {})
    funding_source = (
        ps.get("funding_source")
        or data.get("payment_source", {}).get("funding_source")
        or (list(data.get("payment_source", {}).keys())[0] if data.get("payment_source") else None)
    )
    if funding_source:
        print(f"✓ Funding source: {funding_source}")

    # Extract capture ID
    capture_id = None
    for pu in data.get("purchase_units", []):
        for capture in pu.get("payments", {}).get("captures", []):
            capture_id = capture["id"]
            break

    if not capture_id:
        print("FATAL: No capture ID found in response.")
        sys.exit(1)

    # Extract vault token (may be nested in different places)
    vault_token_id = None
    ps = data.get("payment_source", {}).get("paypal", {})

    # Shape 1: payment_source.paypal.attributes.vault.id
    vault_attrs = ps.get("attributes", {}).get("vault", {})
    if vault_attrs.get("id"):
        vault_token_id = vault_attrs["id"]
        print(f"✓ Vault token found at payment_source.paypal.attributes.vault.id")

    # Shape 2: payment_source.paypal.vault_id (some docs show this)
    if not vault_token_id and ps.get("vault_id"):
        vault_token_id = ps["vault_id"]
        print(f"✓ Vault token found at payment_source.paypal.vault_id")

    if vault_token_id:
        print(f"✓ Capture ID: {capture_id}")
        print(f"✓ Vault token ID: {vault_token_id}")
    else:
        print(f"⚠ Capture succeeded (ID: {capture_id}) but NO vault token found.")
        print(f"  This may mean the vault token arrives asynchronously via webhook.")
        print(f"  Check VAULT.PAYMENT-TOKEN.CREATED webhook events.")
        print(f"  Full payment_source.paypal: {json.dumps(ps, indent=2)}")

    return capture_id, vault_token_id


# ── Step C: Charge with vault token (no browser) ──────────────

def charge_with_vault_token(
    client: httpx.Client, token: str, vault_token_id: str
) -> tuple[str | None, str | None, dict]:
    """
    Try to create + capture an order using the vault token.
    Tests both documented shapes and reports which works.
    Returns (order_id, capture_id, results_dict).
    """
    _log("C", f"Charging $3.00 with vault token {vault_token_id} (NO browser)")

    results = {"shape1": None, "shape2": None, "working_shape": None}

    # ── Shape 1: payment_source.paypal.vault_id ──
    print("\n--- Trying Shape 1: payment_source.paypal.vault_id ---")
    order_body_1 = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": "mandate-gate-vault-test-1",
                "amount": {
                    "currency_code": "USD",
                    "value": "3.00",
                },
                "description": "Mandate gate test – vault charge shape 1",
            }
        ],
        "payment_source": {
            "paypal": {
                "vault_id": vault_token_id,
            }
        },
    }

    req_id_1 = _request_id()
    resp_1 = client.post(
        f"{BASE_URL}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": req_id_1,
        },
        json=order_body_1,
    )
    _log_response("Shape 1 – Create Order", resp_1)

    shape1_order_id = None
    shape1_capture_id = None

    if resp_1.status_code in (200, 201):
        d = resp_1.json()
        shape1_order_id = d.get("id")
        status = d.get("status")
        print(f"  Shape 1 order created: {shape1_order_id}, status: {status}")

        # If status is COMPLETED, the order was auto-captured
        if status == "COMPLETED":
            for pu in d.get("purchase_units", []):
                for c in pu.get("payments", {}).get("captures", []):
                    shape1_capture_id = c["id"]
            print(f"  ✓ Shape 1 auto-captured! Capture ID: {shape1_capture_id}")
            results["shape1"] = "SUCCESS_AUTO_CAPTURED"
            results["working_shape"] = "shape1_vault_id"
        elif status == "APPROVED":
            # Need explicit capture
            print("  Shape 1 order APPROVED, capturing...")
            cap_resp = client.post(
                f"{BASE_URL}/v2/checkout/orders/{shape1_order_id}/capture",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "PayPal-Request-Id": _request_id(),
                },
            )
            _log_response("Shape 1 – Capture", cap_resp)
            if cap_resp.status_code in (200, 201):
                cd = cap_resp.json()
                for pu in cd.get("purchase_units", []):
                    for c in pu.get("payments", {}).get("captures", []):
                        shape1_capture_id = c["id"]
                print(f"  ✓ Shape 1 captured! Capture ID: {shape1_capture_id}")
                results["shape1"] = "SUCCESS_MANUAL_CAPTURE"
                results["working_shape"] = "shape1_vault_id"
            else:
                results["shape1"] = f"CAPTURE_FAILED_{cap_resp.status_code}"
                print(f"  ✗ Shape 1 capture failed: {cap_resp.status_code}")
        elif status == "CREATED":
            print("  Shape 1 returned CREATED — needs buyer approval (vault not working)")
            results["shape1"] = "CREATED_NEEDS_APPROVAL"
        else:
            results["shape1"] = f"UNEXPECTED_STATUS_{status}"
    else:
        error_text = ""
        try:
            error_text = json.dumps(resp_1.json(), indent=2)
        except Exception:
            error_text = resp_1.text[:500]
        results["shape1"] = f"CREATE_FAILED_{resp_1.status_code}"
        print(f"  ✗ Shape 1 failed: {resp_1.status_code}")

    # ── Shape 2: payment_source.token with type PAYMENT_METHOD_TOKEN ──
    # Only try if shape 1 didn't work
    if not shape1_capture_id:
        print("\n--- Trying Shape 2: payment_source.token ---")
        order_body_2 = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": "mandate-gate-vault-test-2",
                    "amount": {
                        "currency_code": "USD",
                        "value": "3.00",
                    },
                    "description": "Mandate gate test – vault charge shape 2",
                }
            ],
            "payment_source": {
                "token": {
                    "id": vault_token_id,
                    "type": "PAYMENT_METHOD_TOKEN",
                }
            },
        }

        req_id_2 = _request_id()
        resp_2 = client.post(
            f"{BASE_URL}/v2/checkout/orders",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "PayPal-Request-Id": req_id_2,
            },
            json=order_body_2,
        )
        _log_response("Shape 2 – Create Order", resp_2)

        if resp_2.status_code in (200, 201):
            d = resp_2.json()
            shape2_order_id = d.get("id")
            status = d.get("status")
            print(f"  Shape 2 order created: {shape2_order_id}, status: {status}")

            if status == "COMPLETED":
                for pu in d.get("purchase_units", []):
                    for c in pu.get("payments", {}).get("captures", []):
                        shape1_capture_id = c["id"]  # reuse var for the return
                print(f"  ✓ Shape 2 auto-captured! Capture ID: {shape1_capture_id}")
                results["shape2"] = "SUCCESS_AUTO_CAPTURED"
                results["working_shape"] = "shape2_token"
                shape1_order_id = shape2_order_id
            elif status == "APPROVED":
                print("  Shape 2 order APPROVED, capturing...")
                cap_resp = client.post(
                    f"{BASE_URL}/v2/checkout/orders/{shape2_order_id}/capture",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "PayPal-Request-Id": _request_id(),
                    },
                )
                _log_response("Shape 2 – Capture", cap_resp)
                if cap_resp.status_code in (200, 201):
                    cd = cap_resp.json()
                    for pu in cd.get("purchase_units", []):
                        for c in pu.get("payments", {}).get("captures", []):
                            shape1_capture_id = c["id"]
                    print(f"  ✓ Shape 2 captured! Capture ID: {shape1_capture_id}")
                    results["shape2"] = "SUCCESS_MANUAL_CAPTURE"
                    results["working_shape"] = "shape2_token"
                    shape1_order_id = shape2_order_id
                else:
                    results["shape2"] = f"CAPTURE_FAILED_{cap_resp.status_code}"
            elif status == "CREATED":
                print("  Shape 2 returned CREATED — needs approval (vault not working)")
                results["shape2"] = "CREATED_NEEDS_APPROVAL"
            else:
                results["shape2"] = f"UNEXPECTED_STATUS_{status}"
        else:
            results["shape2"] = f"CREATE_FAILED_{resp_2.status_code}"
            print(f"  ✗ Shape 2 failed: {resp_2.status_code}")

    # Summary
    print(f"\n{'─'*60}")
    print("VAULT TOKEN CHARGE RESULTS:")
    print(json.dumps(results, indent=2))
    if results["working_shape"]:
        print(f"✓ Working shape: {results['working_shape']}")
    else:
        print("✗ NEITHER SHAPE WORKED. Fallback: pre-approved sandbox buyer.")
        print("  Report this and wait for decision before proceeding.")
    print(f"{'─'*60}")

    return shape1_order_id, shape1_capture_id, results


# ── Step D: Partial refund (60%) ───────────────────────────────

def partial_refund(
    client: httpx.Client,
    token: str,
    capture_id: str,
    original_cents: int,
    order_id: str | None = None,
) -> str | None:
    """Refund 60% of the capture. Returns refund_id or None."""
    refund_cents = int(Decimal(original_cents) * Decimal("0.60"))
    refund_str = _amount_str(refund_cents)

    _log("D", f"Refunding 60% of capture {capture_id}: ${refund_str}")

    req_id = _request_id()
    refund_body = {
        "amount": {
            "currency_code": "USD",
            "value": refund_str,
        },
        "note_to_payer": "Mandate gate test – partial refund (60%)",
    }

    resp = client.post(
        f"{BASE_URL}/v2/payments/captures/{capture_id}/refund",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": req_id,
        },
        json=refund_body,
    )
    _log_response("Partial Refund", resp)

    if resp.status_code not in (200, 201):
        _inspect_order_on_error(client, token, order_id, resp, "Partial Refund")
        return None

    data = resp.json()
    refund_id = data.get("id")
    status = data.get("status")
    print(f"✓ Refund ID: {refund_id}")
    print(f"✓ Refund status: {status}")
    print(f"✓ PayPal-Request-Id: {req_id}")
    return refund_id


# ── Step E: Webhook test ───────────────────────────────────────

def test_webhook_setup(client: httpx.Client, token: str):
    """Check that the webhook is registered and report its config."""
    _log("E", "Checking webhook configuration")

    if not PAYPAL_WEBHOOK_ID:
        print("⚠ PAYPAL_WEBHOOK_ID not set. Skipping webhook verification.")
        print("  You can register a webhook manually at developer.paypal.com")
        print("  or via the API after setting up your tunnel.")
        return

    resp = client.get(
        f"{BASE_URL}/v1/notifications/webhooks/{PAYPAL_WEBHOOK_ID}",
        headers={"Authorization": f"Bearer {token}"},
    )
    _log_response("Webhook Details", resp)

    if resp.status_code == 200:
        data = resp.json()
        print(f"✓ Webhook ID: {data.get('id')}")
        print(f"✓ URL: {data.get('url')}")
        print(f"✓ Event types: {[e['name'] for e in data.get('event_types', [])]}")
    else:
        print(f"⚠ Could not fetch webhook {PAYPAL_WEBHOOK_ID}: {resp.status_code}")
        print("  The webhook events from steps B/C/D may still arrive if the")
        print("  webhook was registered correctly. Check your tunnel logs.")

    print("\n  NOTE: Webhook events from the capture and refund above")
    print("  should arrive at your tunnel endpoint shortly.")
    print("  Expected events:")
    print("    - PAYMENT.CAPTURE.COMPLETED (from step B and/or C)")
    print("    - PAYMENT.CAPTURE.REFUNDED (from step D)")
    print("    - VAULT.PAYMENT-TOKEN.CREATED (from step B)")
    print("  NOTE: Simulator events FAIL signature verification.")
    print("  Real sandbox transactions must be used for webhook testing.")


# ── Channel3 test ──────────────────────────────────────────────

def test_channel3():
    """Test the Channel3 catalog API if CATALOG_API_KEY is set."""
    _log("CHANNEL3", "Testing Channel3 catalog API")

    if not CATALOG_API_KEY:
        print("⚠ CATALOG_API_KEY not set. Will use mock catalog as fallback.")
        print("  Set it in .env to test Channel3 integration.")
        return

    with httpx.Client(timeout=15.0) as client:
        try:
            resp = client.get(
                "https://api.trychannel3.com/v1/search",
                headers={"x-api-key": CATALOG_API_KEY},
                params={"query": "standing desk", "limit": 1},
            )
            _log_response("Channel3 Search", resp)

            if resp.status_code == 200:
                data = resp.json()
                results = data if isinstance(data, list) else data.get("results", data.get("data", []))
                if results:
                    item = results[0] if isinstance(results, list) else results
                    print("✓ Channel3 returned data. Checking fields needed for checks 5, 6, 9:")
                    print(f"  Has 'price': {'price' in str(item)}")
                    print(f"  Has 'category': {'category' in str(item)}")
                    print(f"  Sample item keys: {list(item.keys()) if isinstance(item, dict) else 'N/A'}")
                    print(f"  Full sample item:")
                    print(json.dumps(item, indent=2)[:2000])
                else:
                    print("⚠ Channel3 returned empty results.")
            else:
                print(f"✗ Channel3 returned status {resp.status_code}")
        except Exception as e:
            print(f"✗ Channel3 request failed: {e}")


# ── Main ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Mandate Phase 0 Gate Test")
    parser.add_argument(
        "--skip-webhook",
        action="store_true",
        help="Skip webhook check (Step E) to run only steps (a)-(d)",
    )
    parser.add_argument(
        "--no-vault",
        action="store_true",
        help="Run plain $5.00 order test without vault attributes (order -> capture -> 60%% refund)",
    )
    args = parser.parse_args()

    print(f"\n{'#'*60}")
    print(f"  MANDATE — Phase 0 Gate Test")
    print(f"  Environment: {PAYPAL_ENV}")
    print(f"  Base URL: {BASE_URL}")
    if args.no_vault:
        print("  Mode: PLAIN ORDER FLOW (--no-vault)")
    if args.skip_webhook:
        print("  Mode: Webhook check skipped (--skip-webhook)")
    print(f"{'#'*60}\n")

    # Test Channel3 first (independent of PayPal)
    test_channel3()

    with httpx.Client(timeout=30.0) as client:
        # Step 0: OAuth
        access_token, expires_in = get_access_token(client)

        # ── Plain order flow (--no-vault) ──────────────────────
        if args.no_vault:
            print("\n--- RUNNING PLAIN ORDER TEST (--no-vault) ---")
            order_id, approve_link = create_plain_order(client, access_token)

            print("\n" + "=" * 60)
            input(
                "Press ENTER after you have approved the payment in your browser...\n"
                "> "
            )

            # Refresh token in case it expired while waiting
            access_token, _ = get_access_token(client)

            # Capture order
            capture_id, _ = capture_order(client, access_token, order_id)

            # Refund 60% ($3.00)
            refund_id = partial_refund(
                client, access_token, capture_id, 500, order_id=order_id
            )

            # Webhook check (if not skipped)
            if args.skip_webhook:
                print("\n" + "=" * 60)
                print("[E] Skipping webhook verification (--skip-webhook)")
                print("=" * 60)
            else:
                test_webhook_setup(client, access_token)

            # Summary for plain order flow
            print(f"\n\n{'#'*60}")
            print("GATE TEST SUMMARY (--no-vault)")
            print(f"{'#'*60}")
            print(f"  OAuth token lifetime:   {expires_in}s")
            print(f"  Order created:          {'✓' if order_id else '✗'} {order_id or ''}")
            print(f"  Capture:                {'✓' if capture_id else '✗'} {capture_id or ''}")
            print(f"  Partial refund (60%):   {'✓' if refund_id else '✗'} {refund_id or ''}")
            if args.skip_webhook:
                print("  Webhook configured:     (skipped via --skip-webhook)")
            else:
                print(f"  Webhook configured:     {'✓' if PAYPAL_WEBHOOK_ID else '⚠ not set'}")
            print(f"  Channel3 API key:       {'✓ set' if CATALOG_API_KEY else '⚠ not set (mock fallback)'}")
            print(f"\n  PLAIN FLOW STATUS:      {'PASS' if (order_id and capture_id and refund_id) else 'FAIL'}")
            print(f"{'#'*60}\n")
            return

        # ── Default Vault Flow ─────────────────────────────────
        # Step A: Create vault order
        order_id, approve_link = create_vault_order(client, access_token)

        # Wait for human to approve
        print("\n" + "=" * 60)
        input(
            "Press ENTER after you have approved the payment in your browser...\n"
            "> "
        )

        # Refresh token in case it expired while waiting
        access_token, _ = get_access_token(client)

        # Step B: Capture
        capture_id_1, vault_token_id = capture_order(client, access_token, order_id)

        if not vault_token_id:
            print("\n⚠ No vault token extracted from capture response.")
            print("  Waiting 10 seconds for webhook delivery...")
            time.sleep(10)
            print("  If the VAULT.PAYMENT-TOKEN.CREATED webhook arrived at your")
            print("  tunnel, paste the vault token ID here (or press Enter to skip):")
            manual_token = input("> ").strip()
            if manual_token:
                vault_token_id = manual_token
            else:
                print("  Skipping vault charge test (Step C). Will note fallback needed.")

        # Step C: Charge with vault token
        vault_order_id = None
        vault_capture_id = None
        vault_results = {}
        if vault_token_id:
            vault_order_id, vault_capture_id, vault_results = charge_with_vault_token(
                client, access_token, vault_token_id
            )

        # Step D: Partial refund
        # Refund from whichever capture succeeded (vault charge preferred, else initial)
        refund_capture_id = vault_capture_id or capture_id_1
        refund_original_cents = 300 if vault_capture_id else 500  # $3.00 or $5.00
        refund_id = None
        if refund_capture_id:
            refund_id = partial_refund(
                client,
                access_token,
                refund_capture_id,
                refund_original_cents,
                order_id=vault_order_id or order_id,
            )

        # Step E: Webhook check
        if args.skip_webhook:
            print("\n" + "=" * 60)
            print("[E] Skipping webhook verification (--skip-webhook)")
            print("=" * 60)
        else:
            test_webhook_setup(client, access_token)

    # ── Final report ──────────────────────────────────────────
    print(f"\n\n{'#'*60}")
    print("GATE TEST SUMMARY")
    print(f"{'#'*60}")
    print(f"  OAuth token lifetime (UNVERIFIED): {expires_in}s")
    print(f"  Order created:          {'✓' if order_id else '✗'} {order_id or ''}")
    print(f"  Capture (initial):      {'✓' if capture_id_1 else '✗'} {capture_id_1 or ''}")
    print(f"  Vault token extracted:  {'✓' if vault_token_id else '✗'} {vault_token_id or ''}")
    print(f"  Vault charge (no browser): {'✓' if vault_capture_id else '✗'} {vault_capture_id or ''}")
    if vault_results:
        print(f"    Shape 1 (vault_id):   {vault_results.get('shape1', 'NOT_TESTED')}")
        print(f"    Shape 2 (token):      {vault_results.get('shape2', 'NOT_TESTED')}")
        print(f"    Working shape:        {vault_results.get('working_shape', 'NONE')}")
    if args.skip_webhook:
        print("  Webhook configured:     (skipped via --skip-webhook)")
    else:
        print(f"  Webhook configured:     {'✓' if PAYPAL_WEBHOOK_ID else '⚠ not set'}")
    print(f"  Channel3 API key:       {'✓ set' if CATALOG_API_KEY else '⚠ not set (mock fallback)'}")

    # Determine overall status
    critical_pass = all([order_id, capture_id_1])
    vault_pass = bool(vault_capture_id)
    refund_pass = bool(refund_id)

    print(f"\n  CRITICAL (order+capture):  {'PASS' if critical_pass else 'FAIL'}")
    print(f"  VAULT (no-browser charge): {'PASS' if vault_pass else 'FAIL – need fallback decision'}")
    print(f"  REFUND (partial):          {'PASS' if refund_pass else 'FAIL'}")

    if not vault_pass:
        print(f"\n  ⚠ DECISION NEEDED: Vault charging failed.")
        print(f"    Option A: Debug vault setup (check Vault enabled on sandbox app)")
        print(f"    Option B: Use pre-approved sandbox buyer fallback")
        print(f"    Waiting for your decision before Phase 1.")

    print(f"\n{'#'*60}")
    print("Paste the full output above when reporting results.")
    print(f"{'#'*60}\n")


if __name__ == "__main__":
    main()
