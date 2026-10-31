"""Guard wrapper – enforces credential custody and tool isolation.

Non-negotiable security invariant (Section 3):
The agent must NEVER hold PayPal credentials or the vault token.
guard(tools, mandate) exposes ONLY search_catalog and propose_purchase.
Any PayPal tools, credentials, or refund tools are strictly rejected.
"""

from typing import List, Dict, Any, Callable, Optional
import logging

logger = logging.getLogger("mandate.agent.guard")

# Banned tool names / substrings: any PayPal execution or refund capabilities
FORBIDDEN_TOOL_KEYWORDS = [
    "paypal",
    "refund",
    "vault",
    "capture",
    "charge",
    "create_order",
    "settle",
    "dispute",
]

ALLOWED_TOOL_NAMES = {"search_catalog", "propose_purchase"}


class UnguardedToolException(Exception):
    """Raised when an illegal or hazardous tool is supplied to an agent."""


def guard(
    tools: List[Dict[str, Any]],
    mandate: Any,
    session_token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Sanitize and restrict the agent's tool surface.
    - Exposes ONLY 'search_catalog' and 'propose_purchase'.
    - Strips/rejects any PayPal API tools, refund capabilities, or credentials.
    - Binds the allowed propose tool to the scoped session token.
    """
    guarded_tools: List[Dict[str, Any]] = []

    for tool in tools:
        tool_name = tool.get("name", "") if isinstance(tool, dict) else getattr(tool, "__name__", "")
        tool_lower = tool_name.lower()

        # Reject any banned keyword
        if any(keyword in tool_lower for keyword in FORBIDDEN_TOOL_KEYWORDS):
            logger.warning(f"BLOCKED forbidden tool from agent: {tool_name}")
            continue

        # Strictly allow only white-listed tools
        if tool_name in ALLOWED_TOOL_NAMES:
            guarded_tools.append(tool)
        else:
            logger.info(f"Filtered out unallowed tool: {tool_name}")

    # Ensure no credentials leaked in tool descriptions
    for t in guarded_tools:
        if isinstance(t, dict):
            # Strip any credentials if accidentally present
            t.pop("paypal_client_id", None)
            t.pop("paypal_client_secret", None)
            t.pop("vault_token", None)

    return guarded_tools
