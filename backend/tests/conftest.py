"""Pytest configuration and global test isolation fixtures.

Enforces:
1. No unmocked live network calls to LLMs or PayPal.
2. time.sleep is monkeypatched to zero wait time so retries run instantly.
3. Outbound network calls fail loudly (SocketBlockedError), while loopback / in-process TestClient / respx work.
"""

import socket
import time
import pytest
from pytest_socket import SocketBlockedError
from backend.app import config


@pytest.fixture(autouse=True)
def enforce_test_isolation(monkeypatch):
    """
    Ensure every test runs fully offline, deterministically, and fast:
    - Sets LLM_API_KEY to empty string and LLM_BASE_URL to dummy value.
    - Patches config, environment, and module-level variables.
    - Patches time.sleep so backoff never waits.
    - Blocks real outbound network calls (SocketBlockedError).
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

    # 5. Outbound network guard: block external hosts, permit loopback & in-process
    orig_getaddrinfo = socket.getaddrinfo
    orig_connect = socket.socket.connect

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if host in ("127.0.0.1", "localhost", "::1", None, ""):
            return orig_getaddrinfo(host, port, *args, **kwargs)
        raise SocketBlockedError(f"A test attempted an outbound network call to host: {host}")

    def guarded_connect(sock, address):
        if isinstance(address, tuple) and len(address) >= 1:
            host = address[0]
            if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
                return orig_connect(sock, address)
        raise SocketBlockedError(f"A test attempted an outbound connection to: {address}")

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
