"""Flexcon AI Marketplace as the identity and entitlement source.

The marketplace (React + Supabase) already owns what we would otherwise duplicate: it holds the
customer's Microsoft OAuth connection and it runs Stripe. Re-implementing either here would mean a
**second consent dialog** for the user and Stripe fanning out to two endpoints with two different
tenant models. So in `IDENTITY_SOURCE=marketplace` mode we read from it instead:

  * who may use the agent  → `agent_configurations` (is_active) + `subscriptions` (tier, status)
  * the user's Graph token → the marketplace's `get-oauth-token` edge function

Both go through the Supabase **service-role key**, which that function explicitly accepts for backend
callers. We never touch the OAuth tokens directly: they are encrypted at rest with a key only the
marketplace holds, so `get-oauth-token` is the only correct way in.

`IDENTITY_SOURCE=own` keeps the self-contained path (our own Entra OAuth + Stripe) for direct and
on-prem sales — see auth.py and billing.py.
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("marketplace")


def enabled() -> bool:
    return (settings.identity_source == "marketplace"
            and bool(settings.marketplace_supabase_url)
            and bool(settings.marketplace_service_role_key))


def _headers() -> dict:
    key = settings.marketplace_service_role_key
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _rest(path: str, params: dict) -> list[dict]:
    """PostgREST read against the marketplace database (service-role: bypasses RLS, so treat every
    result as cross-tenant data and scope it yourself)."""
    r = httpx.get(f"{settings.marketplace_supabase_url.rstrip('/')}/rest/v1/{path}",
                  params=params, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def active_subscribers() -> list[dict]:
    """Everyone entitled to run the Meeting Agent right now.

    Entitlement is the AND of two things the marketplace tracks separately: the agent is switched on
    for that user (`agent_configurations.is_active`) and the subscription behind it is live
    (`subscriptions.status`). Checking only one of them is how you end up serving cancelled customers
    — or ignoring paying ones.
    """
    if not enabled():
        return []
    agent = _rest("agents", {"slug": f"eq.{settings.marketplace_agent_slug}",
                             "select": "id,slug", "limit": "1"})
    if not agent:
        log.warning("marketplace: no agent with slug %r — nothing to sync",
                    settings.marketplace_agent_slug)
        return []
    rows = _rest("agent_configurations", {
        "agent_id": f"eq.{agent[0]['id']}",
        "is_active": "eq.true",
        "deleted_at": "is.null",
        "select": "id,user_id,connection_id,external_agent_id,agent_config,"
                  "subscriptions(id,status,tier,seats,organization_id)",
    })
    out = []
    for r in rows:
        sub = r.get("subscriptions") or {}
        if isinstance(sub, list):
            sub = sub[0] if sub else {}
        if sub.get("status") not in ("active", "trialing", "past_due"):
            continue          # cancelled/unpaid → not entitled, regardless of is_active
        out.append({
            "config_id": r.get("id"),
            "user_id": r.get("user_id"),
            "connection_id": r.get("connection_id"),
            "tier": sub.get("tier") or "pro",
            "seats": sub.get("seats") or 1,
            "org_id": sub.get("organization_id") or "",
            "status": sub.get("status"),
        })
    return out


def user_email(user_id: str) -> str:
    """The subscriber's address — protocols are delivered there."""
    rows = _rest("profiles", {"id": f"eq.{user_id}", "select": "email", "limit": "1"})
    return (rows[0].get("email") if rows else "") or ""


def graph_token(connection_id: str) -> str | None:
    """A fresh Microsoft access token for one stored connection.

    The marketplace refreshes and decrypts it; we only ever see a short-lived access token. Returns
    None when the connection is gone or consent was revoked — the caller must then skip that user
    rather than fall back to anything else.
    """
    if not enabled() or not connection_id:
        return None
    try:
        r = httpx.post(
            f"{settings.marketplace_supabase_url.rstrip('/')}/functions/v1/get-oauth-token",
            json={"connection_id": connection_id}, headers=_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        log.warning("marketplace: token fetch failed for connection %s: %s", connection_id, e)
        return None
    return data.get("access_token") or data.get("token") or None
