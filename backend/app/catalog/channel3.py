"""Channel3 catalog adapter."""

import logging
from typing import Optional, List, Dict, Any
from decimal import Decimal
import httpx

from backend.app.config import CATALOG_API_KEY
from backend.app.catalog.mock import Product, MockCatalogAdapter

logger = logging.getLogger("mandate.catalog.channel3")


class Channel3CatalogAdapter:
    """Channel3 live product search adapter."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or CATALOG_API_KEY
        self.base_url = "https://api.trychannel3.com/v1"
        self._fallback = MockCatalogAdapter()

    def get_product(self, product_id: str) -> Optional[Product]:
        """Fetch product details by ID or search fallback."""
        if not self.api_key:
            return self._fallback.get_product(product_id)

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{self.base_url}/search",
                    headers={"x-api-key": self.api_key},
                    params={"query": product_id, "limit": 1},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = data if isinstance(data, list) else data.get("results", data.get("data", []))
                    if results:
                        return self._map_to_product(results[0])
        except Exception as exc:
            logger.warning(f"Channel3 get_product error: {exc}. Falling back to mock catalog.")

        return self._fallback.get_product(product_id)

    def search(self, query: str) -> List[Product]:
        """Search products across Channel3 API."""
        if not self.api_key:
            return self._fallback.search(query)

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{self.base_url}/search",
                    headers={"x-api-key": self.api_key},
                    params={"query": query, "limit": 5},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = data if isinstance(data, list) else data.get("results", data.get("data", []))
                    return [self._map_to_product(item) for item in results]
        except Exception as exc:
            logger.warning(f"Channel3 search error: {exc}. Falling back to mock catalog.")

        return self._fallback.search(query)

    def _map_to_product(self, raw: Dict[str, Any]) -> Product:
        # Extract price safely into integer cents
        price_val = raw.get("price") or raw.get("price_cents") or 0
        if isinstance(price_val, (int, float)):
            price_cents = int(price_val * 100) if isinstance(price_val, float) else price_val
        elif isinstance(price_val, str):
            try:
                price_cents = int(Decimal(price_val.replace("$", "").strip()) * 100)
            except Exception:
                price_cents = 0
        else:
            price_cents = 0

        specs = raw.get("specs") or raw.get("attributes") or {}
        if not isinstance(specs, dict):
            specs = {"raw_specs": str(specs)}

        return Product(
            id=str(raw.get("id") or raw.get("product_id") or "ch3-item"),
            title=str(raw.get("title") or raw.get("name") or "Channel3 Item"),
            price_cents=price_cents,
            currency=raw.get("currency", "USD"),
            category=str(raw.get("category") or "general"),
            is_recurring=bool(raw.get("is_recurring", False)),
            specs=specs,
            merchant_id=str(raw.get("merchant_id") or raw.get("brand") or "CHANNEL3-MERCHANT"),
        )
