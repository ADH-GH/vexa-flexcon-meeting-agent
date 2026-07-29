"""Symmetric encryption for user refresh tokens at rest (Fernet). Key from env (TOKEN_ENCRYPTION_KEY,
a urlsafe base64 32-byte key; generate with `python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"`). If unset, a dev key is derived — set a real one in prod."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from .config import settings


def _key() -> bytes:
    k = settings.token_encryption_key
    if k:
        return k.encode()
    # dev fallback: derive a stable key from the session secret (NOT for production)
    return base64.urlsafe_b64encode(hashlib.sha256(settings.session_secret.encode()).digest())


_f = Fernet(_key())


def encrypt(plaintext: str) -> str:
    return _f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _f.decrypt(ciphertext.encode()).decode() if ciphertext else ""
