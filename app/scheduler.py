"""Scheduler — drives the two pipeline loops (post-call + agent dispatch), PER TENANT.
Each tenant's data access runs inside `tenant_scope` so Row-Level Security keeps it isolated."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from .clients import DiarizerClient, LLMClient, Mailer, VexaClient
from .config import settings
from .db import SessionLocal, tenant_scope
from .models import Tenant
from . import pipeline

log = logging.getLogger("scheduler")


def bootstrap_default_tenant() -> None:
    """Seed one tenant so the pipeline runs during testing before onboarding (Phase 2) exists."""
    db = SessionLocal()
    try:
        if not db.scalars(select(Tenant).limit(1)).first():
            db.add(Tenant(entra_tenant_id=settings.bootstrap_tenant_entra_id,
                          name=settings.bootstrap_tenant_name))
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


def start_scheduler() -> BackgroundScheduler:
    bootstrap_default_tenant()
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(_postcall_tick, "interval", seconds=settings.poll_postcall_s, id="postcall",
                  max_instances=1, coalesce=True)
    sched.add_job(_agent_tick, "interval", seconds=settings.poll_agent_s, id="agent",
                  max_instances=1, coalesce=True)
    sched.start()
    log.info("scheduler started (postcall=%ss, agent=%ss)", settings.poll_postcall_s, settings.poll_agent_s)
    return sched
