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

from backend.app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger("mandate.policy.constraints")


class ExtractedConstraint(BaseModel):
    field: str
    target_value: Any
    unit: Optional[str] = None
    raw_mention: Optional[str] = None


class ConstraintExtractionResult(BaseModel):
    constraints: List[ExtractedConstraint] = Field(default_factory=list)
    extraction_method: str = "regex_fallback"
    model_reported: Optional[str] = None


def extract_constraints(instruction_text: str) -> ConstraintExtractionResult:
    """
    Extract structured dimension / attribute constraints from natural language instruction.
    Uses OpenRouter OpenAI-compatible API if LLM_API_KEY is configured,
    else falls back to deterministic regex extraction.
    """
    if LLM_API_KEY:
        try:
            return _extract_via_openrouter(instruction_text)
        except Exception as exc:
            logger.warning(f"OpenRouter LLM extraction failed: {exc}. Falling back to rule-based parser.")

    return _extract_via_rules(instruction_text)


def _extract_via_rules(text: str) -> ConstraintExtractionResult:
    """Deterministic rule-based constraint parser."""
    constraints: List[ExtractedConstraint] = []
    lower = text.lower()

    # Pattern for dimension: e.g. "160 cm", "160cm wide", "width 160", "120 cm desk"
    if "width" in lower or "wide" in lower:
        m = re.search(r'(\d+)\s*(cm|mm|in)?\s*wide', lower) or re.search(r'width\s*(?:of|:)?\s*(\d+)\s*(cm|mm|in)?', lower)
        if m:
            val = int(m.group(1))
            unit = m.group(2) if len(m.groups()) >= 2 and m.group(2) else "cm"
            constraints.append(
                ExtractedConstraint(
                    field="width",
                    target_value=val,
                    unit=unit,
                    raw_mention=m.group(0),
                )
            )
        else:
            width_match = re.search(r'(\d+)\s*(?:cm|mm|in|inch|inches)?', lower)
            if width_match:
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


def _extract_via_openrouter(text: str) -> ConstraintExtractionResult:
    """
    Call OpenRouter's OpenAI-compatible API for structured constraint extraction
    using strict JSON-schema structured output. Logs the model name each response reports.
    """
    import httpx

    json_schema = {
        "name": "extracted_constraints",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "constraints": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "description": "Normalized attribute name (e.g., width, height, color, dpi)",
                            },
                            "target_value": {
                                "type": ["string", "number"],
                                "description": "Target numeric value or string description",
                            },
                            "unit": {
                                "type": ["string", "null"],
                                "description": "Unit of measurement (e.g. cm, mm, in), or null",
                            },
                            "raw_mention": {
                                "type": ["string", "null"],
                                "description": "Exact text snippet mentioned in user instruction",
                            },
                        },
                        "required": ["field", "target_value"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["constraints"],
            "additionalProperties": False,
        },
    }

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict constraint extraction parser. "
                    "Extract physical or technical constraints requested in the buyer instruction "
                    "(e.g., width, height, depth, color, material, capacity, connectivity). "
                    "Return ONLY valid JSON conforming to the requested schema."
                ),
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": json_schema,
        },
    }

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/mandate",
        "X-Title": "Mandate Policy Engine",
    }

    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, headers=headers, json=payload)
        if resp.status_code == 200:
            res_data = resp.json()
            reported_model = res_data.get("model") or LLM_MODEL
            logger.info(f"OpenRouter constraint extraction response received from model: {reported_model}")

            choices = res_data.get("choices", [])
            if choices:
                raw_content = choices[0].get("message", {}).get("content", "{}")
                parsed = json.loads(raw_content)
                constraints = [
                    ExtractedConstraint(**c) for c in parsed.get("constraints", [])
                ]
                return ConstraintExtractionResult(
                    constraints=constraints,
                    extraction_method="llm_openrouter",
                    model_reported=reported_model,
                )

        logger.warning(
            f"OpenRouter returned status {resp.status_code}: {resp.text[:200]}. Falling back to rules."
        )

    return _extract_via_rules(text)


def _extract_via_llm(text: str) -> ConstraintExtractionResult:
    """Backward compatibility alias for _extract_via_openrouter."""
    return _extract_via_openrouter(text)


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
