"""Refund economics and root-cause refund percentage rules."""

from typing import Dict, Tuple

# Section 10 & Step 4 rules table for root-cause refund ranges
ROOT_CAUSE_REFUND_RANGES: Dict[str, Tuple[int, int]] = {
    "seller_fault": (70, 100),
    "ambiguous_listing": (40, 70),
    "agent_error": (50, 100),
    "vague_instruction": (10, 40),
}


def get_refund_range(root_cause: str) -> Tuple[int, int]:
    """Return (min_pct, max_pct) for a given root cause."""
    return ROOT_CAUSE_REFUND_RANGES.get(root_cause, (10, 50))


def clamp_refund_percentage(root_cause: str, suggested_pct: int) -> int:
    """
    Strictly clamp suggested refund percentage to the authorized bounds in the rules table.
    Ensures that an LLM or caller can NEVER exceed the defined range.
    """
    min_pct, max_pct = get_refund_range(root_cause)
    return max(min_pct, min(max_pct, suggested_pct))
