# First ClickBank affiliate test

This build supplies encrypted ClickBank revenue ingestion, compatible tracking
links, read-only preflight, and a single-server Docker deployment. A local test
passing is not evidence that Meta accepted an ad or ClickBank delivered a sale.

Local verification for this change: 697 tests passed on Python 3.12. Compose
passed the official Compose JSON schema. Docker is unavailable in the audit
environment, so the image, PostgreSQL container, HTTPS deployment and live
Meta/ClickBank round trips have not been executed here.

## What is ready to verify locally

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/
```

`tests/test_clickbank.py` exercises offer creation, simulated campaign creation,
the real `/r` endpoint, encrypted INS delivery, spend/revenue/profit calculations,
duplicate notifications, partial/full refunds, out-of-order refunds and rebills.
It includes an independently generated OpenSSL decryption vector. It does not
contact an ad account or purchase an offer.

The existing simulator is also available:

```bash
python -m adgenie.cli demo --days 21
```

## Required live inputs

| Input | How it is used |
|---|---|
| Actual Water Freedom System HopLink | Affiliate destination; do not substitute a seller's untracked page |
| ClickBank affiliate nickname | Reject notifications belonging to another account or role |
| ClickBank INS secret | Same uppercase alphanumeric value in ClickBank and `CLICKBANK_INS_SECRET`, at most 16 characters |
| Approved payout and marketing facts | Offer record and copy brief; enter verified facts rather than invented testimonials |
| Public tracking hostname and server | Host the Python API with stable HTTPS and persistent storage |
| Meta token, ad account, Page, pixel | Read-only checks followed by an explicitly approved paused launch |
| USD account currency | Required until currency normalization exists |
| KIE API key | Real image generation for the current `--with-media` launch path |
| Approved daily budget/cap | No spending amount is supplied by the deployment template |

`CLICKBANK_API_KEY` is reserved for future reporting integrations. INS does not
use it. A ClickBank INS secret is different from the generic `POSTBACK_SECRET`.

## Deploy after approval

The supplied Compose configuration targets one Linux server with Docker Compose.
Point a public DNS name to that server and allow inbound TCP 80 and 443. Caddy
obtains HTTPS certificates and proxies to the API. PostgreSQL and the API are
not bound to public host ports. This does not change the GitHub Pages policy site.

1. Copy `deployment.env.example` to `.env` on the server and fill in the inputs.
2. Generate each long secret independently with `secrets.token_hex(32)`. Use a hex
   PostgreSQL password so it is safe inside a database URL. Generate the ClickBank
   secret separately with `secrets.token_hex(8).upper()` and enter the same value
   in ClickBank. Keep the filled-in `.env` out of Git.
3. Leave `DRY_RUN=true`. Set the approved USD cap explicitly; the template has no
   default spending amount.
4. Validate and start:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Database and media volumes survive container replacement. Production startup
refuses insecure API configuration. `/healthz` tests database connectivity without
calling Meta or Google. Inspect production logs without sharing secrets.

Back up the database before upgrades and regularly during the trial:

```bash
mkdir -p backups
docker compose exec -T db pg_dump -U adgenie -d adgenie -Fc > backups/adgenie.dump
```

Copy backups to separate storage and verify restoring to a disposable database.
Named volumes are persistence, not a backup. Do not use `docker compose down -v`
against a deployment whose records you need. The schema change is a new receipt
table; existing columns are unchanged and `init_db` creates the table on startup.

## Deployed on Railway instead of Compose

The Compose section above describes a single Linux server, and it remains the
reference for that path. This trial is deployed on Railway, where three things
differ:

- **No Caddy and no `compose.yaml`.** Railway terminates TLS at its own edge, so
  the `proxy` service and the `Caddyfile` are unused. `compose.yaml` is still the
  right file for a VPS; it is simply not what is running.
- **Managed Postgres.** `DATABASE_URL` must name the `psycopg` driver
  explicitly — `postgresql+psycopg://...`. Railway's own `DATABASE_URL` uses the
  bare `postgresql://` scheme, which SQLAlchemy resolves to psycopg2, which is
  not installed. It is built from the Postgres service's `PGUSER`, `PGPASSWORD`,
  `RAILWAY_PRIVATE_DOMAIN` and `PGDATABASE` over the private network.
- **The platform assigns the port.** Railway sets `PORT` (8080 in practice), and
  the container honours it. A container that hardcodes 8000 binds a port nothing
  routes to and receives no traffic while looking healthy.

`TRUST_PROXY_HEADERS=true` is set for the same reason Compose sets it: the API is
only reachable through the platform edge, so without it every click records the
edge's address and the whole `clicks` table shares one `ip_hash`.

Commands in the sections below that begin `docker compose exec api` become
`railway run --service api` from a linked checkout, or the equivalent in
Railway's dashboard shell. Volumes, backups and the `pg_dump` advice still apply;
Railway's Postgres has its own backup settings, and a named volume is still
persistence rather than a backup.

## Offer and preflight

Use `/docs` with the admin `X-API-Key` to create an offer, or `offer-add` inside
the API container. Set `network=clickbank`, the verified HopLink and payout, and
the supported factual brief. Record the returned offer ID. For the first test,
use a new production database so old sandbox IDs and simulated metrics cannot
be mistaken for real campaign objects.

```bash
docker compose exec api python -m adgenie.cli preflight --platform meta --offer OFFER_ID
```

Replace `OFFER_ID` with the returned integer. Exit 1 means a check failed. The
command does not create tables, clicks, ads, images or conversions. It performs
database reads, public route checks, Meta account/Page/pixel reads and a
destination audit. A successful report says `ready_for_paused_launch`; it always
says `ready_to_spend=false`. Account reads do not prove write permissions,
assignment, billing readiness or ad-policy approval. Live API compatibility and
real media generation still require the next step.

If the Meta account uses CAD, stop here: the current engine must not compare
CAD spend to USD ClickBank commissions. Decide whether to implement currency
normalization or use an existing suitable USD account before proceeding.

## Tracking proof

Configure ClickBank INS version 8 to POST to:

```text
https://YOUR_TRACKING_HOST/postback/clickbank
```

Use ClickBank's **Test URL** action. A successful test returns 200 and
`test=true, recorded=false`; no test earnings enter the optimizer. The receiver
accepts the documented encrypted JSON envelope and URL-encoded form envelopes.
Plaintext, malformed ciphertext and unsupported INS versions are rejected.
This endpoint uses ClickBank encryption, not the generic postback secret.

After creating a paused campaign, open its creative's `final_url`. Confirm:

- The response is a 302 to the correct HopLink with one lowercase `tid` value.
- The existing HopLink parameters remain intact.
- The ClickBank checkout affiliate value decodes to your nickname and seller.
- A real affiliate transaction eventually matches the recorded click and the
  actual commission in ClickBank's report.

ClickBank's Test URL proves delivery/decryption, not affiliate sales attribution.
ClickBank documents that test-transaction notifications go to the seller, so do
not assume a seller test purchase will generate an affiliate sale notification.
Synthetic encrypted fixtures verify the local accounting path separately.

## Paused campaign, then controlled activation

Only after approval, set `DRY_RUN=false` and recreate the API container so the
setting takes effect. This permits paid image generation and live account writes,
even while the resulting campaign remains paused.

```bash
docker compose up -d api
docker compose exec api python -m adgenie.cli launch --offer OFFER_ID --platform meta --budget APPROVED_DAILY_USD --angles 1 --per-angle 1 --with-media
```

Use actual numeric values for both placeholders. Production refuses
`--start-active`. Inspect `errors`, blocked creatives and all external IDs, then
confirm the actual campaign, ad set and ad in Meta Ads Manager. Do not equate a
nonzero local campaign ID with a successful ad. Fix first-contact API failures
before spending; a partial failed launch may leave paused objects in the account.

Dry-run objects cannot be promoted into real objects by changing a setting.
Create a new paused live campaign after disabling dry run. With dry run enabled,
the status endpoint reports `applied=false` and leaves local status unchanged.

After approval of the creative, tracking proof and budget, activate through
`POST /api/campaigns/CAMPAIGN_ID/status?active=true` with the admin API key.
The corresponding `active=false` call pauses it. Platform-side budget and
spending limits remain necessary: the app's cap is an allocation check, not a
guaranteed ceiling on actual provider billing or concurrent external changes.

## First operating loop

Keep optimization manual and proposal-only during the first trial:

```bash
docker compose exec api python -m adgenie.cli sync --days 7
docker compose exec api python -m adgenie.cli report
docker compose exec api python -m adgenie.cli optimize
```

The default report and sync use completed days; today's traffic may not appear
until tomorrow. Inspect live spend in Meta Ads Manager during the initial test.
No scheduler is installed by this change. These commands can be scheduled later
after the tracking and live adapter have been verified.

After verifying real network sales, the previously missing CLI command is:

```bash
docker compose exec api python -m adgenie.cli push-conversions --hours 72
```

It honors `DRY_RUN`. With dry run off it sends live conversion events to the ad
platform; configure and verify the pixel/conversion destination before invoking.
Keep automatic budget changes off until network revenue is reconciled.

## Accounting behavior and remaining limits

- Store actual `totalAccountAmount` (affiliate earnings), not order gross value.
- A ledger per nickname and full receipt protects concurrent deliveries and
  retains distinct rebill suffixes. Delivery attempt counts do not create sales.
- Partial refunds subtract commission; full refunds zero the original receipt.
  A refund received before its sale waits without crediting phantom revenue.
- Cancellation and test events do not create sales. Unmatched sales are retained
  without crediting a creative. Customer plaintext is not retained in the ledger.
- The displayed profit is affiliate earnings minus ad spend. It excludes hosting,
  media/LLM costs, taxes and additional network fees beyond reversed commission.
- Reversed receipts are not re-uploaded as new purchases. The existing platform
  conversion-upload path has no refund-adjustment implementation; reconcile those
  separately before relying on platform-reported revenue.
- Generic postbacks remain available for other networks at `/postback`. Use the
  dedicated INS endpoint for ClickBank to preserve receipt reconciliation.
- Existing attribution windows remain in force. Long-delayed rebills without a
  valid click inside that window may remain unattributed; do not use this first
  test to claim subscription lifetime value has been proven.

Protocol references: [ClickBank INS](https://support.clickbank.com/en/articles/10535147-instant-notification-service-ins),
[HopLinks](https://support.clickbank.com/en/articles/10535278-hoplinks-guide), and
[receipt/rebill reporting](https://support.clickbank.com/en/articles/10535320-transaction-reporting-on-clickbank).
Deployment references: [Compose health dependencies](https://docs.docker.com/compose/how-tos/startup-order/)
and [Caddy environment variables](https://caddyserver.com/docs/caddyfile/concepts#environment-variables).
