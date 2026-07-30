"""Entra (Azure AD) onboarding — OAuth auth-code + PKCE. Multi-tenant: any work/school account can
sign in; the first sign-in provisions the tenant + user and stores the delegated refresh token
(encrypted). Local user/password is the admin fallback. This is the zero-onboarding entry point."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import urllib.parse

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from . import crypto, marketplace
from .config import settings
from .db import get_session
from .models import Tenant, User

log = logging.getLogger("auth")
templates = Jinja2Templates(directory="app/templates")
auth_router = APIRouter(prefix="/auth", tags=["auth"])

# Delegated scopes the zero-onboarding pipeline needs (calendar watch + delivery + identity).
SCOPES = "openid profile email offline_access User.Read Calendars.Read Mail.Send"


def _authority() -> str:
    # "organizations" = any Entra work/school tenant (multi-tenant SaaS).
    return f"https://login.microsoftonline.com/{settings.oauth_authority_tenant}"


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _authorize_url(state: str, challenge: str) -> str:
    q = {
        "client_id": settings.graph_client_id,
        "response_type": "code",
        "redirect_uri": settings.oauth_redirect_uri,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{_authority()}/oauth2/v2.0/authorize?" + urllib.parse.urlencode(q)


def _token(extra: dict) -> dict:
    data = {"client_id": settings.graph_client_id, "client_secret": settings.graph_client_secret, **extra}
    r = httpx.post(f"{_authority()}/oauth2/v2.0/token", data=data, timeout=30)
    r.raise_for_status()
    return r.json()


def exchange_code(code: str, verifier: str) -> dict:
    return _token({"grant_type": "authorization_code", "code": code,
                   "redirect_uri": settings.oauth_redirect_uri, "code_verifier": verifier})


def refresh(refresh_token: str) -> dict:
    return _token({"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": SCOPES})


def user_access_token(db, user) -> str | None:
    """Fresh delegated Graph token for a user — the pipeline's single way in.

    In marketplace mode the consent lives there, so we ask it for a token and hold none ourselves.
    In own mode we refresh with our stored token and persist the rotated one. Either way a failure
    means "no token", never a fallback to someone else's."""
    if marketplace.enabled():
        return marketplace.graph_token(user.connection_id)
    if not user.refresh_token_enc:
        return None
    try:
        tok = refresh(crypto.decrypt(user.refresh_token_enc))
    except Exception:  # noqa: BLE001
        user.active = False
        db.commit()
        return None
    if tok.get("refresh_token"):
        user.refresh_token_enc = crypto.encrypt(tok["refresh_token"])
        db.commit()
    return tok.get("access_token")


def _claims(id_token: str) -> dict:
    """Decode id_token claims. It comes over TLS from the token endpoint (back-channel), so decoding
    is safe for identity extraction; TODO harden with JWKS signature/audience verification."""
    p = id_token.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


def provision(db, tokens: dict) -> User:
    """First sign-in creates the tenant + user; later sign-ins refresh their token. Control-plane
    tables (no RLS) — safe to upsert without a tenant scope. Zero-config defaults apply on the tenant."""
    c = _claims(tokens["id_token"])
    tid, oid = c.get("tid"), c.get("oid")
    email = c.get("preferred_username") or c.get("email") or ""
    tenant = db.scalars(select(Tenant).where(Tenant.entra_tenant_id == tid)).first()
    if not tenant:
        tenant = Tenant(entra_tenant_id=tid, name=c.get("tenant_display_name") or email.split("@")[-1])
        db.add(tenant)
        db.flush()
    user = db.scalars(select(User).where(User.tenant_id == tenant.id,
                                         User.external_id == oid)).first()
    if not user:
        user = User(tenant_id=tenant.id, external_id=oid)
        db.add(user)
    user.email, user.display_name, user.active = email, c.get("name") or email, True
    if tokens.get("refresh_token"):
        user.refresh_token_enc = crypto.encrypt(tokens["refresh_token"])
    db.commit()
    return user


# ------------------------------------------------------------------- session dependency
def current_principal(request: Request) -> dict | None:
    return request.session.get("principal")


def require_login(request: Request):
    """For server-rendered pages: redirect to the login screen if not authenticated."""
    p = request.session.get("principal")
    if not p:
        raise HTTPException(status_code=307, headers={"Location": "/auth/login"})
    return p


# ------------------------------------------------------------------- routes
@auth_router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@auth_router.get("/microsoft")
def microsoft(request: Request):
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    request.session["oauth"] = {"state": state, "verifier": verifier}
    return RedirectResponse(_authorize_url(state, challenge))


@auth_router.get("/callback")
def callback(request: Request, code: str = "", state: str = "", db=Depends(get_session)):
    saved = request.session.get("oauth") or {}
    if not code or state != saved.get("state"):
        return RedirectResponse("/auth/login?error=state")
    tokens = exchange_code(code, saved["verifier"])
    user = provision(db, tokens)
    request.session.pop("oauth", None)
    request.session["principal"] = {"kind": "user", "user_id": user.id, "tenant_id": user.tenant_id,
                                    "email": user.email}
    log.info("onboarded user %s (tenant %s)", user.email, user.tenant_id)
    return RedirectResponse("/")


@auth_router.post("/local")
def local(request: Request, username: str = Form(...), password: str = Form(...)):
    """Admin fallback login (Entra-independent)."""
    if settings.admin_password and username == settings.admin_user and password == settings.admin_password:
        request.session["principal"] = {"kind": "admin", "email": settings.admin_user}
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/auth/login?error=creds", status_code=303)


@auth_router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login")
