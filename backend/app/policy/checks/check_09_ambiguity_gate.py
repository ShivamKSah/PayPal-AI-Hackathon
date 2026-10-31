"""Check 9: Ambiguity Gate."""

from typing import Dict, Any, List
from backend.app.policy.constraints import ExtractedConstraint, compare_constraints


def check_9_ambiguity_gate(
    constraints: List[ExtractedConstraint],
    catalog_specs: Dict[str, Any],
    extraction_method: str = "regex_fallback",
) -> Dict[str, Any]:
    """
    Check 9:
    For each constraint extracted from user instruction (e.g. width 160cm),
    the catalog record must contain that field.
    If missing, ESCALATE to human.
    Verdict on fail: ESCALATE.
    """
    if not constraints:
        return {
            "check_id": 9,
            "name": "ambiguity_gate",
            "passed": True,
            "reason": "No ambiguous dimension/attribute constraints in instruction",
            "verdict_on_fail": "ESCALATE",
            "extraction_method": extraction_method,
        }

    all_present, missing_fields = compare_constraints(constraints, catalog_specs)

    if not all_present:
        return {
            "check_id": 9,
            "name": "ambiguity_gate",
            "passed": False,
            "reason": (
                f"Instruction requested constraint(s) {missing_fields}, but catalog item "
                f"does not specify these field(s). Escalating to human."
            ),
            "verdict_on_fail": "ESCALATE",
            "extraction_method": extraction_method,
        }

    return {
        "check_id": 9,
        "name": "ambiguity_gate",
        "passed": True,
        "reason": f"All requested constraint fields {[c.field for c in constraints]} are verified in catalog",
        "verdict_on_fail": "ESCALATE",
        "extraction_method": extraction_method,
    }

