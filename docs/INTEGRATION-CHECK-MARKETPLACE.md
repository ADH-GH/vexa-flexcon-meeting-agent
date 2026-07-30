# Integration check — Flexcon AI Marketplace ↔ Meeting Agent

Read-only review of the marketplace repo (`ciis-capstone-project/summer-2026/team-06/marketplace-agent`,
GitLab, `main` @ 5477148) against this service's design. **Date: 2026-07-30.**

**Verdict: integrable, and the marketplace already owns more than we assumed — which makes the agent
simpler. But there is one hard blocker (a missing Graph scope) that no amount of work on our side can
route around.**

---

## What the marketplace already is

React/Vite frontend + **Supabase** (Postgres + Deno Edge Functions). It owns:

| Concern | Where | Consequence for us |
|---|---|---|
| **Identity + Microsoft OAuth** | `oauth-m365-init/callback`, `oauth-microsoft-admin-*`, tokens stored per connection, retrievable by a backend via `get-oauth-token` (gated by the **Supabase service-role key**) | We should **not** run a second Entra consent |
| **Billing** | `create-checkout` → Stripe → `stripe-webhook` → `subscriptions` table; `check-subscription`, `customer-portal` | Our own Stripe webhook is **not** the marketplace path |
| **Catalog + activation** | `agents` (slug, stripe ids, `configuration`) · `agent_configurations` (`user_id`, `connection_id`, `is_active`, `external_agent_id`, `agent_config`) · `subscriptions` (tier, seats, org) | This is the entitlement source of truth |
| **Existing agents** | n8n-based (`n8n-workflows/`, `admin-n8n-workflow-status`) + ElevenLabs (`elevenlabs-post-call`) | We would be the first standalone-service agent |

---

## 🔴 Blocker — `Calendars.Read` is missing

`supabase/functions/oauth-m365-init/index.ts`:

```ts
const M365_SCOPES = [
  'https://graph.microsoft.com/Mail.Read',
  'https://graph.microsoft.com/Mail.Send',
  'https://graph.microsoft.com/Mail.ReadWrite',
  'https://graph.microsoft.com/User.Read',
  'offline_access', 'openid', 'email', 'profile',
]
```

Mail scopes only — **no calendar scope**. The same is true of `ADMIN_SCOPES` in
`oauth-microsoft-admin-init`.

The Meeting Agent's entire premise is *watch the user's calendar and join their meetings by itself*.
Without `Calendars.Read` there is no calendar to read, so there is **no zero-onboarding product** — the
agent could only be driven manually.

**Fix (marketplace side, one line + re-consent):**
```ts
'https://graph.microsoft.com/Calendars.Read',   // Meeting Agent: watch the user's meetings
```
Note the cost: **users who already connected M365 must re-consent** (the stored token won't carry the new
scope). Worth doing in one pass if other scopes are ever needed — every scope change repeats this.

Only alternative is our own separate consent — i.e. the user consents **twice**. That defeats the
zero-onboarding promise and should be rejected.

---

## 🟡 Architecture correction — the marketplace owns identity and billing

Our `MARKETPLACE-DESIGN.md` and `PRICING-DESIGN.md` assumed this service would run its own Entra OAuth
and its own Stripe webhook, keyed by **Entra tenant**. The marketplace keys everything by **Supabase
user + organization** and already holds both. Two consequences:

1. **Drop our OAuth for the marketplace path.** Instead: read the user's Microsoft token from
   `get-oauth-token` using the service-role key. `agent_configurations.connection_id` already links a
   user's agent to their OAuth connection — exactly the hook we need.
2. **Drop our Stripe webhook for the marketplace path.** Entitlement = `subscriptions.status` +
   `subscriptions.tier` (+ `seats`, `organization_id`), and activation = `agent_configurations.is_active`.
   Our `/billing/*` code stays useful for **direct/on-prem sales**, but must not be the marketplace path
   (Stripe would otherwise have to fan out to two endpoints with two different tenant models).

**Revised integration shape:**
```
Marketplace (Supabase)                     Meeting Agent (this service)
  auth + M365 OAuth  ─── get-oauth-token ──▶ per-user Graph token (service-role key)
  Stripe + subscriptions ── read ──────────▶ entitlement: tier · seats · is_active
  agent_configurations   ── read ──────────▶ who is active, which connection
                                             └─ then: calendar → join → diarise → protocol → mail
```
Mapping: keep our `tenants`/`users` tables, but key them on the **Supabase user/org id** rather than the
Entra tenant, and treat the marketplace as the identity provider.

---

## 🟡 Listing the product — follow their checklist exactly

`NEW_PRODUCT_CHECKLIST.md` documents a bug that has **already happened three times** (`mailsorter` →
`mailsorter_org` → `telefonagent`, root cause tracked as #243): if a new product isn't registered in
**every** place, checkout succeeds and Stripe takes the money, but `stripe-webhook` logs
*"Unknown product, skipping"*, writes **no `subscriptions` row**, and the agent never activates.

Three IDs must match character-for-character across five files:

| ID | Appears in |
|---|---|
| Tier key (`meeting_agent`) | `stripeTiers.ts` key · DB enum `subscription_tier` · `stripe-webhook` `PRODUCT_TIER_MAP.tier` · `get-user-agents` `TIER_PRICE_LABEL` · `check-subscription` `PRODUCT_ID_BY_TIER` |
| Stripe product id | `stripeTiers.ts` · `stripe-webhook` `PRODUCT_TIER_MAP` **key** · `check-subscription` value · `agents.stripe_product_id` |
| Stripe price id | `stripeTiers.ts` · `create-checkout` allowlist · `agents.stripe_price_id` |

Plus: `agents` seed row, i18n `agentCatalog.<slug>` **in all six languages**, and a test checkout that
verifies a `subscriptions` row actually appears.

---

## 🟢 Where we fit cleanly

- **`agent_configurations.external_agent_id`** already exists for agents that live outside Supabase — a
  natural place for our tenant/user handle.
- **`get-oauth-token`** is explicitly built for backend callers (service-role key) — no new auth surface
  needed on the marketplace side.
- **Mail**: their scopes include delegated `Mail.Send`; our delivery uses an app-only Flexcon sender, so
  delivery works either way.
- **Join tiers** (guest vs authenticated Enterprise) map onto `subscriptions.tier` without new concepts.

---

## Open questions for the marketplace team

1. Can `Calendars.Read` be added to `M365_SCOPES` (and the admin consent), accepting the re-consent for
   already-connected users? **This gates the product.**
2. Should activation notify us (a call to this service when `is_active` flips) or do we poll
   `agent_configurations`? A notification is cleaner; polling needs no change on their side.
3. `Calendars.ReadWrite` instead of `.Read`, if we ever want the agent to place or annotate events? Worth
   deciding once, since each scope change forces re-consent.
4. Does the org/seat model (`organization_id`, `seats`) need to gate how many users the agent serves?
