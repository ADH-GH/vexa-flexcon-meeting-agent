"""Database schema — multi-tenant. Control-plane tables (tenants, users) are app-managed; the
data tables carry `tenant_id` and are isolated by Postgres Row-Level Security (see db.apply_rls)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ------------------------------------------------------------------ control plane (no RLS)
class Tenant(Base):
    """One per onboarded Entra tenant. `tier`/`join_mode` are driven by the Stripe subscription."""
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entra_tenant_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String, default="")
    tier: Mapped[str] = mapped_column(String, default="trial")        # trial | pro | enterprise
    join_mode: Mapped[str] = mapped_column(String, default="guest")   # guest | auth
    retention_days: Mapped[int] = mapped_column(Integer, default=90)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # --- billing (Stripe is the source of truth; set by webhook) ---
    stripe_customer_id: Mapped[str] = mapped_column(String, default="", index=True)
    stripe_subscription_id: Mapped[str] = mapped_column(String, default="")
    stripe_usage_item_id: Mapped[str] = mapped_column(String, default="")  # metered overage line
    included_minutes: Mapped[int] = mapped_column(Integer, default=1200)   # per period (20 h)
    trial_ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Enterprise (authenticated join) needs its own Vexa deployment holding the tenant's bot session;
    # empty = use the shared guest pool. An auth tenant without this must NOT silently join as a guest.
    vexa_endpoint: Mapped[str] = mapped_column(String, default="")
    # legacy/bootstrap tenant: ingest ALL completed Vexa meetings (calendar-invite flow) rather than
    # only the ones this app dispatched per user. Onboarded tenants keep this False.
    ingest_all: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class User(Base):
    """A person onboarded via MS-SSO. Their delegated refresh token is stored ENCRYPTED."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    entra_object_id: Mapped[str] = mapped_column(String, index=True)
    email: Mapped[str] = mapped_column(String, index=True)
    display_name: Mapped[str] = mapped_column(String, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")   # Fernet ciphertext
    prefs: Mapped[dict] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("tenant_id", "entra_object_id", name="uq_user_tenant_obj"),)


# ------------------------------------------------------------------ data plane (RLS by tenant_id)
class Meeting(Base):
    """One row per meeting. status: transcribed -> diarized -> summarized -> delivered (+ *_failed)."""
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    meeting_id: Mapped[str] = mapped_column(String, index=True)        # Vexa meeting id
    native_id: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    started_at: Mapped[str] = mapped_column(String, default="")
    ended_at: Mapped[str] = mapped_column(String, default="")
    language: Mapped[str] = mapped_column(String, default="")
    segment_count: Mapped[int] = mapped_column(Integer, default=0)
    participants: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String, default="transcribed", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    diarized_transcript: Mapped[str] = mapped_column(Text, default="")
    speaker_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    billable_minutes: Mapped[int] = mapped_column(Integer, default=0)   # for Stripe metered usage
    # stamped once the minutes have been reported to Stripe, so a retry can never double-bill
    usage_reported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    diarized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("tenant_id", "meeting_id", name="uq_meeting_tenant"),)


class Setting(Base):
    __tablename__ = "settings"
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class MailTemplate(Base):
    __tablename__ = "mail_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String)
    lang: Mapped[str] = mapped_column(String, default="de")
    subject_tpl: Mapped[str] = mapped_column(String, default="Protokoll: {{title}}")
    body_html_tpl: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_template_tenant_name"),)


class ApiKey(Base):
    """Agent-Connect API keys (Flexcon Agents integration), per tenant."""
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String)
    key_hash: Mapped[str] = mapped_column(String, index=True)
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventLog(Base):
    """Audit + usage (feeds Insights & Reports and Stripe metered billing), per tenant."""
    __tablename__ = "event_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)   # dispatch|diarize|summarize|deliver|error
    meeting_id: Mapped[str] = mapped_column(String, default="", index=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


# Data tables that get Postgres RLS (tenant_id isolation).
# Control-plane tables are excluded: tenants/users (identity) and api_keys — a key must be resolvable
# BEFORE the tenant is known (the hash IS the credential), so it cannot sit behind a tenant policy.
RLS_TABLES = ("meetings", "settings", "mail_templates", "event_log")
