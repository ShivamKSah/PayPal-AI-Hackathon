"""Constraint extraction and comparison for the ambiguity gate (Check 9).

The LLM extracts structured constraints from the user instruction.
Code compares those keys to catalog fields.
The LLM output may ONLY push toward ESCALATE.
"""

import re
import json
import logging
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from backend.app.config import LLM_API_KEY, LLM_PROVIDER

logger = logging.getLogger("mandate.policy.constraints")


class ExtractedConstraint(BaseModel):
    field: str
    target_value: Any
    unit: Optional[str] = None
    raw_mention: Optional[str] = None


class ConstraintExtractionResult(BaseModel):
    constraints: List[ExtractedConstraint] = Field(default_factory=list)
    extraction_method: str = "regex_fallback"


def extract_constraints(instruction_text: str) -> ConstraintExtractionResult:
    """
    Extract structured dimension / attribute constraints from natural language instruction.
    Uses LLM if LLM_API_KEY is configured, else deterministic regex extraction.
    """
    if LLM_API_KEY:
        try:
            # When an LLM API key is present, attempt structured extraction
            return _extract_via_llm(instruction_text)
        except Exception as exc:
            logger.warning(f"LLM extraction failed: {exc}. Falling back to rule-based parser.")

    return _extract_via_rules(instruction_text)


def _extract_via_rules(text: str) -> ConstraintExtractionResult:
    """Deterministic rule-based constraint parser."""
    constraints: List[ExtractedConstraint] = []
    lower = text.lower()

    # Pattern for dimension: e.g. "160 cm", "160cm wide", "width 160", "120 cm desk"
    width_match = re.search(r'(\d+)\s*(?:cm|mm|in|inch|inches)?\s*(?:wide|width)?', lower)
    if "width" in lower:
        m = re.search(r'width\s*(?:of|:)?\s*(\d+)\s*(cm|mm|in)?', lower)
        if m:
            constraints.append(
                ExtractedConstraint(
                    field="width",
                    target_value=int(m.group(1)),
                    unit=m.group(2) or "cm",
                    raw_mention=m.group(0),
                )
            )
        elif width_match:
            constraints.append(
                ExtractedConstraint(
                    field="width",
                    target_value=int(width_match.group(1)),
                    unit="cm",
                    raw_mention=width_match.group(0),
                )
            )
    elif re.search(r'\b(\d{2,3})\s*(?:cm)?\s*(?:standing\s+)?desk\b', lower):
        # e.g., "160 cm desk"
        m = re.search(r'\b(\d{2,3})\s*(?:cm)?\s*(?:standing\s+)?desk\b', lower)
        if m:
            constraints.append(
                ExtractedConstraint(
                    field="width",
                    target_value=int(m.group(1)),
                    unit="cm",
                    raw_mention=m.group(0),
                )
            )

    # Pattern for height
    if "height" in lower or "tall" in lower:
        m = re.search(r'(\d+)\s*(?:cm|in)?\s*(?:tall|height)', lower)
        if m:
            constraints.append(
                ExtractedConstraint(
                    field="height",
                    target_value=int(m.group(1)),
                    unit="cm",
                    raw_mention=m.group(0),
                )
            )

    # Pattern for color
    for color in ["black", "white", "oak", "walnut", "silver", "gray", "grey"]:
        if re.search(rf'\b{color}\b', lower):
            constraints.append(
                ExtractedConstraint(
                    field="color",
                    target_value=color,
                    unit=None,
                    raw_mention=color,
                )
            )
            break

    return ConstraintExtractionResult(constraints=constraints, extraction_method="regex_fallback")


def _extract_via_llm(text: str) -> ConstraintExtractionResult:
    """Call external LLM (Gemini or Anthropic) for structured extraction with strict JSON output."""
    import httpx

    prompt = (
        "Extract any specific physical or technical constraints requested in this instruction "
        "(e.g., width, height, depth, color, material, capacity, connectivity).\n"
        f"Instruction: {text}\n"
        "Return ONLY a valid JSON object matching this schema:\n"
        '{"constraints": [{"field": "string", "target_value": "string or number", "unit": "string or null", "raw_mention": "string"}]}'
    )

    provider = (LLM_PROVIDER or "gemini").lower()
    is_anthropic = provider == "anthropic" or (LLM_API_KEY and LLM_API_KEY.startswith("sk-ant"))

    with httpx.Client(timeout=10.0) as client:
        if is_anthropic:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": LLM_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-3-5-haiku-20241022",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code == 200:
                data = resp.json()["content"][0]["text"]
                parsed = json.loads(data)
                constraints = [ExtractedConstraint(**c) for c in parsed.get("constraints", [])]
                return ConstraintExtractionResult(constraints=constraints, extraction_method="llm_anthropic")
        else:
            # Default to Gemini
            resp = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={LLM_API_KEY}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"response_mime_type": "application/json"},
                },
            )
            if resp.status_code == 200:
                data = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(data)
                constraints = [ExtractedConstraint(**c) for c in parsed.get("constraints", [])]
                return ConstraintExtractionResult(constraints=constraints, extraction_method="llm_gemini")

    return _extract_via_rules(text)


def compare_constraints(
    constraints: List[ExtractedConstraint],
    catalog_specs: Dict[str, Any],
) -> tuple[bool, List[str]]:
    """
    Compare extracted constraints against catalog specs.
    Returns (all_present, missing_fields).
    If catalog record lacks any requested field, returns False and the missing field list.
    """
    if not constraints:
        return True, []

    missing: List[str] = []
    # Normalize spec keys to lowercase
    normalized_specs = {k.lower(): v for k, v in catalog_specs.items()}

    for c in constraints:
        key = c.field.lower()
        if key not in normalized_specs:
            missing.append(c.field)

    return len(missing) == 0, missing
