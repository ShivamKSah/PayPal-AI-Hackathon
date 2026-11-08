"""Unit tests for LLM retry and model fallback.

All tests in this module use mock HTTP transports via respx to test retry sequences,
fast-model fallback, deterministic rule fallback, and audit logging.
No live LLM network calls are made.
[LABEL: MOCKED UNIT TESTS]
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest
import respx
import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from backend.app.main import app
from backend.app.db import engine, init_db
from backend.app.models import Mandate, Proposal, AuditLog
from backend.app.policy.constraints import extract_constraints
from backend.app.policy.engine import PolicyEngine
from backend.app.agent.agent import ShoppingAgent
from backend.app.catalog import get_catalog_adapter

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


SAMPLE_CONSTRAINT_SCHEMA_RESPONSE = {
    "id": "gen-test-schema-001",
    "model": "google/gemini-2.5-flash",
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": json.dumps({
                    "constraints": [
                        {
                            "field": "width",
                            "target_value": 160,
                            "unit": "cm",
                            "raw_mention": "160 cm",
                        }
                    ]
                }),
            }
        }
    ],
}

SAMPLE_FAST_MODEL_CONSTRAINT_RESPONSE = {
    "id": "gen-test-schema-002",
    "model": "google/gemini-2.5-flash-lite",
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": json.dumps({
                    "constraints": [
                        {
                            "field": "width",
                            "target_value": 160,
                            "unit": "cm",
                            "raw_mention": "160 cm",
                        }
                    ]
                }),
            }
        }
    ],
}


# ── 1. 503 Then Success (Check 9) ─────────────────────────────────────

@respx.mock
def test_check9_retry_503_then_success_mocked(monkeypatch):
    """
    [MOCKED] Check 9: On initial HTTP 503, retries the primary model and succeeds on attempt 2.
    - llm_fallback must be False
    - attempts must be 2
    - model_reported must match the primary model
    """
    monkeypatch.setattr("backend.app.config.LLM_API_KEY", "sk-test-key")
    monkeypatch.setattr("backend.app.config.LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr("backend.app.config.LLM_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr("backend.app.config.LLM_MODEL_FAST", "google/gemini-2.5-flash-lite")
    monkeypatch.setattr("time.sleep", lambda _: None)

    route = respx.post("https://openrouter.ai/api/v1/chat/completions")
    route.side_effect = [
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(200, json=SAMPLE_CONSTRAINT_SCHEMA_RESPONSE),
    ]

    res = extract_constraints("standing desk 160 cm")
    assert res.llm_fallback is False
    assert res.attempts == 2
    assert res.model_reported == "google/gemini-2.5-flash"
    assert res.extraction_method == "llm_openrouter"
    assert len(res.constraints) == 1
    assert res.constraints[0].field == "width"
    assert route.call_count == 2


@respx.mock
def test_check9_429_on_primary_goes_straight_to_fast_model_mocked(monkeypatch):
    """
    [MOCKED] Check 9: On HTTP 429 quota exceeded, do NOT retry primary;
    go straight to LLM_MODEL_FAST with attempts == 2.
    """
    monkeypatch.setattr("backend.app.config.LLM_API_KEY", "sk-test-key")
    monkeypatch.setattr("backend.app.config.LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr("backend.app.config.LLM_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr("backend.app.config.LLM_MODEL_FAST", "google/gemini-2.5-flash-lite")
    mock_sleep = MagicMock()
    monkeypatch.setattr("time.sleep", mock_sleep)

    route = respx.post("https://openrouter.ai/api/v1/chat/completions")
    route.side_effect = [
        httpx.Response(429, text='{"error": {"message": "Resource has been exhausted (e.g. check quota)."}}'),
        httpx.Response(200, json=SAMPLE_FAST_MODEL_CONSTRAINT_RESPONSE),
    ]

    res = extract_constraints("standing desk 160 cm")
    assert res.llm_fallback is False
    assert res.attempts == 2
    assert res.model_reported == "google/gemini-2.5-flash-lite"
    assert res.extraction_method == "llm_openrouter"
    assert len(res.constraints) == 1
    assert res.constraints[0].field == "width"
    assert route.call_count == 2
    # Verify no backoff sleep was performed on 429
    assert mock_sleep.call_count == 0


# ── 2. 503 Then Fast-Model Success (Check 9) ──────────────────────────

@respx.mock
def test_check9_retry_fast_model_success_mocked(monkeypatch):
    """
    [MOCKED] Check 9: 3 failures on primary model (1 initial + 2 retries),
    then succeeds on attempt 4 with LLM_MODEL_FAST.
    - llm_fallback must be False
    - attempts must be 4
    - model_reported must be LLM_MODEL_FAST
    """
    monkeypatch.setattr("backend.app.config.LLM_API_KEY", "sk-test-key")
    monkeypatch.setattr("backend.app.config.LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr("backend.app.config.LLM_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr("backend.app.config.LLM_MODEL_FAST", "google/gemini-2.5-flash-lite")
    monkeypatch.setattr("time.sleep", lambda _: None)

    route = respx.post("https://openrouter.ai/api/v1/chat/completions")
    route.side_effect = [
        httpx.Response(503, text="Primary try 1 unavailable"),
        httpx.Response(500, text="Primary retry 1 internal error"),
        httpx.Response(502, text="Primary retry 2 bad gateway"),
        httpx.Response(200, json=SAMPLE_FAST_MODEL_CONSTRAINT_RESPONSE),
    ]

    res = extract_constraints("standing desk 160 cm")
    assert res.llm_fallback is False
    assert res.attempts == 4
    assert res.model_reported == "google/gemini-2.5-flash-lite"
    assert res.extraction_method == "llm_openrouter"
    assert len(res.constraints) == 1
    assert res.constraints[0].field == "width"
    assert route.call_count == 4


# ── 3. All Fail Leading to Rules Fallback (Check 9) ───────────────────

@respx.mock
def test_check9_all_fail_fallback_to_rules_mocked(monkeypatch):
    """
    [MOCKED] Check 9: All 4 attempts fail (3 on primary, 1 on fast).
    Falls back to deterministic regex rules with llm_fallback=True.
    """
    monkeypatch.setattr("backend.app.config.LLM_API_KEY", "sk-test-key")
    monkeypatch.setattr("backend.app.config.LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr("backend.app.config.LLM_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr("backend.app.config.LLM_MODEL_FAST", "google/gemini-2.5-flash-lite")
    monkeypatch.setattr("time.sleep", lambda _: None)

    route = respx.post("https://openrouter.ai/api/v1/chat/completions")
    route.side_effect = [
        httpx.Response(503, text="Primary 1 unavailable"),
        httpx.Response(503, text="Primary retry 1 unavailable"),
        httpx.Response(504, text="Primary retry 2 gateway timeout"),
        httpx.Response(500, text="Fast model unavailable"),
    ]

    res = extract_constraints("standing desk 160 cm")
    assert res.llm_fallback is True
    assert res.attempts == 4
    assert res.extraction_method == "regex_fallback"
    assert len(res.constraints) == 1
    assert res.constraints[0].field == "width"
    assert route.call_count == 4


# ── 4. End-to-End Invariant: LLM Failure NEVER Causes DENY & Records Audit ──

@respx.mock
def test_llm_failure_never_causes_deny_and_records_audit_mocked(monkeypatch):
    """
    [MOCKED] Non-negotiable safety invariant:
    1. Complete LLM outage (all retries fail) must NEVER produce a DENY verdict on its own.
    2. Missing catalog spec leads to ESCALATE (never DENY).
    3. Matching catalog spec leads to APPROVE (never DENY).
    4. Audit log records model_used, llm_attempts, and llm_fallback=True.
    """
    monkeypatch.setattr("backend.app.config.LLM_API_KEY", "sk-test-key")
    monkeypatch.setattr("backend.app.config.LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr("backend.app.config.LLM_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr("backend.app.config.LLM_MODEL_FAST", "google/gemini-2.5-flash-lite")
    monkeypatch.setattr("time.sleep", lambda _: None)

    # All LLM requests return 503
    route = respx.post("https://openrouter.ai/api/v1/chat/completions")
    route.side_effect = httpx.Response(503, text="Full LLM outage")

    # Create active mandate
    now = datetime.now(timezone.utc)
    mandate_payload = {
        "session_id": "session-llm-retry-test",
        "budget_cents": 100000,
        "per_txn_cap_cents": 50000,
        "categories": ["office_furniture", "electronics"],
        "merchant_allowlist": ["MERCHANT-OFFICE-SUPPLY", "MERCHANT-TECH-STORE"],
        "allow_new_merchants": False,
        "allow_recurring": False,
        "velocity_per_hour": 10,
        "near_limit_pct": 80,
        "escalate_first_merchant": False,
        "expires_at": (now + timedelta(days=2)).isoformat(),
    }
    m_resp = client.post("/mandates", json=mandate_payload)
    assert m_resp.status_code == 201
    mandate_id = m_resp.json()["id"]

    # Scenario A: Desk instruction with width constraint not in catalog specs -> ESCALATE (not DENY)
    desk_proposal_payload = {
        "mandate_id": mandate_id,
        "instruction_text": "buy a 160 cm standing desk",
        "cart_json": {
            "merchant_id": "MERCHANT-OFFICE-SUPPLY",
            "currency": "USD",
            "items": [
                {
                    "product_id": "desk-no-width",
                    "title": "Minimalist Standing Desk (Unspecified Width)",
                    "quantity": 1,
                    "unit_price_cents": 18000,
                }
            ],
            "subtotal_cents": 18000,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 18000,
        },
    }
    p_resp = client.post("/proposals", json=desk_proposal_payload)
    assert p_resp.status_code == 201
    p_data = p_resp.json()
    assert p_data["verdict"] == "ESCALATE"  # Must ESCALATE on missing width, NOT DENY
    assert p_data["llm_fallback"] is True
    assert p_data["llm_attempts"] == 4

    # Check audit log for Scenario A
    with Session(engine) as db:
        audit_entry = db.exec(
            select(AuditLog)
            .where(AuditLog.ref_id == p_data["id"])
            .order_by(AuditLog.ts.desc())
        ).first()
        assert audit_entry is not None
        assert audit_entry.event == "PROPOSAL_ESCALATE"
        assert audit_entry.payload["llm_fallback"] is True
        assert audit_entry.payload["llm_attempts"] == 4

    # Scenario B: Clean mouse instruction with no ambiguous constraints -> APPROVE (not DENY)
    mouse_proposal_payload = {
        "mandate_id": mandate_id,
        "instruction_text": "buy an ergonomic vertical mouse",
        "cart_json": {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": "USD",
            "items": [
                {
                    "product_id": "mouse-ergonomic",
                    "title": "Ergonomic Vertical Mouse",
                    "quantity": 1,
                    "unit_price_cents": 4500,
                }
            ],
            "subtotal_cents": 4500,
            "tax_cents": 0,
            "shipping_cents": 0,
            "fees_cents": 0,
            "total_cents": 4500,
        },
    }
    p2_resp = client.post("/proposals", json=mouse_proposal_payload)
    assert p2_resp.status_code == 201
    p2_data = p2_resp.json()
    assert p2_data["verdict"] == "APPROVE"  # Approves cleanly under rules fallback!
    assert p2_data["llm_fallback"] is True
    assert p2_data["llm_attempts"] == 4

    # Check audit log for Scenario B
    with Session(engine) as db:
        audit_entry2 = db.exec(
            select(AuditLog)
            .where(AuditLog.ref_id == p2_data["id"])
            .order_by(AuditLog.ts.desc())
        ).first()
        assert audit_entry2 is not None
        assert audit_entry2.event == "PROPOSAL_APPROVE"
        assert audit_entry2.payload["llm_fallback"] is True
        assert audit_entry2.payload["llm_attempts"] == 4


# ── 5. Shopping Agent Retry & Fallback ────────────────────────────────

@respx.mock
def test_shopping_agent_retry_503_then_success_mocked(monkeypatch):
    """
    [MOCKED] Shopping agent: 503 on attempt 1, then tool call proposal on attempt 2.
    """
    monkeypatch.setattr("backend.app.config.LLM_API_KEY", "sk-test-key")
    monkeypatch.setattr("backend.app.config.LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr("backend.app.config.LLM_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr("backend.app.config.LLM_MODEL_FAST", "google/gemini-2.5-flash-lite")
    monkeypatch.setattr("time.sleep", lambda _: None)

    tool_call_resp = {
        "id": "gen-agent-retry-01",
        "model": "google/gemini-2.5-flash",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_agent_1",
                            "type": "function",
                            "function": {
                                "name": "propose_purchase",
                                "arguments": json.dumps({
                                    "cart": {
                                        "merchant_id": "MERCHANT-TECH-STORE",
                                        "currency": "USD",
                                        "items": [
                                            {
                                                "product_id": "mouse-ergonomic",
                                                "quantity": 1,
                                                "unit_price_cents": 4500,
                                            }
                                        ],
                                        "total_cents": 4500,
                                    },
                                    "reasoning": "Proposed after retry",
                                }),
                            },
                        }
                    ],
                }
            }
        ],
    }

    route = respx.post("https://openrouter.ai/api/v1/chat/completions")
    route.side_effect = [
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(200, json=tool_call_resp),
    ]

    dummy_mandate = MagicMock()
    dummy_mandate.id = "mandate-agent-retry-test"
    agent = ShoppingAgent(mandate=dummy_mandate)

    result = agent.run("buy an ergonomic mouse")
    assert result["status"] == "PROPOSED"
    assert result["attempts"] == 2
    assert result["model_reported"] == "google/gemini-2.5-flash"
    assert result["proposal"]["model_used"] == "google/gemini-2.5-flash"
    assert result["proposal"]["llm_attempts"] == 2


@respx.mock
def test_shopping_agent_all_fail_deterministic_fallback_mocked(monkeypatch):
    """
    [MOCKED] Shopping agent: all 4 attempts fail with 503.
    Falls back cleanly to deterministic catalog search with llm_fallback=True.
    """
    monkeypatch.setattr("backend.app.config.LLM_API_KEY", "sk-test-key")
    monkeypatch.setattr("backend.app.config.LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr("backend.app.config.LLM_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr("backend.app.config.LLM_MODEL_FAST", "google/gemini-2.5-flash-lite")
    monkeypatch.setattr("time.sleep", lambda _: None)

    route = respx.post("https://openrouter.ai/api/v1/chat/completions")
    route.side_effect = httpx.Response(503, text="All attempts fail")

    dummy_mandate = MagicMock()
    dummy_mandate.id = "mandate-agent-fail-test"
    agent = ShoppingAgent(mandate=dummy_mandate)

    result = agent.run("ergonomic mouse")
    assert result["status"] == "PROPOSED"
    assert result["llm_fallback"] is True
    assert result["attempts"] == 4
    assert result["proposal"]["llm_fallback"] is True
    assert result["proposal"]["llm_attempts"] == 4
    assert result["proposal"]["cart_json"]["total_cents"] == 4500
