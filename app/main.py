"""App factory. On boot: create tables (+ RLS), start the scheduler, mount auth + API + dashboard."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from .auth import auth_router
from .config import settings
from .db import init_db
from .scheduler import start_scheduler
from .web import agent_router, billing_router, dash_router, health_router

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


app = FastAPI(title="Vexa Flexcon Meeting Agent", version="0.2.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=False)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(billing_router)
app.include_router(dash_router)
