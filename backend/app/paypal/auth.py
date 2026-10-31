"""PayPal OAuth 2.0 token management with caching."""

import time
import base64
import logging
from typing import Optional, Tuple
import httpx

from backend.app.config import (
    PAYPAL_BASE_URL,
    PAYPAL_CLIENT_ID,
    PAYPAL_CLIENT_SECRET,
)

logger = logging.getLogger("mandate.paypal.auth")

_cached_token: Optional[str] = None
_token_expires_at: float = 0.0


def get_access_token(force_refresh: bool = False) -> str:
    """
    Get valid PayPal OAuth 2.0 access token.
    Caches token with a 60-second safety margin.
    """
    global _cached_token, _token_expires_at

    now = time.time()
    if not force_refresh and _cached_token and now < (_token_expires_at - 60):
        return _cached_token

    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise ValueError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be configured")

    creds = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}".encode()).decode()

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{PAYPAL_BASE_URL}/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content="grant_type=client_credentials",
        )
        resp.raise_for_status()
        data = resp.json()

        _cached_token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        _token_expires_at = now + expires_in
        logger.info(f"Refreshed PayPal OAuth token. Expires in {expires_in}s.")

        return _cached_token
