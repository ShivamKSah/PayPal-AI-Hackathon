"""Mock catalog adapter with seeded products for deterministic testing."""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class Product(BaseModel):
    id: str
    title: str
    price_cents: int
    currency: str = "USD"
    category: str
    is_recurring: bool = False
    specs: Dict[str, Any] = Field(default_factory=dict)
    merchant_id: str = "MERCHANT-OFFICE-SUPPLY"


SEEDED_PRODUCTS: Dict[str, Product] = {
    # 1. Desk with no width field (triggers check 9 ambiguity gate)
    "desk-no-width": Product(
        id="desk-no-width",
        title="Minimalist Standing Desk (Unspecified Width)",
        price_cents=18000,
        currency="USD",
        category="office_furniture",
        is_recurring=False,
        specs={"depth": 80, "height": 75, "color": "oak"},
        merchant_id="MERCHANT-OFFICE-SUPPLY",
    ),
    # 2. Gift card (triggers check 5 category gate)
    "gift-card-50": Product(
        id="gift-card-50",
        title="Store Gift Card $50",
        price_cents=5000,
        currency="USD",
        category="gift_cards",
        is_recurring=False,
        specs={},
        merchant_id="MERCHANT-GIFT-CARDS",
    ),
    # 3. Subscription item (triggers check 10 recurring billing)
    "sub-cloud-storage": Product(
        id="sub-cloud-storage",
        title="Cloud Storage Pro Monthly",
        price_cents=2000,
        currency="USD",
        category="software",
        is_recurring=True,
        specs={"storage": "2TB", "billing_interval": "monthly"},
        merchant_id="MERCHANT-CLOUD-INC",
    ),
    # 4. Normal $45 item
    "mouse-ergonomic": Product(
        id="mouse-ergonomic",
        title="Vertical Ergonomic Mouse",
        price_cents=4500,
        currency="USD",
        category="electronics",
        is_recurring=False,
        specs={"dpi": 1600, "connectivity": "wireless"},
        merchant_id="MERCHANT-TECH-STORE",
    ),
    # 5. Standing desk with width 160cm (passes check 9)
    "desk-standing-160": Product(
        id="desk-standing-160",
        title="Adjustable Standing Desk 160cm",
        price_cents=25000,
        currency="USD",
        category="office_furniture",
        is_recurring=False,
        specs={"width": 160, "depth": 80, "height": 75, "unit": "cm"},
        merchant_id="MERCHANT-OFFICE-SUPPLY",
    ),
}


class MockCatalogAdapter:
    """In-memory catalog provider."""

    def __init__(self, products: Optional[Dict[str, Product]] = None):
        self._products = products or SEEDED_PRODUCTS

    def get_product(self, product_id: str) -> Optional[Product]:
        return self._products.get(product_id)

    def search(self, query: str) -> List[Product]:
        q = query.lower()
        return [
            p for p in self._products.values()
            if q in p.title.lower() or q in p.category.lower() or q in p.id.lower()
        ]
