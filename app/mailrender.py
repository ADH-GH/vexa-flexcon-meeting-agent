"""The single mail renderer — used by BOTH the dashboard preview and the actual delivery.

One renderer means the preview can never drift from what a recipient gets. Templates are rendered by
plain `{{var}}` substitution, deliberately NOT Jinja: a tenant-authored template must never be able to
execute code or reach into the app's context.
"""
from __future__ import annotations

import datetime as dt

# Placeholders a template may use (documented on the dashboard's template page).
PLACEHOLDERS = ("title", "date", "speakers", "summary_html", "action_items")

DEFAULT_SUBJECT = "Protokoll: {{title}}"
DEFAULT_BODY = (
    "<p>Automatisch erzeugtes Meeting-Protokoll:</p>"
    "{{summary_html}}"
    '<hr><p style="color:#888;font-size:12px">Erzeugt aus dem Vexa-Transkript, on-prem '
    "zusammengefasst. Teilnehmer: {{speakers}}</p>"
)


def substitute(text: str, data: dict) -> str:
    out = text or ""
    for key in PLACEHOLDERS:
        out = out.replace("{{" + key + "}}", str(data.get(key, "")))
    return out


def render(tpl, data: dict) -> tuple[str, str]:
    """(subject, html) for a MailTemplate — falls back to the built-in layout when tpl is None."""
    subject_tpl = (getattr(tpl, "subject_tpl", "") or DEFAULT_SUBJECT) if tpl else DEFAULT_SUBJECT
    body_tpl = (getattr(tpl, "body_html_tpl", "") or DEFAULT_BODY) if tpl else DEFAULT_BODY
    return substitute(subject_tpl, data), substitute(body_tpl, data)


def sample_data() -> dict:
    """Realistic stand-in data for the dashboard preview — never touches a real meeting."""
    return {
        "title": "Wochen-Sync Vertrieb",
        "date": dt.date.today().strftime("%d.%m.%Y"),
        "speakers": "Alf-David Heermann, Thomas Endler",
        "summary_html": "<h4>Zusammenfassung</h4><p>Beispiel-Protokoll für die Vorschau.</p>"
                        "<h4>Action Items</h4><ul><li>[Thomas] Angebot nachfassen</li></ul>",
        "action_items": "- [Thomas] Angebot nachfassen",
    }


def meeting_data(row, summary_html: str) -> dict:
    """Template data for a real meeting row."""
    started = (row.started_at or "")[:10]
    try:
        started = dt.datetime.fromisoformat(started).strftime("%d.%m.%Y") if started else ""
    except ValueError:
        pass
    action_items = "\n".join(
        l for l in (row.summary or "").splitlines() if l.strip().startswith(("- ", "* ")))
    return {
        "title": row.title or "Meeting",
        "date": started,
        "speakers": str(row.speaker_count) if row.speaker_count else "–",
        "summary_html": summary_html,
        "action_items": action_items,
    }
