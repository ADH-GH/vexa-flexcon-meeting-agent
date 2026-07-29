"""The post-call pipeline + agent dispatch — the proven Flexcon pipeline logic.

State machine:  transcribed -> diarized -> summarized -> delivered   (+ *_failed)
Each scheduler tick advances candidates one step; the diarize step is GPU-bound and single-flight.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import urllib.parse

from sqlalchemy import select

from .. import mailrender, settings_store
from ..auth import user_access_token
from ..clients import DiarizerClient, LLMClient, Mailer, VexaClient, graph_calendar_view
from ..config import settings
from ..models import EventLog, MailTemplate, Meeting, Tenant, User

log = logging.getLogger("pipeline")

CHUNK, OVERLAP = 12000, 1500
DISPATCH_WINDOW_H = 24   # how far ahead to scan each user's calendar


def token(url: str) -> str:
    """Extract Vexa's native meeting id from a Teams join URL — classic thread token or /meet numeric
    (matches how Vexa derives native_meeting_id, so handover can pair them)."""
    u = urllib.parse.unquote(url or "")
    m = re.search(r"(19:meeting_[A-Za-z0-9_\-]+@thread\.v2)", u)
    if m:
        return m.group(1)
    m = re.search(r"/meet/(\d+)", u)
    return m.group(1) if m else ""


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _log(db, tid, kind, meeting_id="", **detail):
    db.add(EventLog(tenant_id=tid, kind=kind, meeting_id=str(meeting_id), detail=detail))


# --------------------------------------------------------------------------- handover
def handover(db, vexa: VexaClient, tid: int) -> None:
    """Advance this tenant's meetings once Vexa reports them completed. Matches our dispatched
    'planned' rows by native id; the bootstrap/legacy tenant (ingest_all) also ingests every completed
    meeting (the calendar-invite flow), for testing continuity against a live Vexa."""
    tenant = db.get(Tenant, tid)
    vexa = tenant_vexa(tenant, vexa)
    if vexa is None:
        return
    planned = {p.native_id: p for p in
               db.scalars(select(Meeting).where(Meeting.status == "planned")).all()}
    have = {m for m in db.scalars(select(Meeting.meeting_id)).all() if m}
    for m in vexa.completed_meetings():
        mid = str(m.get("id"))
        nid = m.get("native_meeting_id", "") or ""
        p = planned.get(nid)
        if p is not None:
            segs = vexa.transcript(mid).get("segments", [])
            p.meeting_id = mid
            p.title = p.title or (m.get("data") or {}).get("title") or "Meeting"
            p.started_at = p.started_at or (m.get("start_time") or "")
            p.ended_at = m.get("end_time") or ""
            p.language, p.segment_count, p.status = _dominant_lang(segs), len(segs), "transcribed"
            p.billable_minutes = _minutes(p.started_at, p.ended_at)
            _log(db, tid, "handover", mid, segments=len(segs), planned=True)
            # A dispatched meeting that produced nothing is the signature of a blocked/lobby'd guest
            # join — exactly what the authenticated (Enterprise) join solves. Record it so Insights can
            # surface the upgrade instead of leaving the user with silent, empty protocols.
            if not segs and tenant is not None and tenant.join_mode == "guest":
                _log(db, tid, "upsell", mid, reason="guest_join_no_transcript")
        elif tenant is not None and tenant.ingest_all and mid not in have:
            segs = vexa.transcript(mid).get("segments", [])
            db.add(Meeting(
                tenant_id=tid, meeting_id=mid, native_id=nid,
                title=(m.get("data") or {}).get("title") or m.get("constructed_meeting_url") or "Meeting",
                started_at=m.get("start_time") or "", ended_at=m.get("end_time") or "",
                language=_dominant_lang(segs), segment_count=len(segs), status="transcribed",
                billable_minutes=_minutes(m.get("start_time") or "", m.get("end_time") or "")))
            _log(db, tid, "handover", mid, segments=len(segs))
    db.commit()


def _minutes(start: str, end: str) -> int:
    """Billable meeting minutes from Vexa's start/end stamps (feeds Insights + metered billing)."""
    try:
        a = dt.datetime.fromisoformat((start or "").replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat((end or "").replace("Z", "+00:00"))
        return max(0, round((b - a).total_seconds() / 60))
    except (ValueError, TypeError):
        return 0


def _dominant_lang(segs) -> str:
    c: dict[str, int] = {}
    for s in segs:
        l = (s.get("language") or "").lower()
        if l:
            c[l] = c.get(l, 0) + 1
    return max(c, key=c.get) if c else "de"


# --------------------------------------------------------------------------- diarize
def diarize_one(db, vexa: VexaClient, diar: DiarizerClient, tid: int) -> bool:
    """Diarize the next transcribed meeting that has a recording. Returns True if it did work."""
    row = db.scalars(
        select(Meeting).where(Meeting.status == "transcribed", Meeting.diarized_at.is_(None)).limit(1)
    ).first()
    if not row:
        return False
    vexa = tenant_vexa(db.get(Tenant, tid), vexa)
    if vexa is None:
        return False
    meetings = {str(m.get("id")): m for m in vexa.completed_meetings()}
    m = meetings.get(row.meeting_id)
    rec_id, mf_id = _audio_ref(m)
    if not rec_id:
        row.speaker_count, row.diarized_at, row.status = 0, _now(), "diarized"  # no recording
        _log(db, tid, "diarize", row.meeting_id, note="no recording")
        db.commit()
        return True
    try:
        audio = vexa.recording_audio(rec_id, mf_id)
        result = diar.diarize_upload(audio, row.meeting_id)
        segs = result.get("segments", [])
        name_map = _correlate_names(segs, vexa.transcript(row.meeting_id).get("segments", []))
        row.diarized_transcript = _render(segs, name_map)
        row.speaker_count = int(result.get("num_speakers") or 0)
        row.diarized_at, row.status = _now(), "diarized"
        _log(db, tid, "diarize", row.meeting_id, speakers=row.speaker_count, named=len(name_map))
    except Exception as e:  # noqa: BLE001
        log.exception("diarize failed for %s", row.meeting_id)
        _log(db, tid, "error", row.meeting_id, step="diarize", err=str(e)[:300])
    db.commit()
    return True


def _audio_ref(meeting: dict | None):
    for rec in ((meeting or {}).get("data") or {}).get("recordings", []) or []:
        mf = next((f for f in rec.get("media_files", []) if f.get("type") == "audio" and f.get("id")), None)
        if rec.get("id") is not None and mf:
            return rec["id"], mf["id"]
    return None, None


def _correlate_names(dsegs, vsegs) -> dict:
    """Map SPEAKER_xx -> real name via time overlap with Vexa's live-named segments.
    Vexa uses ABSOLUTE epoch seconds; diarizer uses seconds-from-start -> align by the minima offset."""
    def real(n):
        s = str(n or "").strip()
        return s and not s.lower().startswith("seg_") and not s.lower().startswith("speaker_")

    if not dsegs or not vsegs:
        return {}
    d_min = min(float(s.get("start") or 0) for s in dsegs)
    v_min = min((float(s["start"]) for s in vsegs if s.get("start") is not None), default=0.0)
    off = v_min - d_min
    ov: dict[str, dict[str, float]] = {}
    for d in dsegs:
        spk, ds, de = d.get("speaker", "UNKNOWN"), float(d.get("start") or 0), float(d.get("end") or 0)
        for v in vsegs:
            if not real(v.get("speaker")):
                continue
            vs, ve = float(v.get("start") or 0) - off, float(v.get("end") or 0) - off
            o = min(de, ve) - max(ds, vs)
            if o > 0:
                ov.setdefault(spk, {})[v["speaker"]] = ov.setdefault(spk, {}).get(v["speaker"], 0) + o
    return {spk: max(names, key=names.get) for spk, names in ov.items() if names}


def _render(segs, name_map) -> str:
    def fmt(s):
        s = max(0, round(float(s or 0)))
        return f"{s // 60}:{s % 60:02d}"
    return "\n".join(f"[{fmt(x['start'])}] {name_map.get(x.get('speaker'), x.get('speaker') or 'UNKNOWN')}: "
                     f"{(x.get('text') or '').strip()}" for x in segs)


def _map_prompt(lang: str, idx: int, total: int) -> str:
    if lang == "en":
        return (f"You are given EXCERPT {idx} of {total} of a meeting transcript. Summarise ONLY this "
                "excerpt's substantive points as bullets: topics, decisions, action items, figures. "
                "Short, bullets only, no preamble.")
    return (f"Du bekommst AUSSCHNITT {idx} von {total} eines Meeting-Transkripts. Fasse NUR die "
            "inhaltlich wichtigen Punkte stichpunktartig auf Deutsch zusammen: Themen, Entscheidungen, "
            "Action Items, Zahlen. Kurz, nur Stichpunkte, keine Einleitung.")


def _reduce_prompt(lang: str) -> str:
    if lang == "en":
        return ("You are a precise minutes assistant. From the partial summaries build ONE consolidated "
                "protocol in English, EXACTLY in this format, no preamble:\n\n## Summary\n(3-6 sentences)"
                "\n\n## Key points\n- ...\n\n## Action items\n- [Owner, if identifiable] Task\n\n"
                "Carry over ALL action items, omit none.")
    return ("Du bist ein praeziser Protokoll-Assistent. Aus den Teil-Zusammenfassungen erstelle EIN "
            "konsolidiertes Protokoll auf Deutsch, GENAU in diesem Format, ohne Einleitung:\n\n"
            "## Zusammenfassung\n(3-6 Saetze)\n\n## Wichtige Punkte\n- ...\n\n## Action Items\n"
            "- [Verantwortlicher, falls erkennbar] Aufgabe\n\nUebernimm ALLE Action Items, lasse keine weg.")


# --------------------------------------------------------------------------- summarize
def summarize_one(db, llm: LLMClient, tid: int) -> bool:
    row = db.scalars(select(Meeting).where(Meeting.status == "diarized").limit(1)).first()
    if not row:
        return False
    src = row.diarized_transcript.strip()
    if not src:  # no recording / empty -> nothing to summarize; move on so delivery can decide
        row.status = "summarized"
        db.commit()
        return True
    cfg = settings_store.get_all(db, tid)
    chunks = _chunk(src.split("\n"), int(cfg.get("llm_chunk") or CHUNK),
                    int(cfg.get("llm_overlap") or OVERLAP))
    temp = float(cfg.get("llm_temperature") or 0.2)
    model = cfg.get("llm_model") or None
    lang = (cfg.get("summary_language") or "de").lower()
    partials = []
    for i, text in enumerate(chunks, 1):
        partials.append(llm.chat(_map_prompt(lang, i, len(chunks)), text,
                                 max_tokens=4000, temperature=temp, model=model))
    label = "Ausschnitt" if lang == "de" else "Excerpt"
    joined = "\n\n".join(f"{label} {i}:\n{p}" for i, p in enumerate(partials, 1) if p)
    summary = llm.chat(_reduce_prompt(lang), joined, max_tokens=8000, temperature=temp, model=model)
    if len(summary.strip()) < 40 and any(partials):
        # reasoning-robustness: a reasoning model can burn its whole budget and return empty content —
        # fall back to the per-chunk summaries rather than losing the meeting entirely.
        summary = ("## Wichtige Punkte\n" if lang == "de" else "## Key points\n") + joined
    row.summary, row.status = summary.strip(), "summarized"
    _log(db, tid, "summarize", row.meeting_id, chunks=len(chunks), chars=len(row.summary))
    db.commit()
    return True


def _chunk(units: list[str], chunk: int = CHUNK, overlap: int = OVERLAP) -> list[str]:
    units = [u.strip() for u in units if u.strip()]
    pieces, cur, clen = [], [], 0
    for u in units:
        if clen and clen + len(u) > chunk:
            pieces.append(" ".join(cur))
            tail, tl = [], 0
            for x in reversed(cur):
                if tl >= overlap:
                    break
                tail.insert(0, x)
                tl += len(x) + 1
            cur, clen = tail, tl
        cur.append(u)
        clen += len(u) + 1
    if cur:
        pieces.append(" ".join(cur))
    return pieces


# --------------------------------------------------------------------------- deliver
def deliver_one(db, mailer: Mailer, tid: int) -> bool:
    row = db.scalars(select(Meeting).where(Meeting.status == "summarized").limit(1)).first()
    if not row:
        return False
    cfg = settings_store.get_all(db, tid)
    if not cfg.get("mail_enabled"):
        row.status = "delivered"          # delivery switched off: complete without mailing
        db.commit()
        return True

    recipients = _recipients(db, row, cfg.get("recipient_policy") or "owner")
    tpl = _mail_template(db, cfg.get("mail_template"))
    subject, html = mailrender.render(tpl, mailrender.meeting_data(row, _md_html(row.summary)))
    try:
        mailer.send(recipients, subject, html)
        row.delivered_at, row.status = _now(), "delivered"
        _log(db, tid, "deliver", row.meeting_id, to=recipients,
             template=getattr(tpl, "name", "built-in"))
    except Exception as e:  # noqa: BLE001
        _log(db, tid, "error", row.meeting_id, step="deliver", err=str(e)[:300])
    db.commit()
    return True


def _recipients(db, row, policy: str) -> list[str]:
    """Who gets the protocol.
    `owner`    → the user whose calendar we watched (default).
    `internal` → the owner plus the meeting's INTERNAL attendees (captured at dispatch time).
    External attendees are NEVER auto-mailed — that needs the organiser's approval (roadmap)."""
    out = []
    if row.user_id:
        u = db.get(User, row.user_id)
        if u and u.email:
            out.append(u.email)
    if policy == "internal":
        for addr in (row.participants or {}).get("internal", []):
            if addr not in out:
                out.append(addr)
    return out or [settings.mail_from]


def _mail_template(db, name: str | None):
    """The tenant's selected template: by name → else the one flagged default → else built-in."""
    rows = db.scalars(select(MailTemplate)).all()
    if name:
        hit = next((t for t in rows if t.name == name), None)
        if hit:
            return hit
    return next((t for t in rows if t.is_default), None)


def _md_html(md: str) -> str:
    out, in_list = [], False
    for raw in md.split("\n"):
        line = raw.strip()
        if line.startswith("## "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h4>{line[3:]}</h4>")
        elif line[:2] in ("- ", "* "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{line[2:]}</li>")
        elif line:
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def tenant_vexa(tenant, shared: VexaClient) -> VexaClient | None:
    """Pick the Vexa deployment for this tenant's join tier.

    guest → the shared pool. auth (Enterprise) → the tenant's own deployment, which holds their
    authenticated bot session. An auth tenant WITHOUT that endpoint gets no bot at all: silently
    falling back to a guest join would break the promise the tier was bought for (strict tenants
    block guest joins), so we fail loudly instead."""
    if tenant is None:
        return shared
    if tenant.join_mode == "auth":
        if not tenant.vexa_endpoint:
            log.error("tenant %s is on authenticated join but has no vexa_endpoint — not dispatching",
                      tenant.id)
            return None
        return VexaClient(base=tenant.vexa_endpoint)
    return shared


def _within_lead(starts_at: str, now: dt.datetime, lead_s: int) -> bool:
    """True once the meeting starts within `lead_s` (already-running meetings included)."""
    if not starts_at:
        return True                       # no start time → don't hold it back
    try:
        s = dt.datetime.fromisoformat(starts_at[:19] + "+00:00")
    except ValueError:
        return True
    return (s - now).total_seconds() <= lead_s


def _attendees(ev: dict, user_email: str) -> dict:
    """Split the event's attendees into internal/external by the user's own mail domain, so delivery
    can honour the recipient policy. Externals are stored but NEVER auto-mailed."""
    domain = ("@" + user_email.split("@")[-1]).lower() if "@" in (user_email or "") else ""
    internal, external = [], []
    for a in ev.get("attendees") or []:
        addr = (((a.get("emailAddress") or {}).get("address")) or "").lower()
        if not addr or addr == (user_email or "").lower():
            continue
        (internal if domain and addr.endswith(domain) else external).append(addr)
    return {"internal": internal, "external": external}


# --------------------------------------------------------------------------- agent dispatch (per user)
def agent_dispatch(db, vexa: VexaClient, tid: int) -> None:
    """Per user: read the calendar with the user's delegated token, plan any upcoming Teams meeting on
    Vexa (guest join; Vexa auto-joins at its own lead), and create an OWNED 'planned' row. Dedupe by
    native id. Vexa native id ↔ our planned row is what handover pairs on."""
    cfg = settings_store.get_all(db, tid)
    if not cfg.get("auto_join", True):
        return
    tenant = db.get(Tenant, tid)
    vexa = tenant_vexa(tenant, vexa)
    if vexa is None:
        return
    users = db.scalars(select(User).where(User.tenant_id == tid, User.active.is_(True))).all()
    if not users:
        return
    now = dt.datetime.now(dt.timezone.utc)
    window = int(cfg.get("dispatch_window_h") or DISPATCH_WINDOW_H)
    lead_s = int(cfg.get("join_lead_s") or 120)
    start, end = now.isoformat(), (now + dt.timedelta(hours=window)).isoformat()
    known = {r for r in db.scalars(select(Meeting.native_id)).all() if r}
    for user in users:
        at = user_access_token(db, user)
        if not at:
            continue
        try:
            events = graph_calendar_view(at, start, end)
        except Exception as e:  # noqa: BLE001
            _log(db, tid, "error", "", step="calendar", err=str(e)[:200], user=user.id)
            continue
        for ev in events:
            join_url = (ev.get("onlineMeeting") or {}).get("joinUrl") or ""
            nid = token(join_url)
            if not nid or nid in known:
                continue
            starts_at = (ev.get("start") or {}).get("dateTime") or ""
            # Only dispatch once the meeting is within the configured join lead — the calendar window
            # is the search horizon, the lead decides WHEN the bot is sent.
            if not _within_lead(starts_at, now, lead_s):
                continue
            try:
                vexa.dispatch_bot(join_url)
            except Exception as e:  # noqa: BLE001
                _log(db, tid, "error", nid, step="dispatch", err=str(e)[:200])
                continue
            db.add(Meeting(tenant_id=tid, user_id=user.id, native_id=nid, meeting_id=nid,
                           title=ev.get("subject") or "Meeting", started_at=starts_at,
                           participants=_attendees(ev, user.email), status="planned"))
            known.add(nid)
            _log(db, tid, "dispatch", nid, user=user.id)
    db.commit()
