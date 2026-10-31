"""Agent tool declarations and implementations.

Only two tools are permitted for the shopping agent:
1. search_catalog: query products and metadata
2. propose_purchase: submit purchase proposal to policy engine
"""

from typing import Dict, Any, List, Optional
from backend.app.catalog import get_catalog_adapter, Product

catalog_adapter = get_catalog_adapter()


def search_catalog(query: str) -> List[Dict[str, Any]]:
    """Search catalog for matching products."""
    products = catalog_adapter.search_products(query)
    return [p.model_dump() for p in products]


def get_tool_definitions() -> List[Dict[str, Any]]:
    """Return JSON schemas for permitted agent tools."""
    return [
        {
            "name": "search_catalog",
            "description": "Search products by keywords to find matching items, prices, and specifications.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query terms (e.g. 'ergonomic mouse', 'standing desk')",
                    }
                },
                "required": ["query"],
            },
        },
        {
            "name": "propose_purchase",
            "description": "Propose a cart for buyer mandate policy evaluation. Does NOT execute payment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cart": {
                        "type": "object",
                        "description": "Proposed cart with merchant_id, currency, items, and total_cents",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Agent justification for selecting these items",
                    },
                },
                "required": ["cart", "reasoning"],
            },
        },
    ]
