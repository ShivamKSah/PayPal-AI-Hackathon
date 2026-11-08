"""Central policy engine orchestrator.

Runs checks 1-11 in strict order without early exit.
Combines verdicts: any DENY wins, else any ESCALATE, else APPROVE.
"""

import json
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Set
from pydantic import BaseModel, Field

from backend.app.catalog import get_catalog_adapter, Product
from backend.app.policy.constraints import extract_constraints, ExtractedConstraint, ConstraintExtractionResult
from backend.app.policy.checks import (
    check_1_mandate_active,
    check_2_remaining_budget,
    check_3_per_txn_cap,
    check_4_merchant_allowlist,
    check_5_category,
    check_6_reconcile,
    check_7_velocity,
    check_8_idempotency,
    check_9_ambiguity_gate,
    check_10_recurring_billing,
    check_11_near_limit,
)


class PolicyEvaluationResult(BaseModel):
    verdict: str  # APPROVE, ESCALATE, DENY
    rule_hits: List[Dict[str, Any]]
    cart_hash: str
    catalog_snapshot: Optional[Dict[str, Any]] = None
    constraints: Optional[Dict[str, Any]] = None
    llm_fallback: bool = False
    model_used: Optional[str] = None
    llm_attempts: int = 0


def compute_cart_hash(cart: Dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of canonical cart JSON."""
    canonical = json.dumps(cart, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PolicyEngine:
    """Evaluates proposals against mandates using plain deterministic code."""

    def __init__(self, catalog_adapter=None):
        self.catalog = catalog_adapter or get_catalog_adapter()

    def evaluate(
        self,
        mandate: Any,
        cart: Dict[str, Any],
        instruction_text: str = "",
        now: Optional[datetime] = None,
        purchases_in_last_hour: int = 0,
        recent_cart_hashes: Optional[Set[str]] = None,
        is_first_purchase_from_merchant: bool = False,
    ) -> PolicyEvaluationResult:
        """
        Evaluate all 11 checks in strict Section 7 order.
        Runs ALL checks (NO early exit).
        """
        eval_time = now or datetime.now(timezone.utc)
        recent_hashes = recent_cart_hashes or set()
        cart_hash = compute_cart_hash(cart)
        total_cents = cart.get("total_cents", 0)
        merchant_id = cart.get("merchant_id", "")

        rule_hits: List[Dict[str, Any]] = []

        # ── Group 1: Checks 1-4 (NO network calls) ────────────
        hit_1 = check_1_mandate_active(mandate, now=eval_time)
        rule_hits.append(hit_1)

        hit_2 = check_2_remaining_budget(mandate, total_cents=total_cents)
        rule_hits.append(hit_2)

        hit_3 = check_3_per_txn_cap(mandate, total_cents=total_cents)
        rule_hits.append(hit_3)

        hit_4 = check_4_merchant_allowlist(mandate, merchant_id=merchant_id)
        rule_hits.append(hit_4)

        # ── Group 2: Fetch Catalog & Run Checks 5, 6, 9, 10 ───
        line_items = cart.get("items", [])
        catalog_products: Dict[str, Optional[Product]] = {}
        primary_product: Optional[Product] = None

        for item in line_items:
            pid = item.get("product_id") or item.get("id")
            if pid:
                prod = self.catalog.get_product(pid)
                catalog_products[pid] = prod
                if not primary_product and prod:
                    primary_product = prod

        # Check 5: Category
        catalog_category = primary_product.category if primary_product else ""
        hit_5 = check_5_category(mandate, catalog_category=catalog_category)
        rule_hits.append(hit_5)

        # Check 6: Reconcile
        hit_6 = check_6_reconcile(cart, catalog_products)
        rule_hits.append(hit_6)

        # Check 9: Ambiguity Gate
        # If extraction fails for any reason, treat as "no constraints" and ESCALATE.
        try:
            extraction_res = extract_constraints(instruction_text)
        except Exception as exc:
            import logging
            logging.getLogger("mandate.policy.engine").warning(
                f"Constraint extraction failed: {exc}. Treating as no constraints (ESCALATE)."
            )
            extraction_res = ConstraintExtractionResult(constraints=[], llm_fallback=True)
        catalog_specs = primary_product.specs if primary_product else {}
        hit_9 = check_9_ambiguity_gate(
            extraction_res.constraints,
            catalog_specs,
            extraction_method=extraction_res.extraction_method,
            model_reported=getattr(extraction_res, "model_reported", None),
            llm_fallback=getattr(extraction_res, "llm_fallback", False),
        )
        rule_hits.append(hit_9)

        # Check 10: Recurring Billing
        is_recurring = any(p.is_recurring for p in catalog_products.values() if p)
        hit_10 = check_10_recurring_billing(mandate, is_recurring=is_recurring)
        rule_hits.append(hit_10)

        # ── Group 3: Velocity, Idempotency, Near-Limit ────────
        # Check 7: Velocity
        hit_7 = check_7_velocity(mandate, purchases_in_last_hour=purchases_in_last_hour)
        rule_hits.append(hit_7)

        # Check 8: Idempotency
        hit_8 = check_8_idempotency(cart_hash, recent_hashes)
        rule_hits.append(hit_8)

        # Check 11: Near Limit
        hit_11 = check_11_near_limit(
            mandate,
            total_cents=total_cents,
            is_first_purchase_from_merchant=is_first_purchase_from_merchant,
        )
        rule_hits.append(hit_11)

        # ── Combine verdicts: any DENY wins, else any ESCALATE, else APPROVE ──
        has_deny = False
        has_escalate = False

        for hit in rule_hits:
            if not hit["passed"]:
                v = hit.get("verdict_on_fail")
                if v == "DENY":
                    has_deny = True
                elif v == "ESCALATE":
                    has_escalate = True

        if has_deny:
            final_verdict = "DENY"
        elif has_escalate:
            final_verdict = "ESCALATE"
        else:
            final_verdict = "APPROVE"

        # Catalog snapshot for audit log & record keeping
        catalog_snapshot = {
            pid: prod.model_dump() if prod else None
            for pid, prod in catalog_products.items()
        }

        return PolicyEvaluationResult(
            verdict=final_verdict,
            rule_hits=rule_hits,
            cart_hash=cart_hash,
            catalog_snapshot=catalog_snapshot,
            constraints=extraction_res.model_dump(),
            llm_fallback=getattr(extraction_res, "llm_fallback", False),
            model_used=getattr(extraction_res, "model_reported", None),
            llm_attempts=getattr(extraction_res, "attempts", 0),
        )
