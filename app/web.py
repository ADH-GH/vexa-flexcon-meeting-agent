"""HTTP surface: health, the Agent-Connect API, and the server-rendered dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .clients import VexaClient
from .db import get_session
from .models import EventLog, Meeting

templates = Jinja2Templates(directory="app/templates")

health_router = APIRouter()
agent_router = APIRouter(prefix="/agent", tags=["agent-connect"])
dash_router = APIRouter()


@health_router.get("/health")
def health(db=Depends(get_session)):
    counts = dict(db.execute(select(Meeting.status, func.count()).group_by(Meeting.status)).all())
    return {"status": "ok", "meetings_by_status": counts}


# --- Agent-Connect API (Flexcon Agents integration — API-key auth added next) ---
@agent_router.post("/dispatch")
def dispatch(join_url: str, db=Depends(get_session)):
    """Send a bot NOW to a meeting URL (the spontaneous-invite case)."""
    return VexaClient().dispatch_bot(join_url)


@agent_router.get("/meetings")
def meetings(db=Depends(get_session)):
    rows = db.scalars(select(Meeting).order_by(Meeting.updated_at.desc()).limit(100)).all()
    return [{"meeting_id": m.meeting_id, "title": m.title, "status": m.status,
             "speaker_count": m.speaker_count, "delivered_at": m.delivered_at} for m in rows]


@agent_router.get("/meetings/{meeting_id}/protocol")
def protocol(meeting_id: str, db=Depends(get_session)):
    m = db.get(Meeting, meeting_id)
    return {"meeting_id": meeting_id, "title": getattr(m, "title", ""),
            "summary": getattr(m, "summary", ""), "status": getattr(m, "status", "unknown")}


# --- Dashboard (server-rendered) ---
@dash_router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db=Depends(get_session)):
    rows = db.scalars(select(Meeting).order_by(Meeting.updated_at.desc()).limit(50)).all()
    by_status = dict(db.execute(select(Meeting.status, func.count()).group_by(Meeting.status)).all())
    delivered = db.scalar(select(func.count()).select_from(Meeting).where(Meeting.status == "delivered"))
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "meetings": rows, "by_status": by_status, "delivered": delivered or 0})
