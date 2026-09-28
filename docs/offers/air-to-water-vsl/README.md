# Air-to-water VSL: ad brief

The ads in `ads.json` drive clicks to the Richard Wilson VSL, a DIY guide to
condensing drinking water out of humid air. `offer.json` is a ready-made
`POST /offers` body. Replace `destination_url` with your hoplink before using it.

All six Meta ads and the Google RSA pass `review_texts` with no findings, and
the offer's `banned_claims` are checked in that run.

## What the ads borrow from the VSL, and what they leave out

An ad has one job here: make someone curious enough to watch the video. The
VSL's strongest material for that is also the material least likely to be
rejected:

| Use | Why it works |
| --- | --- |
| A named farmer, a dry well, the Imperial Valley | A specific story beats a product claim |
| The AC-drip mechanism | Everyone has seen it, so the idea feels plausible without a promise |
| Lake Mead lows, boil-water notices after storms | Real and checkable, so fear without fabrication |
| "Built with a friend who fixes ACs" | Down-to-earth credibility, and it's DIY |

The following VSL claims are kept **out** of the ads, and are listed in
`banned_claims` so the copywriter's regenerations and bred variants can't bring
them back:

- **"Unlimited" water, "for pennies", "60 gallons a day", "no water bill".**
  These are unrealistic-outcome claims. An air-to-water unit's output depends on
  humidity and temperature, and it runs on electricity. Both Meta and Google
  reject copy like this, and FTC Section 5 applies to affiliates as well as
  vendors.
- **"57,347 families" and "cleaner than the treatment plant".** These are
  specific claims you can't substantiate.
- **Suppression framing** ("they don't want you to know", "cease and desist",
  "water corporations", "government", "FEMA"). This is the pattern the
  `MIRACLE_CURE` rule targets, and Meta's deceptive-practices policy flags it
  quickly.
- **NASA, military and university endorsements.** These imply affiliations the
  product doesn't have.

The objection ad goes the other way on purpose: it states the catch (humid air
and electricity). Being upfront like that tends to earn clicks from skeptical
buyers and lowers refunds. The offer defaults to a 10% reversal rate, and
over-promising pushes that number up.

## Angles

The keys match `core/angles.py`, so rotation and the per-angle kill tests read
them directly:

| Angle | Hook |
| --- | --- |
| `problem_solution` | The farmer's dry well |
| `mechanism` | The water dripping from your AC |
| `cost_of_inaction` | "How many days of water do you have stored?" |
| `identity` | Preppers and homesteaders: stored water runs out |
| `objection` | "Sounds like a gimmick. It's condensation." |
| `how_to` | Storms, boil-water notices, empty shelves |

Suggested geos inside `US`: TX, AZ, NV, CA, NM, FL and the Gulf Coast.
Condensation needs humid air, so the storm and hurricane angles fit
Gulf/Southeast buyers better than desert ones.

## Risk the ads can't fix

Meta and Google review the landing page as well as the ad. The VSL itself makes
the suppression and unlimited-water claims above, so a clean ad can still be
disapproved for its destination, and repeated destination disapprovals count
against the ad account. Two ways to lower that risk:

- Put a pre-lander (advertorial or quiz) between the ad and the hoplink. Tell
  the farmer story in compliant language there, and use it as the page the
  platforms review.
- Run `landing --sweep` against the destination before scaling spend.
