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

The demo runs the entire pipeline against a built-in auction simulator. No
credentials, no spend.

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
| `META_*` | Meta calls go to the simulator |
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

### 4. Measure and optimize

```bash
python -m adgenie.cli sync              # pull delivery from the platforms
python -m adgenie.cli optimize          # propose changes, change nothing
python -m adgenie.cli optimize --apply  # act (requires DRY_RUN=false)
python -m adgenie.cli report            # performance by creative
```

### 5. Or run the server

```bash
uvicorn adgenie.main:app --reload
```

Dashboard at `http://localhost:8000`, API docs at `/docs`.

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
system scale a losing campaign. Duplicate postbacks are idempotent and refunds
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
| `zero_conversion_kill` | Lifetime spend past N× payout, no conversions, and breakeven is implausible | Pause |
| `unprofitable_kill` | ROAS and its upper credible bound both below breakeven | Pause |
| `scale_winner` | ROAS above target **and** the lower bound clears breakeven | Raise budget |
| `throttle_marginal` | Profitable but the upper bound cannot reach target | Cut budget |
| `frequency_fatigue` | Frequency above the ceiling | Generate new creative |
| `ctr_decay` | CTR decayed against the ad's own opening week | Generate new creative |

Three properties are deliberate:

- **The bar for scaling is higher than the bar for pausing.** Pausing a good ad
  costs opportunity; scaling a bad one costs cash.
- **The zero-conversion kill reads lifetime data, not the rolling window.** An
  ad that has burned money for three weeks should not get a clean slate every
  Monday because the window moved on.
- **Budget is allocated by Thompson sampling with an exploration floor.**
  Ranking by observed conversion rate hands the budget to whichever ad got
  lucky first. Sampling from each posterior keeps a promising-but-unproven
  creative funded long enough to actually be measured.

### Guard rails

Three independent limits stand between the optimizer and your money:

1. `DRY_RUN` blocks every mutation globally.
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
  api/               FastAPI routers
  static/            dashboard
  cli.py             command line
  demo.py            end-to-end simulation
tests/               277 tests
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

- Image and video generation is not included. Creatives carry an
  `image_prompt`; supplying the asset is still manual.
- Google holds one budget per campaign. An ad-group scale decision therefore
  moves the parent campaign's budget by the delta, and per-ad-group
  reallocation is unavailable there.
- Neither platform funds an individual ad, so creative-level allocation is
  advisory. It tells you which creatives to keep running, not how to fund them.
- The compliance engine is an automated pre-screen and a forcing function for
  better copy. It is not legal advice and does not replace each platform's own
  review.
- Revenue-share offers are modelled with an average order value. Offers with a
  wide order-value distribution will have wider real uncertainty than the ROAS
  interval shows.
