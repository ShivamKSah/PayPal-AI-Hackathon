"""Catalog adapter factory."""

from backend.app.config import MOCK_CATALOG, CATALOG_API_KEY
from backend.app.catalog.mock import Product, MockCatalogAdapter, SEEDED_PRODUCTS
from backend.app.catalog.channel3 import Channel3CatalogAdapter


def get_catalog_adapter():
    """Return active catalog adapter based on config."""
    if MOCK_CATALOG or not CATALOG_API_KEY:
        return MockCatalogAdapter()
    return Channel3CatalogAdapter()


__all__ = [
    "Product",
    "MockCatalogAdapter",
    "Channel3CatalogAdapter",
    "SEEDED_PRODUCTS",
    "get_catalog_adapter",
]
