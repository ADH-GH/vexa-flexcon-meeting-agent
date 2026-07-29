# Vexa Flexcon Meeting Agent

The Flexcon meeting-automation pipeline as **one self-contained service**. Watches a
[Vexa](https://github.com/Vexa-ai/vexa) deployment, turns every recorded meeting into a
**speaker-attributed German protocol**, and delivers it — with a **web dashboard** to configure it and
an **Agent-Connect API** for the Flexcon Agents integration.

Reuses the standalone [`diarizer`](https://github.com/ADH-GH/diarizer) service (German-CT2 Whisper +
pyannote community-1). Built by **Alf-David Heermann** ([@ADH-GH](https://github.com/ADH-GH), Flexcon IT)
and **Claude** (Anthropic, Opus 4.8).

> **Status: Phase 5 — join tiers & billing.** After "Sign in with Microsoft" everything runs hands-free
> (calendar → join → diarise → summarise → deliver), configured from the dashboard, billed through
> Stripe by meeting-minutes, with **guest join** as the zero-config default and **authenticated join**
> as the Enterprise tier for strict Microsoft tenants.
>
> Design rules: **every switch in the UI does something, or it isn't there** (a settings key with no
> consumer is a bug) — and **an Enterprise tenant never silently falls back to a guest join**, because
> that is the exact capability the tier was bought for.

## What it does (5 pipeline modules)

| Module | Does |
|---|---|
| **handover** | polls Vexa `status=completed` → upserts meetings (dedupe/audit) |
| **diarize** | recording → `diarizer /diarize_upload` → correlate `SPEAKER_xx` to **real names** via Vexa's live transcript → store |
| **summarize** | diarized transcript → chunked map-reduce over an **OpenAI-compatible LLM** → German protocol |
| **deliver** | pick recipients per policy (owner / internal attendees) → render the tenant's **mail template** → send via **SMTP or Graph** |
| **agent-dispatch** | per-user calendar (Graph) → plan bots on Vexa once the meeting is within the configured **join lead** |
| **retention** | past `retention_days`, erase transcript + summary content (row kept as audit/billing record) |

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
  config.py         env + secrets            models.py        Postgres schema (tenants, users, meetings,
  db.py             engine · RLS ·                            settings, mail_templates, api_keys, event_log)
                    tenant_scope()           clients/         Vexa · Diarizer · LLM · Mailer (SMTP/Graph)
  auth.py           Entra OAuth (PKCE) ·     pipeline/        handover · diarize · summarize · deliver ·
                    per-user tokens                           agent_dispatch
  crypto.py         token encryption         scheduler.py     post-call · dispatch · refresh · retention
  settings_store.py per-tenant settings,     web.py           health · Agent-Connect API · dashboard
                    zero-config defaults     templates/       server-rendered pages
  apikeys.py        Agent-Connect keys       mailrender.py    ONE renderer for preview *and* delivery
  main.py           app factory
```

## Build phases

1. ✅ **Multi-tenancy** — FastAPI · Postgres 17.5 · per-tenant scheduler · tenants/users · tenant_id + **RLS** · encrypted token store.
2. ✅ **Entra onboarding** — Sign in with Microsoft (auth-code + PKCE) · auto-provision tenant + user · refresh-token worker · local admin fallback.
3. ✅ **Per-user auto-pipeline** — per-user calendar watch → plan on Vexa (guest join) → diarise → summarise → deliver to the owner (Flexcon sender).
4. ✅ **Dashboard & settings** — agent · LLM · mail templates (one renderer for preview *and* delivery) · **Insights & Reports** · **Agent Connector** + API keys · DSGVO retention job.
5. ✅ **Join tiers & billing** — guest (shared pool) vs authenticated Enterprise (own Vexa deployment, no silent fallback) · Stripe webhook sets entitlements · nightly metered usage · marketplace provisioning · Plan &amp; usage page with the Enterprise upsell signal.
6. Cutover — run in parallel with the current setup, compare, then switch over.

Design: `docs/MARKETPLACE-DESIGN.md` (multi-tenant, zero-onboarding, tiered join) · architecture note in the flexcon-workbench.
