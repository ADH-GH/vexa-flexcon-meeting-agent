"""Scheduler — drives the two pipeline loops (post-call + agent dispatch), PER TENANT.
Each tenant's data access runs inside `tenant_scope` so Row-Level Security keeps it isolated."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

import datetime as dt

from sqlalchemy import func

from . import auth, billing, crypto, pipeline, settings_store
from .clients import DiarizerClient, LLMClient, Mailer, VexaClient
from .config import settings
from .db import SessionLocal, tenant_scope
from .models import EventLog, Meeting, Tenant, User

log = logging.getLogger("scheduler")


def _refresh_tick() -> None:
    """Keep each onboarded user's delegated token alive (Microsoft rotates refresh tokens) and
    detect revocation → deactivate. Control-plane query (no RLS)."""
    db = SessionLocal()
    try:
        for u in db.scalars(select(User).where(User.active.is_(True))).all():
            if not u.refresh_token_enc:
                continue
            try:
                tok = auth.refresh(crypto.decrypt(u.refresh_token_enc))
                if tok.get("refresh_token"):
                    u.refresh_token_enc = crypto.encrypt(tok["refresh_token"])
            except Exception:  # noqa: BLE001
                log.warning("token refresh failed for user %s → deactivating", u.id)
                u.active = False
        db.commit()
    except Exception:  # noqa: BLE001
        log.exception("refresh tick failed")
    finally:
        db.close()


def bootstrap_default_tenant() -> None:
    """Seed one tenant so the pipeline runs during testing before onboarding (Phase 2) exists."""
    db = SessionLocal()
    try:
        if not db.scalars(select(Tenant).limit(1)).first():
            db.add(Tenant(entra_tenant_id=settings.bootstrap_tenant_entra_id,
                          name=settings.bootstrap_tenant_name, ingest_all=True))
            db.commit()
            log.info("bootstrapped default tenant %s", settings.bootstrap_tenant_name)
    finally:
        db.close()


def _active_tenants(db):
    return db.scalars(select(Tenant).where(Tenant.active.is_(True))).all()


def _postcall_tick() -> None:
    db = SessionLocal()
    try:
        vexa, diar, llm, mailer = VexaClient(), DiarizerClient(), LLMClient(), Mailer()
        for t in _active_tenants(db):
            with tenant_scope(db, t.id):
                try:
                    pipeline.handover(db, vexa, t.id)
                    pipeline.diarize_one(db, vexa, diar, t.id)   # single-flight (one GPU job/tenant/tick)
                    pipeline.summarize_one(db, llm, t.id)
                    pipeline.deliver_one(db, mailer, t.id)
                except Exception:  # noqa: BLE001
                    log.exception("post-call tick failed for tenant %s", t.id)
                    db.rollback()
    except Exception:  # noqa: BLE001
        log.exception("post-call tick failed")
    finally:
        db.close()


def _agent_tick() -> None:
    db = SessionLocal()
    try:
        vexa = VexaClient()
        for t in _active_tenants(db):
            with tenant_scope(db, t.id):
                try:
                    pipeline.agent_dispatch(db, vexa, t.id)
                except Exception:  # noqa: BLE001
                    log.exception("agent tick failed for tenant %s", t.id)
                    db.rollback()
    finally:
        db.close()


def _retention_tick() -> None:
    """DSGVO retention: past a tenant's `retention_days`, erase the personal CONTENT (transcript +
    summary) while keeping the row as an audit/billing record, and drop aged event_log entries.
    Content-erasure rather than row-deletion keeps the audit trail and billable minutes intact."""
    db = SessionLocal()
    try:
        for t in _active_tenants(db):
            with tenant_scope(db, t.id):
                days = int(settings_store.get(db, t.id, "retention_days") or 0)
                if days <= 0:
                    continue
                cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
                rows = db.scalars(select(Meeting).where(
                    Meeting.updated_at < cutoff,
                    (Meeting.diarized_transcript != "") | (Meeting.summary != ""))).all()
                for r in rows:
                    r.diarized_transcript, r.summary = "", ""
                aged = db.scalars(select(EventLog).where(EventLog.ts < cutoff)).all()
                for e in aged:
                    db.delete(e)
                if rows or aged:
                    db.add(EventLog(tenant_id=t.id, kind="retention",
                                    detail={"erased_meetings": len(rows), "purged_events": len(aged),
                                            "retention_days": days}))
                    log.info("retention: tenant %s erased %d meetings, %d events",
                             t.id, len(rows), len(aged))
                db.commit()
    except Exception:  # noqa: BLE001
        log.exception("retention tick failed")
    finally:
        db.close()


def _usage_tick() -> None:
    """Report metered usage to Stripe: minutes ABOVE the tenant's included quota for this period.

    Meetings are stamped `usage_reported_at` only after Stripe accepts the push, so a crash or retry
    can never bill the same minutes twice. If a tenant has no metered item yet, nothing is stamped and
    the minutes are simply reported later."""
    if not billing.enabled():
        return
    db = SessionLocal()
    try:
        now = dt.datetime.now(dt.timezone.utc)
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for t in _active_tenants(db):
            with tenant_scope(db, t.id):
                unreported = db.scalars(select(Meeting).where(
                    Meeting.usage_reported_at.is_(None), Meeting.billable_minutes > 0)).all()
                if not unreported:
                    continue
                # Meetings from a closed period are stamped but never billed — reporting them now
                # would land the minutes in the wrong invoice.
                this_period = [m for m in unreported if (m.updated_at or now) >= period_start]
                stale = [m for m in unreported if m not in this_period]

                included = max(0, t.included_minutes)
                reported = db.scalar(
                    select(func.coalesce(func.sum(Meeting.billable_minutes), 0)).where(
                        Meeting.usage_reported_at.is_not(None),
                        Meeting.updated_at >= period_start)) or 0
                pending = sum(m.billable_minutes for m in this_period)
                # Only the part above the included quota is metered — and only the part not billed yet.
                billable = max(0, reported + pending - included) - max(0, reported - included)
                try:
                    if billing.report_usage(t, billable):
                        for m in this_period + stale:
                            m.usage_reported_at = now
                        db.add(EventLog(tenant_id=t.id, kind="usage",
                                        detail={"minutes_total": pending, "minutes_billed": billable,
                                                "meetings": len(this_period), "skipped_stale": len(stale)}))
                        db.commit()
                except Exception as e:  # noqa: BLE001
                    log.exception("usage report failed for tenant %s", t.id)
                    db.rollback()
                    db.add(EventLog(tenant_id=t.id, kind="error",
                                    detail={"step": "usage", "err": str(e)[:300]}))
                    db.commit()
    except Exception:  # noqa: BLE001
        log.exception("usage tick failed")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    bootstrap_default_tenant()
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(_postcall_tick, "interval", seconds=settings.poll_postcall_s, id="postcall",
                  max_instances=1, coalesce=True)
    sched.add_job(_agent_tick, "interval", seconds=settings.poll_agent_s, id="agent",
                  max_instances=1, coalesce=True)
    sched.add_job(_refresh_tick, "interval", seconds=settings.refresh_interval_s, id="refresh",
                  max_instances=1, coalesce=True)
    sched.add_job(_retention_tick, "interval", hours=24, id="retention",
                  max_instances=1, coalesce=True)
    sched.add_job(_usage_tick, "interval", seconds=settings.usage_report_interval_s, id="usage",
                  max_instances=1, coalesce=True)
    sched.start()
    log.info("scheduler started (postcall=%ss, agent=%ss)", settings.poll_postcall_s, settings.poll_agent_s)
    return sched
