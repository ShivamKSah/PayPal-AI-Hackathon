"""Shopping agent with tool use under strict mandate guard."""

from typing import Dict, Any, List, Optional
import json
import logging

from backend.app.agent.guard import guard
from backend.app.agent.tools import get_tool_definitions, search_catalog
from backend.app.catalog import get_catalog_adapter
from backend.app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger("mandate.agent")


class ShoppingAgent:
    """
    Autonomous shopping assistant that translates buyer instructions into proposals.
    Never holds PayPal credentials, vault tokens, or refund mechanisms.
    Uses OpenRouter tool calling when LLM_API_KEY is configured.
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
        Attempts OpenRouter LLM tool calling first; falls back to deterministic planner.
        """
        logger.info(f"Agent processing instruction: {instruction_text}")

        if LLM_API_KEY:
            try:
                res = self._run_via_openrouter_tools(instruction_text)
                if res:
                    return res
            except Exception as exc:
                logger.warning(f"OpenRouter tool-calling loop failed: {exc}. Falling back to deterministic planner.")

        return self._run_deterministic_fallback(instruction_text)

    def _run_via_openrouter_tools(self, instruction_text: str) -> Optional[Dict[str, Any]]:
        """Execute OpenRouter tool calling loop with strict guard on exposed tools."""
        import httpx

        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in self.tools
        ]

        messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are an autonomous shopping agent for Mandate. "
                    "You help buyers find items and propose purchases that adhere to their policies. "
                    "First call 'search_catalog' to inspect products and prices, then call 'propose_purchase' "
                    "with the structured cart and reasoning."
                ),
            },
            {"role": "user", "content": instruction_text},
        ]

        headers = {
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/mandate",
            "X-Title": "Mandate Shopping Agent",
        }

        url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"

        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                url,
                headers=headers,
                json={
                    "model": LLM_MODEL,
                    "messages": messages,
                    "tools": openai_tools,
                    "tool_choice": "auto",
                },
            )
            if resp.status_code != 200:
                logger.warning(f"OpenRouter returned {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            reported_model = data.get("model") or LLM_MODEL
            logger.info(f"Shopping agent tool-call response from model: {reported_model}")

            msg = data.get("choices", [{}])[0].get("message", {})
            tool_calls = msg.get("tool_calls", [])

            # Check if direct proposal or catalog search
            for tc in tool_calls:
                fn = tc.get("function", {})
                fn_name = fn.get("name")
                args = json.loads(fn.get("arguments", "{}"))

                if fn_name == "propose_purchase":
                    cart = args.get("cart", {})
                    reasoning = args.get("reasoning", f"Proposed via {reported_model}")
                    return {
                        "status": "PROPOSED",
                        "instruction": instruction_text,
                        "proposal": {
                            "mandate_id": getattr(self.mandate, "id", "mandate-sim"),
                            "instruction_text": instruction_text,
                            "agent_reasoning": reasoning,
                            "cart_json": cart,
                        },
                        "tools_exposed": [t["name"] for t in self.tools],
                        "model_reported": reported_model,
                    }

                elif fn_name == "search_catalog":
                    # Execute catalog search tool
                    query = args.get("query", instruction_text)
                    search_results = search_catalog(query)

                    messages.append(msg)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", "call_1"),
                            "content": json.dumps(search_results),
                        }
                    )

                    # Round 2 call to form proposal
                    round2_resp = client.post(
                        url,
                        headers=headers,
                        json={
                            "model": LLM_MODEL,
                            "messages": messages,
                            "tools": openai_tools,
                        },
                    )
                    if round2_resp.status_code == 200:
                        r2_data = round2_resp.json()
                        r2_model = r2_data.get("model") or reported_model
                        logger.info(f"Shopping agent round 2 tool-call response from model: {r2_model}")
                        r2_msg = r2_data.get("choices", [{}])[0].get("message", {})
                        r2_tool_calls = r2_msg.get("tool_calls", [])
                        for r2_tc in r2_tool_calls:
                            r2_fn = r2_tc.get("function", {})
                            if r2_fn.get("name") == "propose_purchase":
                                r2_args = json.loads(r2_fn.get("arguments", "{}"))
                                return {
                                    "status": "PROPOSED",
                                    "instruction": instruction_text,
                                    "proposal": {
                                        "mandate_id": getattr(self.mandate, "id", "mandate-sim"),
                                        "instruction_text": instruction_text,
                                        "agent_reasoning": r2_args.get("reasoning", f"Proposed via {r2_model}"),
                                        "cart_json": r2_args.get("cart", {}),
                                    },
                                    "tools_exposed": [t["name"] for t in self.tools],
                                    "model_reported": r2_model,
                                }

        return None

    def _run_deterministic_fallback(self, instruction_text: str) -> Dict[str, Any]:
        """Deterministic search & proposal creation fallback."""
        # Query catalog for best matching items
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
            f"'{instruction_text}' from catalog (deterministic planner)."
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
            "model_reported": "fallback_deterministic",
        }
