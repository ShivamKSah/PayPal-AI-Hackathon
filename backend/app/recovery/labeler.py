"""LLM root-cause labeler with automatic retries, fast fallback, and deterministic rules."""

import sys
import json
import logging
from typing import Dict, Any, Tuple
from pydantic import BaseModel

from backend.app import config
from backend.app.llm import call_llm_with_retry
from backend.app.recovery.economics import clamp_refund_percentage, get_refund_range

logger = logging.getLogger("mandate.recovery.labeler")

LLM_API_KEY = config.LLM_API_KEY

VALID_ROOT_CAUSES = ["seller_fault", "ambiguous_listing", "agent_error", "vague_instruction"]


class RootCauseDiagnosis(BaseModel):
    root_cause: str
    suggested_pct: int
    explanation: str
    llm_fallback: bool = False
    model_used: str = "deterministic_rules"
    attempts: int = 0


def diagnose_root_cause(
    human_claim: str,
    instruction_text: str,
    agent_reasoning: str,
    catalog_snapshot: Dict[str, Any],
) -> RootCauseDiagnosis:
    """
    Diagnose purchase failure root cause using LLM with retry & fallback,
    or deterministic rule fallback. Clamps percentage strictly to defined range.
    """
    curr_key = getattr(sys.modules[__name__], "LLM_API_KEY", None) or config.LLM_API_KEY
    if curr_key:
        try:
            return _diagnose_via_llm(human_claim, instruction_text, agent_reasoning, catalog_snapshot)
        except Exception as exc:
            attempts = getattr(exc, "attempts", 0)
            logger.warning(f"Root cause LLM diagnosis failed: {exc}. Using deterministic rule fallback.")
            return _diagnose_via_rules(human_claim, instruction_text, llm_fallback=True, attempts=attempts)

    return _diagnose_via_rules(human_claim, instruction_text, llm_fallback=False, attempts=0)


def _diagnose_via_rules(
    human_claim: str,
    instruction_text: str,
    llm_fallback: bool = False,
    attempts: int = 0,
) -> RootCauseDiagnosis:
    """Deterministic rule-based root cause classifier."""
    claim_lower = human_claim.lower()
    inst_lower = instruction_text.lower()

    if any(k in claim_lower for k in ["broken", "damaged", "fake", "defective", "counterfeit", "wrong item shipped"]):
        cause = "seller_fault"
        raw_pct = 100
        explanation = f"Seller fulfilled defective or incorrect item per claim: '{human_claim}'."
    elif any(k in claim_lower for k in ["missing spec", "dimension wrong", "not as pictured", "listing misleading", "unclear listing"]):
        cause = "ambiguous_listing"
        raw_pct = 60
        explanation = f"Product listing was ambiguous or omitted critical specifications per claim: '{human_claim}'."
    elif any(k in claim_lower for k in ["agent picked", "agent chose wrong", "overpaid", "didn't want this"]):
        cause = "agent_error"
        raw_pct = 80
        explanation = f"Shopping agent selected unsuitable product despite explicit instructions: '{human_claim}'."
    else:
        cause = "vague_instruction"
        raw_pct = 25
        explanation = f"Initial instruction '{instruction_text}' was insufficiently specific: '{human_claim}'."

    clamped_pct = clamp_refund_percentage(cause, raw_pct)
    return RootCauseDiagnosis(
        root_cause=cause,
        suggested_pct=clamped_pct,
        explanation=explanation,
        llm_fallback=llm_fallback,
        model_used="rules_heuristic",
        attempts=attempts,
    )


def _diagnose_via_llm(
    human_claim: str,
    instruction_text: str,
    agent_reasoning: str,
    catalog_snapshot: Dict[str, Any],
) -> RootCauseDiagnosis:
    """Diagnose root cause via OpenAI-compatible chat completion."""
    schema = {
        "name": "root_cause_diagnosis",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "root_cause": {
                    "type": "string",
                    "enum": VALID_ROOT_CAUSES,
                },
                "suggested_pct": {
                    "type": "integer",
                    "description": "Suggested refund percentage (0 to 100)",
                },
                "explanation": {
                    "type": "string",
                    "description": "Clear rationale explaining who was at fault and why",
                },
            },
            "required": ["root_cause", "suggested_pct", "explanation"],
            "additionalProperties": False,
        },
    }

    prompt = (
        f"Buyer claim: {human_claim}\n"
        f"Original instruction: {instruction_text}\n"
        f"Agent reasoning: {agent_reasoning}\n"
        f"Catalog specs: {json.dumps(catalog_snapshot)[:500]}\n\n"
        "Diagnose the root cause. Must be exactly one of: seller_fault, ambiguous_listing, agent_error, vague_instruction."
    )

    def payload_builder(model: str) -> Dict[str, Any]:
        return {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an objective dispute recovery analyst. Analyze the claim and output strict JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": schema,
            },
        }

    res_data, reported_model, attempts = call_llm_with_retry(payload_builder, timeout=45.0)
    choices = res_data.get("choices", [])
    if not choices:
        raise RuntimeError("Empty response from LLM diagnosis")

    content = choices[0].get("message", {}).get("content", "{}")
    parsed = json.loads(content)

    cause = parsed.get("root_cause")
    if cause not in VALID_ROOT_CAUSES:
        cause = "vague_instruction"

    raw_pct = int(parsed.get("suggested_pct", 50))
    # Crucial safety requirement: clamp in code so the model can never exceed the range
    clamped_pct = clamp_refund_percentage(cause, raw_pct)
    explanation = parsed.get("explanation", f"Diagnosed by {reported_model}")

    return RootCauseDiagnosis(
        root_cause=cause,
        suggested_pct=clamped_pct,
        explanation=explanation,
        llm_fallback=False,
        model_used=reported_model,
        attempts=attempts,
    )
