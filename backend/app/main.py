"""FastAPI application entry point."""

import json
import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response

from backend.app.config import (
    PROJECT_NAME,
    PAYPAL_BASE_URL,
    PAYPAL_CLIENT_ID,
    PAYPAL_CLIENT_SECRET,
    PAYPAL_WEBHOOK_ID,
)

app = FastAPI(title=PROJECT_NAME, version="0.1.0")

# ── Webhook log directory (gitignored) ─────────────────────────
_log_dir = Path("./logs")
_log_dir.mkdir(exist_ok=True)

_wh_logger = logging.getLogger("mandate.webhooks")
_wh_handler = logging.FileHandler(_log_dir / "webhooks.log")
_wh_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
_wh_logger.addHandler(_wh_handler)
_wh_logger.setLevel(logging.INFO)

# ── In-memory event dedup (will be DB-backed in Phase 1) ───────
_seen_event_ids: set[str] = set()


# ── Helper: get a fresh PayPal OAuth token ─────────────────────
async def _get_paypal_token() -> str:
    """OAuth 2.0 client-credentials token for webhook verification."""
    creds = base64.b64encode(
        f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}".encode()
    ).decode()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{PAYPAL_BASE_URL}/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content="grant_type=client_credentials",
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


# ── Routes ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "project": PROJECT_NAME}


@app.post("/webhooks/paypal")
async def receive_webhook(request: Request):
    """
    Receive PayPal webhook events.
    (a) Log raw body + headers to ./logs/ (gitignored).
    (b) Verify signature via PayPal POST /v1/notifications/verify-webhook-signature.
    (c) Dedup on event_id; return 200 only if verification_status == SUCCESS.
    """
    # ── Parse body ─────────────────────────────────────────────
    raw_body = await request.body()
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        _wh_logger.error("Invalid JSON body received")
        return Response(status_code=400, content="Invalid JSON")

    event_id = event.get("id", "unknown")
    event_type = event.get("event_type", "unknown")

    # ── (a) Log to file ───────────────────────────────────────
    paypal_headers = {
        "PayPal-Auth-Algo": request.headers.get("paypal-auth-algo", ""),
        "PayPal-Cert-Url": request.headers.get("paypal-cert-url", ""),
        "PayPal-Transmission-Id": request.headers.get("paypal-transmission-id", ""),
        "PayPal-Transmission-Sig": request.headers.get("paypal-transmission-sig", ""),
        "PayPal-Transmission-Time": request.headers.get("paypal-transmission-time", ""),
    }

    log_entry = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "event_id": event_id,
        "event_type": event_type,
        "headers": paypal_headers,
        "body": event,
    }
    _wh_logger.info(json.dumps(log_entry))

    # Write individual event file for easy inspection
    (_log_dir / f"webhook_{event_id}.json").write_text(
        json.dumps(log_entry, indent=2), encoding="utf-8"
    )

    # ── (c) Dedup on event_id ─────────────────────────────────
    if event_id in _seen_event_ids:
        _wh_logger.info(f"Duplicate event_id={event_id}, skipping")
        return {"status": "duplicate", "event_id": event_id}

    # ── (b) Verify signature via PayPal API ───────────────────
    if not PAYPAL_WEBHOOK_ID:
        _wh_logger.warning("PAYPAL_WEBHOOK_ID not configured; cannot verify")
        return Response(status_code=500, content="Webhook ID not configured")

    try:
        access_token = await _get_paypal_token()
    except Exception as exc:
        _wh_logger.error(f"OAuth token error during verification: {exc}")
        return Response(status_code=500, content="Token error")

    verify_payload = {
        "auth_algo": paypal_headers["PayPal-Auth-Algo"],
        "cert_url": paypal_headers["PayPal-Cert-Url"],
        "transmission_id": paypal_headers["PayPal-Transmission-Id"],
        "transmission_sig": paypal_headers["PayPal-Transmission-Sig"],
        "transmission_time": paypal_headers["PayPal-Transmission-Time"],
        "webhook_id": PAYPAL_WEBHOOK_ID,
        "webhook_event": event,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        verify_resp = await client.post(
            f"{PAYPAL_BASE_URL}/v1/notifications/verify-webhook-signature",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=verify_payload,
        )

    if verify_resp.status_code != 200:
        _wh_logger.error(
            f"Verification API returned {verify_resp.status_code}: "
            f"{verify_resp.text[:500]}"
        )
        return Response(status_code=500, content="Verification API error")

    verification_status = verify_resp.json().get("verification_status", "")
    _wh_logger.info(
        f"event_id={event_id}  type={event_type}  "
        f"verification={verification_status}"
    )

    if verification_status == "SUCCESS":
        _seen_event_ids.add(event_id)
        return {
            "status": "verified",
            "event_id": event_id,
            "event_type": event_type,
        }

    _wh_logger.warning(f"Verification FAILED for event_id={event_id}")
    return Response(status_code=403, content="Verification failed")
