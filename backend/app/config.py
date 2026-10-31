"""Central configuration. All tunables and env-var loading live here."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from repo root
_env_path = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_env_path)

# ── Project identity (rename here, nowhere else) ──────────────
PROJECT_NAME: str = "Mandate"

# ── PayPal ─────────────────────────────────────────────────────
PAYPAL_CLIENT_ID: str = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET: str = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_WEBHOOK_ID: str = os.environ.get("PAYPAL_WEBHOOK_ID", "")
PAYPAL_ENV: str = os.environ.get("PAYPAL_ENV", "sandbox")

PAYPAL_BASE_URL: str = (
    "https://api-m.sandbox.paypal.com"
    if PAYPAL_ENV == "sandbox"
    else "https://api-m.paypal.com"
)

# ── AI / LLM ──────────────────────────────────────────────────
LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "")
DAILY_LLM_BUDGET: float = float(os.environ.get("DAILY_LLM_BUDGET", "5.00"))

# ── Catalog ────────────────────────────────────────────────────
CATALOG_API_KEY: str = os.environ.get("CATALOG_API_KEY", "")
MOCK_CATALOG: bool = (
    os.environ.get("MOCK_CATALOG", "").lower() in ("true", "1", "yes")
    or not bool(CATALOG_API_KEY)
)

# ── Database ───────────────────────────────────────────────────
DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///./mandate.db")

# ── Security ───────────────────────────────────────────────────
TOKEN_ENCRYPTION_KEY: str = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
SESSION_SECRET: str = os.environ.get("SESSION_SECRET", "")

# ── Tunnel (webhook dev) ──────────────────────────────────────
TUNNEL_URL: str = os.environ.get("TUNNEL_URL", "")
