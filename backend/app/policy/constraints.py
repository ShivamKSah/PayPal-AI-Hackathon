"""Constraint extraction and comparison for the ambiguity gate (Check 9).

The LLM extracts structured constraints from the user instruction.
Code compares those keys to catalog fields.
The LLM output may ONLY push toward ESCALATE.
"""

import re
import json
import logging
import httpx
import sys
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from backend.app import config
from backend.app.llm import call_llm_with_retry

logger = logging.getLogger("mandate.policy.constraints")

LLM_API_KEY = config.LLM_API_KEY
LLM_BASE_URL = config.LLM_BASE_URL
LLM_MODEL = config.LLM_MODEL


class ExtractedConstraint(BaseModel):
    field: str
    target_value: Any
    unit: Optional[str] = None
    raw_mention: Optional[str] = None


class ConstraintExtractionResult(BaseModel):
    constraints: List[ExtractedConstraint] = Field(default_factory=list)
    extraction_method: str = "regex_fallback"
    model_reported: Optional[str] = None
    llm_fallback: bool = False
    attempts: int = 0


def extract_constraints(instruction_text: str) -> ConstraintExtractionResult:
    """
    Extract structured dimension / attribute constraints from natural language instruction.
    Uses OpenAI-compatible API if LLM_API_KEY is configured,
    else falls back to deterministic regex extraction.
    """
    curr_key = getattr(sys.modules[__name__], "LLM_API_KEY", None) or config.LLM_API_KEY
    if curr_key:
        try:
            return _extract_via_openrouter(instruction_text)
        except Exception as exc:
            attempts = getattr(exc, "attempts", 0)
            logger.warning(f"LLM extraction request exception: {exc}. Falling back to rule-based parser.")
            return _extract_via_rules(instruction_text, llm_fallback=True, attempts=attempts)

    return _extract_via_rules(instruction_text, llm_fallback=False, attempts=0)


def _extract_via_rules(text: str, llm_fallback: bool = False, attempts: int = 0) -> ConstraintExtractionResult:
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
    elif re.search(r'\b(?:standing\s+)?desk\s+(\d{2,3})\s*(?:cm)?\b', lower):
        # e.g., "standing desk 160 cm"
        m = re.search(r'\b(?:standing\s+)?desk\s+(\d{2,3})\s*(?:cm)?\b', lower)
        if m:
            constraints.append(
                ExtractedConstraint(
                    field="width",
                    target_value=int(m.group(1)),
                    unit="cm",
                    raw_mention=m.group(0),
                )
            )
    elif re.search(r'\b(\d{2,3})\s*cm\b', lower):
        # e.g. "160 cm"
        m = re.search(r'\b(\d{2,3})\s*cm\b', lower)
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

    return ConstraintExtractionResult(
        constraints=constraints,
        extraction_method="regex_fallback",
        llm_fallback=llm_fallback,
        attempts=attempts,
    )


def _extract_via_openrouter(text: str) -> ConstraintExtractionResult:
    """
    Call OpenRouter's OpenAI-compatible API for structured constraint extraction
    using strict JSON-schema structured output, automatic retries, and fast-model fallback.
    Logs the model name each response reports.
    """
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

    def payload_builder(model: str) -> Dict[str, Any]:
        return {
            "model": model,
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

    try:
        res_data, reported_model, attempts = call_llm_with_retry(payload_builder)
        choices = res_data.get("choices", [])
        if choices:
            raw_content = choices[0].get("message", {}).get("content", "{}")
            parsed = json.loads(raw_content)
            constraints = [
                ExtractedConstraint(**c) for c in parsed.get("constraints", [])
            ]
            logger.info(f"OpenRouter constraint extraction response from {reported_model} in {attempts} attempt(s)")
            return ConstraintExtractionResult(
                constraints=constraints,
                extraction_method="llm_openrouter",
                model_reported=reported_model,
                llm_fallback=False,
                attempts=attempts,
            )
        raise RuntimeError("No choices returned in LLM response")
    except Exception as exc:
        attempts = getattr(exc, "attempts", 0)
        logger.warning(
            f"LLM extraction failed after retry/fallback: {exc}. Falling back to rules."
        )
        return _extract_via_rules(text, llm_fallback=True, attempts=attempts)


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
