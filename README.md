# Vexa Flexcon Meeting Agent

The Flexcon meeting-automation pipeline as **one self-contained service**. Watches a
[Vexa](https://github.com/Vexa-ai/vexa) deployment, turns every recorded meeting into a
**speaker-attributed German protocol**, and delivers it — with a **web dashboard** to configure it and
an **Agent-Connect API** for the Flexcon Agents integration.

Reuses the standalone [`diarizer`](https://github.com/ADH-GH/diarizer) service (German-CT2 Whisper +
pyannote community-1). Built by **Alf-David Heermann** ([@ADH-GH](https://github.com/ADH-GH), Flexcon IT)
and **Claude** (Anthropic, Opus 4.8).

> **Status: Phase 1 — multi-tenant foundation.** Runs structurally (compose up → dashboard + API +
> scheduler + migrated Postgres 17.5) with **tenant isolation via Postgres RLS** and an **encrypted
> token store**. Onboarding + the per-user auto-pipeline are next (see docs/MARKETPLACE-DESIGN.md).

## What it does (5 pipeline modules)

| Module | Does |
|---|---|
| **handover** | polls Vexa `status=completed` → upserts meetings (dedupe/audit) |
| **diarize** | recording → `diarizer /diarize_upload` → correlate `SPEAKER_xx` to **real names** via Vexa's live transcript → store |
| **summarize** | diarized transcript → chunked map-reduce over an **OpenAI-compatible LLM** → German protocol |
| **deliver** | resolve recipients (internal direct / external owner-approval) → render **template** → send via **SMTP or Graph** |
| **agent-dispatch** | calendar (Graph) → plan bots on Vexa with a configurable **join lead** *(phase 2)* |

State machine: `transcribed → diarized → summarized → delivered`. The diarize step is single-flight (one
GPU job per tick).

## Stack & decisions

- Python 3.12 · FastAPI (API + **server-rendered** dashboard) · APScheduler · SQLAlchemy · **Postgres 17.5**
- Auth: **Entra SSO** primary + **local user/password fallback**
- Calendar: **MS Graph** · Mail: **SMTP or Graph** (configurable), selectable templates
- Dashboard adds **Insights & Reports**; the **Agent Connector** (Flexcon Agents) is a headline value-add

## Run

```bash
cp .env.example .env       # fill in Vexa/LLM/mail; set POSTGRES_PASSWORD (never commit .env)
docker compose up -d       # app + postgres:17.5
curl -s localhost:8080/health | jq
# dashboard: http://localhost:8080/
```

## Layout

```
app/
  config.py      env + settings          models.py    Postgres schema (meetings, settings, templates, api_keys, event_log)
  db.py          engine/session          clients/     Vexa · Diarizer · LLM · Mailer (SMTP/Graph)
  pipeline/      handover·diarize·summarize·deliver·agent_dispatch (pipeline logic)
  scheduler.py   APScheduler loops       web.py       health · Agent-Connect API · dashboard
  main.py        app factory             templates/   server-rendered dashboard
```

## Build phases

1. ✅ Skeleton + **multi-tenancy** — FastAPI · Postgres 17.5 · scheduler (per-tenant) · tenants/users · +**RLS** · encrypted token store.
2. Post-call pipeline — finish + live-test handover · diarize · summarize · deliver (SMTP + Graph, templates).
3. Dashboard — settings (agent lead · LLM · mail templates · **Insights & Reports** · API keys).
4. **agent-dispatch** + **Agent-Connect** API (Flexcon Agents).
5. Cutover — run in parallel with the current setup, compare, then switch over.

Design: `docs/MARKETPLACE-DESIGN.md` (multi-tenant, zero-onboarding, tiered join) · architecture note in the flexcon-workbench.
