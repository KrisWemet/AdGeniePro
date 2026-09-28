# Systeme.io test funnel

## Live account build

- Funnel: https://systeme.io/dashboard/funnels/7606805
- Capture page: https://ouimettest.systeme.io/20b1992c
- Thank-you page: https://ouimettest.systeme.io/ce2e9d19
- Campaign: https://systeme.io/dashboard/campaigns/1194131
- Seven email steps for days 0, 1, 2, 3, 5, 7 and 9
- Sender name: Water Ready Tips
- Form rule: add the Water Freedom lead tag and subscribe the contact to the campaign

The seven emails remain inactive until one real opt-in, redirect, unsubscribe and sender test passes. No paid ad should be enabled while this check is open.

## Render settings

Set:

```env
SYSTEME_CAPTURE_URL=https://ouimettest.systeme.io/20b1992c
```

Ads keep using `PUBLIC_BASE_URL/offer/{offer_id}`. AdGenie adds the opaque `s` token and redirects to Systeme. Do not put an email address or API key in that URL.

## Opt-in webhook

Configure the Systeme Opt-In webhook to POST to:

```text
PUBLIC_BASE_URL/api/systeme/optin?offer_id=1
```

Send `POSTBACK_SECRET` as `X-Webhook-Secret`. If Systeme cannot set the header, add the same value as a `secret` query parameter. The adapter stores the normalized email hash and attribution fields, not the raw body. Retries do not make a second lead.

ClickBank INS remains the source of truth for sales, refunds and rebills.

## Before ads point here

Checked on 2026-09-28 against the live pages, read-only:

- The ad link `PUBLIC_BASE_URL/offer/{offer_id}` redirects here with the ad's
  `s` token and Meta's `fbclid` intact, so each lead can be credited to the ad.
- This is the page Meta reviews, because it is where the ad lands. AdGenie's
  audit blocks it for having no privacy link and warns that it has no terms or
  contact link. Do not link AdGenie's own `/privacy`: it says the offer page
  collects no email addresses. The list needs its own policy.
- The thank-you button links straight to a HopLink, and not the one on the
  offer. Point it, and every email link, at `PUBLIC_BASE_URL/r?s=` plus the
  offer's token instead (`o1` for offer 1; ids are base 36, so offer 10 is
  `oa`). That records the click and sends the visitor to the offer's HopLink
  with a `tid`, which is how a ClickBank sale is matched. A sale through a bare
  HopLink carries no `tid` and is credited to nothing.
- Even through that link, the sale is credited to the offer, not to the ad
  that earned the lead: see the known gap in `AGENTS.md`.
