# First live test runbook

Goal: prove one real affiliate offer can move through AdGenie into a real Meta
ad account, remain **paused**, and have working click/postback plumbing before
any paid delivery is enabled.

This is intentionally narrower than "production ready". Friday is successful if
we can create a real paused test cleanly and know that attribution will work when
we enable a small budget.

## Gate 0 — automated tests

```bash
python -m pytest tests/
python -m adgenie.cli demo --days 21
```

Both must pass before touching a live account.

## Gate 1 — deployment and secrets

Deploy the repository using `Dockerfile` or `Procfile`, then set at least:

```text
ENVIRONMENT=staging
PUBLIC_BASE_URL=https://<public-host>
API_KEY=<random operator key>
SECRET_KEY=<random secret>
POSTBACK_SECRET=<different random secret>
DRY_RUN=true
GLOBAL_DAILY_BUDGET_CAP_USD=25

META_ACCESS_TOKEN=...
META_AD_ACCOUNT_ID=...
META_PAGE_ID=...
META_PIXEL_ID=...       # may be added after the paused-object smoke test
```

Do not put any of those values in Git.

A single-instance SQLite database is sufficient for the smoke test. Use a
persistent volume or a managed database before relying on it for real traffic.

## Gate 2 — read-only first contact

From the deployed environment, while `DRY_RUN=true`:

```bash
python -m adgenie.preflight --platform meta --live
```

Required result:

```text
READY FOR LIVE TEST: YES
```

The live Meta check only reads the configured ad account. It does not create or
change campaigns. The public check calls `/api/health` with the configured
operator key.

If this fails, fix the credential, account, deployment or tracking issue it
names before moving on.

## Gate 3 — choose one low-risk offer

For the first test prefer an offer with:

- a normal consumer product or service, not health cures, weight loss, wealth,
  gambling, adult content, weapons, credit repair or another restricted area;
- a landing page that is reachable over HTTPS and does not cloak;
- clear product facts, benefits and proof that AdGenie can use without inventing
  claims;
- an affiliate link/sub-id scheme that can carry AdGenie's click id;
- a payout large enough that a $10–$25/day learning budget is meaningful.

Register it:

```bash
python -m adgenie.cli offer-add \
  --name "<offer>" \
  --url "<affiliate destination>" \
  --payout <commission> \
  --network clickbank \
  --vertical <vertical> \
  --description "<facts only>" \
  --benefit "<supported benefit>" \
  --proof "<supported proof>"
```

Then audit the destination:

```bash
python -m adgenie.cli landing --offer 1
```

A blocking result stops the test.

## Gate 4 — full dry-run campaign

Keep `DRY_RUN=true` and run the exact campaign shape we intend to create:

```bash
python -m adgenie.cli launch \
  --offer 1 \
  --platform meta \
  --budget 10 \
  --angles 3 \
  --per-angle 1 \
  --with-media
```

Do **not** pass `--start-active`.

Inspect the output for compliance failures, landing-page failures, missing Page
or Pixel settings, media errors and malformed tracking URLs.

## Gate 5 — real objects, still paused

Only after Gates 0–4 pass, set:

```text
DRY_RUN=false
```

Then immediately rerun the preflight:

```bash
python -m adgenie.preflight --platform meta --live
```

It will warn that mutations are now enabled but remains read-only itself.

Create the real test with the same launch command used in Gate 4. Campaigns,
ad sets and ads are paused by default. Do not pass `--start-active`.

Success means:

- Meta accepted the campaign fields;
- Meta accepted ad-set targeting and budget fields;
- Meta accepted the creative and Page association;
- generated media uploaded and attached correctly;
- all external ids were stored in AdGenie;
- the campaign, ad sets and ads are visibly **PAUSED** in Meta Ads Manager.

If Meta rejects a field, treat the live error as the specification, fix the
adapter, add a regression test, and repeat while the objects remain paused.

## Gate 6 — attribution proof

Before enabling paid delivery, verify the public tracking route and postback
with a test click/conversion. The ad final URL should point at AdGenie `/r`, not
directly at the advertiser.

The affiliate network must return AdGenie's click id in its postback. Configure
that network's sub-id/TID and postback macros according to its current
instructions, then confirm one synthetic/test conversion appears against the
right offer/creative.

Do not send fabricated revenue into a production optimizer. Use a network test
facility if available, or a clearly isolated staging transaction/database.

## Gate 7 — tiny live spend

Only after attribution is proven:

1. keep `GLOBAL_DAILY_BUDGET_CAP_USD` at a deliberately small value;
2. enable only the one approved campaign;
3. start at roughly $10/day, not the global cap;
4. watch the first delivery, tracking redirects and network postbacks manually;
5. leave autonomous budget application off until real metrics have been synced
   and inspected.

The first objective is correctness, not profit. Once one complete conversion can
be traced from ad click to network revenue and back to the correct creative, the
system has crossed the important boundary from simulation to a real operating
loop.
