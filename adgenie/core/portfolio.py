"""Deciding which offers deserve the money.

Everything else in this project optimizes *within* an offer: which creative,
which ad group, which placement. That is the smaller half of the problem. An
affiliate account usually fails not because the creatives inside an offer were
badly ranked but because most of the daily budget sat on an offer that was
never going to pay, while the one that would have paid was starved into
statistical silence.

This module allocates the global daily budget *across* offers, and it differs
from the creative-level allocator in three ways that matter:

**It allocates on return per dollar, not return per click.** Ranking by
expected revenue per click is only correct when clicks cost the same
everywhere, and across offers they never do. An offer converting at 3% on
$2.00 clicks loses to one converting at 1% on $0.40 clicks, and a
per-click ranking gets that backwards.

**A loser gets zero, not a floor.** The creative-level allocator gives every
candidate a `min_share` so exploration never stops, which is right when the
candidates are creatives for one offer that is already known to work. Applied
across offers it is a permanent leak: an offer whose return is confidently
below breakeven is not an exploration opportunity, it is a subscription to
losing money.

**Exploration is concentrated, not spread.** Funding eight untested offers at
$5/day each buys eight windows too small to conclude anything from, and after
a month there are still eight unproven offers and the money is gone. The
budget for an unproven offer is sized from what it would actually take to
reach a verdict, and only as many offers are explored as can be funded at
that level.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import AdGroup, Campaign, EntityLevel, EntityStatus, Offer
from ..money import micros_to_usd
from .lag import LagModel
from .ltv import LeadValueModel
from .metrics import PerformanceWindow, apply_pooled_prior, load_performance
from .stats import thompson_sample_beta

__all__ = [
    "PortfolioPolicy",
    "OfferPosition",
    "OfferAllocation",
    "PortfolioPlan",
    "clicks_to_decide",
    "allocate_portfolio",
    "load_offer_positions",
    "PortfolioAllocator",
]


@dataclass
class PortfolioPolicy:
    """Thresholds. Every number here is a business decision, not a constant."""

    target_roas: float = 1.30
    # An offer whose *upper* credible bound is below this is not uncertain,
    # it is bad. Note the asymmetry with scaling: an offer is retired only
    # when even the optimistic reading loses money.
    retire_below_roas: float = 1.00
    credible_level: float = 0.90
    # Evidence needed before an offer is judged rather than explored.
    min_clicks_to_judge: int = 150
    # No single offer takes more than this share, however good it looks. This
    # is not a statistical rule. Affiliate offers get pulled, capped, or have
    # their payout cut overnight by someone who does not tell you first, and a
    # portfolio with 90% on one offer goes to zero revenue on that morning.
    max_share: float = 0.40
    # Sizing an exploration budget: enough clicks that this many conversions
    # would be expected at the offer's own breakeven rate. Below roughly five
    # the interval is too wide to separate a winner from a loser.
    min_conversions_to_decide: int = 5
    exploration_days: int = 7
    # How many unproven offers to fund at once. Concentrated, not spread.
    max_exploration_slots: int = 3
    # And how much of the portfolio they may claim between them. Testing is
    # how tomorrow's winner is found; it must not starve today's.
    max_exploration_share: float = 0.30
    # Platform learning phases reset on large budget changes, so a
    # theoretically better allocation reached in one jump can deliver worse
    # than the one it replaced.
    max_daily_change: float = 0.50
    # Maturity needed before an offer's budget is re-sized. Deliberately far
    # above the bar for retiring one: `is_mature` (15%) is the point at which
    # a confident loser can be cut, and the asymmetry is the same one the
    # optimizer makes. A wrong cut on incomplete data throws away a winner.
    judge_maturity_floor: float = 0.60
    min_daily_budget_micros: int = 5_000_000  # $5
    samples: int = 800

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "PortfolioPolicy":
        settings = settings or get_settings()
        return cls(
            target_roas=settings.target_roas,
            credible_level=settings.optimizer_credible_level,
        )


@dataclass
class OfferPosition:
    """One offer's standing in the portfolio."""

    offer_id: int
    name: str
    window: PerformanceWindow
    campaign_ids: list[int] = field(default_factory=list)
    committed_micros: int = 0
    # Set by the allocator.
    verdict: str = "hold"
    reason: str = ""

    def value_per_dollar(self, cvr: float) -> float:
        """Return per dollar of spend, if the true conversion rate were `cvr`.

        Every click already paid for is credited at `cvr`, not only the matured
        ones — the outstanding clicks will convert at the same rate, and
        pricing them at zero is exactly the censoring mistake the lag model
        exists to avoid. Lead value the funnel has already banked is added on
        top, because for a lead-magnet offer that is most of what the spend
        bought and judging it on completed sales alone retires it on day one.
        """
        window = self.window
        if not window.spend_micros:
            return 0.0
        payout = window.effective_payout_micros()
        sales = window.clicks * cvr * payout
        return (sales + window.outstanding_lead_value_micros) / window.spend_micros


@dataclass
class OfferAllocation:
    offer_id: int
    name: str
    verdict: str
    reason: str
    current_micros: int
    target_micros: int
    prob_best: float = 0.0
    roas_lower: float = 0.0
    roas_mean: float = 0.0
    roas_upper: float = 0.0

    @property
    def delta_micros(self) -> int:
        return self.target_micros - self.current_micros

    def as_dict(self) -> dict:
        return {
            "offer_id": self.offer_id,
            "name": self.name,
            "verdict": self.verdict,
            "reason": self.reason,
            "current_usd": micros_to_usd(self.current_micros),
            "target_usd": micros_to_usd(self.target_micros),
            "delta_usd": micros_to_usd(self.delta_micros),
            "prob_best": round(self.prob_best, 4),
            "roas": {
                "lower": round(self.roas_lower, 3),
                "mean": round(self.roas_mean, 3),
                "upper": round(self.roas_upper, 3),
            },
        }


@dataclass
class PortfolioPlan:
    total_micros: int
    allocations: list[OfferAllocation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def allocated_micros(self) -> int:
        return sum(a.target_micros for a in self.allocations)

    @property
    def unallocated_micros(self) -> int:
        return max(0, self.total_micros - self.allocated_micros)

    def as_dict(self) -> dict:
        return {
            "total_usd": micros_to_usd(self.total_micros),
            "allocated_usd": micros_to_usd(self.allocated_micros),
            "held_back_usd": micros_to_usd(self.unallocated_micros),
            "offers": [a.as_dict() for a in self.allocations],
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# sizing a test
# --------------------------------------------------------------------------
def clicks_to_decide(
    payout_micros: int, cpc_micros: float, min_conversions: int = 5
) -> int:
    """Clicks needed before an offer's return can be called either way.

    Enough that `min_conversions` would be expected at the offer's own
    breakeven rate. Fewer than that and the interval spans both profit and
    loss, so the spend bought no decision.

    Note what falls out of the arithmetic: at breakeven the money spent per
    conversion *is* the payout, so the cost of deciding an offer is
    `min_conversions x payout` regardless of what its clicks cost. Cheap
    traffic does not make a test cheaper, it only makes it slower.
    """
    if payout_micros <= 0 or cpc_micros <= 0:
        return 0
    breakeven_cvr = min(1.0, cpc_micros / payout_micros)
    if breakeven_cvr <= 0:
        return 0
    return int(min_conversions / breakeven_cvr)


def exploration_daily_micros(
    payout_micros: int, min_conversions: int, days: int
) -> int:
    """Daily spend that reaches a verdict on an unproven offer in `days`."""
    if payout_micros <= 0 or days <= 0:
        return 0
    return int(min_conversions * payout_micros / days)


# --------------------------------------------------------------------------
# the allocation itself
# --------------------------------------------------------------------------
def allocate_portfolio(
    positions: list[OfferPosition],
    total_micros: int,
    *,
    policy: PortfolioPolicy | None = None,
    rng: random.Random | None = None,
) -> PortfolioPlan:
    """Split a daily budget across offers by probability of being the best."""
    policy = policy or PortfolioPolicy()
    rng = rng or random.Random()
    plan = PortfolioPlan(total_micros=max(0, total_micros))
    if not positions or total_micros <= 0:
        return plan

    windows = [p.window for p in positions]
    apply_pooled_prior(windows)

    retired: list[OfferPosition] = []
    waiting: list[OfferPosition] = []
    unproven: list[OfferPosition] = []
    candidates: list[OfferPosition] = []

    for position in positions:
        window = position.window
        # Pipeline-aware: an offer whose leads are worth most of what the
        # spend bought is not a loser, and retiring it on completed sales
        # alone would kill every funnel before it had a chance to pay.
        interval = window.pipeline_roas_interval(policy.credible_level)
        measurable = (
            window.clicks >= policy.min_clicks_to_judge and window.can_model_roas
        )
        if (
            measurable
            and window.is_mature
            and interval.upper < policy.retire_below_roas
        ):
            position.verdict = "retire"
            position.reason = (
                f"Even the optimistic end of the range returns "
                f"{interval.upper:.2f}x, below breakeven, over "
                f"{window.clicks} clicks. This is not uncertainty."
            )
            retired.append(position)
        elif measurable and window.maturity < policy.judge_maturity_floor:
            # It has the traffic; the conversions have not had time to arrive.
            # Re-sizing it as though it were unproven would cut spend on an
            # offer whose numbers are incomplete rather than bad, which is the
            # censoring mistake the lag model exists to prevent. Hold it where
            # it is and decide when the window has run. Note this sits *after*
            # the retire test: an offer that loses money even on the optimistic
            # reading of partial data is not waiting for good news.
            position.verdict = "hold"
            position.reason = (
                f"{window.clicks} clicks, but only {window.maturity:.0%} of the "
                f"conversion window has elapsed. Held at its current budget "
                f"until the data is in."
            )
            waiting.append(position)
        elif not measurable:
            position.verdict = "explore"
            unproven.append(position)
        else:
            position.verdict = "fund"
            candidates.append(position)

    # A held offer keeps what it already has, and that money is off the table
    # before anything else is decided.
    held = {
        position.offer_id: min(position.committed_micros, total_micros)
        for position in waiting
    }
    committed_to_holds = min(total_micros, sum(held.values()))

    # -- exploration, concentrated ------------------------------------------
    # Most-progressed first. Finishing a test that is nearly conclusive is
    # worth more than starting another one that will not conclude either, and
    # it keeps slots sticky as a side effect: an offer already spending has
    # more clicks than one that has not started, so it keeps its slot instead
    # of being swapped out and restarting its learning phase every day.
    def progress(position: OfferPosition) -> float:
        needed = clicks_to_decide(
            position.window.effective_payout_micros() or position.window.offer_payout_micros,
            position.window.cpc_micros,
            policy.min_conversions_to_decide,
        )
        if needed <= 0:
            return 0.0
        return position.window.clicks / needed

    unproven.sort(key=progress, reverse=True)
    exploration: dict[int, int] = {}
    spent = 0
    explore_budget = min(
        max(0, total_micros - committed_to_holds),
        int(total_micros * policy.max_exploration_share),
    )
    for position in unproven:
        payout = (
            position.window.effective_payout_micros()
            or position.window.offer_payout_micros
        )
        want = max(
            policy.min_daily_budget_micros,
            exploration_daily_micros(
                payout, policy.min_conversions_to_decide, policy.exploration_days
            ),
        )
        if (
            len(exploration) >= policy.max_exploration_slots
            or spent + want > explore_budget
        ):
            position.verdict = "queued"
            position.reason = (
                "No exploration slot free. Funding it at a fraction of what a "
                "verdict costs would buy a test that cannot conclude."
            )
            continue
        exploration[position.offer_id] = want
        spent += want
        position.reason = (
            f"Unproven: {position.window.clicks} clicks so far. "
            f"${micros_to_usd(want):,.2f}/day reaches a verdict in about "
            f"{policy.exploration_days} days."
        )

    # -- the rest, by probability of being the best -------------------------
    # Every candidate keeps a floor. This is the opposite of the rule applied
    # to a retired offer, and deliberately so: a candidate is an offer whose
    # return is *not* confidently below breakeven, and the probability of
    # being the single best use of the next dollar undervalues a second earner
    # that is merely good. Losing the draw is not a reason to stop an offer
    # that makes money — it is a reason to give it less.
    discretionary = max(0, total_micros - spent - committed_to_holds)
    reserve = min(discretionary, len(candidates) * policy.min_daily_budget_micros)
    shares = _probability_best(candidates, policy, rng)
    ceiling = _concentration_ceiling(
        total_micros,
        len(candidates) + len(exploration) + len(held),
        policy,
    )
    funded = _apply_concentration_cap(
        {p.offer_id: shares.get(p.offer_id, 0.0) for p in candidates},
        discretionary - reserve,
        ceiling,
    )
    if candidates and reserve:
        floor_each = reserve // len(candidates)
        for position in candidates:
            funded[position.offer_id] = min(
                ceiling, funded.get(position.offer_id, 0) + floor_each
            )

    # -- assemble, clamping the size of each move ---------------------------
    raws: dict[int, int] = {}
    targets: dict[int, int] = {}
    for position in positions:
        raw = exploration.get(
            position.offer_id,
            held.get(position.offer_id, funded.get(position.offer_id, 0)),
        )
        if position.verdict in ("retire", "queued"):
            raw = 0
        raws[position.offer_id] = raw
        targets[position.offer_id] = _clamp_change(
            position.committed_micros, raw, policy, position.verdict
        )

    # Gliding a budget down instead of dropping it is a preference. The daily
    # cap is not. Where the two collide the glide gives way, or a portfolio
    # already committed above its cap would keep it there for days.
    targets = _fit_to_budget(targets, raws, total_micros, policy)

    for position in positions:
        target = targets[position.offer_id]
        interval = position.window.pipeline_roas_interval(policy.credible_level)
        if position.verdict == "fund" and not position.reason:
            position.reason = (
                f"Return between {interval.lower:.2f}x and {interval.upper:.2f}x "
                f"over {position.window.clicks} clicks; "
                f"{shares.get(position.offer_id, 0.0):.0%} chance it is the best "
                f"use of the next dollar."
            )
        plan.allocations.append(
            OfferAllocation(
                offer_id=position.offer_id,
                name=position.name,
                verdict=position.verdict,
                reason=position.reason,
                current_micros=position.committed_micros,
                target_micros=target,
                prob_best=shares.get(position.offer_id, 0.0),
                roas_lower=interval.lower,
                roas_mean=interval.mean,
                roas_upper=interval.upper,
            )
        )

    plan.allocations.sort(key=lambda a: a.target_micros, reverse=True)
    if plan.unallocated_micros:
        reasons = []
        if any(
            a.verdict == "fund" and a.target_micros >= ceiling
            for a in plan.allocations
        ):
            reasons.append(
                f"no offer may hold more than {policy.max_share:.0%} of the "
                f"portfolio, because an affiliate offer can be pulled overnight"
            )
        if any(
            a.current_micros > 0 and a.target_micros
            >= int(a.current_micros * (1 + policy.max_daily_change))
            for a in plan.allocations
        ):
            reasons.append(
                f"a budget rises at most {policy.max_daily_change:.0%} a day so "
                f"the platforms do not re-enter the learning phase"
            )
        if not reasons:
            reasons.append("nothing on offer earns it yet")
        plan.notes.append(
            f"${micros_to_usd(plan.unallocated_micros):,.2f}/day held back: "
            + "; ".join(reasons)
            + "."
        )
    if retired:
        plan.notes.append(
            f"{len(retired)} offer(s) cut to zero. A losing offer gets no "
            f"exploration floor: that is a subscription, not a test."
        )
    return plan


def _fit_to_budget(
    targets: dict[int, int],
    raws: dict[int, int],
    total: int,
    policy: PortfolioPolicy,
) -> dict[int, int]:
    """Bring a clamped allocation back under the daily cap.

    The excess is taken first from offers being let down gently, back toward
    what the allocation actually called for, since that slack exists only to
    protect a learning phase. Only if that is not enough does everything scale
    down together.
    """
    over = sum(targets.values()) - total
    if over <= 0:
        return targets

    out = dict(targets)
    slack = {oid: out[oid] - raws.get(oid, 0) for oid in out}
    slack_total = sum(v for v in slack.values() if v > 0)
    if slack_total > 0:
        take = min(over, slack_total)
        for oid, amount in slack.items():
            if amount > 0:
                out[oid] -= int(take * amount / slack_total)
        over = sum(out.values()) - total

    if over > 0:
        allocated = sum(out.values())
        scale = total / allocated if allocated else 0.0
        out = {oid: int(value * scale) for oid, value in out.items()}

    return {
        oid: value if value >= policy.min_daily_budget_micros else 0
        for oid, value in out.items()
    }


def _probability_best(
    candidates: list[OfferPosition],
    policy: PortfolioPolicy,
    rng: random.Random,
) -> dict[int, float]:
    """Fraction of draws in which each offer returns the most per dollar."""
    if not candidates:
        return {}
    if len(candidates) == 1:
        return {candidates[0].offer_id: 1.0}

    wins = {p.offer_id: 0 for p in candidates}
    for _ in range(policy.samples):
        best_id, best_value = None, float("-inf")
        for position in candidates:
            window = position.window
            cvr = thompson_sample_beta(
                window.conversions,
                window.trials(),
                prior_a=window.prior_a,
                prior_b=window.prior_b,
                rng=rng,
            )
            value = position.value_per_dollar(cvr)
            if value > best_value:
                best_id, best_value = position.offer_id, value
        if best_id is not None:
            wins[best_id] += 1
    return {oid: count / policy.samples for oid, count in wins.items()}


def _concentration_ceiling(
    total: int, fundable: int, policy: PortfolioPolicy
) -> int:
    """The most any one offer may hold.

    The cap exists so a payout cut or a pulled offer cannot take the whole
    portfolio down with it. That risk is only worth paying for when there is
    somewhere else for the money to go: with a single live offer, holding back
    60% of the budget does not diversify anything, it just leaves the money
    earning nothing while the one offer that works runs at a fraction of what
    it could. So the cap can never bind harder than an even split — it starts
    to bite exactly when there are enough offers for diversification to mean
    something, and not before.
    """
    if fundable <= 0:
        return total
    return max(int(total * policy.max_share), total // fundable)


def _apply_concentration_cap(
    shares: dict[int, float], budget: int, ceiling: int
) -> dict[int, int]:
    """Turn shares into money, with no offer over the cap.

    Overflow from a capped offer is redistributed among the uncapped ones,
    which can push one of *them* over the cap, so this repeats until it
    settles rather than capping once and calling it done.
    """
    if not shares or budget <= 0:
        return {oid: 0 for oid in shares}

    remaining = dict(shares)
    allocation: dict[int, int] = {oid: 0 for oid in shares}
    pool = budget

    for _ in range(len(shares) + 1):
        weight = sum(remaining.values())
        if not remaining or weight <= 0 or pool <= 0:
            break
        overflowed = False
        for oid in list(remaining):
            amount = int(pool * remaining[oid] / weight)
            if amount > ceiling:
                allocation[oid] = ceiling
                pool -= ceiling
                del remaining[oid]
                overflowed = True
                break
        if overflowed:
            continue
        for oid, share in remaining.items():
            allocation[oid] = int(pool * share / weight)
        break
    return allocation


def _clamp_change(
    current: int, target: int, policy: PortfolioPolicy, verdict: str
) -> int:
    """Limit how far one day may move a budget.

    A cut is clamped in the same way as a rise, with one exception. Dropping a
    working offer by 90% overnight does the same learning-phase damage as
    tripling it, and does it to something that was making money — so an offer
    still in the portfolio glides down rather than falling. An offer being
    retired is not gliding anywhere: a learning phase is only a cost if you
    intend to keep spending, and stopping a confirmed loser gradually just
    means losing money more slowly.
    """
    if target <= 0 or target < policy.min_daily_budget_micros:
        return 0
    if current <= 0:
        return target
    if target < current:
        if verdict in ("retire", "queued"):
            return target
        floor = int(current * (1 - policy.max_daily_change))
        return max(target, max(floor, policy.min_daily_budget_micros))
    ceiling = int(current * (1 + policy.max_daily_change))
    return min(target, ceiling)


# --------------------------------------------------------------------------
# reading the portfolio out of the database
# --------------------------------------------------------------------------
def load_offer_positions(
    session: Session,
    since: date,
    until: date,
    credible_level: float = 0.90,
    lag_model: LagModel | None = None,
    lead_value: LeadValueModel | None = None,
) -> list[OfferPosition]:
    """Every offer currently spending, with its aggregate performance."""
    offers = list(
        session.execute(
            select(Offer).where(Offer.status == EntityStatus.ACTIVE)
        ).scalars()
    )
    positions: list[OfferPosition] = []
    for offer in offers:
        campaigns = list(
            session.execute(
                select(Campaign).where(
                    Campaign.offer_id == offer.id,
                    Campaign.status == EntityStatus.ACTIVE,
                )
            ).scalars()
        )
        if not campaigns:
            continue
        window = load_performance(
            session,
            EntityLevel.OFFER,
            offer.id,
            since,
            until,
            credible_level,
            lag_model=lag_model,
            lead_value=lead_value,
        )
        positions.append(
            OfferPosition(
                offer_id=offer.id,
                name=offer.name,
                window=window,
                campaign_ids=[c.id for c in campaigns],
                committed_micros=sum(
                    _campaign_commitment(session, c) for c in campaigns
                ),
            )
        )
    return positions


def _campaign_commitment(session: Session, campaign: Campaign) -> int:
    """What a campaign can spend in a day.

    On Meta the budget may sit on the campaign or on its ad sets, and reading
    only one of the two understates the commitment. Whichever is larger is
    what the account is actually exposed to.
    """
    group_total = sum(
        session.execute(
            select(AdGroup.daily_budget_micros).where(
                AdGroup.campaign_id == campaign.id,
                AdGroup.status == EntityStatus.ACTIVE,
            )
        ).scalars()
    )
    return max(campaign.daily_budget_micros, group_total)


class PortfolioAllocator:
    """Plans and applies budget moves across offers."""

    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        policy: PortfolioPolicy | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.policy = policy or PortfolioPolicy.from_settings(self.settings)
        self.rng = rng or random.Random()

    def total_budget_micros(self) -> int:
        return int(self.settings.global_daily_budget_cap_usd * 1_000_000)

    def plan(
        self,
        since: date,
        until: date,
        total_micros: int | None = None,
        lag_model: LagModel | None = None,
    ) -> PortfolioPlan:
        positions = load_offer_positions(
            self.session,
            since,
            until,
            credible_level=self.policy.credible_level,
            lag_model=lag_model,
        )
        return allocate_portfolio(
            positions,
            total_micros if total_micros is not None else self.total_budget_micros(),
            policy=self.policy,
            rng=self.rng,
        )

    def apply(
        self, plan: PortfolioPlan, orchestrator=None, apply: bool | None = None
    ) -> dict:
        """Push a plan's targets onto the offers' campaigns.

        Honours DRY_RUN. An offer's target is split across its live campaigns
        in proportion to what they already spend, because the split between a
        Meta campaign and a Google one was a decision made with information
        this allocator does not have. A target of zero pauses them: that is
        the only case where the campaign objects themselves change state.
        """
        apply = (not self.settings.dry_run) if apply is None else apply
        changes: list[dict] = []

        for allocation in plan.allocations:
            campaigns = list(
                self.session.execute(
                    select(Campaign).where(
                        Campaign.offer_id == allocation.offer_id,
                        Campaign.status == EntityStatus.ACTIVE,
                    )
                ).scalars()
            )
            if not campaigns:
                continue
            current = [_campaign_commitment(self.session, c) for c in campaigns]
            total_current = sum(current)

            for campaign, was in zip(campaigns, current):
                if allocation.target_micros <= 0:
                    target = 0
                elif total_current > 0:
                    target = int(allocation.target_micros * was / total_current)
                else:
                    target = allocation.target_micros // len(campaigns)
                if target == was:
                    continue
                changes.append(
                    {
                        "offer_id": allocation.offer_id,
                        "campaign_id": campaign.id,
                        "from_usd": micros_to_usd(was),
                        "to_usd": micros_to_usd(target),
                        "verdict": allocation.verdict,
                    }
                )
                if not apply:
                    continue
                if target <= 0:
                    campaign.status = EntityStatus.PAUSED
                    campaign.last_error = (
                        "Paused by portfolio allocation: "
                        + allocation.reason
                    )
                    if orchestrator is not None and campaign.external_id:
                        orchestrator.client(campaign.platform).set_status(
                            "campaign", campaign.external_id, False
                        )
                    continue
                campaign.daily_budget_micros = target
                if orchestrator is not None and campaign.external_id:
                    orchestrator.client(campaign.platform).set_budget(
                        "campaign", campaign.external_id, target
                    )

        if apply:
            self.session.commit()
        return {"applied": apply, "changes": changes}
