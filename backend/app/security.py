"""Security utilities for token encryption and session signing."""

import base64
from typing import Optional
from cryptography.fernet import Fernet

from backend.app.config import TOKEN_ENCRYPTION_KEY


def _get_fernet() -> Fernet:
    key = TOKEN_ENCRYPTION_KEY
    if not key:
        # Generate temporary key if not configured
        key = Fernet.generate_key().decode()
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        # Ensure 32-byte urlsafe base64 key
        padded = base64.urlsafe_b64encode(key[:32].ljust(32, "0").encode())
        return Fernet(padded)


def encrypt_vault_token(raw_token: str) -> str:
    """Encrypt a PayPal vault token for rest storage."""
    if not raw_token:
        return ""
    fernet = _get_fernet()
    return fernet.encrypt(raw_token.encode("utf-8")).decode("utf-8")


def decrypt_vault_token(encrypted_token: str) -> str:
    """Decrypt a stored PayPal vault token."""
    if not encrypted_token:
        return ""
    fernet = _get_fernet()
    return fernet.decrypt(encrypted_token.encode("utf-8")).decode("utf-8")


def mask_token(token: Optional[str]) -> str:
    """Mask vault token IDs, customer IDs, and sensitive secrets in logs."""
    if not token:
        return ""
    if len(token) <= 8:
        return "***REDACTED***"
    return f"{token[:4]}...{token[-4:]}"


def sign_session_token(mandate_id: str, session_id: str) -> str:
    """Create HMAC-SHA256 signed session token scoped to mandate."""
    import hmac
    import hashlib
    from backend.app.config import SESSION_SECRET
    secret = (SESSION_SECRET or "default-insecure-secret").encode("utf-8")
    payload = f"{mandate_id}:{session_id}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    # base64url encode payload + signature
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def verify_session_token(token_str: str) -> Optional[dict]:
    """Verify HMAC signature and return mandate_id and session_id."""
    import hmac
    import hashlib
    from backend.app.config import SESSION_SECRET
    try:
        raw = base64.urlsafe_b64decode(token_str.encode("utf-8")).decode("utf-8")
        mandate_id, session_id, sig = raw.split(":")
        secret = (SESSION_SECRET or "default-insecure-secret").encode("utf-8")
        expected_sig = hmac.new(secret, f"{mandate_id}:{session_id}".encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected_sig):
            return {"mandate_id": mandate_id, "session_id": session_id}
    except Exception:
        pass
    return None

