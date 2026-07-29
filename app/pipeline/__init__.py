"""The post-call pipeline + agent dispatch — the proven Flexcon pipeline logic.

State machine:  transcribed -> diarized -> summarized -> delivered   (+ *_failed)
Each scheduler tick advances candidates one step; the diarize step is GPU-bound and single-flight.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from ..clients import DiarizerClient, LLMClient, Mailer, VexaClient
from ..config import settings
from ..models import EventLog, Meeting

log = logging.getLogger("pipeline")

CHUNK, OVERLAP = 12000, 1500


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _log(db, kind, meeting_id="", **detail):
    db.add(EventLog(kind=kind, meeting_id=str(meeting_id), detail=detail))


# --------------------------------------------------------------------------- handover
def handover(db, vexa: VexaClient) -> None:
    """Land newly-completed Vexa meetings as rows (dedupe/audit)."""
    have = {m.meeting_id for m in db.scalars(select(Meeting.meeting_id)).all()}
    have = {r for r in have}
    for m in vexa.completed_meetings():
        mid = str(m.get("id"))
        if mid in have:
            continue
        segs = vexa.transcript(mid).get("segments", [])
        row = Meeting(
            meeting_id=mid,
            native_id=m.get("native_meeting_id", "") or "",
            title=(m.get("data") or {}).get("title") or m.get("constructed_meeting_url") or "Meeting",
            started_at=m.get("start_time") or "",
            ended_at=m.get("end_time") or "",
            language=_dominant_lang(segs),
            segment_count=len(segs),
            status="transcribed",
        )
        db.add(row)
        _log(db, "handover", mid, segments=len(segs))
    db.commit()


def _dominant_lang(segs) -> str:
    c: dict[str, int] = {}
    for s in segs:
        l = (s.get("language") or "").lower()
        if l:
            c[l] = c.get(l, 0) + 1
    return max(c, key=c.get) if c else "de"


# --------------------------------------------------------------------------- diarize
def diarize_one(db, vexa: VexaClient, diar: DiarizerClient) -> bool:
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
        _log(db, "diarize", row.meeting_id, note="no recording")
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
        _log(db, "diarize", row.meeting_id, speakers=row.speaker_count, named=len(name_map))
    except Exception as e:  # noqa: BLE001
        log.exception("diarize failed for %s", row.meeting_id)
        _log(db, "error", row.meeting_id, step="diarize", err=str(e)[:300])
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
def summarize_one(db, llm: LLMClient) -> bool:
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
    _log(db, "summarize", row.meeting_id, chunks=len(chunks), chars=len(row.summary))
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
def deliver_one(db, mailer: Mailer) -> bool:
    row = db.scalars(select(Meeting).where(Meeting.status == "summarized").limit(1)).first()
    if not row:
        return False
    # TODO(next): calendar recipient resolution + external owner-approval; for now internal policy
    #             falls back to the configured owner. Templates rendered from mail_templates.
    recipients = (row.participants or {}).get("internal") or [settings.mail_from]
    html = f"<h3>{row.title}</h3>{_md_html(row.summary)}"
    try:
        mailer.send(recipients, f"Protokoll: {row.title}", html)
        row.delivered_at, row.status = _now(), "delivered"
        _log(db, "deliver", row.meeting_id, to=recipients)
    except Exception as e:  # noqa: BLE001
        _log(db, "error", row.meeting_id, step="deliver", err=str(e)[:300])
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


# --------------------------------------------------------------------------- agent dispatch (phase 2)
def agent_dispatch(db, vexa: VexaClient) -> None:
    """Calendar (Graph) -> plan upcoming meetings on Vexa with a configurable join lead.
    TODO(next): Graph calendarView for the configured account, dedupe, dispatch with lead time."""
    return
