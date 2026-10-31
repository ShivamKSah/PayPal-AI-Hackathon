"""Shopping agent with tool use under strict mandate guard."""

from typing import Dict, Any, List, Optional
import json
import logging

from backend.app.agent.guard import guard
from backend.app.agent.tools import get_tool_definitions, search_catalog
from backend.app.catalog import get_catalog_adapter
from backend.app.config import LLM_API_KEY, LLM_PROVIDER

logger = logging.getLogger("mandate.agent")


class ShoppingAgent:
    """
    Autonomous shopping assistant that translates buyer instructions into proposals.
    Never holds PayPal credentials, vault tokens, or refund mechanisms.
    """

    def __init__(self, mandate: Any, session_token: Optional[str] = None):
        self.mandate = mandate
        self.session_token = session_token
        # Pass all candidate tools through security guard
        raw_tools = get_tool_definitions()
        self.tools = guard(raw_tools, mandate, session_token=session_token)
        self.catalog = get_catalog_adapter()

    def run(self, instruction_text: str) -> Dict[str, Any]:
        """
        Execute agent reasoning loop for user instruction.
        Finds matching catalog products and forms a purchase proposal.
        """
        logger.info(f"Agent processing instruction: {instruction_text}")

        # 1. Query catalog for best matching items
        matching_products = self.catalog.search_products(instruction_text)

        if not matching_products:
            # Fallback search with simplified query
            tokens = instruction_text.lower().split()
            for token in tokens:
                matching_products = self.catalog.search_products(token)
                if matching_products:
                    break

        if not matching_products:
            return {
                "status": "NO_PRODUCTS_FOUND",
                "instruction": instruction_text,
                "proposal": None,
            }

        # Select primary match
        selected = matching_products[0]

        cart = {
            "merchant_id": selected.merchant_id or "MERCHANT-DEFAULT",
            "currency": selected.currency,
            "items": [
                {
                    "product_id": selected.id,
                    "title": selected.title,
                    "quantity": 1,
                    "unit_price_cents": selected.price_cents,
                }
            ],
            "subtotal_cents": selected.price_cents,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": selected.price_cents,
        }

        reasoning = (
            f"Selected '{selected.title}' ({selected.price_cents}¢) matching user instruction "
            f"'{instruction_text}' from catalog."
        )

        proposal_payload = {
            "mandate_id": getattr(self.mandate, "id", "mandate-sim"),
            "instruction_text": instruction_text,
            "agent_reasoning": reasoning,
            "cart_json": cart,
        }

        return {
            "status": "PROPOSED",
            "instruction": instruction_text,
            "proposal": proposal_payload,
            "tools_exposed": [t["name"] for t in self.tools],
        }
