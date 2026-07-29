"""Thin outbound clients: Vexa API, the diarizer service, an OpenAI-compatible LLM, and mail
(SMTP or MS Graph). Kept dependency-light and synchronous to match the scheduler worker."""
from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from ..config import settings


class VexaClient:
    """Vexa gateway (X-API-Key auth).

    `base` overrides the shared deployment: guest joins all share one pool, while an Enterprise tenant
    gets its own Vexa deployment holding that tenant's authenticated bot session."""

    def __init__(self, base: str | None = None) -> None:
        self.base = (base or settings.vexa_api_url).rstrip("/")
        self.h = {"X-API-Key": settings.vexa_api_key}

    def completed_meetings(self, limit: int = 100) -> list[dict]:
        r = httpx.get(f"{self.base}/meetings", params={"status": "completed", "limit": limit},
                      headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json().get("meetings", [])

    def transcript(self, meeting_id: str) -> dict:
        r = httpx.get(f"{self.base}/transcripts/by-id/{meeting_id}", headers=self.h, timeout=60)
        r.raise_for_status()
        return r.json()

    def recording_audio(self, rec_id, media_file_id) -> bytes:
        """The /raw byte route also finalises the master on read (see diarizer S3 notes)."""
        url = f"{self.base}/recordings/{rec_id}/media/{media_file_id}/raw"
        r = httpx.get(url, params={"type": "audio"}, headers=self.h, timeout=180)
        r.raise_for_status()
        return r.content

    def dispatch_bot(self, join_url: str, auto_join: bool = True) -> dict:
        r = httpx.post(f"{self.base}/meetings", json={"url": join_url, "auto_join": auto_join},
                       headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json()


class DiarizerClient:
    def __init__(self) -> None:
        self.base = settings.diarizer_url.rstrip("/")

    def diarize_upload(self, audio: bytes, meeting_id: str) -> dict:
        files = {"file": ("audio.webm", audio, "audio/webm")}
        r = httpx.post(f"{self.base}/diarize_upload", files=files, data={"meeting_id": meeting_id},
                       timeout=3000)
        r.raise_for_status()
        return r.json()


class LLMClient:
    """OpenAI-compatible chat completions."""

    def __init__(self) -> None:
        self.url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        self.h = {"Authorization": f"Bearer {settings.llm_api_key}"}
        self.model = settings.llm_model

    def chat(self, system: str, user: str, max_tokens: int = 4000, temperature: float = 0.2,
             model: str | None = None) -> str:
        body = {"model": model or self.model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        r = httpx.post(self.url, json=body, headers=self.h, timeout=180)
        r.raise_for_status()
        ch = (r.json().get("choices") or [{}])[0]
        return ((ch.get("message") or {}).get("content") or "").strip()


class Mailer:
    """SMTP or MS Graph, chosen by settings.mail_transport."""

    def send(self, to: list[str], subject: str, html: str) -> None:
        if not to:
            return
        if settings.mail_transport == "graph":
            self._graph(to, subject, html)
        else:
            self._smtp(to, subject, html)

    def _smtp(self, to: list[str], subject: str, html: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, settings.mail_from, ", ".join(to)
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            if settings.smtp_starttls:
                s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.sendmail(settings.mail_from, to, msg.as_string())

    def _graph(self, to: list[str], subject: str, html: str) -> None:
        token = _graph_token()
        payload = {"message": {"subject": subject, "body": {"contentType": "HTML", "content": html},
                               "toRecipients": [{"emailAddress": {"address": a}} for a in to]},
                   "saveToSentItems": False}
        r = httpx.post(f"https://graph.microsoft.com/v1.0/users/{settings.mail_from}/sendMail",
                       json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()


def graph_calendar_view(access_token: str, start_iso: str, end_iso: str) -> list[dict]:
    """A user's upcoming events (delegated token) — for the per-user agent dispatch."""
    r = httpx.get(
        "https://graph.microsoft.com/v1.0/me/calendarView",
        params={"startDateTime": start_iso, "endDateTime": end_iso,
                "$select": "subject,onlineMeeting,start,end,organizer,attendees", "$top": "100"},
        headers={"Authorization": f"Bearer {access_token}", "Prefer": 'outlook.timezone="UTC"'},
        timeout=30)
    r.raise_for_status()
    return r.json().get("value", [])


def _graph_token() -> str:
    """Client-credentials token for MS Graph (mail + calendar)."""
    r = httpx.post(
        f"https://login.microsoftonline.com/{settings.graph_tenant_id}/oauth2/v2.0/token",
        data={"client_id": settings.graph_client_id, "client_secret": settings.graph_client_secret,
              "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
        timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]
