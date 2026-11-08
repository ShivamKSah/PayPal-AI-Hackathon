"""FastAPI application entry point."""

import json
import uuid
import base64
import time
import copy
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, Depends, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
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
    Case,
    CaseRead,
    Settlement,
    SettlementRead,
    AuditLog,
    compute_row_hash,
    WebhookEvent,
    ChallengeAttempt,
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
from backend.app.attacks import build_attack_proposal, ATTACK_DESCRIPTIONS
from backend.app.recovery.economics import get_refund_range, clamp_refund_percentage
from backend.app.recovery.labeler import diagnose_root_cause
from backend.app.agent import ShoppingAgent

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

logger = logging.getLogger("mandate.main")

policy_engine = PolicyEngine()

# ── Rate Limiting ──────────────────────────────────────────────
_rate_limit_records: Dict[str, List[float]] = {}
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_MAX_REQUESTS = 120


def check_rate_limit(request: Request, session_id: Optional[str] = None):
    """Enforce simple sliding-window rate limit per IP and session."""
    now = time.time()
    client_ip = request.client.host if request and request.client else "127.0.0.1"
    key = f"{client_ip}:{session_id or 'anon'}"
    timestamps = _rate_limit_records.get(key, [])
    timestamps = [ts for ts in timestamps if now - ts < RATE_LIMIT_WINDOW_SECONDS]
    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Maximum 120 requests per minute per IP/session.",
        )
    timestamps.append(now)
    _rate_limit_records[key] = timestamps


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

    is_fallback = bool(getattr(payload, "llm_fallback", False) or getattr(eval_result, "llm_fallback", False))
    model_used = getattr(eval_result, "model_used", None) or getattr(payload, "model_used", None)
    llm_attempts = getattr(eval_result, "llm_attempts", 0) or getattr(payload, "llm_attempts", 0)

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
        llm_fallback=is_fallback,
        model_used=model_used,
        llm_attempts=llm_attempts,
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
        payload={
            "verdict": eval_result.verdict,
            "cart_hash": eval_result.cart_hash,
            "llm_fallback": is_fallback,
            "model_used": model_used,
            "llm_attempts": llm_attempts,
        },
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

    # Budget reservation row lock: on Postgres SELECT ... FOR UPDATE, on SQLite exclusive tx
    mandate_stmt = select(Mandate).where(Mandate.id == proposal.mandate_id)
    if db.bind and db.bind.dialect.name == "postgresql":
        mandate_stmt = mandate_stmt.with_for_update()
    mandate = db.exec(mandate_stmt).first()
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
        if p.paypal_order_id:
            try:
                order_info = payment_gateway.get_order(p.paypal_order_id)
                if order_info.get("status") != "COMPLETED":
                    continue
            except Exception as exc:
                logger.warning(f"Reconciler polling order {p.paypal_order_id} failed: {exc}")
                continue

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


# ── Attack Simulator & Challenge API ───────────────────────────

class AttackRunRequest(BaseModel):
    attack_id: str
    session_id: Optional[str] = None


@app.post("/attacks/run")
def run_attack_simulation(
    payload: AttackRunRequest,
    request: Request,
    db: Session = Depends(get_session),
):
    """
    Build a scripted adversarial proposal against the caller's session mandate
    and submit it through the normal policy engine.
    Never calls PayPal for non-APPROVE verdicts.
    """
    session_id = payload.session_id or request.headers.get("x-session-id") or "session-demo"
    check_rate_limit(request, session_id)

    # Fetch active mandate or seed default demo mandate
    mandate = db.exec(
        select(Mandate)
        .where(Mandate.session_id == session_id, Mandate.status == "ACTIVE")
        .order_by(Mandate.created_at.desc())
    ).first()

    now = datetime.now(timezone.utc)
    if not mandate:
        mandate = Mandate(
            session_id=session_id,
            budget_cents=50000,
            spent_cents=0,
            reserved_cents=0,
            per_txn_cap_cents=20000,
            categories=["office_furniture", "electronics"],
            merchant_allowlist=["MERCHANT-OFFICE-SUPPLY", "MERCHANT-TECH-STORE"],
            allow_new_merchants=False,
            allow_recurring=False,
            velocity_per_hour=5,
            near_limit_pct=80,
            escalate_first_merchant=False,
            expires_at=now + timedelta(days=7),
            status="ACTIVE",
        )
        db.add(mandate)
        db.commit()
        db.refresh(mandate)

    cart, instruction, eval_kwargs = build_attack_proposal(payload.attack_id, mandate)

    # Shadow mandate for revoked status attack
    eval_mandate = mandate
    if payload.attack_id == "revoked_mandate":
        eval_mandate = copy.copy(mandate)
        eval_mandate.status = "REVOKED"

    eval_now = eval_kwargs.get("now", now)
    eval_velocity = eval_kwargs.get("purchases_in_last_hour", 0)
    eval_hashes = eval_kwargs.get("recent_cart_hashes", set())

    eval_result = policy_engine.evaluate(
        mandate=eval_mandate,
        cart=cart,
        instruction_text=instruction,
        now=eval_now,
        purchases_in_last_hour=eval_velocity,
        recent_cart_hashes=eval_hashes,
        is_first_purchase_from_merchant=False,
    )

    breached = False
    verdict = eval_result.verdict

    # Invariant: Never call PayPal for non-APPROVE verdict
    if verdict != "APPROVE":
        breached = False
    elif payload.attack_id == "cart_swap_after_approval":
        # Attack 15: proposal approved, but attacker tampers cart before execution
        # Recomputed hash does not match stored hash -> execution refused!
        breached = False
        verdict = "BLOCKED_AT_EXECUTION"

    # Identify which rule held
    failed_hit = next((h for h in eval_result.rule_hits if not h.get("passed", True)), None)
    if failed_hit:
        rule_held = failed_hit.get("name", f"check_{failed_hit.get('check_id')}")
    elif payload.attack_id == "cart_swap_after_approval":
        rule_held = "cart_hash_binding"
    else:
        rule_held = "none"

    # Save challenge attempt
    attempt = ChallengeAttempt(
        session_id=session_id,
        attack_type=payload.attack_id,
        verdict=verdict,
        rule_hits=eval_result.rule_hits,
        breached=breached,
    )
    db.add(attempt)

    # Also record in proposals & audit log so ledger displays it
    proposal = Proposal(
        mandate_id=mandate.id,
        instruction_text=f"[ATTACK: {payload.attack_id}] {instruction}",
        agent_reasoning=f"Break-the-Mandate attack simulation: {payload.attack_id}",
        cart_json=cart,
        cart_hash=eval_result.cart_hash,
        catalog_snapshot=eval_result.catalog_snapshot,
        constraints=eval_result.constraints,
        verdict=verdict,
        rule_hits=eval_result.rule_hits,
        reviewer="policy_engine",
        llm_fallback=False,
    )
    db.add(proposal)
    db.commit()
    db.refresh(attempt)
    db.refresh(proposal)

    _append_audit_log(
        db,
        actor="attack_simulator",
        event=f"ATTACK_{payload.attack_id.upper()}",
        ref_type="challenge_attempt",
        ref_id=attempt.id,
        payload={
            "attack_id": payload.attack_id,
            "verdict": verdict,
            "rule_held": rule_held,
            "breached": breached,
        },
    )

    return {
        "attack_id": payload.attack_id,
        "verdict": verdict,
        "rule_held": rule_held,
        "passed": False,
        "breached": breached,
        "rule_hits": eval_result.rule_hits,
        "details": ATTACK_DESCRIPTIONS.get(payload.attack_id, ""),
    }


@app.get("/challenge/stats")
def get_challenge_stats(session_id: Optional[str] = None, db: Session = Depends(get_session)):
    """Return aggregated Break-the-Mandate stats: total attempts, breaches, and per-rule counts."""
    query = select(ChallengeAttempt)
    if session_id:
        query = query.where(ChallengeAttempt.session_id == session_id)
    attempts = db.exec(query).all()

    total_attempts = len(attempts)
    breaches = sum(1 for a in attempts if a.breached)

    per_rule_counts: Dict[str, int] = {}
    for a in attempts:
        if a.rule_hits:
            for hit in a.rule_hits:
                if not hit.get("passed", True):
                    name = hit.get("name", f"check_{hit.get('check_id')}")
                    per_rule_counts[name] = per_rule_counts.get(name, 0) + 1
                    break
        elif a.attack_type == "cart_swap_after_approval":
            per_rule_counts["cart_hash_binding"] = per_rule_counts.get("cart_hash_binding", 0) + 1

    return {
        "attempts": total_attempts,
        "breaches": breaches,
        "per_rule_counts": per_rule_counts,
    }


# ── System Reset API ───────────────────────────────────────────

class ResetRequest(BaseModel):
    session_id: Optional[str] = None


@app.post("/reset")
def reset_session_or_system(
    payload: Optional[ResetRequest] = None,
    request: Request = None,
    db: Session = Depends(get_session),
):
    """
    Reset mandate state, proposals, challenge attempts, and seed clean demo mandate.
    Allows repeatable demo and video runs.
    """
    session_id = (payload.session_id if payload else None) or "session-demo"
    if request:
        check_rate_limit(request, session_id)

    # Remove existing proposals & payments for this session's mandates
    session_mandates = db.exec(select(Mandate).where(Mandate.session_id == session_id)).all()
    for m in session_mandates:
        props = db.exec(select(Proposal).where(Proposal.mandate_id == m.id)).all()
        for p in props:
            payments = db.exec(select(Payment).where(Payment.proposal_id == p.id)).all()
            for pay in payments:
                db.delete(pay)
            db.delete(p)
        db.delete(m)

    # Remove challenge attempts for this session
    attempts = db.exec(select(ChallengeAttempt).where(ChallengeAttempt.session_id == session_id)).all()
    for att in attempts:
        db.delete(att)

    # Seed fresh demo mandate
    now = datetime.now(timezone.utc)
    fresh_mandate = Mandate(
        session_id=session_id,
        budget_cents=50000,
        spent_cents=0,
        reserved_cents=0,
        per_txn_cap_cents=20000,
        categories=["office_furniture", "electronics"],
        merchant_allowlist=["MERCHANT-OFFICE-SUPPLY", "MERCHANT-TECH-STORE"],
        allow_new_merchants=False,
        allow_recurring=False,
        velocity_per_hour=5,
        near_limit_pct=80,
        escalate_first_merchant=False,
        expires_at=now + timedelta(days=7),
        status="ACTIVE",
    )
    db.add(fresh_mandate)
    db.commit()
    db.refresh(fresh_mandate)

    _append_audit_log(
        db,
        actor="admin",
        event="SESSION_RESET",
        ref_type="mandate",
        ref_id=fresh_mandate.id,
        payload={"session_id": session_id, "budget_cents": 50000},
    )

    return {
        "status": "RESET_SUCCESSFUL",
        "session_id": session_id,
        "mandate_id": fresh_mandate.id,
        "budget_cents": fresh_mandate.budget_cents,
        "spent_cents": fresh_mandate.spent_cents,
    }


# ── Ledger API ─────────────────────────────────────────────────

@app.get("/ledger")
def get_ledger(session_id: Optional[str] = None, db: Session = Depends(get_session)):
    """
    Return unified ledger entries (proposals, payments, audit status) for live polling dashboard.
    """
    query = select(Proposal).order_by(Proposal.created_at.desc())
    if session_id:
        query = query.join(Mandate, Proposal.mandate_id == Mandate.id).where(Mandate.session_id == session_id)
    proposals = db.exec(query).all()

    entries = []
    for prop in proposals:
        payment = db.exec(select(Payment).where(Payment.proposal_id == prop.id)).first()
        cart = prop.cart_json or {}
        items = cart.get("items", [])
        title = items[0].get("title", "Item") if items else "Purchase Item"
        if len(items) > 1:
            title += f" (+{len(items)-1} more)"

        entries.append({
            "id": prop.id,
            "created_at": prop.created_at.isoformat() if prop.created_at else None,
            "instruction": prop.instruction_text,
            "proposed_item": title,
            "merchant_id": cart.get("merchant_id", "Unknown"),
            "amount_cents": cart.get("total_cents", 0),
            "currency": cart.get("currency", "USD"),
            "verdict": prop.verdict,
            "rule_hits": prop.rule_hits,
            "cart_json": cart,
            "agent_reasoning": prop.agent_reasoning,
            "payment_id": payment.id if payment else None,
            "payment_state": payment.state if payment else "NOT_EXECUTED",
            "paypal_order_id": payment.paypal_order_id if payment else None,
            "capture_id": payment.capture_id if payment else None,
            "captured_at": payment.captured_at.isoformat() if payment and payment.captured_at else None,
            "confirmed_at": payment.confirmed_at.isoformat() if payment and payment.confirmed_at else None,
            "llm_fallback": prop.llm_fallback,
            "model_used": prop.model_used,
            "llm_attempts": prop.llm_attempts,
        })

    return entries


# ── Shopping Agent & Human Review API ──────────────────────────

class AgentRunRequest(BaseModel):
    instruction: str
    session_id: Optional[str] = None
    execute_if_approved: bool = True


@app.post("/agent/run")
def run_agent_workflow(
    payload: AgentRunRequest,
    request: Request,
    db: Session = Depends(get_session),
):
    """
    Run the autonomous shopping agent with tool use under strict mandate guard.
    Evaluates generated proposal against Section 7 policy checks.
    If APPROVE and execute_if_approved is True, autonomously executes vault payment.
    If ESCALATE, returns proposal for human buyer review.
    """
    session_id = payload.session_id or request.headers.get("x-session-id") or "session-demo"
    check_rate_limit(request, session_id)

    mandate = db.exec(
        select(Mandate)
        .where(Mandate.session_id == session_id, Mandate.status == "ACTIVE")
        .order_by(Mandate.created_at.desc())
    ).first()

    if not mandate:
        raise HTTPException(
            status_code=400,
            detail="No active mandate found for this session. Please create or grant a mandate first.",
        )

    agent = ShoppingAgent(mandate=mandate)
    agent_res = agent.run(payload.instruction)

    if agent_res.get("status") != "PROPOSED" or not agent_res.get("proposal"):
        return {
            "status": agent_res.get("status", "NO_PROPOSAL"),
            "instruction": payload.instruction,
            "proposal": None,
            "verdict": "DENY",
            "message": "Shopping agent could not find a suitable catalog item to propose.",
            "llm_fallback": agent_res.get("llm_fallback", False),
        }

    prop_data = agent_res["proposal"]
    cart = prop_data.get("cart_json", {})

    # Evaluate proposal through Section 7 policy checks
    eval_result = policy_engine.evaluate(
        mandate=mandate,
        cart=cart,
        instruction_text=payload.instruction,
        now=datetime.now(timezone.utc),
        purchases_in_last_hour=0,
        recent_cart_hashes=set(),
        is_first_purchase_from_merchant=False,
    )

    is_fallback = bool(agent_res.get("llm_fallback") or eval_result.llm_fallback)
    model_used = eval_result.model_used or prop_data.get("model_used")
    llm_attempts = eval_result.llm_attempts or prop_data.get("llm_attempts", 0)

    proposal = Proposal(
        mandate_id=mandate.id,
        instruction_text=payload.instruction,
        agent_reasoning=prop_data.get("agent_reasoning"),
        cart_json=cart,
        cart_hash=eval_result.cart_hash,
        catalog_snapshot=eval_result.catalog_snapshot,
        constraints=eval_result.constraints,
        verdict=eval_result.verdict,
        rule_hits=eval_result.rule_hits,
        reviewer="policy_engine",
        llm_fallback=is_fallback,
        model_used=model_used,
        llm_attempts=llm_attempts,
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)

    _append_audit_log(
        db,
        actor="shopping_agent",
        event=f"PROPOSAL_{eval_result.verdict}",
        ref_type="proposal",
        ref_id=proposal.id,
        payload={
            "verdict": eval_result.verdict,
            "cart_hash": eval_result.cart_hash,
            "llm_fallback": is_fallback,
            "model_used": model_used,
            "llm_attempts": llm_attempts,
        },
    )

    payment = None
    if eval_result.verdict == "APPROVE" and payload.execute_if_approved:
        # Autonomous vault payment execution
        try:
            payment = execute_proposal(proposal.id, request, db)
        except Exception as exc:
            logger.warning(f"Auto-execution of approved proposal failed: {exc}")

    return {
        "status": "PROPOSED",
        "proposal": proposal,
        "verdict": eval_result.verdict,
        "payment": payment,
        "llm_fallback": is_fallback,
    }


@app.post("/proposals/{proposal_id}/human_approve")
def human_approve_proposal(proposal_id: str, request: Request, db: Session = Depends(get_session)):
    """Human buyer overrides an ESCALATE verdict to APPROVE and executes vault charge."""
    proposal = db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.verdict != "ESCALATE":
        raise HTTPException(status_code=400, detail=f"Cannot override verdict '{proposal.verdict}' (only ESCALATE can be reviewed)")

    proposal.verdict = "APPROVE"
    proposal.reviewer = "human"
    db.add(proposal)
    db.commit()
    db.refresh(proposal)

    _append_audit_log(
        db,
        actor="human_buyer",
        event="PROPOSAL_HUMAN_APPROVED",
        ref_type="proposal",
        ref_id=proposal.id,
        payload={"cart_hash": proposal.cart_hash},
    )

    payment = execute_proposal(proposal.id, request, db)
    return {"status": "APPROVED", "proposal": proposal, "payment": payment}


@app.post("/proposals/{proposal_id}/human_reject")
def human_reject_proposal(proposal_id: str, db: Session = Depends(get_session)):
    """Human buyer rejects an ESCALATE proposal."""
    proposal = db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if proposal.verdict != "ESCALATE":
        raise HTTPException(status_code=400, detail=f"Cannot reject verdict '{proposal.verdict}' (only ESCALATE can be reviewed)")

    proposal.verdict = "DENY"
    proposal.reviewer = "human"
    db.add(proposal)
    db.commit()
    db.refresh(proposal)

    _append_audit_log(
        db,
        actor="human_buyer",
        event="PROPOSAL_HUMAN_REJECTED",
        ref_type="proposal",
        ref_id=proposal.id,
        payload={"cart_hash": proposal.cart_hash},
    )
    return {"status": "REJECTED", "proposal": proposal}


# ── Dispute Recovery & Partial Refund API (Step 4) ────────────

class CaseCreateRequest(BaseModel):
    payment_id: str
    human_claim: str


@app.post("/cases", response_model=CaseRead)
def open_case(payload: CaseCreateRequest, db: Session = Depends(get_session)):
    """
    Open dispute recovery case on a confirmed payment ('This is wrong' button).
    Diagnoses root cause, clamps refund percentage to rules table, and proposes settlement.
    """
    payment = db.get(Payment, payload.payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    proposal = db.get(Proposal, payment.proposal_id) if payment.proposal_id else None
    instruction = proposal.instruction_text if proposal else "Unknown purchase"
    reasoning = proposal.agent_reasoning or "" if proposal else ""
    catalog_snapshot = proposal.catalog_snapshot or {} if proposal else {}

    diagnosis = diagnose_root_cause(
        human_claim=payload.human_claim,
        instruction_text=instruction,
        agent_reasoning=reasoning,
        catalog_snapshot=catalog_snapshot,
    )

    suggested_pct = clamp_refund_percentage(diagnosis.root_cause, diagnosis.suggested_pct)
    proposed_refund_cents = int(round(payment.amount_cents * suggested_pct / 100))
    min_pct, max_pct = get_refund_range(diagnosis.root_cause)

    case = Case(
        payment_id=payment.id,
        human_claim=payload.human_claim,
        evidence={
            "instruction": instruction,
            "agent_reasoning": reasoning,
            "catalog_snapshot": catalog_snapshot,
            "paypal_order_id": payment.paypal_order_id,
            "capture_id": payment.capture_id,
            "amount_cents": payment.amount_cents,
        },
        root_cause=diagnosis.root_cause,
        allowed_min_pct=min_pct,
        allowed_max_pct=max_pct,
        economic_inputs={"model_used": diagnosis.model_used, "llm_fallback": diagnosis.llm_fallback},
        suggested_pct=suggested_pct,
        proposed_refund_cents=proposed_refund_cents,
        explanation=diagnosis.explanation,
        status="PROPOSED",
    )
    db.add(case)
    db.commit()
    db.refresh(case)

    _append_audit_log(
        db,
        actor="buyer",
        event="CASE_OPENED",
        ref_type="case",
        ref_id=case.id,
        payload={
            "payment_id": payment.id,
            "root_cause": diagnosis.root_cause,
            "suggested_pct": suggested_pct,
            "proposed_refund_cents": proposed_refund_cents,
            "llm_fallback": diagnosis.llm_fallback,
        },
    )

    return case


@app.get("/cases")
def list_cases(db: Session = Depends(get_session)):
    """List all dispute recovery cases."""
    return db.exec(select(Case).order_by(Case.created_at.desc())).all()


@app.post("/cases/{case_id}/accept")
def accept_case_settlement(case_id: str, db: Session = Depends(get_session)):
    """
    Accept proposed settlement and execute real partial refund via PayPal Payments v2.
    Asserts refund total can never exceed capture minus prior refunds.
    """
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if case.status not in ("OPEN", "PROPOSED"):
        raise HTTPException(status_code=400, detail=f"Case status is {case.status}; cannot accept")

    payment = db.get(Payment, case.payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Associated payment not found")

    # Invariant: Refund total can never exceed capture minus prior refunds
    existing_settlements = db.exec(
        select(Settlement)
        .join(Case, Settlement.case_id == Case.id)
        .where(Case.payment_id == payment.id)
    ).all()
    prior_refunds_cents = sum(s.final_amount_cents for s in existing_settlements)

    refund_amount_cents = case.proposed_refund_cents or 0
    if prior_refunds_cents + refund_amount_cents > payment.amount_cents:
        raise HTTPException(
            status_code=400,
            detail=f"Refund {refund_amount_cents}¢ exceeds remaining capturable amount ({payment.amount_cents - prior_refunds_cents}¢)",
        )

    request_id = str(uuid.uuid4())
    capture_id = payment.capture_id or f"CAP-{payment.id}"
    note = (case.explanation or "Settlement refund")[:250]

    refund_res = payment_gateway.refund(
        capture_id=capture_id,
        amount_cents=refund_amount_cents,
        note=note,
        request_id=request_id,
    )

    paypal_refund_id = refund_res.get("refund_id") or f"REFUND-MOCK-{uuid.uuid4().hex[:8]}"

    settlement = Settlement(
        case_id=case.id,
        human_accepted=True,
        paypal_refund_id=paypal_refund_id,
        request_id=request_id,
        final_amount_cents=refund_amount_cents,
    )
    case.status = "REFUNDED"
    db.add(settlement)
    db.add(case)

    # Adjust mandate spent amount
    proposal = db.get(Proposal, payment.proposal_id) if payment.proposal_id else None
    if proposal:
        mandate = db.get(Mandate, proposal.mandate_id)
        if mandate:
            mandate.spent_cents = max(0, mandate.spent_cents - refund_amount_cents)
            db.add(mandate)

    db.commit()
    db.refresh(settlement)
    db.refresh(case)

    _append_audit_log(
        db,
        actor="buyer",
        event="CASE_SETTLED_REFUNDED",
        ref_type="case",
        ref_id=case.id,
        payload={
            "paypal_refund_id": paypal_refund_id,
            "refund_cents": refund_amount_cents,
            "case_id": case.id,
        },
    )

    return {
        "status": "REFUNDED",
        "case": CaseRead.model_validate(case).model_dump(mode="json"),
        "settlement": SettlementRead.model_validate(settlement).model_dump(mode="json"),
    }


@app.post("/cases/{case_id}/reject")
def reject_case(case_id: str, db: Session = Depends(get_session)):
    """Reject proposed settlement."""
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    case.status = "REJECTED"
    db.add(case)
    db.commit()
    db.refresh(case)

    _append_audit_log(
        db,
        actor="buyer",
        event="CASE_REJECTED",
        ref_type="case",
        ref_id=case.id,
        payload={"case_id": case.id},
    )
    return {"status": "REJECTED", "case": CaseRead.model_validate(case).model_dump(mode="json")}


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
        _wh_logger.warning("PAYPAL_WEBHOOK_ID not configured; refusing webhook delivery with 503")
        return Response(status_code=503, content="Webhook ID not configured")

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
            processed_at=datetime.now(timezone.utc),
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

        # Handle PAYMENT.CAPTURE.REFUNDED
        elif event_type == "PAYMENT.CAPTURE.REFUNDED":
            res = event.get("resource", {})
            refund_id = res.get("id")
            amount_val = res.get("amount", {}).get("value", "0.00")
            try:
                refund_cents = int(round(float(amount_val) * 100))
            except (ValueError, TypeError):
                refund_cents = 0

            capture_id = res.get("capture_id")
            if not capture_id:
                for link in res.get("links", []):
                    if link.get("rel") == "up" and "/captures/" in link.get("href", ""):
                        capture_id = link.get("href").split("/captures/")[-1].split("/")[0]
                        break

            payment = None
            if capture_id:
                payment = db.exec(select(Payment).where(Payment.capture_id == capture_id)).first()

            from backend.app.models.cases import Case
            from backend.app.models.settlements import Settlement

            case = None
            settlement = None
            if payment:
                case = db.exec(select(Case).where(Case.payment_id == payment.id)).first()
                if case:
                    case.status = "REFUNDED"
                    db.add(case)
                    settlement = db.exec(select(Settlement).where(Settlement.case_id == case.id)).first()
                    if settlement:
                        settlement.paypal_refund_id = refund_id
                        db.add(settlement)

                # Adjust mandate spent amount
                proposal = db.get(Proposal, payment.proposal_id)
                if proposal:
                    mandate = db.get(Mandate, proposal.mandate_id)
                    if mandate:
                        mandate.spent_cents = max(0, mandate.spent_cents - refund_cents)
                        db.add(mandate)

            _append_audit_log(
                db,
                actor="paypal_webhook",
                event="PAYMENT_REFUNDED_VIA_WEBHOOK",
                ref_type="payment" if payment else "refund",
                ref_id=payment.id if payment else refund_id,
                payload={
                    "event_id": event_id,
                    "refund_id": refund_id,
                    "capture_id": capture_id,
                    "refund_cents": refund_cents,
                    "case_id": case.id if case else None,
                },
            )

        db.commit()
        return {"status": "verified", "event_id": event_id, "event_type": event_type}

    _wh_logger.warning(f"Verification FAILED for event_id={event_id}")
    return Response(status_code=400, content="Verification failed")


# ── Static Files & Dashboard Mount ─────────────────────────────
_static_dir = Path("./static")
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/")
def serve_dashboard():
    """Serve the static demo dashboard (no build step)."""
    index_file = _static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {
        "status": "ok",
        "message": "Mandate Policy Engine API active. Static dashboard not yet loaded.",
    }
