# Mandate

> Never give an AI agent an unbounded wallet; give it a mandate.

Mandate is a deterministic policy engine and autonomous payment execution gateway that sits between AI shopping agents and payment providers (PayPal). It enforces hard spending limits, category boundaries, merchant allowlists, velocity throttles, and cart integrity in plain code. The AI agent searches and proposes purchases; the policy engine verifies the proposal against the buyer's explicit rules; and the gateway autonomously executes approved purchases using securely vaulted tokens. No LLM ever sits in the money path.

---

## The Problem

AI shopping agents are increasingly capable of finding products, negotiating, and filling carts. However, giving an autonomous agent direct access to credit cards or unrestricted credentials creates unbounded financial risk: hallucinated items, malicious merchant redirection, price gouging, runaway velocity, and lack of verifiable audit trails. 

Mandate solves this by enforcing:
* **Zero Trust in the Agent:** The agent is only granted candidate search and proposal tools. It possesses no PayPal credentials, no refund mechanisms, and no ability to initiate payments directly.
* **Deterministic Policy Gates:** 11 mandatory checks written in plain Python evaluate every proposal with zero early exit.
* **Cryptographic Cart Binding:** Approved carts are bound by canonical SHA-256 hashes. If the cart is modified between evaluation and execution, the executor refuses to charge.
* **Hash-Chained Audit Trail:** Every mandate creation, evaluation, payment state transition, and webhook delivery is appended to a tamper-evident SHA-256 hash chain verified via `GET /audit/verify`.

---

## How It Works

```
[ Buyer ] ──( 1. Create Mandate & Approve $1 Grant )──> [ Mandate API ] ──> [ PayPal Vault ]
                                                                                  │
[ Shopping Agent ] ──( 2. Search & Propose Purchase )─> [ Policy Engine ]        │
                                                               │ (11 Checks)      │
                                                         [ APPROVE ]              │
                                                               │                  │
[ Executor ] ─────────( 3. Server-Side Autonomous Charge via vault_id )───────────┘
                                                               │
[ Webhook / Reconciler ] ──( 4. Move CAPTURED -> CONFIRMED )───┘
```

The system operates across five core stages:

1. **Grant:** The buyer establishes a mandate with a spending budget, per-transaction cap, allowlisted merchants, and category limits. To authorize autonomous charges, the buyer performs a one-time approval of a refundable $1.00 setup order in PayPal Sandbox. Mandate captures the order, extracts the vault ID, encrypts it with Fernet (`TOKEN_ENCRYPTION_KEY`), stores it in `mandate.vault_token_ref`, and immediately refunds the $1.00 test authorization.
2. **Propose:** The agent analyzes the buyer's intent, queries the catalog, and submits a proposal via `POST /proposals` with a short-lived session token.
3. **Verify (11 Deterministic Checks):** The engine evaluates:
   * **Check 1:** Mandate status must be `ACTIVE` and within `expires_at`.
   * **Check 2:** Cart total must be $\le$ `per_txn_cap_cents`.
   * **Check 3:** Cart total must be $\le$ remaining unreserved budget.
   * **Check 4:** Merchant must be on `merchant_allowlist` (unless `allow_new_merchants` is true).
   * **Check 5:** All item categories must be on allowlist.
   * **Check 6:** Cart price and currency must match live/seeded catalog snapshot.
   * **Check 7:** Transaction velocity in the last hour must be $\le$ `velocity_per_hour`.
   * **Check 8:** Idempotency gate rejects duplicate identical cart hashes.
   * **Check 9:** Ambiguity gate extracts user constraints (e.g., dimensions) and confirms catalog specifications exist (can only lead to `ESCALATE`, never `DENY`).
   * **Check 10:** Recurring billing items are refused unless explicitly permitted.
   * **Check 11:** Near-limit threshold ($\ge 80\%$) and first-time merchant escalation triggers review.
4. **Execute:** Approved proposals advance through an atomic budget reservation transaction: `RESERVED` $\rightarrow$ `ORDER_CREATED` $\rightarrow$ `CAPTURED`. The server-side executor charges PayPal using the pinned vault shape `payment_source.paypal.vault_id` with a fresh `PayPal-Request-Id` and token refresh. No buyer browser interaction is required.
5. **Reconcile & Audit:** Inbound webhooks (`PAYMENT.CAPTURE.COMPLETED`) or polling reconcilers (`GET /v2/checkout/orders/{id}`) advance payments from `CAPTURED` to `CONFIRMED`, shifting reserved budget to spent budget. Every transition is recorded in the hash-chained audit log.

---

## How the Grant Works

* **One-Time Authorization:** Mandate initiates a $1.00 vault setup order with PayPal:
  ```json
  {
    "intent": "CAPTURE",
    "payment_source": {
      "paypal": {
        "attributes": {
          "vault": {
            "store_in_vault": "ON_SUCCESS",
            "usage_type": "MERCHANT",
            "customer_type": "CONSUMER"
          }
        }
      }
    }
  }
  ```
* **Capture & Extraction:** When approved by the buyer in the PayPal sandbox popup/browser, Mandate captures the order, extracts the customer vault token from `payment_source.paypal.attributes.vault.id`, and encrypts it locally.
* **Immediate Refund:** Mandate immediately refunds the $1.00 setup capture via `POST /v2/payments/captures/{capture_id}/refund`.
* **Zero Buyer Friction for Purchases:** All subsequent purchases execute headlessly using `payment_source.paypal.vault_id`.
* **Sandbox Only:** Operates entirely within the PayPal Sandbox environment. No real money moves.

---

## What is Real and What is Mocked

To maintain strict engineering transparency:

### Real (Live Sandbox Network Calls)
* **PayPal OAuth 2.0:** Live token exchanges at `https://api-m.sandbox.paypal.com/v1/oauth2/token`.
* **Grant Creation:** Live $1.00 setup orders created via `POST /v2/checkout/orders`.
* **Capture & Token Storage:** Real captures and vault token IDs extracted from PayPal Sandbox.
* **Autonomous Charges:** Real server-side vault charges using `payment_source.paypal.vault_id`.
* **Refunds:** Real live PayPal refunds issued via `POST /v2/payments/captures/{id}/refund`.
* **Order Inspection:** Live order status checks via `GET /v2/checkout/orders/{id}`.
* **Webhook Signature Verification:** Verified against PayPal's live `POST /v1/notifications/verify-webhook-signature`.

### Mocked, Seeded, or Fallbacks
* **Catalog:** Uses an in-memory seeded catalog ([backend/app/catalog/mock.py](file:///e:/mandate%20PP%20Shivam/backend/app/catalog/mock.py)) when `CATALOG_API_KEY` is omitted.
* **LLM Fallback:** If the upstream LLM endpoint (Google AI Studio or OpenRouter) fails or runs out of credits, extraction falls back to a deterministic regex parser, logging a WARNING and recording `llm_fallback=True` in the proposal and audit log.
* **Unit Tests:** All automated unit tests in `backend/tests/` use `respx` or `unittest.mock` to mock network responses for fast, deterministic, offline CI execution. Mocks are never used in live scripts (`scripts/e2e_phase2_live.py`).

---

## Environment Variables

| Variable | Description | Default / Example |
| :--- | :--- | :--- |
| `PAYPAL_CLIENT_ID` | PayPal Sandbox REST App Client ID | *(Required for live PayPal calls)* |
| `PAYPAL_CLIENT_SECRET` | PayPal Sandbox REST App Client Secret | *(Required for live PayPal calls)* |
| `PAYPAL_ENV` | PayPal Environment (`sandbox` or `production`) | `sandbox` |
| `PAYPAL_WEBHOOK_ID` | PayPal Webhook ID for verifying webhook signatures | *(Optional in dev, required in prod)* |
| `TOKEN_ENCRYPTION_KEY` | 32-byte base64 Fernet key for encrypting vault tokens | *(Generate via `cryptography.fernet`)* |
| `SESSION_SECRET` | Secret key for signing buyer/agent session JWT tokens | *(Random high-entropy string)* |
| `DATABASE_URL` | SQLite or PostgreSQL connection string | `sqlite:///./mandate.db` |
| `LLM_API_KEY` | API key for OpenAI-compatible LLM endpoint | *(Google AI Studio or OpenRouter key)* |
| `LLM_BASE_URL` | Base URL for chat completions | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `LLM_MODEL` | Primary model identifier | `gemini-2.5-flash` |
| `LLM_MODEL_FAST` | Fast/compact model identifier | `gemini-2.5-flash` |
| `DAILY_LLM_BUDGET` | Daily budget cap for LLM inference in USD | `5.00` |
| `CATALOG_API_KEY` | Optional API key for live catalog providers | *(Empty for mock catalog)* |
| `TUNNEL_URL` | Public webhook tunnel URL for local dev | *(Optional, e.g. ngrok or cloudflare)* |

---

## Running Locally

### 1. Prerequisites
* Python 3.11+
* Git

### 2. Setup Virtual Environment
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
```

### 3. Configure Environment
Copy the template and configure your sandbox credentials:
```powershell
Copy-Item .env.example .env
```
Generate an encryption key:
```powershell
python -c "from cryptography.fernet import Fernet; print('TOKEN_ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
```
Paste the generated key and your `SESSION_SECRET` into `.env`.

### 4. Verify LLM Endpoint (Optional)
```powershell
python scripts/check_llm.py
```

### 5. Start the Server
```powershell
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload
```
API Documentation is available at `http://127.0.0.1:8000/docs`.

---

## Running Tests

Execute the automated test suite (all unit tests run with offline mocks):
```powershell
pytest backend/tests
```

Run with coverage:
```powershell
pytest backend/tests --cov=backend/app
```

---

## Deploying to Render

The repository includes a ready-to-use [render.yaml](./render.yaml) blueprint that configures a Python web service and a managed PostgreSQL database.

### Frontend Architecture
* **Phase 2:** The backend FastAPI service serves all API endpoints, health checks, and webhooks. The `frontend/` directory is a placeholder for the Phase 3 React dashboard.

### Manual Deployment Steps

1. **Push Repository:** Ensure your repository is pushed to GitHub or GitLab.
2. **Connect to Render:**
   * Log in to the [Render Dashboard](https://dashboard.render.com/).
   * Click **New +** $\rightarrow$ **Blueprint**.
   * Connect your `mandate` repository.
3. **Review Services:** Render reads `render.yaml` and will detect:
   * **Web Service:** `mandate` (Python runtime, running `uvicorn backend.app.main:app`).
   * **Database:** `mandate-db` (PostgreSQL).
4. **Database Plan Selection (Important):**
   * *Note:* Render's free PostgreSQL tier expires automatically after 30 days. For persistent deployments, select a paid tier (e.g. Starter) in the database settings.
5. **Enter Environment Secrets:**
   In the Render dashboard, populate the secrets marked `sync: false`:
   * `PAYPAL_CLIENT_ID`: Your PayPal sandbox client ID.
   * `PAYPAL_CLIENT_SECRET`: Your PayPal sandbox client secret.
   * `TOKEN_ENCRYPTION_KEY`: Your 32-byte Fernet key.
   * `SESSION_SECRET`: Your session signing secret.
   * `LLM_API_KEY`: Your Google AI Studio or OpenRouter key.
   * `PAYPAL_WEBHOOK_ID`: The webhook ID created in your PayPal Developer Dashboard pointing to `https://<your-render-url>/webhooks/paypal`.
6. **Deploy:** Click **Apply**. Render will provision the database, install requirements, run table creation migrations, and start the service.
7. **Verify Deployment:**
   * Test health: `GET https://<your-render-url>/health` $\rightarrow$ `{"status": "ok"}`.
   * Verify audit chain: `GET https://<your-render-url>/audit/verify` $\rightarrow$ `{"verified": true, "count": 0}`.

---

## License

MIT License — see [LICENSE](./LICENSE).
