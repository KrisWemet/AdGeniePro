# AdGenie Pro

Writes, launches and optimizes ads on Meta and Google for affiliate offers.

Give it an offer. It generates the ad copy, checks it against both platforms'
policies before anything goes live, builds a structured test, measures the
result against network-reported revenue rather than platform pixels, and then
manages budget across the winners and losers with decisions you can audit.

```bash
pip install -r requirements.txt
python -m adgenie.cli demo --days 21
```

The demo runs the entire pipeline against a built-in auction simulator,
including realistic conversion reporting delay. No credentials, no spend.

---

## Why this is built the way it is

Most "AI ad bot" tooling fails in one of four specific ways. Each of those
failure modes drove a design decision here.

**It gets the ad account banned.** Affiliate marketing dies of policy
enforcement, not bad copy. So every creative passes a policy engine before it
can reach an ad account, and a blocking finding stops the launch instead of
producing a warning nobody reads. The rules cover Meta's personal-attributes
policy, unrealistic-outcome and health claims, prohibited categories, Google's
editorial standards, and the FTC's disclosure requirement for affiliate links.
See `adgenie/core/compliance.py`.

**It optimizes toward the wrong number.** Affiliate conversions fire on the
advertiser's domain, where neither the Meta pixel nor a Google tag can see
them. A platform's reported conversion count is therefore not a safe basis for
spending money. This platform builds its own measurement path: every ad click
passes through a tracking redirect that assigns an opaque click id, that id
travels to the network as its sub-id, and the network's postback carries it
back. Revenue and ROAS come from that side. See `adgenie/core/tracking.py`.

**It acts on noise.** Kill a good ad after three unlucky days and you burn the
winner; keep a bad one because "it might turn around" and you burn the budget.
Both are the same mistake: acting on a point estimate with no error bar. Every
decision here is gated on a credible interval from a Beta-Binomial posterior,
with an empirical-Bayes prior pooled across comparable ads so a creative with
ten clicks is not judged as though it had a thousand. See
`adgenie/core/stats.py` and `adgenie/core/optimizer.py`.

**You cannot tell what it did or why.** Every decision records the rule that
fired, the metrics it saw, and its confidence, before anything is applied.
Every mutation sent to an ad account is written to an append-only audit log.

---

## What a cycle looks like

```
offer ──► copy generation ──► policy review ──► launch (paused)
                                   │
                                   └── blocked ──► held for human review

                    ┌──────────────────────────────────────┐
                    │                                      │
              sync delivery                         apply decisions
                    │                                      │
                    ▼                                      │
          join network conversions ──► evaluate ──► record decisions
```

Copy that fails review is sent back to the generator with the specific findings
attached, up to a bounded number of attempts. Copy that passes is hard-trimmed
to the platform's character limits before the API call, because an asset one
character over the limit is a rejected ad.

---

## Getting started

### 1. Configure

```bash
cp .env.example .env
```

Everything is optional. Any integration you leave blank falls back to the
sandbox, and the logs say so explicitly rather than silently doing nothing.

| Setting | Effect when unset |
|---|---|
| `ANTHROPIC_API_KEY` | Copy comes from the built-in angle templates instead of Claude |
| `META_*` | Meta calls go to the simulator; competitor research is unavailable |
| `KIE_API_KEY` | Images are simulated placeholders at the correct dimensions |
| `GOOGLE_*` | Google calls go to the simulator |
| `DRY_RUN` | Defaults to `true`: nothing is sent to a live ad account |
| `API_KEY` | The `/api` routes are unauthenticated; bind to localhost |

### 2. Add an offer

```bash
python -m adgenie.cli offer-add \
  --name "CalmLeaf Sleep Support" \
  --url "https://your-affiliate-link.example/lp" \
  --payout 42 --network clickbank --vertical supplements \
  --benefit "wind down without next-morning grogginess" \
  --proof "Third-party tested in a US facility" \
  --regulated
```

`--payout` is what the network pays per conversion. The optimizer discounts it
by the expected reversal rate, because scaling a campaign on revenue that later
refunds is a way to lose money slowly.

### 3. Launch a test

```bash
python -m adgenie.cli launch --offer 1 --platform google --budget 45 \
  --angles 3 --keyword "natural sleep aid"
```

One ad group per angle. An angle is the *argument* an ad makes, not its
wording. Rotating wording produces ads that all fail together; rotating the
angle is what actually finds a winner. Ten angles ship in
`adgenie/core/angles.py`.

Campaigns start paused. Turning on spend is a separate, deliberate call.

### 4. See what the market is already running

```bash
python -m adgenie.cli research --term "sleep supplement" --country GB --country DE
```

Read the section below on what this can and cannot tell you before relying on
it. Add `--research` to a launch to feed the patterns straight into the
copywriter.

### 5. Generate the imagery

```bash
python -m adgenie.cli launch --offer 1 --platform meta --budget 45 --with-media
python -m adgenie.cli media --creative 3 --kind video
```

One asset per placement, at the size the placement actually serves. Every
prompt is screened against Meta's imagery rules *before* generation, because a
rejected prompt costs nothing and a generated one costs money and a minute.
Google search ads carry no imagery and are skipped.

### 6. Measure and optimize

```bash
python -m adgenie.cli sync              # pull delivery from the platforms
python -m adgenie.cli optimize          # propose changes, change nothing
python -m adgenie.cli optimize --apply  # act (requires DRY_RUN=false)
python -m adgenie.cli report            # performance by creative
```

### 7. Or run the server

```bash
uvicorn adgenie.main:app --reload
```

Dashboard at `http://localhost:8000`, API docs at `/docs`.

---

## Competitor research: what it can and cannot tell you

Read this before trusting the output.

**The Ad Library has no performance data for commercial ads.** No click-through
rate, no conversions, no ROAS, no spend. Political and issue ads report spend
and impressions as wide ranges; EU commercial ads report a single reach figure;
everything else reports nothing.

**Outside the EU and UK it carries no commercial ads at all.** Ordinary product
ads are archived under the Digital Services Act, which covers the EU and UK
only. A US search returns political and issue ads plus the US special
categories (housing, employment, financial products). An empty US result means
"not carried", not "no competition" — so `/api/research/coverage` and the CLI
both say so out loud rather than handing back an empty list.

**What it can tell you is what is still running, and for how long.** That is
the inference experienced buyers make from it, and this platform makes it
explicit:

| Signal | What it means |
|---|---|
| Days running | The main one. Nobody funds a losing ad for three months. |
| Still live | A stopped ad is a finished experiment. |
| Variant count | Fifteen versions of one idea means the advertiser is scaling it. |
| EU reach | Actual delivery volume, where the DSA requires it. |
| Ads that stopped fast | The only negative signal available (`/api/research/retired`). |

The retirement signal needs its own pass: a scan restricted to live ads can
never *see* an ad stop, it just stops being returned. Run
`adgenie research-sweep` (or `POST /api/research/sweep-retirements`) on a
schedule to re-scan stored searches including stopped ads.

Each ad gets a **staying-power** score weighting longevity on a log scale — the
step from 7 days to 30 says far more than 90 to 120 — plus whether it is live
and how many variants exist. Angles are weighted by that score rather than
counted, so one advertiser flooding the archive with new ads cannot outvote a
durable competitor. The result carries a `confidence` of none, low, moderate or
high, because a handful of ads from two advertisers is an anecdote.

**Competitor copy is never reused.** What reaches the copywriter is *pattern*
guidance — which arguments survive, how long-running copy is structured, the
register and CTA distribution — never wording. Reproducing a competitor's copy
risks their trademark, and this platform's own policy engine would block it.

Scans are stored, so repeated scans build a history. That history is what makes
the negative signal possible: an ad you saw last month that has since vanished
was probably not working.

## Media generation

Meta ads need imagery; a text-only Meta ad barely delivers. Generation runs
through [kie.ai](https://kie.ai), which fronts Nano Banana, Flux, Veo, Kling and
others behind one asynchronous job API.

The sequence is deliberate:

1. **Plan** a prompt from the offer and the creative's angle, so the image
   carries the same argument as the copy. A mismatch between them is a common
   reason a well-written ad still fails.
2. **Screen** it against Meta's imagery rules — no before-and-after, no
   idealised or negative body framing, nothing mimicking a UI element, no
   third-party marks or likenesses, no graphic medical or wealth-bait imagery.
   A rejected prompt is never submitted.
3. **Generate**, polling the task. A timeout says explicitly not to resubmit,
   because a resubmitted task is charged twice.
4. **Download immediately.** Provider URLs expire in about a day, so the local
   copy is the source of truth and files are content-addressed.

One asset per placement, at the size the placement serves: Meta feed 4:5,
square 1:1, story 9:16, Google Demand Gen 1.91:1, 1:1 and 4:5. Text-only
formats generate nothing.

To attach imagery to a *live* ad, set `MEDIA_PUBLIC_BASE_URL` — the platforms
fetch the image over HTTP, they do not read your disk. Without it the files are
still generated and stored, but nothing is attached to the ad and the server
says so rather than handing Meta a filesystem path.

Generation is also suppressed under `DRY_RUN`, which falls back to the sandbox.
A mode whose purpose is to have no side effects should not have billing as its
one exception.

---

## Wiring up tracking

This is the part that makes revenue attributable, and the part most setups get
wrong.

Each creative's final URL points at **this** platform, not at the offer:

```
https://track.yourdomain.com/r?s=o7-a56-pm&pc={{campaign.id}}&pa={{ad.id}}
```

The `/r` endpoint records the click, then 302s to the advertiser with the click
id attached as a sub-id. Configure the network to send that sub-id back:

```
https://track.yourdomain.com/postback
  ?transaction_id={order_id}
  &click_id={subid}
  &revenue={commission}
  &status=approved
  &secret=YOUR_POSTBACK_SECRET
```

Both GET and POST are accepted, since most networks only support a GET pixel.
The endpoint is authenticated with a shared secret: it writes the revenue
numbers the optimizer spends against, so leaving it open is a way to make the
system scale a losing campaign. Until `POSTBACK_SECRET` is changed from the
example value the endpoint rejects everything with a 503 and the server warns
at startup, so an unconfigured deployment cannot be fed forged revenue. Duplicate postbacks are idempotent and refunds
update the original conversion in place.

`push-conversions` sends network-confirmed sales back to Meta's Conversions API
and Google's offline conversion upload. Without that step, Smart Bidding and
Advantage+ are optimizing toward landing-page views they can see rather than
the sales they cannot.

---

## The optimizer's rules

Evaluated in order; the first match wins.

| Rule | Fires when | Action |
|---|---|---|
| `compliance_block` | Creative has a blocking policy finding | Pause |
| `cooldown` | Acted on this entity within the cooldown window | Hold |
| `learning` | Not enough spend or clicks to say anything | Hold |
| `awaiting_conversions` | Too little of the conversion window has elapsed | Hold |
| `zero_conversion_kill` | Lifetime spend past N× payout, no conversions, and breakeven is implausible | Pause |
| `unprofitable_kill` | ROAS and its upper credible bound both below breakeven | Pause |
| `scale_winner` | ROAS above target **and** the lower bound clears breakeven | Raise budget |
| `throttle_marginal` | Profitable but the upper bound cannot reach target | Cut budget |
| `decayed_winner_throttle` | Bad window, but a profitable lifetime record | Cut budget |
| `frequency_fatigue` | Frequency above the ceiling | Generate new creative |
| `ctr_decay` | CTR decayed against the ad's own opening week | Generate new creative |

Levers are matched to the level that has them. Neither platform funds an
individual ad, so at creative level a winner is held (its gain is realised by
funding the parent ad set and by the budget split across its siblings) and the
usable actions are pause, resume and creative refresh. Budget changes apply to
ad sets and campaigns.

### Conversion lag

A click does not convert instantly. A third convert in-session, most within a
day, and a long tail runs for weeks on trials and networks that confirm late.
That makes recent data **right-censored**: the spend has happened, some of the
conversions it bought have not been reported yet.

Comparing the two as if both were complete is the most expensive mistake an ad
optimizer can make, because incomplete looks exactly like failure. A four-day-
old ad with a real 5% conversion rate shows zero conversions, and a naive kill
rule retires it with 98% "confidence" the week before it starts paying.

So each day of clicks is weighted by how much of its conversion window has
elapsed, and the posterior counts that **effective exposure** instead of raw
clicks. The curve is fitted per offer from your own history and shrunk toward a
sensible default while that history is thin, so a trial offer that confirms
after ten days is judged on a slower clock than an impulse purchase.

Two asymmetries fall out of this, both deliberate:

- **Killing is blocked early** (below 15% maturity nothing is judged at all),
  because a wrong kill destroys a winner.
- **Scaling waits much longer** (60% maturity), because a wrong scale spends
  real money where a slow scale only forgoes a little upside. Projected ROAS is
  reported as evidence and never funded against.

The rate is estimated from matured clicks only, then applied to *every* click
already paid for — scaling it by matured clicks instead would quietly write off
the outstanding ones, which is the same censoring error in a different place.

Three further properties are deliberate:

- **The bar for scaling is higher than the bar for pausing.** Pausing a good ad
  costs opportunity; scaling a bad one costs cash.
- **Kill rules read lifetime data, not the rolling window.** An ad that has
  burned money for three weeks should not get a clean slate every Monday
  because the window moved on. Conversely a creative with a profitable lifetime
  record that has one bad week has *decayed*, not failed, so it is throttled
  rather than retired.
- **Budget is allocated by Thompson sampling with an exploration floor.**
  Ranking by observed conversion rate hands the budget to whichever ad got
  lucky first. Sampling from each posterior keeps a promising-but-unproven
  creative funded long enough to actually be measured.
- **The shrinkage prior is leave-one-out.** Each creative is judged against a
  prior built from its peers, never from itself, so an ad group holding a
  single creative does not treat that creative's own rate as extra evidence
  for it.

### Guard rails

Three independent limits stand between the optimizer and your money:

1. `DRY_RUN` blocks every mutation globally. While it is on, the API and CLI
   both refuse to apply rather than rewriting stored budgets to match changes
   that were never sent.
2. Budget changes above `AUTO_APPLY_BUDGET_CEILING_USD` are held for approval
   (`POST /api/optimizer/actions/{id}/approve`).
3. `GLOBAL_DAILY_BUDGET_CAP_USD` caps total committed daily spend, so no
   sequence of individually-reasonable increases can run away. A campaign
   counts the larger of its own budget and the sum of its ad sets, so neither
   budgeting style escapes the cap.

### Access control

The `/api` routes launch campaigns and move budgets, so set `API_KEY` for any
deployment reachable beyond localhost. Every `/api` route then requires it in
an `X-API-Key` header, and the server logs a warning at startup when it is
unset. The two public routes stay open by necessity: `/r` takes anonymous ad
clicks, and `/postback` authenticates with its own shared secret because
affiliate networks cannot send custom headers. Narrow `CORS_ORIGINS` from `*`
whenever the dashboard is served from a known origin.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/offers` | Register an affiliate offer |
| `POST /api/copy/generate` | Generate reviewed ad copy variants |
| `POST /api/copy/review` | Check existing copy against ad policy |
| `POST /api/campaigns/launch` | Generate, review and build a campaign |
| `POST /api/campaigns/{id}/status` | Turn a campaign on or off |
| `GET /api/performance` | Metrics with credible intervals |
| `POST /api/optimizer/sync` | Pull delivery data |
| `POST /api/optimizer/run` | Evaluate and decide |
| `GET /api/optimizer/actions` | Review proposals |
| `POST /api/optimizer/actions/{id}/approve` | Approve a held proposal |
| `GET /api/optimizer/rebalance/{ad_group_id}` | Advisory split across creatives |
| `GET /api/optimizer/rebalance-campaign/{id}` | Applicable split across ad groups |
| `POST /api/optimizer/push-conversions` | Send sales back to the platforms |
| `GET /api/research/coverage` | What the Ad Library will actually return |
| `POST /api/research/scan` | Scan the archive and summarise what is running |
| `POST /api/research/sweep-retirements` | Re-scan including stopped ads |
| `GET /api/research/brief` | Rebuild a brief from stored scans, no API call |
| `GET /api/research/retired` | Competitor ads that stopped quickly |
| `GET /api/media/placements` | Placement sizes per platform |
| `POST /api/media/preview-prompt` | Build and screen a prompt, generating nothing |
| `POST /api/media/generate/{creative_id}` | Generate the imagery a creative needs |
| `GET /api/media/assets` | Generated assets |
| `GET /api/audit` | Every mutation ever sent to an ad account |
| `GET /r` | Click redirect (public) |
| `GET,POST /postback` | Network conversion postback (public, authenticated) |

---

## Layout

```
adgenie/
  config.py          settings, with graceful degradation everywhere
  models.py          domain model; money is integer micros throughout
  money.py           micro conversions
  core/
    stats.py         Beta-Binomial helpers, no numpy or scipy
    lag.py           conversion delay curves and maturity weighting
    compliance.py    Meta and Google policy engine
    angles.py        the angle library
    copywriter.py    Claude generation + template fallback + repair loop
    tracking.py      click tracking and conversion attribution
    metrics.py       joins platform delivery with network revenue
    optimizer.py     the decision rules
    launcher.py      offer to structured test
    orchestrator.py  the control loop
  platforms/
    base.py          the adapter interface
    specs.py         hard format limits per platform
    meta.py          Meta Marketing API
    google.py        Google Ads API
    sandbox.py       auction simulator
  media/
    specs.py         placement sizes
    prompts.py       prompt building and pre-generation screening
    kie.py           kie.ai async job client
    store.py         download before the URL expires
    sandbox.py       real PNGs at the right size, no key needed
    studio.py        plan, screen, generate, persist
  research/
    ad_library.py    Meta Ad Library client
    signals.py       staying power, angle inference, market brief
    service.py       persistence and history
  api/               FastAPI routers
  static/            dashboard
  cli.py             command line
  demo.py            end-to-end simulation
tests/               444 tests
legacy/              the original prototype scripts, kept for reference
```

## Tests

```bash
python -m pytest
```

The suite covers the statistics against known closed forms, every policy rule,
copy generation for all ten angles on both platforms, sub-id round trips and
attribution edge cases, both live adapters against mocked transports, and the
whole loop end to end against the simulator.

`tests/test_regressions.py` is kept separate: each test there documents a
specific defect found in review, so a fix that silently reverts fails loudly.

## Limits worth knowing

- The Ad Library carries no commercial ads outside the EU and UK, and no
  performance data anywhere. Longevity is a proxy for profitability, not a
  measurement of it.
- Ad Library creative images sit behind a rendered snapshot page rather than a
  media endpoint. This platform records the URL and does not fetch it.
- Generated video is short-form only, and the models are stronger at product
  and lifestyle imagery than at anything needing legible on-screen text.
- Google holds one budget per campaign. An ad-group scale decision therefore
  moves the parent campaign's budget by the delta, and per-ad-group
  reallocation is unavailable there.
- Neither platform funds an individual ad, so creative-level allocation is
  advisory. It tells you which creatives to keep running, not how to fund them.
- The compliance engine is an automated pre-screen and a forcing function for
  better copy. It is not legal advice and does not replace each platform's own
  review.
- Revenue-share offers are modelled with an average order value, falling back
  to observed revenue per conversion. Offers with a wide order-value
  distribution will have wider real uncertainty than the ROAS interval shows,
  and an offer with neither a payout nor any revenue yet is reported as
  unjudgeable rather than guessed at.
