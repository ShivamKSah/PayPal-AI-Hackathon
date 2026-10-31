"""API endpoints integration tests."""

from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
import pytest

from backend.app.main import app
from backend.app.db import init_db

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_database():
    init_db()


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_mandate_lifecycle_and_proposal_submission():
    # 1. Create Mandate
    now = datetime.now(timezone.utc)
    mandate_payload = {
        "session_id": "session-test-01",
        "budget_cents": 50000,
        "per_txn_cap_cents": 30000,
        "categories": ["office_furniture", "electronics"],
        "merchant_allowlist": ["MERCHANT-OFFICE-SUPPLY", "MERCHANT-TECH-STORE"],
        "allow_new_merchants": False,
        "allow_recurring": False,
        "velocity_per_hour": 5,
        "near_limit_pct": 80,
        "expires_at": (now + timedelta(days=2)).isoformat(),
    }

    create_resp = client.post("/mandates", json=mandate_payload)
    assert create_resp.status_code == 201
    mandate_data = create_resp.json()
    mandate_id = mandate_data["id"]
    assert mandate_data["status"] == "ACTIVE"

    # 2. Get Mandate
    get_resp = client.get(f"/mandates/{mandate_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == mandate_id

    # 3. Submit Hand-Written Proposal (Approving scenario: normal ergonomic mouse $45)
    proposal_payload = {
        "mandate_id": mandate_id,
        "instruction_text": "buy a comfortable vertical mouse",
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
        "agent_reasoning": "Found ergonomic mouse with 1600 DPI",
    }

    prop_resp = client.post("/proposals", json=proposal_payload)
    assert prop_resp.status_code == 201
    prop_data = prop_resp.json()

    assert prop_data["mandate_id"] == mandate_id
    assert prop_data["verdict"] == "APPROVE"
    assert "cart_hash" in prop_data
    # Crucial acceptance criterion: verdict with ALL rule hits listed (11 checks)
    assert "rule_hits" in prop_data
    assert len(prop_data["rule_hits"]) == 11
    assert all("check_id" in hit for hit in prop_data["rule_hits"])

    # 4. Fetch the created proposal
    prop_id = prop_data["id"]
    get_prop_resp = client.get(f"/proposals/{prop_id}")
    assert get_prop_resp.status_code == 200
    assert get_prop_resp.json()["verdict"] == "APPROVE"

    # 5. Revoke the mandate
    revoke_resp = client.post(f"/mandates/{mandate_id}/revoke")
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["status"] == "REVOKED"

    # 6. Proposal submitted against revoked mandate must DENY on Check 1
    prop_revoked_resp = client.post("/proposals", json=proposal_payload)
    assert prop_revoked_resp.status_code == 201
    assert prop_revoked_resp.json()["verdict"] == "DENY"
    check_1_hit = next(h for h in prop_revoked_resp.json()["rule_hits"] if h["check_id"] == 1)
    assert check_1_hit["passed"] is False
    assert check_1_hit["verdict_on_fail"] == "DENY"

