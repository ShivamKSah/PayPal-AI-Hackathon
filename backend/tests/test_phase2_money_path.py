"""Unit and integration tests for Phase 2 Money Path.

Tests:
1. Cart hash binding (tampered cart refused).
2. DENIED proposal cannot execute (zero outbound charges).
3. Budget reservation state machine (RESERVED -> CAPTURED -> CONFIRMED).
4. Executor charges via pinned vault shape with fresh PayPal-Request-Id.
5. Reconciler and webhook advance CAPTURED -> CONFIRMED.
6. Audit log hash chain verifies completely via GET /audit/verify.
7. Shopping agent tool isolation (NO PayPal tools, NO refund tools, NO credentials).
8. Masking helper masks vault tokens and customer IDs.
9. Session token scoping and verification.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.db import init_db
from backend.app.security import mask_token, sign_session_token, verify_session_token
from backend.app.agent import ShoppingAgent, guard
from backend.app.agent.tools import get_tool_definitions

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_database():
    init_db()


# ── 1. Masking Helper Tests ──────────────────────────────────────────

def test_mask_token():
    assert mask_token("VAULT-1234567890ABCDEF") == "VAUL...CDEF"
    assert mask_token("CUST-987654321") == "CUST...4321"
    assert mask_token("short") == "***REDACTED***"
    assert mask_token("") == ""
    assert mask_token(None) == ""


# ── 2. Shopping Agent Tool Isolation Tests ───────────────────────────

def test_shopping_agent_tool_isolation():
    """Verify shopping agent tools contain NO PayPal credentials or refund tools."""
    dummy_mandate = MagicMock()
    dummy_mandate.id = "mandate-guard-test"
    agent = ShoppingAgent(mandate=dummy_mandate)

    tool_names = [t["name"] for t in agent.tools]
    assert "search_catalog" in tool_names
    assert "propose_purchase" in tool_names

    # Non-negotiable safety invariant (Section 3):
    # Agent must NEVER hold PayPal tools or refund tools
    for name in tool_names:
        assert "paypal" not in name.lower()
        assert "refund" not in name.lower()
        assert "capture" not in name.lower()
        assert "vault" not in name.lower()
        assert "charge" not in name.lower()

    # Test guard explicitly against hazardous candidate tools
    hazardous_tools = [
        {"name": "search_catalog", "description": "Safe search"},
        {"name": "paypal_charge_card", "description": "Execute PayPal charge"},
        {"name": "issue_refund", "description": "Refund customer"},
        {"name": "manage_vault", "description": "Vault management"},
        {"name": "propose_purchase", "description": "Submit proposal"},
    ]
    sanitized = guard(hazardous_tools, dummy_mandate)
    sanitized_names = [t["name"] for t in sanitized]
    assert sanitized_names == ["search_catalog", "propose_purchase"]


def test_shopping_agent_with_recorded_openrouter_tool_calls(monkeypatch):
    """Verify shopping agent handles OpenRouter tool calling with reported model logging."""
    import httpx
    from backend.app.agent.agent import ShoppingAgent

    # Mock tool call response for propose_purchase
    tool_call_resp = {
        "id": "gen-agent-1234",
        "model": "google/gemini-2.5-flash",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_prop_1",
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
                                    "reasoning": "Selected ergonomic mouse via OpenRouter tool calling",
                                }),
                            },
                        }
                    ],
                }
            }
        ],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = tool_call_resp

    monkeypatch.setattr("backend.app.agent.agent.LLM_API_KEY", "sk-or-v1-agent-testkey")
    monkeypatch.setattr("backend.app.agent.agent.LLM_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setattr(httpx.Client, "post", lambda self, *args, **kwargs: mock_resp)

    dummy_mandate = MagicMock()
    dummy_mandate.id = "mandate-agent-tool-test"
    agent = ShoppingAgent(mandate=dummy_mandate)

    result = agent.run("buy an ergonomic mouse")
    assert result["status"] == "PROPOSED"
    assert result["model_reported"] == "google/gemini-2.5-flash"
    assert result["proposal"]["cart_json"]["total_cents"] == 4500
    assert "OpenRouter tool calling" in result["proposal"]["agent_reasoning"]


# ── 3. Session Token Scope Tests ─────────────────────────────────────

def test_session_token_signing_and_verification():
    mandate_id = "mandate-session-xyz"
    session_id = "session-buyer-001"
    token = sign_session_token(mandate_id, session_id)
    assert token is not None and len(token) > 20

    verified = verify_session_token(token)
    assert verified is not None
    assert verified["mandate_id"] == mandate_id
    assert verified["session_id"] == session_id

    # Tampered token fails
    assert verify_session_token(token + "corrupt") is None


# ── 4. Money Path: Lifecycle, Cart Hash Binding, and Execution ──────

def test_full_money_path_lifecycle_and_invariants():
    """
    End-to-end Money Path test:
    1. Create mandate with vault_token_ref.
    2. Submit valid proposal -> APPROVE with cart_hash.
    3. Execute proposal -> reserves budget, charges via pinned vault shape -> CAPTURED.
    4. Verify budget reservation: reserved_cents incremented.
    5. Reconcile / Webhook -> moves CAPTURED to CONFIRMED, moves reserved -> spent.
    6. Verify audit chain via GET /audit/verify.
    """
    now = datetime.now(timezone.utc)
    mandate_payload = {
        "session_id": "session-money-test",
        "budget_cents": 50000,
        "per_txn_cap_cents": 20000,
        "categories": ["electronics"],
        "merchant_allowlist": ["MERCHANT-TECH-STORE"],
        "allow_new_merchants": False,
        "allow_recurring": False,
        "velocity_per_hour": 5,
        "near_limit_pct": 80,
        "escalate_first_merchant": False,
        "expires_at": (now + timedelta(days=2)).isoformat(),
        "vault_token_ref": "sandbox-vault-token-987",
    }

    create_resp = client.post("/mandates", json=mandate_payload)
    assert create_resp.status_code == 201
    mandate_data = create_resp.json()
    mandate_id = mandate_data["id"]
    session_token = mandate_data.get("session_token")
    assert session_token is not None

    # Submit an approving proposal ($45 ergonomic mouse)
    proposal_payload = {
        "mandate_id": mandate_id,
        "instruction_text": "buy an ergonomic vertical mouse",
        "agent_reasoning": "Selected ergonomic mouse within budget",
        "cart_json": {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": "USD",
            "items": [
                {
                    "product_id": "mouse-ergonomic",
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

    prop_resp = client.post(
        "/proposals",
        json=proposal_payload,
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert prop_resp.status_code == 201
    prop_data = prop_resp.json()
    assert prop_data["verdict"] == "APPROVE"
    proposal_id = prop_data["id"]
    cart_hash = prop_data["cart_hash"]
    assert cart_hash is not None

    import uuid
    cap_id = f"CAPTURE-PP-{uuid.uuid4()}"
    mock_charge_response = {
        "order_id": f"ORDER-PP-{uuid.uuid4()}",
        "capture_id": cap_id,
        "status": "COMPLETED",
        "raw": {"status": "COMPLETED"},
    }

    with patch(
        "backend.app.main.payment_gateway.charge",
        return_value=mock_charge_response,
    ) as mock_charge:
        exec_resp = client.post(
            f"/proposals/{proposal_id}/execute",
            headers={"Authorization": f"Bearer {session_token}"},
        )
        assert exec_resp.status_code == 201
        payment_data = exec_resp.json()

        # Executor verified status COMPLETED as CAPTURED
        assert payment_data["state"] == "CAPTURED"
        assert payment_data["capture_id"] == cap_id
        assert payment_data["amount_cents"] == 4500

        # Confirm charge called with pinned vault shape and decrypted token
        mock_charge.assert_called_once()
        call_kwargs = mock_charge.call_args[1]
        assert call_kwargs["vault_token"] == "sandbox-vault-token-987"
        assert call_kwargs["amount_cents"] == 4500
        assert "req-exec-" in call_kwargs["request_id"]

    # Verify mandate budget reservation
    m_check = client.get(f"/mandates/{mandate_id}").json()
    assert m_check["reserved_cents"] == 4500
    assert m_check["spent_cents"] == 0

    # Advance CAPTURED -> CONFIRMED via Reconciler
    rec_resp = client.post("/reconcile")
    assert rec_resp.status_code == 200
    assert rec_resp.json()["reconciled_count"] >= 1

    # Check mandate updated: reserved moved to spent
    m_after_rec = client.get(f"/mandates/{mandate_id}").json()
    assert m_after_rec["reserved_cents"] == 0
    assert m_after_rec["spent_cents"] == 4500

    # Audit chain verification
    verify_resp = client.get("/audit/verify")
    assert verify_resp.status_code == 200
    verify_data = verify_resp.json()
    assert verify_data["verified"] is True
    assert verify_data["count"] >= 4


# ── 5. Invariant Tests: DENY Refusal and Hash Mismatch ──────────────

def test_denied_proposal_cannot_execute():
    """A DENIED proposal must never reach executor code path."""
    now = datetime.now(timezone.utc)
    mandate_payload = {
        "session_id": "session-deny-exec",
        "budget_cents": 50000,
        "per_txn_cap_cents": 20000,
        "categories": ["electronics"],
        "merchant_allowlist": ["MERCHANT-TECH-STORE"],
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "vault_token_ref": "token-test",
    }
    m = client.post("/mandates", json=mandate_payload).json()

    # Proposal with forbidden category -> DENY
    deny_prop_payload = {
        "mandate_id": m["id"],
        "instruction_text": "buy a gift card",
        "cart_json": {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": "USD",
            "items": [{"product_id": "gift-card-50", "quantity": 1, "unit_price_cents": 5000}],
            "total_cents": 5000,
        },
    }
    p = client.post("/proposals", json=deny_prop_payload).json()
    assert p["verdict"] == "DENY"

    # Attempting to execute must be rejected
    exec_resp = client.post(f"/proposals/{p['id']}/execute")
    assert exec_resp.status_code == 400
    assert "Cannot execute proposal with verdict 'DENY'" in exec_resp.json()["detail"]


def test_cart_hash_tamper_refused():
    """If cart is modified after evaluation, execute must refuse execution."""
    from sqlmodel import Session
    from backend.app.db import engine
    from backend.app.models import Proposal

    now = datetime.now(timezone.utc)
    m = client.post("/mandates", json={
        "session_id": "session-tamper-test",
        "budget_cents": 50000,
        "per_txn_cap_cents": 20000,
        "categories": ["electronics"],
        "merchant_allowlist": ["MERCHANT-TECH-STORE"],
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "vault_token_ref": "token-test",
    }).json()

    prop = client.post("/proposals", json={
        "mandate_id": m["id"],
        "instruction_text": "buy mouse",
        "cart_json": {
            "merchant_id": "MERCHANT-TECH-STORE",
            "currency": "USD",
            "items": [{"product_id": "mouse-ergonomic", "quantity": 1, "unit_price_cents": 4500}],
            "total_cents": 4500,
        },
    }).json()
    assert prop["verdict"] == "APPROVE"

    # Tamper with cart in database behind the engine's back
    with Session(engine) as session:
        db_prop = session.get(Proposal, prop["id"])
        tampered_cart = dict(db_prop.cart_json)
        tampered_cart["total_cents"] = 9999
        db_prop.cart_json = tampered_cart
        session.add(db_prop)
        session.commit()

    # Execute must detect hash mismatch and refuse
    exec_resp = client.post(f"/proposals/{prop['id']}/execute")
    assert exec_resp.status_code == 400
    assert "Cart hash binding mismatch" in exec_resp.json()["detail"]
