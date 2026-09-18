"""Allocating budget across offers.

The optimizer decides which creative wins inside an offer. This decides
whether the offer deserves the money at all, which is where most affiliate
accounts actually lose.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from sqlalchemy import func, select

from adgenie.core.metrics import PerformanceWindow
from adgenie.core.portfolio import (
    OfferPosition,
    PortfolioAllocator,
    PortfolioPolicy,
    allocate_portfolio,
    clicks_to_decide,
    exploration_daily_micros,
    load_offer_positions,
)
from adgenie.models import (
    AdGroup,
    Campaign,
    Conversion,
    ConversionStatus,
    EntityLevel,
    EntityStatus,
    MetricSnapshot,
    Platform,
)
from adgenie.money import usd_to_micros

@pytest.fixture(autouse=True)
def portfolio_clients(monkeypatch):
    from adgenie.core import orchestrator
    from tests.test_regressions import _NullPlatform

    monkeypatch.setattr(orchestrator, "get_platform", lambda *args: _NullPlatform())


SINCE = date(2026, 1, 1)
UNTIL = date(2026, 1, 14)


def position(
    offer_id: int,
    name: str,
    clicks: int,
    conversions: int,
    spend_usd: float,
    payout_usd: float,
    committed_usd: float = 50.0,
    leads: int = 0,
    lead_value_usd: float = 0.0,
) -> OfferPosition:
    window = PerformanceWindow(
        level=EntityLevel.OFFER,
        entity_id=offer_id,
        since=SINCE,
        until=UNTIL,
        clicks=clicks,
        impressions=clicks * 40,
        conversions=conversions,
        spend_micros=usd_to_micros(spend_usd),
        revenue_micros=usd_to_micros(conversions * payout_usd),
        offer_payout_micros=usd_to_micros(payout_usd),
        effective_clicks=float(clicks),
        leads=leads,
        lead_value_micros=usd_to_micros(lead_value_usd),
    )
    return OfferPosition(
        offer_id=offer_id,
        name=name,
        window=window,
        campaign_ids=[offer_id],
        committed_micros=usd_to_micros(committed_usd),
    )


def by_id(plan) -> dict:
    return {a.offer_id: a for a in plan.allocations}


# --- sizing a test ---------------------------------------------------------


def test_the_cost_of_deciding_an_offer_is_a_multiple_of_its_payout():
    """Cheap traffic makes a test slower, not cheaper.

    At breakeven the money spent per conversion is the payout, so the spend
    needed to reach a verdict is the same whatever a click costs.
    """
    cheap = clicks_to_decide(usd_to_micros(30), usd_to_micros(0.25), 5)
    dear = clicks_to_decide(usd_to_micros(30), usd_to_micros(2.50), 5)

    assert cheap == pytest.approx(dear * 10, rel=0.01)
    assert cheap * 0.25 == pytest.approx(dear * 2.50, rel=0.01)
    assert cheap * 0.25 == pytest.approx(5 * 30, rel=0.01)


def test_an_unpriced_offer_cannot_be_sized():
    assert clicks_to_decide(0, usd_to_micros(1)) == 0
    assert clicks_to_decide(usd_to_micros(30), 0) == 0
    assert exploration_daily_micros(0, 5, 7) == 0


# --- ranking ---------------------------------------------------------------


def test_allocation_ranks_by_return_per_dollar_not_per_click():
    """The crux. Across offers, clicks do not cost the same.

    A 3% conversion rate on $2.00 clicks loses money; 1% on $0.40 clicks
    makes it. Ranking on revenue per click gets this exactly backwards.
    """
    high_cvr = position(1, "3% cvr, $2.00 clicks", 2000, 60, 4000, 40)
    low_cvr = position(2, "1% cvr, $0.40 clicks", 2000, 20, 800, 40)

    assert high_cvr.window.cvr > low_cvr.window.cvr
    assert high_cvr.window.epc_micros > low_cvr.window.epc_micros
    assert low_cvr.window.roas > high_cvr.window.roas

    plan = allocate_portfolio(
        [high_cvr, low_cvr], usd_to_micros(500), rng=random.Random(3)
    )
    allocations = by_id(plan)
    assert allocations[2].target_micros > allocations[1].target_micros


@pytest.mark.parametrize("committed", [0, 30])
def test_an_uncertain_offer_cannot_take_new_funding_merely_for_not_being_a_loser(committed):
    uncertain = position(1, "uncertain", 200, 6, 200, 40, committed_usd=committed)
    allocation = allocate_portfolio([uncertain], usd_to_micros(500)).allocations[0]
    assert allocation.roas_lower < 1 < allocation.roas_upper
    assert allocation.verdict == "hold"
    assert allocation.target_micros == uncertain.committed_micros


def test_a_confident_loser_gets_nothing_not_a_floor():
    """An offer that reliably loses money is not an exploration opportunity."""
    loser = position(1, "loser", 3000, 15, 1500, 20)
    winner = position(2, "winner", 3000, 150, 1500, 20)

    plan = allocate_portfolio(
        [loser, winner], usd_to_micros(500), rng=random.Random(5)
    )
    allocations = by_id(plan)
    assert allocations[1].verdict == "retire"
    assert allocations[1].target_micros == 0
    assert allocations[2].target_micros > 0


def test_a_profitable_offer_that_loses_the_draw_keeps_a_floor():
    """Losing the draw means less money, not no money.

    Probability of being the single best use of the next dollar undervalues a
    second earner that is merely good, and pausing it would throw away a
    working asset over a ranking.
    """
    great = position(1, "great", 3000, 200, 1000, 20)
    good = position(2, "merely good", 3000, 80, 1000, 20)

    plan = allocate_portfolio(
        [great, good], usd_to_micros(500), rng=random.Random(5)
    )
    allocations = by_id(plan)
    assert allocations[2].verdict == "fund"
    assert allocations[2].prob_best < 0.05
    assert allocations[2].target_micros > 0


def test_a_funnel_offer_is_valued_on_its_leads_too():
    """Judged on completed sales alone, a lead magnet looks like a failure."""
    policy = PortfolioPolicy(max_daily_change=10.0)
    bare = position(1, "no funnel", 2000, 25, 1000, 20)
    funnel = position(
        2, "lead magnet", 2000, 25, 1000, 20, leads=400, lead_value_usd=900
    )

    assert bare.window.roas == pytest.approx(funnel.window.roas)

    plan = allocate_portfolio(
        [bare, funnel], usd_to_micros(500), policy=policy, rng=random.Random(9)
    )
    allocations = by_id(plan)
    assert allocations[2].target_micros > allocations[1].target_micros


# --- concentration ---------------------------------------------------------


def test_no_offer_takes_more_than_the_concentration_cap():
    """Not a statistical rule. Affiliate offers get pulled overnight."""
    policy = PortfolioPolicy(max_share=0.40, max_daily_change=100.0)
    positions = [
        position(1, "star", 5000, 400, 1000, 20, committed_usd=200),
        position(2, "other", 5000, 90, 1000, 20, committed_usd=200),
        position(3, "third", 5000, 85, 1000, 20, committed_usd=200),
    ]
    plan = allocate_portfolio(
        positions, usd_to_micros(1000), policy=policy, rng=random.Random(2)
    )
    allocations = by_id(plan)
    assert allocations[1].prob_best > 0.95
    assert allocations[1].target_micros <= usd_to_micros(400)


def test_the_cap_never_leaves_money_idle_for_want_of_somewhere_to_go():
    """With one live offer there is nothing to diversify into.

    Capping the only earner at 40% does not spread risk, it just leaves 60%
    of the budget earning nothing while the offer that works runs at a
    fraction of what it could.
    """
    policy = PortfolioPolicy(max_share=0.40, max_daily_change=100.0)
    only = position(1, "the only one", 5000, 400, 1000, 20, committed_usd=200)

    plan = allocate_portfolio(
        [only], usd_to_micros(1000), policy=policy, rng=random.Random(2)
    )
    assert by_id(plan)[1].target_micros == usd_to_micros(1000)


def test_the_cap_tightens_as_offers_are_added():
    """It binds exactly when diversification becomes possible."""
    policy = PortfolioPolicy(max_share=0.40, max_daily_change=100.0)

    def star_share(count: int) -> int:
        positions = [
            position(1, "star", 5000, 400, 1000, 20, committed_usd=200)
        ] + [
            position(i, f"other {i}", 5000, 90, 1000, 20, committed_usd=200)
            for i in range(2, count + 1)
        ]
        plan = allocate_portfolio(
            positions, usd_to_micros(1000), policy=policy, rng=random.Random(2)
        )
        return by_id(plan)[1].target_micros

    assert star_share(1) == usd_to_micros(1000)
    assert star_share(2) == usd_to_micros(500)
    # Past the point where an even split is tighter than the policy share,
    # the policy share takes over and stops tightening.
    assert star_share(3) == usd_to_micros(400)
    assert star_share(5) == usd_to_micros(400)


def test_overflow_from_a_capped_offer_does_not_re_breach_the_cap():
    """Redistributing a capped offer's share can push another over the cap."""
    policy = PortfolioPolicy(max_share=0.40, max_daily_change=100.0)
    positions = [
        position(1, "a", 5000, 400, 1000, 20, committed_usd=200),
        position(2, "b", 5000, 395, 1000, 20, committed_usd=200),
        position(3, "c", 5000, 100, 1000, 20, committed_usd=200),
    ]
    plan = allocate_portfolio(
        positions, usd_to_micros(1000), policy=policy, rng=random.Random(4)
    )
    ceiling = usd_to_micros(400)
    for allocation in plan.allocations:
        assert allocation.target_micros <= ceiling


# --- exploration -----------------------------------------------------------


def test_exploration_is_concentrated_rather_than_spread():
    """Eight tests at $5/day buy eight windows that cannot conclude."""
    policy = PortfolioPolicy(max_exploration_slots=2)
    unproven = [position(i, f"new {i}", 0, 0, 0, 30, committed_usd=0) for i in range(1, 7)]

    plan = allocate_portfolio(
        unproven, usd_to_micros(500), policy=policy, rng=random.Random(1)
    )
    funded = [a for a in plan.allocations if a.target_micros > 0]
    assert len(funded) == 2
    # Each funded slot is large enough to reach a verdict in the test window.
    for allocation in funded:
        assert allocation.target_micros >= usd_to_micros(
            5 * 30 / policy.exploration_days
        )
    queued = [a for a in plan.allocations if a.verdict == "queued"]
    assert len(queued) == 4


def test_a_nearly_finished_test_keeps_its_slot():
    """Finishing a test beats starting one, and keeps slots from churning."""
    policy = PortfolioPolicy(max_exploration_slots=1)
    started = position(1, "half done", 120, 2, 60, 30, committed_usd=20)
    fresh = position(2, "not started", 0, 0, 0, 30, committed_usd=0)

    plan = allocate_portfolio(
        [started, fresh], usd_to_micros(500), policy=policy, rng=random.Random(1)
    )
    allocations = by_id(plan)
    assert allocations[1].verdict == "explore"
    assert allocations[2].verdict == "queued"


def test_exploration_stops_at_the_budget():
    policy = PortfolioPolicy(max_exploration_slots=5)
    unproven = [position(i, f"new {i}", 0, 0, 0, 100, committed_usd=0) for i in range(1, 6)]
    plan = allocate_portfolio(
        unproven, usd_to_micros(80), policy=policy, rng=random.Random(1)
    )
    assert plan.allocated_micros <= usd_to_micros(80)


# --- pacing ----------------------------------------------------------------


def test_a_budget_rise_is_capped_so_the_learning_phase_survives():
    policy = PortfolioPolicy(max_daily_change=0.50)
    winner = position(1, "winner", 5000, 500, 500, 20, committed_usd=40)
    plan = allocate_portfolio(
        [winner], usd_to_micros(500), policy=policy, rng=random.Random(1)
    )
    assert by_id(plan)[1].target_micros == usd_to_micros(60)


def test_a_working_offer_glides_down_rather_than_falling():
    """A 90% overnight cut damages a learning phase as much as tripling does."""
    policy = PortfolioPolicy(max_daily_change=0.50)
    great = position(1, "great", 4000, 400, 1000, 20, committed_usd=100)
    good = position(2, "merely good", 4000, 80, 1000, 20, committed_usd=100)

    plan = allocate_portfolio(
        [great, good], usd_to_micros(300), policy=policy, rng=random.Random(6)
    )
    allocations = by_id(plan)
    assert allocations[2].verdict == "fund"
    assert allocations[2].target_micros == usd_to_micros(50)


def test_a_retired_offer_is_cut_to_zero_immediately():
    """Stopping a confirmed loser gradually just loses money more slowly."""
    policy = PortfolioPolicy(max_daily_change=0.50)
    loser = position(1, "loser", 4000, 20, 2000, 20, committed_usd=100)
    plan = allocate_portfolio(
        [loser], usd_to_micros(500), policy=policy, rng=random.Random(1)
    )
    assert by_id(plan)[1].target_micros == 0


def test_nothing_is_allocated_without_a_budget():
    assert allocate_portfolio([position(1, "a", 100, 5, 50, 20)], 0).allocations == []
    assert allocate_portfolio([], usd_to_micros(500)).allocations == []


def test_the_plan_never_exceeds_the_budget():
    positions = [
        position(i, f"offer {i}", 2000, 40 + i * 10, 800, 20, committed_usd=1000)
        for i in range(1, 6)
    ]
    total = usd_to_micros(500)
    plan = allocate_portfolio(positions, total, rng=random.Random(8))
    assert plan.allocated_micros <= total


# --- reading the portfolio out of the database -----------------------------


def _campaign(session, offer, name, budget_usd, platform=Platform.META):
    campaign = Campaign(
        offer_id=offer.id,
        platform=platform,
        name=name,
        external_id=f"ext-{name}",
        status=EntityStatus.ACTIVE,
        daily_budget_micros=usd_to_micros(budget_usd),
    )
    session.add(campaign)
    session.commit()
    return campaign


def _deliver(session, campaign, day, clicks, spend_usd, impressions=None):
    session.add(
        MetricSnapshot(
            level=EntityLevel.CAMPAIGN,
            entity_id=campaign.id,
            day=day,
            impressions=impressions if impressions is not None else clicks * 40,
            clicks=clicks,
            spend_micros=usd_to_micros(spend_usd),
        )
    )


def test_an_offers_delivery_is_its_campaigns_summed_per_day(session, offer):
    """Per day, not concatenated.

    Two campaigns delivering on the same date are one day of delivery for the
    offer. Appending both would hand the lag model a date it thinks it has
    seen twice and distort the maturity of everything downstream.
    """
    meta = _campaign(session, offer, "meta", 30)
    google = _campaign(session, offer, "google", 30, Platform.GOOGLE)
    day = date(2026, 3, 1)
    _deliver(session, meta, day, clicks=100, spend_usd=40)
    _deliver(session, google, day, clicks=60, spend_usd=20)
    session.commit()

    positions = load_offer_positions(session, day, day)
    assert len(positions) == 1
    window = positions[0].window
    assert window.clicks == 160
    assert window.spend_micros == usd_to_micros(60)
    assert len(window.daily) == 1
    assert window.days_with_delivery == 1


def test_a_position_reports_what_its_campaigns_commit(session, offer):
    _campaign(session, offer, "meta", 30)
    _campaign(session, offer, "google", 45, Platform.GOOGLE)
    day = date(2026, 3, 1)
    session.commit()

    positions = load_offer_positions(session, day, day)
    assert positions[0].committed_micros == usd_to_micros(75)


def test_an_offer_with_no_live_campaigns_is_not_a_position(session, offer):
    day = date(2026, 3, 1)
    assert load_offer_positions(session, day, day) == []


def test_conversions_reach_an_offer_window(session, offer):
    campaign = _campaign(session, offer, "meta", 30)
    day = date(2026, 3, 1)
    _deliver(session, campaign, day, clicks=200, spend_usd=100)
    for _ in range(4):
        session.add(
            Conversion(
                offer_id=offer.id,
                campaign_id=campaign.id,
                status=ConversionStatus.APPROVED,
                revenue_micros=usd_to_micros(40),
                occurred_at=date(2026, 3, 1),
                network_txn_id=f"c{_}",
            )
        )
    session.commit()

    window = load_offer_positions(session, day, day)[0].window
    assert window.conversions == 4
    assert window.revenue_micros == usd_to_micros(160)


# --- applying a plan -------------------------------------------------------


def test_applying_a_plan_moves_campaign_budgets(session, offer, settings):
    campaign = _campaign(session, offer, "meta", 40)
    day = date(2026, 3, 1)
    _deliver(session, campaign, day, clicks=3000, spend_usd=600)
    for i in range(120):
        session.add(
            Conversion(
                offer_id=offer.id,
                campaign_id=campaign.id,
                status=ConversionStatus.APPROVED,
                revenue_micros=usd_to_micros(40),
                occurred_at=date(2026, 3, 1),
                network_txn_id=f"c{i}",
            )
        )
    session.commit()

    allocator = PortfolioAllocator(
        session, settings=settings, rng=random.Random(1)
    )
    plan = allocator.plan(day, day, total_micros=usd_to_micros(500))
    assert by_id(plan)[offer.id].verdict == "fund"

    result = allocator.apply(plan)
    assert result["applied"] is True
    session.refresh(campaign)
    assert campaign.daily_budget_micros == usd_to_micros(60)


@pytest.mark.parametrize("apply", [None, True, False])
def test_a_dry_run_moves_nothing(session, offer, settings, apply):
    campaign = _campaign(session, offer, "meta", 40)
    day = date(2026, 3, 1)
    _deliver(session, campaign, day, clicks=3000, spend_usd=600)
    session.commit()
    dry = settings.model_copy(update={"dry_run": True})

    allocator = PortfolioAllocator(session, settings=dry, rng=random.Random(1))
    plan = allocator.plan(day, day, total_micros=usd_to_micros(500))

    class Exploding:
        def client(self, platform):  # pragma: no cover - must never be called
            raise AssertionError("a dry run reached the platform")

    result = allocator.apply(plan, orchestrator=Exploding(), apply=apply)
    assert result["applied"] is False
    session.refresh(campaign)
    assert campaign.daily_budget_micros == usd_to_micros(40)


def test_a_zero_target_pauses_the_campaign(session, offer, settings):
    campaign = _campaign(session, offer, "meta", 100)
    day = date(2026, 3, 1)
    _deliver(session, campaign, day, clicks=4000, spend_usd=2000)
    for i in range(10):
        session.add(
            Conversion(
                offer_id=offer.id,
                campaign_id=campaign.id,
                status=ConversionStatus.APPROVED,
                revenue_micros=usd_to_micros(40),
                occurred_at=date(2026, 3, 1),
                network_txn_id=f"c{i}",
            )
        )
    session.commit()

    allocator = PortfolioAllocator(
        session, settings=settings, rng=random.Random(1)
    )
    plan = allocator.plan(day, day, total_micros=usd_to_micros(500))
    assert by_id(plan)[offer.id].verdict == "retire"

    allocator.apply(plan)
    session.refresh(campaign)
    assert campaign.status is EntityStatus.PAUSED


def test_a_target_splits_across_campaigns_by_what_they_already_spend(
    session, offer, settings
):
    """The Meta/Google split was a decision made with information this
    allocator does not have, so it is preserved rather than re-guessed."""
    meta = _campaign(session, offer, "meta", 75)
    google = _campaign(session, offer, "google", 25, Platform.GOOGLE)
    day = date(2026, 3, 1)
    _deliver(session, meta, day, clicks=3000, spend_usd=450)
    _deliver(session, google, day, clicks=1000, spend_usd=150)
    for i in range(200):
        session.add(
            Conversion(
                offer_id=offer.id,
                campaign_id=meta.id,
                status=ConversionStatus.APPROVED,
                revenue_micros=usd_to_micros(40),
                occurred_at=date(2026, 3, 1),
                network_txn_id=f"c{i}",
            )
        )
    session.commit()

    allocator = PortfolioAllocator(
        session, settings=settings, rng=random.Random(1)
    )
    plan = allocator.plan(day, day, total_micros=usd_to_micros(1000))
    allocator.apply(plan)

    session.refresh(meta)
    session.refresh(google)
    assert meta.daily_budget_micros == 3 * google.daily_budget_micros


# --- incomplete data is not bad data ---------------------------------------


def _immature(offer_id, name, clicks, conversions, spend_usd, payout_usd, committed_usd):
    position_ = position(
        offer_id, name, clicks, conversions, spend_usd, payout_usd, committed_usd
    )
    position_.window.maturity = 0.25
    position_.window.effective_clicks = clicks * 0.25
    return position_


def test_an_offer_awaiting_its_conversion_window_is_held_not_resized():
    """Traffic in, conversions not yet reported. That is incomplete, not bad.

    Sizing it as an unproven test would cut spend on an offer whose numbers
    have simply not arrived — the censoring mistake the lag model exists to
    prevent.
    """
    waiting = _immature(1, "still maturing", 900, 3, 400, 30, committed_usd=60)
    plan = allocate_portfolio(
        [waiting], usd_to_micros(500), rng=random.Random(1)
    )
    allocation = by_id(plan)[1]
    assert allocation.verdict == "hold"
    assert allocation.target_micros == usd_to_micros(60)
    assert "conversion window" in allocation.reason


def test_a_held_offer_is_paid_before_exploration_gets_a_slot():
    policy = PortfolioPolicy(max_exploration_slots=3)
    waiting = _immature(1, "still maturing", 900, 3, 400, 30, committed_usd=80)
    fresh = [
        position(i, f"new {i}", 0, 0, 0, 30, committed_usd=0) for i in range(2, 5)
    ]
    plan = allocate_portfolio(
        [waiting, *fresh], usd_to_micros(100), policy=policy, rng=random.Random(1)
    )
    assert by_id(plan)[1].target_micros == usd_to_micros(80)
    assert plan.allocated_micros <= usd_to_micros(100)


def test_exploration_cannot_starve_a_proven_earner():
    """Testing is how tomorrow's winner is found; not at today's expense."""
    policy = PortfolioPolicy(max_exploration_share=0.30, max_daily_change=10.0)
    earner = position(1, "earner", 4000, 400, 1000, 20, committed_usd=50)
    fresh = [
        position(i, f"new {i}", 0, 0, 0, 100, committed_usd=0) for i in range(2, 6)
    ]
    plan = allocate_portfolio(
        [earner, *fresh], usd_to_micros(200), policy=policy, rng=random.Random(1)
    )
    explored = sum(
        a.target_micros for a in plan.allocations if a.verdict == "explore"
    )
    assert explored <= usd_to_micros(60)
    assert by_id(plan)[1].target_micros > explored


def test_a_confident_loser_is_cut_even_on_partial_data():
    """Holding sits after retiring, not before.

    An offer that loses money even on the optimistic reading of incomplete
    data is not waiting for good news. The interval is already widened for
    the censoring, so if its upper bound is still under breakeven, it is bad.
    """
    loser = _immature(1, "bad and early", 4000, 5, 3000, 20, committed_usd=100)
    plan = allocate_portfolio([loser], usd_to_micros(500), rng=random.Random(1))
    allocation = by_id(plan)[1]
    assert allocation.verdict == "retire"
    assert allocation.target_micros == 0


# --- where budget meets creative supply ------------------------------------


def _exhausted(position_):
    position_.creative_supply_exhausted = True
    return position_


def test_an_offer_with_no_arguments_left_is_harvested_not_scaled():
    """It still makes money. It is just not going to make more.

    The return being read was earned by ads that are wearing out and cannot be
    replaced, so scaling against it commits money at a number that is about to
    stop being true.
    """
    spent = _exhausted(
        position(1, "no creative left", 4000, 400, 1000, 20, committed_usd=60)
    )
    plan = allocate_portfolio([spent], usd_to_micros(500), rng=random.Random(1))

    allocation = by_id(plan)[1]
    assert allocation.verdict == "harvest"
    assert allocation.target_micros == usd_to_micros(60)
    assert "no argument left" in allocation.reason
    assert allocation.roas_mean > 2.0


def test_a_harvested_offer_is_not_retired():
    """Stopping a profitable offer because it has no upside left is worse
    than taking the profit while it lasts."""
    spent = _exhausted(position(1, "spent", 4000, 400, 1000, 20, committed_usd=60))
    plan = allocate_portfolio([spent], usd_to_micros(500), rng=random.Random(1))
    assert by_id(plan)[1].target_micros > 0


def test_an_exhausted_offer_that_loses_money_is_still_retired():
    """Retire comes first. Harvesting a loser is just losing money slower."""
    loser = _exhausted(position(1, "spent loser", 4000, 15, 2000, 20, committed_usd=60))
    plan = allocate_portfolio([loser], usd_to_micros(500), rng=random.Random(1))
    allocation = by_id(plan)[1]
    assert allocation.verdict == "retire"
    assert allocation.target_micros == 0


def test_the_budget_a_harvested_offer_does_not_take_goes_elsewhere():
    policy = PortfolioPolicy(max_daily_change=10.0)
    spent = _exhausted(position(1, "spent", 4000, 400, 1000, 20, committed_usd=50))
    fresh = position(2, "room to grow", 4000, 300, 1000, 20, committed_usd=50)

    plan = allocate_portfolio(
        [spent, fresh], usd_to_micros(400), policy=policy, rng=random.Random(3)
    )
    allocations = by_id(plan)
    assert allocations[1].target_micros == usd_to_micros(50)
    # The offer that can still be improved takes what the spent one cannot.
    assert allocations[2].target_micros > usd_to_micros(50)


def test_an_exhausted_offer_already_stopped_is_not_restarted():
    spent = _exhausted(position(1, "spent", 4000, 400, 1000, 20, committed_usd=0))
    plan = allocate_portfolio([spent], usd_to_micros(500), rng=random.Random(1))
    assert by_id(plan)[1].verdict != "harvest"


def test_creative_supply_is_read_from_the_rotation_analysis(session, offer, settings):
    """The bridge itself: a real exhausted offer is marked as one."""
    from adgenie.core.portfolio import annotate_creative_supply
    from tests.test_rotation import run_angle
    from adgenie.core.angles import ANGLES

    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="c",
        external_id="c1", status=EntityStatus.ACTIVE,
        daily_budget_micros=usd_to_micros(60),
    )
    session.add(campaign)
    session.commit()
    group = AdGroup(
        campaign_id=campaign.id, name="g", external_id="g1",
        status=EntityStatus.ACTIVE,
    )
    session.add(group)
    session.commit()

    # Every argument in the library tried, and every one worn out.
    for angle in ANGLES:
        run_angle(session, offer, group, angle.key, 3, 0.020, 0.005, 0.03)

    day = date(2026, 3, 1)
    positions = load_offer_positions(session, day, day)
    annotate_creative_supply(
        session, positions, now=datetime(2026, 2, 5, tzinfo=timezone.utc)
    )
    assert positions[0].creative_supply_exhausted is True
    assert positions[0].angles_available == 0


def test_an_offer_with_angles_left_is_not_marked_exhausted(session, offer, settings):
    from adgenie.core.portfolio import annotate_creative_supply
    from tests.test_rotation import run_angle

    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="c",
        external_id="c1", status=EntityStatus.ACTIVE,
        daily_budget_micros=usd_to_micros(60),
    )
    session.add(campaign)
    session.commit()
    group = AdGroup(
        campaign_id=campaign.id, name="g", external_id="g1",
        status=EntityStatus.ACTIVE,
    )
    session.add(group)
    session.commit()
    run_angle(session, offer, group, "problem_solution", 3, 0.02, 0.019, 0.04)

    day = date(2026, 3, 1)
    positions = load_offer_positions(session, day, day)
    annotate_creative_supply(
        session, positions, now=datetime(2026, 2, 5, tzinfo=timezone.utc)
    )
    assert positions[0].creative_supply_exhausted is False
    assert positions[0].angles_available > 0


def test_the_creative_check_changes_the_plan_and_can_be_switched_off(
    session, offer, settings
):
    """Both directions, so the switch is shown to matter.

    With the check on, an offer that has run out of arguments is capped at
    what it already spends. With it off, the allocator sees only the numbers
    and scales it — which is exactly the mistake the connection prevents.
    """
    from adgenie.core.angles import ANGLES
    from tests.test_rotation import run_angle

    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="c",
        external_id="c1", status=EntityStatus.ACTIVE,
        daily_budget_micros=usd_to_micros(60),
    )
    session.add(campaign)
    session.commit()
    group = AdGroup(
        campaign_id=campaign.id, name="g", external_id="g1",
        status=EntityStatus.ACTIVE,
    )
    session.add(group)
    session.commit()

    # Every argument tried, every one worn out, and the offer still profitable.
    for angle in ANGLES:
        run_angle(session, offer, group, angle.key, 3, 0.020, 0.005, 0.03)
    # Delivery is recorded against the creatives; the offer window reads its
    # campaigns, so mirror the totals up to campaign level.
    for row in session.execute(
        select(
            MetricSnapshot.day,
            func.sum(MetricSnapshot.impressions),
            func.sum(MetricSnapshot.clicks),
            func.sum(MetricSnapshot.spend_micros),
        )
        .where(MetricSnapshot.level == EntityLevel.CREATIVE)
        .group_by(MetricSnapshot.day)
    ).all():
        session.add(
            MetricSnapshot(
                level=EntityLevel.CAMPAIGN, entity_id=campaign.id, day=row[0],
                impressions=int(row[1]), clicks=int(row[2]),
                spend_micros=int(row[3]),
            )
        )
    session.commit()

    since, until = date(2026, 1, 1), date(2026, 1, 30)
    # Inside the rest window: the worn-out angles are not back yet.
    now = datetime(2026, 2, 5, tzinfo=timezone.utc)
    allocator = PortfolioAllocator(session, settings=settings, rng=random.Random(1))

    with_check = allocator.plan(
        since, until, total_micros=usd_to_micros(500), now=now
    )
    assert by_id(with_check)[offer.id].verdict == "harvest"
    assert by_id(with_check)[offer.id].target_micros == usd_to_micros(60)

    without = allocator.plan(
        since, until, total_micros=usd_to_micros(500),
        check_creative_supply=False, now=now,
    )
    assert by_id(without)[offer.id].verdict == "fund"
    assert by_id(without)[offer.id].target_micros > usd_to_micros(60)


def test_harvesting_lifts_itself_once_the_rested_angles_come_back(
    session, offer, settings
):
    """Exhaustion is a state with an expiry date, not a death sentence.

    Every angle worn out means nothing left to say *today*. Thirty days on,
    the rested arguments are due back and the offer can be funded again — so
    the same data gives a different answer depending on when it is asked, and
    that is the correct behaviour rather than a bug in the clock.
    """
    from adgenie.core.angles import ANGLES
    from adgenie.core.portfolio import annotate_creative_supply
    from tests.test_rotation import run_angle

    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="c",
        external_id="c1", status=EntityStatus.ACTIVE,
        daily_budget_micros=usd_to_micros(60),
    )
    session.add(campaign)
    session.commit()
    group = AdGroup(
        campaign_id=campaign.id, name="g", external_id="g1",
        status=EntityStatus.ACTIVE,
    )
    session.add(group)
    session.commit()
    for angle in ANGLES:
        run_angle(session, offer, group, angle.key, 3, 0.020, 0.005, 0.03)

    day = date(2026, 1, 30)

    def exhausted_at(when: datetime) -> bool:
        positions = load_offer_positions(session, day, day)
        annotate_creative_supply(session, positions, now=when)
        return positions[0].creative_supply_exhausted

    assert exhausted_at(datetime(2026, 2, 5, tzinfo=timezone.utc)) is True
    assert exhausted_at(datetime(2026, 4, 1, tzinfo=timezone.utc)) is False


# --- writing the budget where it actually lives ----------------------------


def _profitable(session, offer, campaign, clicks=3000, conversions=150):
    day = date(2026, 3, 1)
    _deliver(session, campaign, day, clicks=clicks, spend_usd=600)
    for i in range(conversions):
        session.add(
            Conversion(
                offer_id=offer.id, campaign_id=campaign.id,
                status=ConversionStatus.APPROVED,
                revenue_micros=usd_to_micros(40),
                occurred_at=day, network_txn_id=f"{campaign.id}-{i}",
            )
        )
    session.commit()
    return day


def test_a_campaign_budget_is_written_on_the_campaign(session, offer, settings):
    campaign = _campaign(session, offer, "cbo", 40)
    group = AdGroup(
        campaign_id=campaign.id, name="g", external_id="g1",
        status=EntityStatus.ACTIVE, daily_budget_micros=0,
    )
    session.add(group)
    day = _profitable(session, offer, campaign)

    allocator = PortfolioAllocator(session, settings=settings, rng=random.Random(1))
    plan = allocator.plan(day, day, total_micros=usd_to_micros(500))
    result = allocator.apply(plan)

    assert [c["level"] for c in result["changes"]] == ["campaign"]
    session.refresh(campaign)
    assert campaign.daily_budget_micros == usd_to_micros(60)


def test_an_ad_set_budget_is_not_turned_into_a_campaign_budget(
    session, offer, settings
):
    """On Meta that is ABO becoming CBO — a structural change that resets
    learning across every ad set, dressed up as a budget adjustment."""
    campaign = _campaign(session, offer, "abo", 0)
    groups = [
        AdGroup(
            campaign_id=campaign.id, name=f"g{i}", external_id=f"g{i}",
            status=EntityStatus.ACTIVE,
            daily_budget_micros=usd_to_micros(budget),
        )
        for i, budget in enumerate((30, 10))
    ]
    session.add_all(groups)
    day = _profitable(session, offer, campaign)

    allocator = PortfolioAllocator(session, settings=settings, rng=random.Random(1))
    plan = allocator.plan(day, day, total_micros=usd_to_micros(500))
    assert by_id(plan)[offer.id].target_micros == usd_to_micros(60)
    result = allocator.apply(plan)

    session.refresh(campaign)
    assert campaign.daily_budget_micros == 0
    assert {c["level"] for c in result["changes"]} == {"ad_group"}

    for group in groups:
        session.refresh(group)
    # Split in the proportion they already ran at, and summing to the target.
    assert [g.daily_budget_micros for g in groups] == [
        usd_to_micros(45), usd_to_micros(15)
    ]
    assert sum(g.daily_budget_micros for g in groups) == usd_to_micros(60)


def test_the_parts_always_sum_back_to_the_target(session, offer, settings):
    """Rounding three ways must not quietly underspend the plan."""
    campaign = _campaign(session, offer, "abo", 0)
    groups = [
        AdGroup(
            campaign_id=campaign.id, name=f"g{i}", external_id=f"g{i}",
            status=EntityStatus.ACTIVE, daily_budget_micros=usd_to_micros(10),
        )
        for i in range(3)
    ]
    session.add_all(groups)
    day = _profitable(session, offer, campaign)

    allocator = PortfolioAllocator(session, settings=settings, rng=random.Random(1))
    plan = allocator.plan(day, day, total_micros=usd_to_micros(500))
    target = by_id(plan)[offer.id].target_micros
    allocator.apply(plan)

    for group in groups:
        session.refresh(group)
    assert sum(g.daily_budget_micros for g in groups) == target


def test_a_campaign_with_no_budget_anywhere_is_left_alone(session, offer, settings):
    campaign = _campaign(session, offer, "empty", 0)
    session.add(
        AdGroup(
            campaign_id=campaign.id, name="g", external_id="g1",
            status=EntityStatus.ACTIVE, daily_budget_micros=0,
        )
    )
    day = _profitable(session, offer, campaign)

    allocator = PortfolioAllocator(session, settings=settings, rng=random.Random(1))
    plan = allocator.plan(day, day, total_micros=usd_to_micros(500))
    result = allocator.apply(plan)
    assert result["changes"] == []


def test_the_level_written_is_the_level_that_was_measured(session, offer, settings):
    """The plan's "from" figure is the larger of the campaign budget and its
    ad sets' total. Writing the "to" figure anywhere else leaves the two
    disagreeing and the plan describing a move that did not happen.
    """
    campaign = _campaign(session, offer, "both", 40)
    groups = [
        AdGroup(
            campaign_id=campaign.id, name=f"g{i}", external_id=f"g{i}",
            status=EntityStatus.ACTIVE, daily_budget_micros=usd_to_micros(30),
        )
        for i in range(2)
    ]
    session.add_all(groups)
    day = _profitable(session, offer, campaign)

    # The ad sets commit $60 between them, above the campaign's own $40, so
    # $60 is what the plan reports and the ad sets are what governs spend.
    positions = load_offer_positions(session, day, day)
    assert positions[0].committed_micros == usd_to_micros(60)

    allocator = PortfolioAllocator(session, settings=settings, rng=random.Random(1))
    plan = allocator.plan(day, day, total_micros=usd_to_micros(500))
    allocation = by_id(plan)[offer.id]
    assert allocation.current_micros == usd_to_micros(60)
    result = allocator.apply(plan)

    assert {c["level"] for c in result["changes"]} == {"ad_group"}
    for group in groups:
        session.refresh(group)
    assert sum(g.daily_budget_micros for g in groups) == allocation.target_micros
    session.refresh(campaign)
    assert campaign.daily_budget_micros == usd_to_micros(40)
