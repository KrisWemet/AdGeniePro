# AdGeniePro — AI-run affiliate ad campaigns

A Next.js + Postgres app where AI runs the affiliate-ad loop end to end, inside
hard budget guardrails you control:

1. **Discover** — a daily job pulls the ClickBank marketplace, pre-filters for
   proven products (gravity ≥ 15, commission ≥ 40%, ≥ $20/sale), and has Claude
   score each one 0–100 for sellability via paid Meta traffic. Score ≥ 70 →
   shortlisted.
2. **Build** — for a shortlisted product, Claude writes 3 policy-compliant ad
   variants with distinct angles; the app creates the campaign, ad set, and ads
   on Meta — always in `PAUSED` state first.
3. **Run** — in `approve` mode you click *Approve & launch* in the dashboard; in
   `auto` mode the AI activates it itself, but only if the guardrails allow.
4. **Optimize** — every 4 hours a deterministic rules engine reviews ROAS per
   campaign: pauses losers (ROAS < 0.5 after real spend), scales winners
   (ROAS ≥ target for 3+ days, capped at ±20% per pass), and logs the rationale
   for every action to the AI Activity page.

### Guardrails (non-negotiable, enforced in code)

- **Daily budget cap** across all campaigns, and a **lifetime spend cap**.
- Budget changes are clamped to ±20% per optimizer pass.
- Everything is created `PAUSED` on Meta; activation is a separate gated step.
- **Kill switch**: one checkbox stops all delivery and freezes the AI.
- Every AI decision — including refusals ("wanted to scale but guardrail said
  no") — is written to `ai_actions` with a plain-English rationale.

### The funnel layer (own products + email)

Beyond running ads for ClickBank offers, AdGeniePro monetizes the traffic twice
more with AI-created digital products of your own:

- **Upsell (buyers)** — after a ClickBank sale (detected via the ClickBank INS
  webhook), the buyer enters a 4-email sequence selling a companion product
  Claude designed to fill the biggest gap the main product leaves open
  ($27–$67).
- **Stepping stone (non-buyers)** — leads who opt in on your bridge page but
  don't buy enter a 5-email sequence: value first, then a low-priced ($5–$17)
  quick-win product Claude wrote, then a bridge back to the ClickBank offer.

Generate both for any discovered product:

```bash
curl -X POST $APP/api/products/own/generate -u admin:$PASS \
  -H 'content-type: application/json' \
  -d '{"product_id": "<uuid>", "kind": "tripwire"}'   # and again with "upsell"
```

Review the generated product and emails on the **Funnel** page, optionally add
a Stripe Payment Link as the checkout URL (empty = delivered free at
`/p/<slug>`), and flip it to **live** to start enrollment. Emails send via
Resend every 15 minutes; every send honors unsubscribes and the kill switch.

Wire-up on the traffic side: your bridge page posts opt-ins to
`POST /api/leads` (fields `email`, `name`, `campaign_id`), and ClickBank INS
posts sale notifications to `POST /api/webhooks/clickbank`.

## Setup

1. **Postgres**: create a database (Neon is the easy path — from your Vercel
   project's *Storage* tab click *Create Database → Neon*, which also sets
   `DATABASE_URL` automatically; or sign up at neon.tech). Run
   `db/migrations/0001_init.sql` then `0002_funnel.sql` against it (Neon SQL
   editor or `psql $DATABASE_URL -f ...`).
2. **Meta**: create a Meta app with Marketing API access, generate a
   long-lived system-user token with `ads_management`, and note your ad
   account id and Page id. Your ad account must have a payment method and the
   Page must pass Meta review.
3. **ClickBank**: create an affiliate account; set `CLICKBANK_NICKNAME` so
   hoplinks can be generated. Optionally add API keys for automatic revenue
   sync (otherwise revenue can be recorded manually in `metrics_daily`).
4. **Anthropic**: set `ANTHROPIC_API_KEY`.
5. Copy `.env.example` → `.env.local`, fill it in, then:

```bash
npm install
npm run dev
```

Deploy to Vercel with the same env vars; `vercel.json` schedules the three cron
jobs (discovery daily, metrics sync hourly, optimizer every 4h). Set
`CRON_SECRET` in Vercel so the cron endpoints are authenticated.

## Launching a campaign

From the Products page, pick a shortlisted product id, then:

```bash
curl -X POST https://your-app.vercel.app/api/campaigns/launch \
  -u admin:$DASHBOARD_PASSWORD \
  -H 'content-type: application/json' \
  -d '{"product_id": "<uuid>", "daily_budget_cents": 1000, "landing_url": "https://..."}'
```

Then approve it from the Campaigns page (or it activates itself in `auto` mode).

## Important operational notes

- **Direct affiliate links usually fail Meta review.** Use a bridge/landing
  page you own (the GitHub Pages site in this repo's root can host one) and
  pass it as `landing_url`. Direct hoplinks are supported but expect
  rejections.
- **Revenue attribution**: ClickBank reports revenue at the account level. With
  one active campaign it's attributed automatically; with several, use
  ClickBank tracking IDs (`?tid=`) per campaign before trusting per-campaign
  ROAS.
- **Give campaigns learning time.** The optimizer deliberately does nothing
  until a campaign has spent the learning threshold (default $20).

## Phase 2 (outside funding) — read before building

The long-term idea of letting other people fund ad budgets in exchange for a
share of profit is, in most jurisdictions (including Canada and the US), a
**regulated investment offering** — regardless of whether real ads run.
ai.marketing, the inspiration, was widely alleged to be a Ponzi scheme and
collapsed with participant funds. Before accepting a dollar of outside money,
get securities-law advice. This codebase deliberately contains no
investor-facing functionality; the `ai_actions` audit trail and per-campaign
accounting were designed so honest per-funder bookkeeping *could* be built
later, if and only if it's done legally.
