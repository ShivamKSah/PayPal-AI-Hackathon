"""FastAPI application entry point."""

import json
import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, Depends, HTTPException, status
from sqlmodel import Session, select

from backend.app.config import (
    PROJECT_NAME,
    PAYPAL_BASE_URL,
    PAYPAL_CLIENT_ID,
    PAYPAL_CLIENT_SECRET,
    PAYPAL_WEBHOOK_ID,
)
from backend.app.db import init_db, get_session
from backend.app.models import (
    Mandate,
    MandateCreate,
    MandateRead,
    Proposal,
    ProposalCreate,
    ProposalRead,
    AuditLog,
    compute_row_hash,
    WebhookEvent,
)
from backend.app.policy import PolicyEngine
from backend.app.security import encrypt_vault_token

# ── Lifespan for startup DB initialization ─────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title=PROJECT_NAME, version="0.1.0", lifespan=lifespan)

# ── Webhook log directory (gitignored) ─────────────────────────
_log_dir = Path("./logs")
_log_dir.mkdir(exist_ok=True)

_wh_logger = logging.getLogger("mandate.webhooks")
_wh_handler = logging.FileHandler(_log_dir / "webhooks.log")
_wh_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
_wh_logger.addHandler(_wh_handler)
_wh_logger.setLevel(logging.INFO)

policy_engine = PolicyEngine()


# ── Helper: OAuth token for webhook verification ──────────────
async def _get_paypal_token() -> str:
    creds = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}".encode()).decode()
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


# ── Health ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "project": PROJECT_NAME}


# ── Mandates API ───────────────────────────────────────────────

@app.post("/mandates", response_model=MandateRead, status_code=status.HTTP_201_CREATED)
def create_mandate(payload: MandateCreate, db: Session = Depends(get_session)):
    """Create a new buyer mandate."""
    mandate = Mandate(
        session_id=payload.session_id,
        budget_cents=payload.budget_cents,
        per_txn_cap_cents=payload.per_txn_cap_cents,
        categories=payload.categories,
        merchant_allowlist=payload.merchant_allowlist,
        allow_new_merchants=payload.allow_new_merchants,
        allow_recurring=payload.allow_recurring,
        velocity_per_hour=payload.velocity_per_hour,
        near_limit_pct=payload.near_limit_pct,
        expires_at=payload.expires_at,
        vault_token_ref=encrypt_vault_token(payload.vault_token_ref) if payload.vault_token_ref else None,
        status="ACTIVE",
    )
    db.add(mandate)
    db.commit()
    db.refresh(mandate)
    return mandate


@app.get("/mandates/{mandate_id}", response_model=MandateRead)
def get_mandate(mandate_id: str, db: Session = Depends(get_session)):
    """Fetch mandate details."""
    mandate = db.get(Mandate, mandate_id)
    if not mandate:
        raise HTTPException(status_code=404, detail="Mandate not found")
    return mandate


@app.post("/mandates/{mandate_id}/revoke", response_model=MandateRead)
def revoke_mandate(mandate_id: str, db: Session = Depends(get_session)):
    """Revoke an active mandate."""
    mandate = db.get(Mandate, mandate_id)
    if not mandate:
        raise HTTPException(status_code=404, detail="Mandate not found")
    mandate.status = "REVOKED"
    db.add(mandate)
    db.commit()
    db.refresh(mandate)
    return mandate


# ── Proposals API ──────────────────────────────────────────────

@app.post("/proposals", response_model=ProposalRead, status_code=status.HTTP_201_CREATED)
def submit_proposal(payload: ProposalCreate, db: Session = Depends(get_session)):
    """
    Submit a purchase proposal.
    Executes the policy engine (all 11 checks in Section 7 order)
    and stores verdict and full rule_hits breakdown.
    """
    mandate = db.get(Mandate, payload.mandate_id)
    if not mandate:
        raise HTTPException(status_code=404, detail="Mandate not found")

    # Fetch recent cart hashes for idempotency check (check 8)
    recent_proposals = db.exec(
        select(Proposal.cart_hash).where(Proposal.mandate_id == mandate.id)
    ).all()
    recent_hashes = set(recent_proposals)

    # Count recent purchases for velocity check (check 7)
    # (Count of approved proposals/payments in last hour)
    now = datetime.now(timezone.utc)
    purchases_in_last_hour = 0  # In Phase 1, starts at 0

    eval_result = policy_engine.evaluate(
        mandate=mandate,
        cart=payload.cart_json,
        instruction_text=payload.instruction_text,
        now=now,
        purchases_in_last_hour=purchases_in_last_hour,
        recent_cart_hashes=recent_hashes,
        is_first_purchase_from_merchant=False,
    )

    proposal = Proposal(
        mandate_id=mandate.id,
        instruction_text=payload.instruction_text,
        agent_reasoning=payload.agent_reasoning,
        cart_json=payload.cart_json,
        cart_hash=eval_result.cart_hash,
        catalog_snapshot=eval_result.catalog_snapshot,
        constraints=eval_result.constraints,
        verdict=eval_result.verdict,
        rule_hits=eval_result.rule_hits,
        reviewer="policy_engine",
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)

    # Append to hash-chained audit log
    last_log = db.exec(select(AuditLog).order_by(AuditLog.ts.desc())).first()
    prev_hash = last_log.row_hash if last_log else "0" * 64
    row_hash = compute_row_hash(
        prev_hash=prev_hash,
        ts=now,
        actor="policy_engine",
        event=f"PROPOSAL_{eval_result.verdict}",
        ref_type="proposal",
        ref_id=proposal.id,
        payload={"verdict": eval_result.verdict, "cart_hash": eval_result.cart_hash},
    )
    audit_entry = AuditLog(
        ts=now,
        actor="policy_engine",
        event=f"PROPOSAL_{eval_result.verdict}",
        ref_type="proposal",
        ref_id=proposal.id,
        payload={"verdict": eval_result.verdict, "cart_hash": eval_result.cart_hash},
        prev_hash=prev_hash,
        row_hash=row_hash,
    )
    db.add(audit_entry)
    db.commit()

    return proposal


@app.get("/proposals/{proposal_id}", response_model=ProposalRead)
def get_proposal(proposal_id: str, db: Session = Depends(get_session)):
    """Fetch proposal with verdict and rule_hits."""
    proposal = db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return proposal


# ── Webhook Handler ────────────────────────────────────────────

@app.post("/webhooks/paypal")
async def receive_webhook(request: Request, db: Session = Depends(get_session)):
    """
    Receive PayPal webhook events.
    (a) Log raw body + headers to ./logs/ (gitignored).
    (b) Verify signature via PayPal POST /v1/notifications/verify-webhook-signature.
    (c) Dedup on event_id via DB; return 200 only if verification_status == SUCCESS.
    """
    raw_body = await request.body()
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        _wh_logger.error("Invalid JSON body received")
        return Response(status_code=400, content="Invalid JSON")

    event_id = event.get("id", "unknown")
    event_type = event.get("event_type", "unknown")

    # (a) Log to file
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
    (_log_dir / f"webhook_{event_id}.json").write_text(json.dumps(log_entry, indent=2), encoding="utf-8")

    # (c) Dedup on event_id via DB
    existing_event = db.get(WebhookEvent, event_id)
    if existing_event:
        _wh_logger.info(f"Duplicate event_id={event_id}, skipping")
        return {"status": "duplicate", "event_id": event_id}

    # (b) Verify signature via PayPal API
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
        _wh_logger.error(f"Verification API returned {verify_resp.status_code}: {verify_resp.text[:500]}")
        return Response(status_code=500, content="Verification API error")

    verification_status = verify_resp.json().get("verification_status", "")
    _wh_logger.info(f"event_id={event_id}  type={event_type}  verification={verification_status}")

    if verification_status == "SUCCESS":
        webhook_rec = WebhookEvent(
            event_id=event_id,
            event_type=event_type,
            verified=True,
            received_at=datetime.now(timezone.utc),
        )
        db.add(webhook_rec)
        db.commit()
        return {"status": "verified", "event_id": event_id, "event_type": event_type}

    _wh_logger.warning(f"Verification FAILED for event_id={event_id}")
    return Response(status_code=403, content="Verification failed")
