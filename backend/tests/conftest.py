"""Pytest configuration and global test fixtures.

Enforces unit test isolation:
- No unmocked live calls to PayPal or live LLMs.
"""

import pytest
from backend.app import config


@pytest.fixture(autouse=True)
def disable_live_network_by_default(monkeypatch):
    """
    Ensure unit tests run deterministically without calling live LLMs or PayPal.
    Tests testing LLM integration (e.g. test_llm_retry.py) use respx and explicitly
    set config.LLM_API_KEY within their test functions.
    """
    monkeypatch.setattr(config, "LLM_API_KEY", "")

