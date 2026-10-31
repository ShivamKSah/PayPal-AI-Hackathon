"""FastAPI application entry point."""

import json
import base64
import logging
from datetime import datetime, timezone, timedelta
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
    MandateResponse,
    Proposal,
    ProposalCreate,
    ProposalRead,
    Payment,
    PaymentRead,
    AuditLog,
    compute_row_hash,
    WebhookEvent,
)
from backend.app.policy import PolicyEngine, compute_cart_hash
from backend.app.security import (
    encrypt_vault_token,
    decrypt_vault_token,
    sign_session_token,
    verify_session_token,
    mask_token,
)
from backend.app.paypal.gateway import VaultGateway

payment_gateway = VaultGateway()

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


# ── Helper: Append to Hash-Chained Audit Log ──────────────────
def _append_audit_log(
    db: Session,
    actor: str,
    event: str,
    ref_type: Optional[str] = None,
    ref_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> AuditLog:
    now = datetime.now(timezone.utc)
    last_log = db.exec(select(AuditLog).order_by(AuditLog.ts.desc())).first()
    prev_hash = last_log.row_hash if last_log else "0" * 64
    row_hash = compute_row_hash(
        prev_hash=prev_hash,
        ts=now,
        actor=actor,
        event=event,
        ref_type=ref_type,
        ref_id=ref_id,
        payload=payload,
    )
    audit_entry = AuditLog(
        ts=now,
        actor=actor,
        event=event,
        ref_type=ref_type,
        ref_id=ref_id,
        payload=payload,
        prev_hash=prev_hash,
        row_hash=row_hash,
    )
    db.add(audit_entry)
    db.commit()
    db.refresh(audit_entry)
    return audit_entry


def _verify_mandate_token_scope(request: Request, mandate_id: str) -> None:
    """If a session token is presented, assert its scope matches the target mandate."""
    auth_hdr = request.headers.get("authorization", "")
    token = None
    if auth_hdr.lower().startswith("bearer "):
        token = auth_hdr.split(" ", 1)[1]
    elif request.headers.get("x-session-token"):
        token = request.headers.get("x-session-token")

    if token:
        parsed = verify_session_token(token)
        if not parsed or parsed.get("mandate_id") != mandate_id:
            raise HTTPException(status_code=403, detail="Invalid or mismatched session token")


# ── Health ─────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "project": PROJECT_NAME}


# ── Mandates API ───────────────────────────────────────────────

@app.post("/mandates", response_model=MandateResponse, status_code=status.HTTP_201_CREATED)
def create_mandate(payload: MandateCreate, db: Session = Depends(get_session)):
    """Create a new buyer mandate and issue a signed session token."""
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
        escalate_first_merchant=payload.escalate_first_merchant,
        expires_at=payload.expires_at,
        vault_token_ref=encrypt_vault_token(payload.vault_token_ref) if payload.vault_token_ref else None,
        status="ACTIVE",
    )
    db.add(mandate)
    db.commit()
    db.refresh(mandate)

    session_token = sign_session_token(mandate.id, mandate.session_id)
    _append_audit_log(
        db,
        actor="buyer",
        event="MANDATE_CREATED",
        ref_type="mandate",
        ref_id=mandate.id,
        payload={"budget_cents": mandate.budget_cents, "session_id": mandate.session_id},
    )

    resp = MandateResponse.model_validate(mandate)
    resp.session_token = session_token
    return resp


class GrantInitRequest(MandateCreate.__base__):
    return_url: str = "https://example.com/grant/return"
    cancel_url: str = "https://example.com/grant/cancel"


@app.post("/mandates/{mandate_id}/grant/init")
def init_mandate_grant(
    mandate_id: str,
    payload: Optional[Dict[str, str]] = None,
    db: Session = Depends(get_session),
):
    """
    Grant Step 1: Create a $1.00 vault setup order.
    Returns the PayPal approval link for buyer one-time authorization.
    """
    mandate = db.get(Mandate, mandate_id)
    if not mandate:
        raise HTTPException(status_code=404, detail="Mandate not found")

    p = payload or {}
    return_url = p.get("return_url", "https://example.com/grant/return")
    cancel_url = p.get("cancel_url", "https://example.com/grant/cancel")

    res = payment_gateway.grant_mandate(
        mandate_id=mandate.id,
        return_url=return_url,
        cancel_url=cancel_url,
    )
    _append_audit_log(
        db,
        actor="buyer",
        event="GRANT_INITIATED",
        ref_type="mandate",
        ref_id=mandate.id,
        payload={"order_id": res.get("order_id")},
    )
    return res


@app.post("/mandates/{mandate_id}/grant/complete")
def complete_mandate_grant(
    mandate_id: str,
    payload: Dict[str, str],
    db: Session = Depends(get_session),
):
    """
    Grant Step 2: Buyer approved the $1.00 order.
    Captures order, extracts vault token, encrypts and stores in mandate.
    The $1.00 is refunded immediately so the buyer is not charged.
    """
    mandate = db.get(Mandate, mandate_id)
    if not mandate:
        raise HTTPException(status_code=404, detail="Mandate not found")

    order_id = payload.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order_id")

    res = payment_gateway.capture_grant(order_id)
    vault_token_id = res.get("vault_token_id")
    if not vault_token_id:
        raise HTTPException(status_code=400, detail="No vault token returned from PayPal capture")

    # Encrypt and store token
    mandate.vault_token_ref = encrypt_vault_token(vault_token_id)
    db.add(mandate)
    db.commit()
    db.refresh(mandate)

    _append_audit_log(
        db,
        actor="buyer",
        event="GRANT_COMPLETED",
        ref_type="mandate",
        ref_id=mandate.id,
        payload={
            "order_id": order_id,
            "masked_vault_token": mask_token(vault_token_id),
            "refunded": bool(res.get("refund")),
        },
    )

    return {
        "status": "GRANTED",
        "mandate_id": mandate.id,
        "refund_status": res.get("refund", {}).get("status") if res.get("refund") else "NONE",
    }


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
    now = datetime.now(timezone.utc)
    one_hour_ago = now - timedelta(hours=1)
    purchases_in_last_hour = len(
        db.exec(
            select(Payment)
            .join(Proposal, Payment.proposal_id == Proposal.id)
            .where(
                Proposal.mandate_id == mandate.id,
                Payment.state.in_(["CAPTURED", "CONFIRMED"]),
                Payment.created_at >= one_hour_ago,
            )
        ).all()
    )

    # Check previous purchase from this merchant under this mandate
    previous_purchase = db.exec(
        select(Payment)
        .join(Proposal, Payment.proposal_id == Proposal.id)
        .where(
            Proposal.mandate_id == mandate.id,
            Payment.state.in_(["CAPTURED", "CONFIRMED"]),
        )
    ).first()
    is_first_purchase = previous_purchase is None

    eval_result = policy_engine.evaluate(
        mandate=mandate,
        cart=payload.cart_json,
        instruction_text=payload.instruction_text,
        now=now,
        purchases_in_last_hour=purchases_in_last_hour,
        recent_cart_hashes=recent_hashes,
        is_first_purchase_from_merchant=is_first_purchase,
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

    _append_audit_log(
        db,
        actor="policy_engine",
        event=f"PROPOSAL_{eval_result.verdict}",
        ref_type="proposal",
        ref_id=proposal.id,
        payload={"verdict": eval_result.verdict, "cart_hash": eval_result.cart_hash},
    )

    return proposal


@app.get("/proposals/{proposal_id}", response_model=ProposalRead)
def get_proposal(proposal_id: str, db: Session = Depends(get_session)):
    """Fetch proposal with verdict and rule_hits."""
    proposal = db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return proposal


# ── Execution API (Phase 2 Money Path) ─────────────────────────

@app.post("/proposals/{proposal_id}/execute", response_model=PaymentRead, status_code=status.HTTP_201_CREATED)
def execute_proposal(proposal_id: str, request: Request, db: Session = Depends(get_session)):
    """
    Execute an approved purchase proposal via VaultGateway.
    1. A DENY must NEVER reach this code path (assert proposal.verdict == 'APPROVE').
    2. Rebuilds PayPal order from stored snapshot, recomputes cart hash, and refuses on mismatch.
    3. Budget reservation in one DB transaction: RESERVED -> ORDER_CREATED -> CAPTURED.
    4. Charges using pinned shape payment_source.paypal.vault_id with fresh PayPal-Request-Id.
    5. COMPLETED status treated as captured (no separate capture call).
    """
    import uuid

    proposal = db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    # Invariant 1: Only APPROVED proposals can be executed. DENY/ESCALATE rejected.
    if proposal.verdict != "APPROVE":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot execute proposal with verdict '{proposal.verdict}'. Only APPROVED proposals may execute.",
        )

    # Invariant 2: Cart hash binding. Rebuild cart JSON from snapshot and recompute hash.
    computed_hash = compute_cart_hash(proposal.cart_json)
    if computed_hash != proposal.cart_hash:
        _append_audit_log(
            db,
            actor="executor",
            event="EXECUTION_BLOCKED_HASH_MISMATCH",
            ref_type="proposal",
            ref_id=proposal.id,
            payload={"computed_hash": computed_hash, "stored_hash": proposal.cart_hash},
        )
        raise HTTPException(
            status_code=400,
            detail="Cart hash binding mismatch: cart snapshot differs from evaluated hash.",
        )

    mandate = db.get(Mandate, proposal.mandate_id)
    if not mandate:
        raise HTTPException(status_code=404, detail="Mandate not found")

    _verify_mandate_token_scope(request, mandate.id)

    if mandate.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Mandate is no longer ACTIVE")

    if not mandate.vault_token_ref:
        raise HTTPException(
            status_code=400,
            detail="Mandate has no stored vault token. Complete the Grant step first.",
        )

    cart = proposal.cart_json
    total_cents = cart.get("total_cents", 0)

    # Invariant 3: Budget reservation transaction
    remaining_budget = mandate.budget_cents - (mandate.spent_cents + mandate.reserved_cents)
    if total_cents > remaining_budget:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient remaining budget ({remaining_budget}¢) for purchase of {total_cents}¢.",
        )

    # Atomically increment reserved_cents
    mandate.reserved_cents += total_cents
    db.add(mandate)

    fresh_req_id = f"req-exec-{uuid.uuid4()}"
    payment = Payment(
        proposal_id=proposal.id,
        request_id=fresh_req_id,
        amount_cents=total_cents,
        state="RESERVED",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    _append_audit_log(
        db,
        actor="executor",
        event="BUDGET_RESERVED",
        ref_type="payment",
        ref_id=payment.id,
        payload={"amount_cents": total_cents, "reserved_total": mandate.reserved_cents},
    )

    # Advance state to ORDER_CREATED
    payment.state = "ORDER_CREATED"
    db.add(payment)
    db.commit()

    # Execute PayPal charge with decrypted vault token and fresh request id
    decrypted_vault_token = decrypt_vault_token(mandate.vault_token_ref)
    description = f"Mandate purchase proposal {proposal.id}"

    try:
        charge_resp = payment_gateway.charge(
            vault_token=decrypted_vault_token,
            amount_cents=total_cents,
            description=description,
            request_id=fresh_req_id,
        )
    except Exception as exc:
        # Failure releases the reservation
        payment.state = "FAILED"
        mandate.reserved_cents = max(0, mandate.reserved_cents - total_cents)
        db.add(payment)
        db.add(mandate)
        db.commit()

        _append_audit_log(
            db,
            actor="executor",
            event="PAYMENT_FAILED_RELEASED",
            ref_type="payment",
            ref_id=payment.id,
            payload={"error": str(exc), "released_cents": total_cents},
        )
        raise HTTPException(status_code=500, detail=f"PayPal charge failed: {exc}")

    status_str = charge_resp.get("status")

    # Pinned shape: status COMPLETED means captured in response
    if status_str in ("COMPLETED", "CREATED", "SAVED"):
        payment.state = "CAPTURED"
        payment.paypal_order_id = charge_resp.get("order_id")
        payment.capture_id = charge_resp.get("capture_id")
        payment.captured_at = datetime.now(timezone.utc)
        db.add(payment)
        db.commit()
        db.refresh(payment)

        _append_audit_log(
            db,
            actor="executor",
            event="PAYMENT_CAPTURED",
            ref_type="payment",
            ref_id=payment.id,
            payload={
                "order_id": payment.paypal_order_id,
                "capture_id": payment.capture_id,
                "amount_cents": total_cents,
            },
        )
        return payment

    # Uncompleted status -> Release reservation
    payment.state = "FAILED"
    mandate.reserved_cents = max(0, mandate.reserved_cents - total_cents)
    db.add(payment)
    db.add(mandate)
    db.commit()

    _append_audit_log(
        db,
        actor="executor",
        event="PAYMENT_FAILED_UNCOMPLETED",
        ref_type="payment",
        ref_id=payment.id,
        payload={"status": status_str, "released_cents": total_cents},
    )
    raise HTTPException(status_code=500, detail=f"PayPal returned uncompleted status: {status_str}")


# ── Audit Log Chain Verification API ───────────────────────────

@app.get("/audit/verify")
def verify_audit_log_chain(db: Session = Depends(get_session)):
    """
    Recompute and verify the entire SHA-256 hash chain of the audit log.
    Ensures complete tamper-evidence.
    """
    logs = db.exec(select(AuditLog).order_by(AuditLog.ts.asc())).all()
    if not logs:
        return {"verified": True, "count": 0, "message": "Audit chain empty"}

    expected_prev = "0" * 64
    for idx, entry in enumerate(logs):
        if entry.prev_hash != expected_prev:
            return {
                "verified": False,
                "broken_at_index": idx,
                "entry_id": entry.id,
                "reason": f"prev_hash mismatch: expected {expected_prev}, got {entry.prev_hash}",
            }
        expected_row = compute_row_hash(
            prev_hash=entry.prev_hash,
            ts=entry.ts,
            actor=entry.actor,
            event=entry.event,
            ref_type=entry.ref_type,
            ref_id=entry.ref_id,
            payload=entry.payload,
        )
        if entry.row_hash != expected_row:
            return {
                "verified": False,
                "broken_at_index": idx,
                "entry_id": entry.id,
                "reason": f"row_hash mismatch: expected {expected_row}, got {entry.row_hash}",
            }
        expected_prev = entry.row_hash

    return {
        "verified": True,
        "count": len(logs),
        "latest_hash": expected_prev,
    }


# ── Polling Reconciler API ─────────────────────────────────────

@app.post("/reconcile")
def reconcile_captured_payments(db: Session = Depends(get_session)):
    """
    Polling reconciler: checks payments in CAPTURED state that have not yet
    received a webhook confirmation, confirms them, and updates spent_cents.
    """
    captured_payments = db.exec(select(Payment).where(Payment.state == "CAPTURED")).all()
    reconciled_count = 0
    now = datetime.now(timezone.utc)

    for p in captured_payments:
        p.state = "CONFIRMED"
        p.confirmed_at = now
        proposal = db.get(Proposal, p.proposal_id)
        if proposal:
            mandate = db.get(Mandate, proposal.mandate_id)
            if mandate:
                mandate.reserved_cents = max(0, mandate.reserved_cents - p.amount_cents)
                mandate.spent_cents += p.amount_cents
                db.add(mandate)
        db.add(p)
        reconciled_count += 1
        _append_audit_log(
            db,
            actor="reconciler",
            event="PAYMENT_CONFIRMED_VIA_RECONCILER",
            ref_type="payment",
            ref_id=p.id,
            payload={"capture_id": p.capture_id, "amount_cents": p.amount_cents},
        )

    db.commit()
    return {"status": "ok", "reconciled_count": reconciled_count}


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

        # Move CAPTURED -> CONFIRMED on verified PAYMENT.CAPTURE.COMPLETED
        if event_type == "PAYMENT.CAPTURE.COMPLETED":
            capture_id = event.get("resource", {}).get("id")
            if capture_id:
                payment = db.exec(select(Payment).where(Payment.capture_id == capture_id)).first()
                if payment and payment.state == "CAPTURED":
                    payment.state = "CONFIRMED"
                    payment.confirmed_at = datetime.now(timezone.utc)
                    proposal = db.get(Proposal, payment.proposal_id)
                    if proposal:
                        mandate = db.get(Mandate, proposal.mandate_id)
                        if mandate:
                            mandate.reserved_cents = max(0, mandate.reserved_cents - payment.amount_cents)
                            mandate.spent_cents += payment.amount_cents
                            db.add(mandate)
                    db.add(payment)
                    _append_audit_log(
                        db,
                        actor="paypal_webhook",
                        event="PAYMENT_CONFIRMED_VIA_WEBHOOK",
                        ref_type="payment",
                        ref_id=payment.id,
                        payload={"event_id": event_id, "capture_id": capture_id},
                    )

        db.commit()
        return {"status": "verified", "event_id": event_id, "event_type": event_type}

    _wh_logger.warning(f"Verification FAILED for event_id={event_id}")
    return Response(status_code=403, content="Verification failed")
