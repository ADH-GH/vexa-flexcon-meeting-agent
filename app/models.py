"""Database schema — the meeting store + dashboard-editable config."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Meeting(Base):
    """One row per Vexa meeting — the pipeline state machine lives here.
    status: transcribed -> diarized -> summarized -> delivered  (+ *_failed guards)."""
    __tablename__ = "meetings"

    meeting_id: Mapped[str] = mapped_column(String, primary_key=True)   # Vexa meeting id
    native_id: Mapped[str] = mapped_column(String, default="")          # platform_specific_id
    title: Mapped[str] = mapped_column(String, default="")
    started_at: Mapped[str] = mapped_column(String, default="")
    ended_at: Mapped[str] = mapped_column(String, default="")
    language: Mapped[str] = mapped_column(String, default="")
    segment_count: Mapped[int] = mapped_column(Integer, default=0)
    participants: Mapped[dict] = mapped_column(JSONB, default=dict)     # resolved recipients/attendees
    status: Mapped[str] = mapped_column(String, default="transcribed", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    diarized_transcript: Mapped[str] = mapped_column(Text, default="")
    speaker_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diarized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Setting(Base):
    """Key/value operational settings the dashboard edits (non-secret). Secrets stay in env."""
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class MailTemplate(Base):
    """Selectable protocol email templates (subject + HTML body with {{variables}})."""
    __tablename__ = "mail_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    lang: Mapped[str] = mapped_column(String, default="de")
    subject_tpl: Mapped[str] = mapped_column(String, default="Protokoll: {{title}}")
    body_html_tpl: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiKey(Base):
    """Agent-Connect API keys (Flexcon Agents integration — a headline value-add)."""
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    key_hash: Mapped[str] = mapped_column(String, index=True)
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventLog(Base):
    """Audit: dispatches + deliveries (feeds Insights & Reports)."""
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)   # dispatch | diarize | summarize | deliver | error
    meeting_id: Mapped[str] = mapped_column(String, default="", index=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
