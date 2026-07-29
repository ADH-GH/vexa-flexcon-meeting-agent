"""HTTP surface: health, the Agent-Connect API, and the server-rendered dashboard.
Data tables are tenant-isolated by RLS, so reads run inside `tenant_scope`. (Auth/API-key → tenant
binding lands in phases 2/4; for now admin views aggregate across tenants.)"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .clients import VexaClient
from .db import get_session, tenant_scope
from .models import Meeting, Tenant, User

templates = Jinja2Templates(directory="app/templates")

health_router = APIRouter()
agent_router = APIRouter(prefix="/agent", tags=["agent-connect"])
dash_router = APIRouter()


def _tenant_overview(db):
    """Per-tenant meeting counts by status (aggregated with RLS scoping)."""
    rows = []
    for t in db.scalars(select(Tenant).order_by(Tenant.id)).all():
        with tenant_scope(db, t.id):
            by_status = dict(db.execute(select(Meeting.status, func.count()).group_by(Meeting.status)).all())
        rows.append({"tenant": t, "by_status": by_status, "total": sum(by_status.values())})
    return rows


@health_router.get("/health")
def health(db=Depends(get_session)):
    tenants = db.scalar(select(func.count()).select_from(Tenant)) or 0
    users = db.scalar(select(func.count()).select_from(User)) or 0
    meetings = sum(o["total"] for o in _tenant_overview(db))
    return {"status": "ok", "tenants": tenants, "users": users, "meetings": meetings}


# --- Agent-Connect API (API-key → tenant binding is phase 4; tenant_id is explicit for now) ---
@agent_router.post("/dispatch")
def dispatch(join_url: str):
    """Send a bot NOW to a meeting URL (the spontaneous-invite case)."""
    return VexaClient().dispatch_bot(join_url)


@agent_router.get("/meetings")
def meetings(tenant_id: int, db=Depends(get_session)):
    with tenant_scope(db, tenant_id):
        rows = db.scalars(select(Meeting).order_by(Meeting.updated_at.desc()).limit(100)).all()
        return [{"meeting_id": m.meeting_id, "title": m.title, "status": m.status,
                 "speaker_count": m.speaker_count, "delivered_at": m.delivered_at} for m in rows]


@agent_router.get("/meetings/{meeting_id}/protocol")
def protocol(meeting_id: str, tenant_id: int, db=Depends(get_session)):
    with tenant_scope(db, tenant_id):
        m = db.scalars(select(Meeting).where(Meeting.meeting_id == meeting_id).limit(1)).first()
        return {"meeting_id": meeting_id, "title": getattr(m, "title", ""),
                "summary": getattr(m, "summary", ""), "status": getattr(m, "status", "unknown")}


# --- Dashboard (server-rendered admin overview) ---
@dash_router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db=Depends(get_session)):
    overview = _tenant_overview(db)
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "overview": overview,
        "tenants": db.scalar(select(func.count()).select_from(Tenant)) or 0,
        "users": db.scalar(select(func.count()).select_from(User)) or 0})
