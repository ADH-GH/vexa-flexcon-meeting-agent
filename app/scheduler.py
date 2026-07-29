"""Scheduler — drives the two pipeline loops (post-call + agent dispatch)."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .clients import DiarizerClient, LLMClient, Mailer, VexaClient
from .config import settings
from .db import SessionLocal
from . import pipeline

log = logging.getLogger("scheduler")


def _postcall_tick() -> None:
    db = SessionLocal()
    try:
        vexa, diar, llm, mailer = VexaClient(), DiarizerClient(), LLMClient(), Mailer()
        pipeline.handover(db, vexa)
        pipeline.diarize_one(db, vexa, diar)     # single-flight (one GPU job/tick)
        pipeline.summarize_one(db, llm)
        pipeline.deliver_one(db, mailer)
    except Exception:  # noqa: BLE001
        log.exception("post-call tick failed")
    finally:
        db.close()


def _agent_tick() -> None:
    db = SessionLocal()
    try:
        pipeline.agent_dispatch(db, VexaClient())
    except Exception:  # noqa: BLE001
        log.exception("agent tick failed")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(_postcall_tick, "interval", seconds=settings.poll_postcall_s, id="postcall",
                  max_instances=1, coalesce=True)
    sched.add_job(_agent_tick, "interval", seconds=settings.poll_agent_s, id="agent",
                  max_instances=1, coalesce=True)
    sched.start()
    log.info("scheduler started (postcall=%ss, agent=%ss)", settings.poll_postcall_s, settings.poll_agent_s)
    return sched
