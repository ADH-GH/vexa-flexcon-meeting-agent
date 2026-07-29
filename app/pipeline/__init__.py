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

from ..auth import user_access_token
from ..clients import DiarizerClient, LLMClient, Mailer, VexaClient, graph_calendar_view
from ..config import settings
from ..models import EventLog, Meeting, Tenant, User

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
            _log(db, tid, "handover", mid, segments=len(segs), planned=True)
        elif tenant is not None and tenant.ingest_all and mid not in have:
            segs = vexa.transcript(mid).get("segments", [])
            db.add(Meeting(
                tenant_id=tid, meeting_id=mid, native_id=nid,
                title=(m.get("data") or {}).get("title") or m.get("constructed_meeting_url") or "Meeting",
                started_at=m.get("start_time") or "", ended_at=m.get("end_time") or "",
                language=_dominant_lang(segs), segment_count=len(segs), status="transcribed"))
            _log(db, tid, "handover", mid, segments=len(segs))
    db.commit()


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
    chunks = _chunk(src.split("\n"))
    partials = []
    for i, text in enumerate(chunks, 1):
        sys = (f"Du bekommst AUSSCHNITT {i} von {len(chunks)} eines Meeting-Transkripts (Flexcon IT). "
               "Fasse NUR die inhaltlich wichtigen Punkte stichpunktartig auf Deutsch zusammen: Themen, "
               "Entscheidungen, Action Items, Zahlen. Kurz, nur Stichpunkte.")
        partials.append(llm.chat(sys, text, max_tokens=4000))
    joined = "\n\n".join(f"Ausschnitt {i}:\n{p}" for i, p in enumerate(partials, 1) if p)
    reduce_sys = (
        "Du bist ein praeziser Protokoll-Assistent fuer Flexcon IT. Aus den Teil-Zusammenfassungen "
        "erstelle EIN konsolidiertes Protokoll auf Deutsch, GENAU in diesem Format, ohne Einleitung:\n\n"
        "## Zusammenfassung\n(3-6 Saetze)\n\n## Wichtige Punkte\n- ...\n\n## Action Items\n"
        "- [Verantwortlicher, falls erkennbar] Aufgabe\n\nUebernimm ALLE Action Items, lasse keine weg.")
    summary = llm.chat(reduce_sys, joined, max_tokens=8000)
    if len(summary.strip()) < 40 and any(partials):
        summary = "## Wichtige Punkte\n" + joined  # reasoning-robustness fallback
    row.summary, row.status = summary.strip(), "summarized"
    _log(db, tid, "summarize", row.meeting_id, chunks=len(chunks), chars=len(row.summary))
    db.commit()
    return True


def _chunk(units: list[str]) -> list[str]:
    units = [u.strip() for u in units if u.strip()]
    pieces, cur, clen = [], [], 0
    for u in units:
        if clen and clen + len(u) > CHUNK:
            pieces.append(" ".join(cur))
            tail, tl = [], 0
            for x in reversed(cur):
                if tl >= OVERLAP:
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
    # Deliver to the owning user (the person whose calendar we watched); FROM the Flexcon sender.
    # (Full attendee resolution + external owner-approval is a later step; templates from mail_templates.)
    recipients = []
    if row.user_id:
        u = db.get(User, row.user_id)
        if u and u.email:
            recipients = [u.email]
    if not recipients:
        recipients = [settings.mail_from]
    html = f"<h3>{row.title}</h3>{_md_html(row.summary)}"
    try:
        mailer.send(recipients, f"Protokoll: {row.title}", html)
        row.delivered_at, row.status = _now(), "delivered"
        _log(db, tid, "deliver", row.meeting_id, to=recipients)
    except Exception as e:  # noqa: BLE001
        _log(db, tid, "error", row.meeting_id, step="deliver", err=str(e)[:300])
    db.commit()
    return True


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


# --------------------------------------------------------------------------- agent dispatch (per user)
def agent_dispatch(db, vexa: VexaClient, tid: int) -> None:
    """Per user: read the calendar with the user's delegated token, plan any upcoming Teams meeting on
    Vexa (guest join; Vexa auto-joins at its own lead), and create an OWNED 'planned' row. Dedupe by
    native id. Vexa native id ↔ our planned row is what handover pairs on."""
    users = db.scalars(select(User).where(User.tenant_id == tid, User.active.is_(True))).all()
    if not users:
        return
    now = dt.datetime.now(dt.timezone.utc)
    start, end = now.isoformat(), (now + dt.timedelta(hours=DISPATCH_WINDOW_H)).isoformat()
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
            nid = token(((ev.get("onlineMeeting") or {}).get("joinUrl")) or "")
            if not nid or nid in known:
                continue
            try:
                vexa.dispatch_bot((ev.get("onlineMeeting") or {}).get("joinUrl"))
            except Exception as e:  # noqa: BLE001
                _log(db, tid, "error", nid, step="dispatch", err=str(e)[:200])
                continue
            db.add(Meeting(tenant_id=tid, user_id=user.id, native_id=nid, meeting_id=nid,
                           title=ev.get("subject") or "Meeting",
                           started_at=((ev.get("start") or {}).get("dateTime")) or "", status="planned"))
            known.add(nid)
            _log(db, tid, "dispatch", nid, user=user.id)
    db.commit()
