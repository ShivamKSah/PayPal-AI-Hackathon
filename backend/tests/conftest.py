"""Pytest configuration and global test isolation fixtures.

Enforces:
1. No unmocked live network calls to LLMs or PayPal.
2. time.sleep is monkeypatched to zero wait time so retries run instantly.
3. Outbound network connections to non-loopback addresses fail immediately (allow only 127.0.0.1 and ::1).
"""

import time
import pytest
import pytest_socket
from backend.app import config


@pytest.fixture(autouse=True)
def enforce_test_isolation(monkeypatch):
    """
    Ensure every test runs fully offline, deterministically, and fast:
    - Sets LLM_API_KEY to empty string and LLM_BASE_URL to dummy value.
    - Patches config, environment, and module-level variables.
    - Patches time.sleep so backoff never waits.
    - Allows only loopback connections (127.0.0.1, ::1); any outbound connect fails immediately.
    """
    # 1. Environment variables
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.invalid/v1")

    # 2. Config object
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://llm.invalid/v1")

    # 3. Module-level variables across app
    try:
        from backend.app.policy import constraints
        monkeypatch.setattr(constraints, "LLM_API_KEY", "")
        monkeypatch.setattr(constraints, "LLM_BASE_URL", "https://llm.invalid/v1")
    except (ImportError, AttributeError):
        pass

    try:
        from backend.app.agent import agent
        monkeypatch.setattr(agent, "LLM_API_KEY", "")
        monkeypatch.setattr(agent, "LLM_BASE_URL", "https://llm.invalid/v1")
    except (ImportError, AttributeError):
        pass

    try:
        from backend.app.recovery import labeler
        monkeypatch.setattr(labeler, "LLM_API_KEY", "")
    except (ImportError, AttributeError):
        pass

    # 4. Instant sleep for fast retries
    monkeypatch.setattr(time, "sleep", lambda *args, **kwargs: None)

    # 5. Outbound network guard: allow ONLY loopback connections
    pytest_socket.socket_allow_hosts(["127.0.0.1", "::1"], allow_unix_socket=True)

    yield

    pytest_socket._remove_restrictions()
