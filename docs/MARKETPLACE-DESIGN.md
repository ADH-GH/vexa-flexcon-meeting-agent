# Marketplace Design — Meeting Agent (multi-tenant, zero-onboarding)

**Goal.** Ship the Meeting Agent to the Flexcon AI Marketplace (`flexcon-ai.de`) as a **zero-onboarding
SaaS**: a user signs in with Microsoft once, and from that moment their meetings are automatically
recorded, transcribed, speaker-attributed, summarised (German), and the protocol delivered — **no
configuration required**. Config is possible but never mandatory. EU / on-prem, **DSGVO-first**.

Reference model: Fireflies — connect your calendar via SSO, the notetaker joins automatically, done.
Our differentiator on top: **German-grade STT + real-name diarisation** and an **authenticated Teams
join** that also works on strict tenants.

This reshapes the current **single-tenant** service (one `automation@` account) into **multi-tenant
SaaS**. The existing skeleton (FastAPI + Postgres 17.5 + pipeline modules) is the foundation.

---

## Onboarding flow (the entire user journey)

```
Marketplace card "Meeting Agent"  →  [ Sign in with Microsoft ]
        ↓  Entra consent (delegated scopes below)  — the ONLY step
Create tenant + user, store refresh token (encrypted), apply zero-config defaults.  ✅ done.
        ↓  (background, no user action)
Watch the user's calendar → bot auto-joins their Teams meetings → transcribe · diarize · summarise
→ deliver the German protocol.   Configuration optional, in the dashboard.
```

**Entra scopes (delegated):** `User.Read`, `Calendars.Read`, `Mail.Send` (delivery), `offline_access`.
A multi-tenant Azure AD app; org-wide rollout via admin consent. OAuth **auth-code + PKCE**; refresh
tokens stored **encrypted**; a refresh worker keeps them alive.

---

## Tiered join (approved) — where our USP becomes a premium lever

The one hard constraint: zero-config wants an *anonymous* join, but strict tenants block that; our
authenticated join solves strict tenants but needs a signed-in identity (not zero-config). So we tier it:

| Tier | Join | Onboarding | For |
|---|---|---|---|
| **Zero-Config (default)** | **Guest join** via the calendar link (Fireflies-style) | MS-SSO only | the broad base / permissive tenants |
| **Enterprise (upgrade)** | **Authenticated join** (our fork) | one-time per-tenant bot identity (account + session, admin-consented) | strict/enterprise tenants that block guest join |

**Auto-detect:** if a guest join lands in the lobby / is blocked, the meeting is flagged and the tenant
is offered the Enterprise upgrade — the strict-tenant capability is a paid differentiator, not an
onboarding wall.

Join identity is **separate** from user SSO (SSO = calendar/identity/mail; the bot join is infra).

---

## Multi-tenant data model (extends the skeleton)

- **tenants** — `id`, `entra_tenant_id`, `name`, `tier` (free|enterprise), `join_mode` (guest|auth),
  `retention_days`, `created_at`.
- **users** — `id`, `tenant_id (fk)`, `entra_object_id`, `email`, `display_name`,
  `refresh_token_enc`, `prefs (jsonb)`, `active`.
- **meetings** — the existing table + `tenant_id`, `user_id` (owner). Every query scoped by tenant.
- **settings/mail_templates/api_keys/event_log** — gain `tenant_id`; API keys + audit are per-tenant.

**Isolation:** tenant_id on every row + enforced in every query; tokens encrypted at rest (per-tenant or
app key, Fernet/KMS); recordings + transcripts partitioned per tenant; retention + right-to-erasure per
tenant. The pipeline runs **per user**, using that user's delegated token for calendar + delivery.

---

## Zero-config defaults (works out of the box; all optional to change)

| Knob | Default |
|---|---|
| Language | auto (de/en) · summary in German |
| Auto-join | every calendar meeting with a Teams link (per-meeting/global toggle) |
| Join lead | 2 min before start |
| Delivery | the German protocol to the organiser/user, right after the call |
| Recording notice | the bot announces itself on join (consent-friendly; greeting already built) |
| Retention | tenant default (configurable; DSGVO) |

Nothing above requires input at onboarding — sensible defaults apply the moment SSO consent lands.

---

## The pipeline, per user (reuses the modules)

- **agent-dispatch** — per-user calendar (their token) → plan bots on Vexa; **guest or authenticated**
  per the tenant tier.
- **handover · diarize · summarize · deliver** — as built, scoped per user; delivery via the user's
  mailbox (`Mail.Send`) or a Flexcon sender, per settings.

---

## DSGVO / data protection (a headline promise)

EU/on-prem hosting · audio + transcripts encrypted at rest · **per-tenant retention + delete** ·
right-to-erasure endpoints · purpose limitation + consent (recording notice) · per-tenant audit
(`event_log`) · data-processing agreement. STT + diarisation + summarisation stay **on-prem** — audio
never leaves the environment.

---

## Marketplace integration (`flexcon-ai.de`)

- Listing **"Meeting Agent"** → **"Connect Microsoft"** = the onboarding above.
- Provisioning: on connect, the marketplace calls the agent's onboarding endpoint; tier/billing come
  from the marketplace. Shared Entra SSO session with the marketplace.
- Upsell path: guest-join users who hit a strict tenant are offered the **Enterprise** tier in-product.

---

## Security

Least-privilege Graph scopes · encrypted token store · tenant isolation enforced in every query ·
per-tenant Agent-Connect API keys · signed webhooks · admin-consent for org-wide rollout.

---

## Build phases (from the current skeleton)

1. **Multi-tenancy** — tenants/users tables, tenant-scope every query, encrypted token store.
2. **Entra onboarding** — auth-code+PKCE, consent, "Sign in with Microsoft", refresh worker.
3. **Per-user auto-pipeline** — calendar watch + dispatch + deliver with the user's token; zero-config defaults.
4. **Join tiers** — guest join (default) + the authenticated-join Enterprise upgrade path.
5. **Dashboard** — onboarding status · optional per-user settings · Insights & Reports · Agent Connector.
6. **Marketplace** — listing + provisioning + billing hooks + DSGVO (retention/erasure).

---

## Open questions for review

- **Delivery identity** — send FROM the user's mailbox (`Mail.Send` delegated) vs. a Flexcon sender
  (consistent branding, simpler consent)? Recommend a Flexcon sender by default, user-mailbox optional.
- **Guest-join infra at scale** — one shared bot pool for all guest joins vs. per-tenant. Guest joins
  can share a pool; authenticated needs a per-tenant session.
- **Billing boundary** — free/zero-config (guest join) vs. Enterprise (authenticated join) — same line
  as the join tier?
- **Recording storage** — shared object store with per-tenant prefixes vs. per-tenant buckets.

---

## Decisions (2026-07-29)

- **Delivery identity:** **Flexcon sender** (consistent branding, simpler consent). User-mailbox optional later.
- **Guest-join infra:** **one shared bot pool** for all guest joins (authenticated joins get a per-tenant session).
- **Tenant isolation:** **shared tables + `tenant_id` + Postgres Row-Level Security (RLS)** — forecast below.
- **Free tier:** paid-only (trial instead) — see `PRICING-DESIGN.md`.

### Tenancy forecast — table-per-tenant vs. one table with `tenant_id` (~1000 tenants)
**Recommendation: one shared set of tables, `tenant_id` on every row, composite indexes (tenant_id
first), and Postgres RLS enforcing tenant scope at the database.**

| Model | 1000 tenants | Isolation | Migrations | Verdict |
|---|---|---|---|---|
| **Table per tenant** (~6k tables) | catalog bloat (pg_class), autovacuum + plan-cache pressure, backup pain | physical | ALTER ~6000 tables per change | operational wall before 1000; **avoid** |
| **Schema per tenant** (1000 schemas) | same fan-out + catalog bloat at 1000s | strong | 1000× | OK ~100s, heavy at 1000s |
| **Shared + tenant_id + RLS** ✅ | trivial — 1000 (even 100k) tenants is nothing; the limit is rows/disk, not tenant count | **DB-enforced** (RLS) | **one** migration | **scales; recommended** |

RLS gives "eindeutig getrennt" **at the DB layer** (Postgres itself blocks cross-tenant reads), not just
in app code — 1000 tenants is nowhere near any Postgres limit. For customers who demand *physical*
separation, offer an **optional dedicated DB / on-prem deployment (Enterprise)**.

### Enterprise — maximum MS-tenant security (positioning)
The security flagship tier: **authenticated Entra join** (no anonymous/guest — works under Conditional
Access / MFA / lobby-locked tenants) · **admin consent + least-privilege Graph** · **tenant isolation +
EU data residency** (on-prem option) · **encryption at rest + in transit** · **full per-tenant audit
trail** · **retention + right-to-erasure / DLP** · **SSO (+ SCIM)** · **no data used for model training**
· **DPA**. Everything a Microsoft-tenant security/compliance team requires — the reason a regulated
enterprise picks us over US notetakers.
