"""HTTP surface: health, the Agent-Connect API (API-key auth), and the server-rendered dashboard.
Data tables are tenant-isolated by RLS, so every data read runs inside `tenant_scope`."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import apikeys, mailrender, settings_store
from .auth import require_login
from .clients import VexaClient
from .db import get_session, tenant_scope
from .models import ApiKey, EventLog, MailTemplate, Meeting, Tenant, User

templates = Jinja2Templates(directory="app/templates")

health_router = APIRouter()
agent_router = APIRouter(prefix="/agent", tags=["agent-connect"])
dash_router = APIRouter()


# ----------------------------------------------------------------- helpers
def _tenant_id(principal, db) -> int | None:
    """The tenant a dashboard request acts on: the signed-in user's, or (for the local admin) the
    first tenant — the admin fallback is single-operator, not a multi-tenant console."""
    if principal and principal.get("tenant_id"):
        return principal["tenant_id"]
    t = db.scalars(select(Tenant).order_by(Tenant.id).limit(1)).first()
    return t.id if t else None


def _page(request, name, principal, db, **ctx):
    ctx.update({"request": request, "principal": principal,
                "tenant": db.get(Tenant, _tenant_id(principal, db))})
    return templates.TemplateResponse(name, ctx)


# ----------------------------------------------------------------- health
@health_router.get("/health")
def health(db=Depends(get_session)):
    tenants = db.scalar(select(func.count()).select_from(Tenant)) or 0
    users = db.scalar(select(func.count()).select_from(User)) or 0
    return {"status": "ok", "tenants": tenants, "users": users}


# ----------------------------------------------------------------- Agent-Connect API (API key)
def api_tenant(db=Depends(get_session), x_api_key: str = Header(default="")) -> int:
    """Authenticate an Agent-Connect call and return its tenant id."""
    row = apikeys.resolve(db, x_api_key)
    if not row:
        raise HTTPException(status_code=401, detail="invalid API key")
    return row.tenant_id


@agent_router.post("/dispatch")
def dispatch(join_url: str, tid: int = Depends(api_tenant)):
    """Send a bot NOW to a meeting URL (the spontaneous-invite case)."""
    return VexaClient().dispatch_bot(join_url)


@agent_router.get("/meetings")
def meetings(tid: int = Depends(api_tenant), db=Depends(get_session)):
    with tenant_scope(db, tid):
        rows = db.scalars(select(Meeting).order_by(Meeting.updated_at.desc()).limit(100)).all()
        return [{"meeting_id": m.meeting_id, "title": m.title, "status": m.status,
                 "speaker_count": m.speaker_count, "delivered_at": m.delivered_at} for m in rows]


@agent_router.get("/meetings/{meeting_id}/protocol")
def protocol(meeting_id: str, tid: int = Depends(api_tenant), db=Depends(get_session)):
    with tenant_scope(db, tid):
        m = db.scalars(select(Meeting).where(Meeting.meeting_id == meeting_id).limit(1)).first()
        if not m:
            raise HTTPException(status_code=404, detail="unknown meeting")
        return {"meeting_id": m.meeting_id, "title": m.title, "summary": m.summary,
                "status": m.status, "speakers": m.speaker_count}


# ----------------------------------------------------------------- dashboard: meetings
@dash_router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, principal=Depends(require_login), db=Depends(get_session)):
    tid = _tenant_id(principal, db)
    rows, by_status = [], {}
    if tid:
        with tenant_scope(db, tid):
            rows = db.scalars(select(Meeting).order_by(Meeting.updated_at.desc()).limit(50)).all()
            by_status = dict(db.execute(select(Meeting.status, func.count()).group_by(Meeting.status)).all())
    return _page(request, "dashboard.html", principal, db, nav="meetings", meetings=rows, by_status=by_status)


# ----------------------------------------------------------------- dashboard: settings
@dash_router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, principal=Depends(require_login), db=Depends(get_session),
                  saved: int = 0):
    tid = _tenant_id(principal, db)
    groups = settings_store.form_model(db, tid) if tid else []
    return _page(request, "settings.html", principal, db, nav="settings", groups=groups, saved=saved)


@dash_router.post("/settings")
async def settings_save(request: Request, principal=Depends(require_login), db=Depends(get_session)):
    tid = _tenant_id(principal, db)
    form = await request.form()
    if tid:
        for key, (_d, _g, _l, kind) in settings_store.DEFAULTS.items():
            if kind == "bool":
                settings_store.put(db, tid, key, key in form)   # unchecked boxes are absent
            elif key in form:
                settings_store.put(db, tid, key, settings_store.coerce(key, form[key]))
        db.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


# ----------------------------------------------------------------- dashboard: mail templates
@dash_router.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, principal=Depends(require_login), db=Depends(get_session),
                   edit: int = 0):
    tid = _tenant_id(principal, db)
    rows, current = [], None
    if tid:
        with tenant_scope(db, tid):
            rows = db.scalars(select(MailTemplate).order_by(MailTemplate.id)).all()
            current = next((r for r in rows if r.id == edit), None)
    return _page(request, "templates.html", principal, db, nav="templates", templates_list=rows,
                 current=current, preview=_preview(current))


@dash_router.post("/templates")
def templates_save(principal=Depends(require_login), db=Depends(get_session),
                   tpl_id: int = Form(0), name: str = Form(...), subject_tpl: str = Form(...),
                   body_html_tpl: str = Form(""), is_default: str = Form("")):
    tid = _tenant_id(principal, db)
    if tid:
        with tenant_scope(db, tid):
            row = db.get(MailTemplate, tpl_id) if tpl_id else None
            if not row:
                row = MailTemplate(tenant_id=tid, name=name)
                db.add(row)
            row.name, row.subject_tpl, row.body_html_tpl = name, subject_tpl, body_html_tpl
            row.is_default = bool(is_default)
            db.commit()
    return RedirectResponse("/templates", status_code=303)


def _preview(tpl) -> dict:
    """Preview via the SAME renderer delivery uses — so what you see is what recipients get."""
    subject, html = mailrender.render(tpl, mailrender.sample_data())
    return {"subject": subject, "html": html}


# ----------------------------------------------------------------- dashboard: insights & reports
@dash_router.get("/insights", response_class=HTMLResponse)
def insights(request: Request, principal=Depends(require_login), db=Depends(get_session), days: int = 30):
    tid = _tenant_id(principal, db)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    stats, by_day, top, errors = {}, [], [], []
    if tid:
        with tenant_scope(db, tid):
            stats = {
                "meetings": db.scalar(select(func.count()).select_from(Meeting)) or 0,
                "delivered": db.scalar(select(func.count()).select_from(Meeting)
                                       .where(Meeting.status == "delivered")) or 0,
                "minutes": db.scalar(select(func.coalesce(func.sum(Meeting.billable_minutes), 0))) or 0,
                "speakers_avg": round(db.scalar(select(func.coalesce(func.avg(Meeting.speaker_count), 0))) or 0, 1),
            }
            by_day = db.execute(
                select(func.date(EventLog.ts), func.count()).where(EventLog.ts >= since,
                                                                   EventLog.kind == "deliver")
                .group_by(func.date(EventLog.ts)).order_by(func.date(EventLog.ts))).all()
            top = db.execute(
                select(Meeting.title, Meeting.speaker_count, Meeting.billable_minutes)
                .order_by(Meeting.billable_minutes.desc()).limit(10)).all()
            errors = db.scalars(select(EventLog).where(EventLog.kind == "error")
                                .order_by(EventLog.ts.desc()).limit(10)).all()
    return _page(request, "insights.html", principal, db, nav="insights", stats=stats, by_day=by_day, top=top,
                 errors=errors, days=days)


# ----------------------------------------------------------------- dashboard: agent connector + keys
@dash_router.get("/connector", response_class=HTMLResponse)
def connector(request: Request, principal=Depends(require_login), db=Depends(get_session),
              new_key: str = ""):
    tid = _tenant_id(principal, db)
    keys = db.scalars(select(ApiKey).where(ApiKey.tenant_id == tid).order_by(ApiKey.id)).all() if tid else []
    return _page(request, "connector.html", principal, db, nav="connector", keys=keys, new_key=new_key)


@dash_router.post("/connector/keys")
def create_key(principal=Depends(require_login), db=Depends(get_session), name: str = Form("")):
    tid = _tenant_id(principal, db)
    key = apikeys.mint(db, tid, name) if tid else ""
    # Shown once — after this redirect it is unrecoverable (only the hash is stored).
    return RedirectResponse(f"/connector?new_key={key}", status_code=303)


@dash_router.post("/connector/keys/{key_id}/revoke")
def revoke_key(key_id: int, principal=Depends(require_login), db=Depends(get_session)):
    tid = _tenant_id(principal, db)
    if tid:
        apikeys.revoke(db, tid, key_id)
    return RedirectResponse("/connector", status_code=303)
