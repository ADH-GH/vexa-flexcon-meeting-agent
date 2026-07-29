"""Agent-Connect API keys — the Flexcon Agents integration surface.

A key is shown ONCE at creation; only its SHA-256 hash is stored. Lookup is by hash, so a leaked
database never yields usable keys. Keys are per tenant and bind an API request to that tenant.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import secrets

from sqlalchemy import select

from .models import ApiKey

PREFIX = "fxma_"   # Flexcon Meeting Agent


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def mint(db, tenant_id: int, name: str, scopes: list[str] | None = None) -> str:
    """Create a key and return the PLAINTEXT once — it is never recoverable afterwards."""
    key = PREFIX + secrets.token_urlsafe(32)
    db.add(ApiKey(tenant_id=tenant_id, name=name or "unnamed", key_hash=_hash(key),
                  scopes=scopes or ["meetings:read", "dispatch"]))
    db.commit()
    return key


def resolve(db, key: str) -> ApiKey | None:
    """Map a presented key to its record (and stamp last_used). Control-plane query: RLS is not yet
    scoped when authenticating, so this reads by hash across tenants — the hash is the credential."""
    if not key:
        return None
    row = db.scalars(select(ApiKey).where(ApiKey.key_hash == _hash(key))).first()
    if row:
        row.last_used = dt.datetime.now(dt.timezone.utc)
        db.commit()
    return row


def revoke(db, tenant_id: int, key_id: int) -> None:
    row = db.get(ApiKey, key_id)
    if row and row.tenant_id == tenant_id:
        db.delete(row)
        db.commit()
