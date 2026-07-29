"""Stripe billing — subscriptions decide a tenant's tier, metered usage bills the overage.

Stripe is the **source of truth** for `tier` and therefore for `join_mode`: a webhook sets them, the
app never promotes itself. Usage is pushed nightly: minutes above the tenant's included quota are
reported as metered usage. Each meeting is stamped once it has been reported, so a retry or a crashed
run can never double-bill.

Talks to Stripe over plain REST (httpx) like every other client here — no SDK dependency.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import httpx

from .config import settings

log = logging.getLogger("billing")
API = "https://api.stripe.com/v1"


def enabled() -> bool:
    return bool(settings.stripe_api_key)


def _post(path: str, data: dict) -> dict:
    r = httpx.post(f"{API}{path}", data=data,
                   auth=(settings.stripe_api_key, ""), timeout=30)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------- webhook
def verify_signature(payload: bytes, sig_header: str, tolerance_s: int = 300) -> bool:
    """Stripe's `t=…,v1=…` scheme: HMAC-SHA256 over "<timestamp>.<raw body>".
    Rejects stale timestamps so a captured webhook can't be replayed."""
    if not settings.stripe_webhook_secret:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    ts, sent = parts.get("t"), parts.get("v1")
    if not ts or not sent:
        return False
    try:
        if abs(time.time() - int(ts)) > tolerance_s:
            return False
    except ValueError:
        return False
    expected = hmac.new(settings.stripe_webhook_secret.encode(),
                        f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent)


def tier_for(subscription: dict) -> tuple[str, str, int]:
    """(tier, join_mode, included_minutes) for a Stripe subscription.

    Enterprise is the authenticated-join tier — the capability strict Microsoft tenants require — and is
    sold on an invoiced/custom plan, so it is flagged on the subscription's metadata rather than by
    price id. Everything else is Pro (guest join)."""
    meta = subscription.get("metadata") or {}
    if (meta.get("tier") or "").lower() == "enterprise":
        return "enterprise", "auth", int(meta.get("included_minutes") or 6000)
    return "pro", "guest", int(meta.get("included_minutes") or 1200)


def usage_item_of(subscription: dict) -> str:
    """The metered line item (the overage) on a subscription, if present."""
    for item in ((subscription.get("items") or {}).get("data") or []):
        price = item.get("price") or {}
        if price.get("recurring", {}).get("usage_type") == "metered" or \
           price.get("id") == settings.stripe_price_overage:
            return item.get("id") or ""
    return ""


def apply_subscription(tenant, subscription: dict) -> None:
    """Point a tenant's entitlements at what Stripe says. Cancelled/unpaid → back to guest join."""
    status = subscription.get("status")
    if status in ("canceled", "incomplete_expired", "unpaid"):
        tenant.tier, tenant.join_mode = "canceled", "guest"
        tenant.active = False
        return
    tier, join_mode, included = tier_for(subscription)
    tenant.tier, tenant.join_mode, tenant.included_minutes = tier, join_mode, included
    tenant.stripe_subscription_id = subscription.get("id") or ""
    tenant.stripe_usage_item_id = usage_item_of(subscription)
    cust = subscription.get("customer")
    if isinstance(cust, str) and cust:
        tenant.stripe_customer_id = cust
    tenant.active = True


# --------------------------------------------------------------------- usage
def report_usage(tenant, minutes: int) -> bool:
    """Push metered minutes for one tenant. Returns True when Stripe accepted them (or when there is
    nothing to bill), False when the caller must NOT stamp the meetings as reported."""
    if minutes <= 0:
        return True
    if not enabled() or not tenant.stripe_usage_item_id:
        log.info("usage: tenant %s has %d billable minutes but no Stripe usage item — skipping",
                 tenant.id, minutes)
        return False
    _post(f"/subscription_items/{tenant.stripe_usage_item_id}/usage_records",
          {"quantity": minutes, "timestamp": int(time.time()), "action": "increment"})
    log.info("usage: reported %d minutes for tenant %s", minutes, tenant.id)
    return True


def parse_event(payload: bytes) -> dict:
    try:
        return json.loads(payload)
    except ValueError:
        return {}
