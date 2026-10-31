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

