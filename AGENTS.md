# Working on AdGenie Pro

An affiliate ad platform: it writes ads, launches them on Meta and Google,
measures them against affiliate-network revenue, and decides what to scale,
throttle, kill or rotate.

This file is for coding agents. `README.md` explains what the system does and
why; this explains what will bite you while changing it.

## Getting running

```bash
pip install -r requirements.txt
python3 -m pytest tests/          # 663 tests, ~40s, no credentials needed
python3 -m adgenie.cli demo --days 21   # full pipeline against the simulator
```

Everything runs with no API keys. Missing credentials degrade to a simulator
rather than failing: `platforms/sandbox.py` is an auction model, `media/
sandbox.py` produces correctly-sized PNGs, and the copywriter falls back to a
deterministic template generator. If you needed a key to run the tests, that is
a bug.

## Branches, and more than one agent

`main` is the codebase. Branch from it, and open a pull request rather than
pushing to it — more than one agent works here, and a shared branch means
someone's commits get clobbered by someone else's force-push.

Two things in the repository are older than the platform and are deliberately
kept:

- `legacy/` holds the prototype agent scripts this replaced. Nothing imports
  them. Do not extend them; do not delete them either without asking.
- `CNAME`, `index.html`, `privacy-policy.html`, `terms.html` and
  `delete-data.html` serve a GitHub Pages site. Meta's app review process
  requires a reachable privacy policy and data-deletion page, so breaking
  these breaks something that is not visible from the code.

## What this codebase is careful about

These are not style preferences. Each one is a way the system loses money if
you get it wrong, and most of them have a test that names the failure.

**Money is integer micros.** 1 USD = 1,000,000. Columns are `BigInteger`,
because a 32-bit column overflows at $2,147 and a lifetime spend total passes
that quickly. Never introduce a float for money. `money.py` has the
conversions.

**Estimates carry their uncertainty.** Decisions are made on credible intervals
from `core/stats.py`, not point estimates. Where money is being committed, use
the conservative end: a scale decision reads the lower bound of ROAS, so a
creative that got lucky does not get funded. There is no numpy or scipy —
the incomplete beta function is a hand-rolled continued fraction. Keep it that
way; the dependency is not worth it and the code is tested.

**Recent data is incomplete, not bad.** A click from yesterday has not had its
conversion window. `PerformanceWindow.trials()` returns matured clicks, not raw
clicks, and treating a young ad's zero conversions as failure is the single
most expensive bug this project has had. If you add a rule, ask what it does to
a four-day-old ad.

**Killing is gated earlier than scaling.** Deliberate asymmetry: a wrong kill
destroys a winner, a wrong scale spends some cash. Maturity floors, confidence
thresholds and evidence bars are all lower for stopping than for starting.
Preserve that direction in anything you add.

**Priors are leave-one-out.** `apply_pooled_prior` gives each entity a prior
built from its *peers*, never from a group including itself — otherwise an
entity is shrunk toward its own result and its interval tightens with
pseudo-observations of itself, right before the kill gate reads it.

**Multiple comparisons are adjusted.** Testing seven segments or angles at 90%
finds a "loser" by chance more often than not. Both `core/segments.py` and
`core/rotation.py` raise the bar by the number of things compared.

**Every mutation path honours `DRY_RUN`** and reports what it did. The
convention is a return value carrying `applied: bool` plus what *would* have
happened, so a dry run is informative rather than silent. If you add something
that touches an ad account, it needs this, and it needs a test proving a dry
run reaches no platform client.

**Approval gates and the global daily cap** exist so no sequence of
individually-reasonable decisions can run away. Do not route around them.

## What is verified and what is not

Take this seriously — it is the difference between a passing suite and working
software.

- **Verified:** everything above the platform adapters. The optimizer, the
  statistics, tracking and attribution, compliance, landing-page auditing,
  budget allocation and angle rotation are exercised against real logic.
- **Not verified:** the Meta and Google adapters have never made a successful
  call against the live APIs. Their tests use `httpx.MockTransport` returning
  responses shaped the way the docs were read — so they prove the adapter is
  self-consistent, not that the platform agrees. Expect wrong field names,
  missing required params and enum mismatches on first contact. Green tests
  here are not evidence.

When you fix something that first contact reveals, say so in the commit. It is
the most valuable information in this repository.

## Conventions

**Tests are the specification.** A test name states a claim
(`test_a_confident_loser_gets_nothing_not_a_floor`) and the docstring explains
what it costs to get wrong. Follow that: a test named `test_allocate_2` is not
useful to the next reader. Prefer a test that fails for the reason you care
about over one that merely passes.

**Comments explain why, not what.** The code says what. Where a number is a
business decision rather than a constant, say so where it is defined.

**No migration tooling.** `Base.metadata.create_all` only. A new table is safe;
a new column on an existing table will not appear in anyone's existing
database. Prefer a new table, or an existing JSON `extra` column.

**Never put a model identifier** — a Claude version, an OpenAI model name, any
of it — in a commit message, PR, code comment or anything else pushed to the
repository.

**Do not open a pull request unless asked.**

## Layout

```
adgenie/
  core/       the decisions: optimizer, portfolio, rotation, metrics, stats
  platforms/  Meta, Google and the auction simulator behind one interface
  media/      generation, local storage, upload into the ad account
  research/   Meta Ad Library
  api/        FastAPI routes
  cli.py      every capability has a command
tests/        663 of them; start here to understand a subsystem
```

`README.md` has a fuller map and the reasoning behind each subsystem.

## Known gaps

Roughly in the order they block getting real ads running:

1. Docker/Compose deployment and a ClickBank INS receiver are implemented;
   the deployed service and actual ClickBank delivery still need verification.
   See LIVE_TEST_RUNBOOK.md. Never mistake encrypted fixtures for a live sale.
2. Google builds only responsive search ads. No image or video ad path, which
   is why `GoogleAdsClient.upload_media` refuses rather than uploading an asset
   nothing would reference.
3. First contact with the live Meta and Google APIs has not happened.
4. `preflight` performs read-only checks, but cannot prove write permissions,
   policy approval or successful affiliate sale attribution.
