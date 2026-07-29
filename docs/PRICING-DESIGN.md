# Pricing & Billing — Meeting Agent (marketplace, Stripe)

Unlike pure-software SaaS, we have a **real marginal cost per meeting** (GPU + LLM + storage + bot). So
pricing must be **cost-anchored**, not seat-only. Reference model: Fireflies (freemium + per-seat) — but
tuned for (a) **cost coverage**, (b) the **DSGVO / EU-sovereignty upsell**, (c) the **Enterprise**
(authenticated-join) tier.

---

## Unit economics — what a meeting costs us

Cost scales with **meeting-minutes**, not seats:

| Driver | ~ per meeting-hour |
|---|---|
| **GPU** (dominant) — live STT ≈ 1× duration + post-call diarize ≈ +2–3 min | ~1.0 GPU-hour |
| **LLM** — chunked map-reduce summary | ~a few k tokens / meeting |
| **Storage** — ~50 MB/h audio (webm) + transcript, retention-dependent | ~0.05 GB/h × retention |
| **Bot container** — one browser/CPU per concurrent meeting, for its duration | ~1.0 CPU-hour |
| Fixed overhead — calendar poll, DB, per-tenant | small |

→ **A per-seat-only price loses money on heavy users** (back-to-back meetings all day). Fix:
**per-seat + included meeting-hours + metered overage** — the cap bounds GPU exposure, overage stays
profitable.

**Cost model (fill in your infra numbers):**

| Item | Your € | × per meeting-hour |
|---|---|---|
| GPU-hour (STT + diarize) | € ___ | ~1.0 |
| LLM (summary) | € ___ | 1 / meeting |
| Storage (per GB·month × retention) | € ___ | ~0.05 GB/h |
| Bot container (CPU-hour) | € ___ | ~1.0 |
| **Marginal cost / meeting-hour** | **€ ___** | |

Price each included hour ≥ marginal cost × (1 + target margin). That is the cost-coverage guarantee.

---

## Pricing model (recommended)

**Freemium → per-seat tiers with included hours → metered overage → Enterprise.** Same shape as
Fireflies; cost-anchored via the hour caps.

| Tier | Join | Included (example) | Positioning | Stripe |
|---|---|---|---|---|
| **Free** | guest | ~5 h/mo · 7-day retention | funnel, zero-config | $0, no card |
| **Pro (DSGVO)** | guest | per-seat + ~20 h/mo, overage metered | **EU / on-prem sovereignty** — the reason to pick us over US tools | subscription + metered |
| **Enterprise** | **authenticated** | custom hours · SSO · retention · SLA | strict tenants, our USP | custom / invoice |

- **(a) Cost coverage** — included-hour cap + metered overage keep GPU always covered; Free's low cap
  bounds free-tier cost.
- **(b) DSGVO upsell** — Pro's core value is **EU/on-prem data sovereignty** (US tools ship audio out of
  the EU). This is *the* buying reason for German/EU customers and justifies parity-or-premium pricing.
- **(c) Enterprise** — authenticated join for strict tenants + admin SSO + SLA; custom-priced (our
  hardest capability = the top tier).

Numbers above are **placeholders** — set them from the cost model + your margin target.

---

## Fireflies as reference (adopt the shape, verify the numbers)

Freemium + per-seat (historically ~$10–30/user/mo) + AI-credit limits + Enterprise custom. We take the
shape; our differentiators (German STT, real-name diarisation, **EU/on-prem DSGVO**, authenticated join)
justify parity-or-premium to EU buyers. Confirm Fireflies' *current* tiers before finalising — SaaS
pricing drifts.

---

## Stripe mechanics

- Tiers = Stripe **Products/Prices**. Per-seat = licensed Price (quantity = seats); overage = **metered**
  Price (usage records). Free = $0 / no card. Enterprise = manual invoice.
- The marketplace (`flexcon-ai.de`) runs **Checkout + Customer Portal**; the Meeting Agent **reports
  billable usage** (meeting-minutes per tenant) → Stripe usage records for the metered overage.
- **Webhooks**: `subscription.created/updated/deleted` → provision/deprovision the tenant's tier +
  `join_mode` (guest vs authenticated); usage pushed on a nightly job.
- EU **VAT / reverse-charge** handled by Stripe Tax.

---

## Usage metering (in the app — new)

Extend `event_log`: on each processed meeting, record **billable minutes** (meeting duration) per
tenant/user. A daily aggregator → Stripe usage records (metered overage) — the billing source of truth,
and it also feeds **Insights & Reports**. Tier + `join_mode` come from the Stripe subscription via webhook.

---

## Open questions (need your inputs)

- **Infra cost** per GPU-hour (owned vs rented) + LLM + storage → sets the included-hour price.
- **Target gross margin** (e.g. 60–70 %)?
- **Per-seat vs usage weighting** — more per-seat (predictable for buyers) vs. more metered (fairer on cost)?
- **Free-tier cap** (hours + retention) — funnel reach vs. free-cost exposure.
- **Enterprise = authenticated-join = the paywall line?** (i.e. strict-tenant capability is Enterprise-only.)

---

## Decisions & numbers (2026-07-29)

**Inputs (ADH):** server **€250/mo** (fixed) · LLM **€2 / 1M tokens** · target **50 % gross margin** (floor)
· structure weighted to **margin-max** (lean on metered usage) · delivery from a **Flexcon sender**.

**Marginal cost per meeting-hour (derived):**
- LLM ≈ 30k tokens/h → **€0.06/h** · storage ≈ **€0.02/h** (noise).
- Server €250/mo ÷ capacity (capacity = concurrency × ~176 business-h/mo):
  C=4 → ~700 h → €0.36/h · C=8 → ~1400 h → €0.18/h.
- → **marginal cost ≈ €0.25–0.45 / meeting-hour** (server-bound).
- At 50 % margin (price = 2× cost) → **≈ €0.70 / included meeting-hour** (floor).

**Unit economics — one €250 server:** ~700–1400 meeting-h/mo. At ~20 h/user/mo → **~35–70 paid users per
server**. Revenue (Pro @ €19) €665–1330 vs. cost ~€300–334 → **50–75 % gross margin.** Cost-covering with
headroom; each extra server scales linearly.

**Illustrative tiers (confirm the capacity assumption first):**
| Tier | €/seat/mo | Included | Overage | Notes |
|---|---|---|---|---|
| **Pro (DSGVO)** | ~€19 | 20 h/mo | ~€0.90/h | competitive w/ Fireflies, ~2× cost |
| **Enterprise** | custom (€39+) | custom | custom | authenticated join + max MS-tenant security + SLA |

Overage priced for **margin-max** (≥2× cost); included hours kept modest so heavy use flows to metered.

**Free tier — recommendation: NO perpetual free tier** (matches "präferiert nur paid"). A free user at
5 h/mo costs ~€2–3.50 + LLM; at ~2–5 % free→paid that's an effective CAC of **~€40–175 / paid user** —
often worse than direct marketing. Instead: a **14-day full-featured trial** (bounded cost) + put the
would-be free subsidy into targeted marketing/sales. Revisit only if traction data beats paid CAC.

**One number to confirm:** the server's **meeting-hour capacity** (sustained concurrency) — it swings the
per-hour cost between €0.18 and €0.71. Give a realistic concurrent-meeting number and the tier prices lock.
