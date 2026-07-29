"""HTTP surface: health, the Agent-Connect API (API-key auth), and the server-rendered dashboard.
Data tables are tenant-isolated by RLS, so every data read runs inside `tenant_scope`."""
from __future__ import annotations

import datetime as dt
import hmac

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import apikeys, billing, mailrender, settings_store
from .auth import require_login
from .clients import VexaClient
from .config import settings
from .db import get_session, tenant_scope
from .models import ApiKey, EventLog, MailTemplate, Meeting, Tenant, User

templates = Jinja2Templates(directory="app/templates")

health_router = APIRouter()
agent_router = APIRouter(prefix="/agent", tags=["agent-connect"])
billing_router = APIRouter(prefix="/billing", tags=["billing"])
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


# ----------------------------------------------------------------- billing: Stripe webhook
@billing_router.post("/webhook")
async def stripe_webhook(request: Request, db=Depends(get_session)):
    """Stripe decides entitlements: subscription events set the tenant's tier and join mode.
    An unverified signature is rejected outright — this endpoint grants paid capability."""
    payload = await request.body()
    if not billing.verify_signature(payload, request.headers.get("stripe-signature", "")):
        raise HTTPException(status_code=400, detail="bad signature")
    event = billing.parse_event(payload)
    obj = ((event.get("data") or {}).get("object")) or {}
    if not str(event.get("type", "")).startswith("customer.subscription."):
        return {"ignored": event.get("type")}

    tenant = _tenant_for_subscription(db, obj)
    if not tenant:
        return {"ignored": "unknown tenant", "customer": obj.get("customer")}
    billing.apply_subscription(tenant, obj)
    db.add(EventLog(tenant_id=tenant.id, kind="billing",
                    detail={"event": event.get("type"), "tier": tenant.tier,
                            "join_mode": tenant.join_mode}))
    db.commit()
    return {"ok": True, "tenant": tenant.id, "tier": tenant.tier}


def _tenant_for_subscription(db, sub: dict):
    """Map a Stripe subscription to a tenant: by stored customer id, else by the entra tenant id the
    marketplace put in the subscription metadata at checkout."""
    cust = sub.get("customer")
    if isinstance(cust, str) and cust:
        hit = db.scalars(select(Tenant).where(Tenant.stripe_customer_id == cust)).first()
        if hit:
            return hit
    entra = (sub.get("metadata") or {}).get("entra_tenant_id")
    if entra:
        return db.scalars(select(Tenant).where(Tenant.entra_tenant_id == entra)).first()
    return None


@billing_router.post("/provision")
def marketplace_provision(payload: dict, db=Depends(get_session),
                          x_marketplace_secret: str = Header(default="")):
    """Called by the marketplace when a customer connects the Meeting Agent: links the tenant to its
    Stripe customer so the first subscription webhook can find it. Shared-secret authenticated."""
    if not settings.marketplace_secret or \
            not hmac.compare_digest(x_marketplace_secret, settings.marketplace_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    entra = payload.get("entra_tenant_id")
    if not entra:
        raise HTTPException(status_code=400, detail="entra_tenant_id required")
    tenant = db.scalars(select(Tenant).where(Tenant.entra_tenant_id == entra)).first()
    if not tenant:
        tenant = Tenant(entra_tenant_id=entra, name=payload.get("name") or entra)
        db.add(tenant)
    tenant.stripe_customer_id = payload.get("stripe_customer_id") or tenant.stripe_customer_id
    if not tenant.trial_ends_at and tenant.tier == "trial":
        tenant.trial_ends_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=settings.trial_days)
    db.commit()
    return {"ok": True, "tenant": tenant.id, "tier": tenant.tier,
            "trial_ends_at": tenant.trial_ends_at}


# ----------------------------------------------------------------- dashboard: plan & usage
@dash_router.get("/plan", response_class=HTMLResponse)
def plan(request: Request, principal=Depends(require_login), db=Depends(get_session)):
    tid = _tenant_id(principal, db)
    t = db.get(Tenant, tid) if tid else None
    used, blocked = 0, 0
    if tid:
        period_start = dt.datetime.now(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0,
                                                                microsecond=0)
        with tenant_scope(db, tid):
            used = db.scalar(select(func.coalesce(func.sum(Meeting.billable_minutes), 0))
                             .where(Meeting.updated_at >= period_start)) or 0
            blocked = db.scalar(select(func.count()).select_from(EventLog)
                                .where(EventLog.kind == "upsell",
                                       EventLog.ts >= period_start)) or 0
    included = (t.included_minutes if t else 0) or 0
    return _page(request, "plan.html", principal, db, nav="plan", used=used, included=included,
                 over=max(0, used - included), blocked=blocked)


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
