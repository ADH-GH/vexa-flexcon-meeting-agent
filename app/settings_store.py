"""Per-tenant operational settings with zero-config defaults.

Onboarding writes nothing: every knob has a sensible default, so the pipeline runs the moment a user
signs in. The dashboard only *overrides* — a tenant row appears in `settings` when someone changes it.
Secrets stay in env (`config.py`); this holds the dashboard-editable operational knobs.
"""
from __future__ import annotations

from sqlalchemy import select

from .config import settings as env
from .models import Setting

# key -> (default, group, label, kind)  — `kind` drives the form widget
DEFAULTS: dict[str, tuple] = {
    # agent
    "auto_join":        (True,  "agent", "Meetings automatisch beitreten", "bool"),
    "join_lead_s":      (120,   "agent", "Vorlauf beim Beitritt (Sekunden)", "int"),
    "dispatch_window_h": (24,   "agent", "Kalender-Vorschau (Stunden)", "int"),
    # llm
    "llm_model":        (env.llm_model, "llm", "Modell", "str"),
    "llm_chunk":        (12000, "llm", "Chunk-Größe (Zeichen)", "int"),
    "llm_overlap":      (1500,  "llm", "Chunk-Überlappung (Zeichen)", "int"),
    "llm_temperature":  (0.2,   "llm", "Temperatur", "float"),
    "summary_language": ("de",  "llm", "Protokoll-Sprache", "str"),
    # mail
    "mail_enabled":     (True,  "mail", "Protokoll per E-Mail zustellen", "bool"),
    "mail_template":    ("default", "mail", "Vorlage", "str"),
    "recipient_policy": ("owner", "mail", "Empfänger (owner | internal)", "str"),
    # data protection
    "retention_days":   (90,    "dsgvo", "Aufbewahrung (Tage)", "int"),
}

GROUPS = {"agent": "Meeting Agent", "llm": "LLM & Zusammenfassung", "mail": "Mail & Vorlagen",
          "dsgvo": "Datenschutz"}


def get_all(db, tenant_id: int) -> dict:
    """Effective settings = defaults overlaid with this tenant's overrides."""
    out = {k: v[0] for k, v in DEFAULTS.items()}
    for row in db.scalars(select(Setting).where(Setting.tenant_id == tenant_id)).all():
        if row.key in out:
            out[row.key] = (row.value or {}).get("v", out[row.key])
    return out


def get(db, tenant_id: int, key: str):
    return get_all(db, tenant_id).get(key)


def put(db, tenant_id: int, key: str, value) -> None:
    """Store one override (upsert). Values are wrapped as {"v": ...} so JSONB holds scalars too."""
    if key not in DEFAULTS:
        return
    row = db.get(Setting, {"tenant_id": tenant_id, "key": key})
    if row:
        row.value = {"v": value}
    else:
        db.add(Setting(tenant_id=tenant_id, key=key, value={"v": value}))


def coerce(key: str, raw: str):
    """Form strings -> typed values, per the declared kind."""
    _default, _group, _label, kind = DEFAULTS[key]
    if kind == "bool":
        return str(raw).lower() in ("1", "true", "on", "yes")
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return DEFAULTS[key][0]
    if kind == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return DEFAULTS[key][0]
    return raw


def form_model(db, tenant_id: int) -> list[dict]:
    """Grouped view for the settings page."""
    eff = get_all(db, tenant_id)
    groups: dict[str, list] = {g: [] for g in GROUPS}
    for key, (default, group, label, kind) in DEFAULTS.items():
        groups[group].append({"key": key, "label": label, "kind": kind,
                              "value": eff[key], "default": default})
    return [{"id": g, "title": GROUPS[g], "fields": groups[g]} for g in GROUPS]
