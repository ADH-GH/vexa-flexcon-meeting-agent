"""App factory. On boot: create tables, start the scheduler, mount the API + dashboard."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import init_db
from .scheduler import start_scheduler
from .web import agent_router, dash_router, health_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_sched = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    global _sched
    _sched = start_scheduler()
    yield
    if _sched:
        _sched.shutdown(wait=False)


app = FastAPI(title="Vexa Flexcon Meeting Agent", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(agent_router)
app.include_router(dash_router)
