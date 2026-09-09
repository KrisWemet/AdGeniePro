"""Optimizer decisions. Each test states the media-buying judgment being encoded."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from adgenie.core.metrics import PerformanceWindow, apply_pooled_prior, pooled_prior
from adgenie.core.optimizer import Optimizer, OptimizerPolicy, allocate_budget
from adgenie.models import ActionType, EntityLevel
from adgenie.money import usd_to_micros

PAYOUT = usd_to_micros(40)


def window(
    clicks=0,
    conversions=0,
    spend_usd=0.0,
    *,
    entity_id=1,
    payout_micros=PAYOUT,
    budget_usd=25.0,
    frequency=1.0,
    impressions=None,
) -> PerformanceWindow:
    w = PerformanceWindow(
        level=EntityLevel.CREATIVE,
        entity_id=entity_id,
        since=date(2026, 3, 1),
        until=date(2026, 3, 7),
    )
    w.clicks = clicks
    w.conversions = conversions
    w.impressions = impressions if impressions is not None else clicks * 70
    w.spend_micros = usd_to_micros(spend_usd)
    w.offer_payout_micros = payout_micros
    w.revenue_micros = conversions * payout_micros
    w.daily_budget_micros = usd_to_micros(budget_usd)
    w.frequency = frequency
    return w


@pytest.fixture
def optimizer() -> Optimizer:
    return Optimizer(OptimizerPolicy(), rng=random.Random(7))


# --- derived metrics -------------------------------------------------------


def test_roas_uses_network_revenue_not_platform_conversions():
    w = window(clicks=200, conversions=5, spend_usd=100)
    w.platform_conversions = 40.0
    w.platform_conversion_value_micros = usd_to_micros(1600)
    assert w.roas == pytest.approx(2.0)
    assert w.platform_roas == pytest.approx(16.0)
    assert w.attribution_gap == pytest.approx(0.875)


def test_breakeven_cvr_is_cpc_over_payout():
    w = window(clicks=100, conversions=0, spend_usd=100)  # $1.00 CPC
    assert w.breakeven_cvr == pytest.approx(1.0 / 40.0)


def test_epc_and_profit():
    w = window(clicks=100, conversions=4, spend_usd=80)
    assert w.epc_micros == pytest.approx(usd_to_micros(1.60))
    assert w.profit_micros == usd_to_micros(80)


def test_roas_interval_brackets_the_point_estimate():
    w = window(clicks=400, conversions=16, spend_usd=400)
    interval = w.roas_interval(0.90)
    assert interval.lower < w.roas < interval.upper


# --- kill rules ------------------------------------------------------------


def test_zero_conversion_kill_fires_once_the_evidence_is_there(optimizer):
    decision = optimizer.evaluate(window(clicks=250, conversions=0, spend_usd=250))
    assert decision.action is ActionType.PAUSE
    assert decision.rule == "zero_conversion_kill"
    assert decision.confidence > 0.9


def test_zero_conversion_kill_waits_for_enough_spend(optimizer):
    """One slow day is not evidence. $40 against a $40 payout proves nothing."""
    decision = optimizer.evaluate(window(clicks=40, conversions=0, spend_usd=40))
    assert decision.action is not ActionType.PAUSE


def test_lifetime_evidence_survives_a_rolling_window(optimizer):
    """A losing ad must not get a clean slate because the window moved on.

    The recent window alone looks inconclusive; the lifetime record does not.
    """
    recent = window(clicks=45, conversions=0, spend_usd=40)
    lifetime = window(clicks=300, conversions=0, spend_usd=280)

    assert optimizer.evaluate(recent).action is not ActionType.PAUSE
    decision = optimizer.evaluate(recent, lifetime=lifetime)
    assert decision.action is ActionType.PAUSE
    assert "lifetime" in decision.reason


def test_unprofitable_kill_needs_the_upper_bound_below_breakeven(optimizer):
    decision = optimizer.evaluate(window(clicks=600, conversions=6, spend_usd=600))
    assert decision.action is ActionType.PAUSE
    assert decision.rule == "unprofitable_kill"


def test_a_bad_looking_but_uncertain_ad_is_not_killed(optimizer):
    """Same measured ROAS, far less data. The correct answer is 'wait'."""
    decision = optimizer.evaluate(window(clicks=45, conversions=1, spend_usd=45))
    assert decision.action is not ActionType.PAUSE


def test_compliance_block_overrides_every_performance_rule(optimizer):
    decision = optimizer.evaluate(
        window(clicks=900, conversions=90, spend_usd=300), compliance_blocked=True
    )
    assert decision.action is ActionType.PAUSE
    assert decision.rule == "compliance_block"
    assert decision.confidence == 1.0


# --- scale and throttle ----------------------------------------------------


def test_winner_is_scaled_by_the_configured_step(optimizer):
    decision = optimizer.evaluate(window(clicks=400, conversions=25, spend_usd=300))
    assert decision.action is ActionType.INCREASE_BUDGET
    assert decision.payload["to_micros"] == usd_to_micros(30.0)


def test_scaling_requires_the_lower_bound_to_clear_breakeven(optimizer):
    """A high mean ROAS on thin data is luck until proven otherwise."""
    thin = window(clicks=25, conversions=3, spend_usd=25)
    assert thin.roas > 4.0
    assert optimizer.evaluate(thin).action is not ActionType.INCREASE_BUDGET


def test_marginal_performance_is_throttled_not_killed(optimizer):
    decision = optimizer.evaluate(window(clicks=3000, conversions=78, spend_usd=3000))
    assert decision.action is ActionType.DECREASE_BUDGET
    assert decision.rule == "throttle_marginal"


def test_budget_increase_respects_the_cap():
    policy = OptimizerPolicy(max_daily_budget_micros=usd_to_micros(27))
    decision = Optimizer(policy).evaluate(
        window(clicks=400, conversions=25, spend_usd=300)
    )
    assert decision.payload["to_micros"] == usd_to_micros(27)


def test_no_action_when_already_at_the_cap():
    policy = OptimizerPolicy(max_daily_budget_micros=usd_to_micros(25))
    decision = Optimizer(policy).evaluate(
        window(clicks=400, conversions=25, spend_usd=300, budget_usd=25)
    )
    assert decision.action is ActionType.NO_ACTION
    assert decision.rule.endswith("_capped")


def test_large_budget_moves_require_human_approval():
    policy = OptimizerPolicy(auto_apply_budget_ceiling_micros=usd_to_micros(5))
    decision = Optimizer(policy).evaluate(
        window(clicks=2000, conversions=140, spend_usd=1400, budget_usd=200)
    )
    assert decision.action is ActionType.INCREASE_BUDGET
    assert decision.requires_approval


def test_small_budget_moves_apply_automatically():
    decision = Optimizer(OptimizerPolicy()).evaluate(
        window(clicks=400, conversions=25, spend_usd=300, budget_usd=25)
    )
    assert not decision.requires_approval


# --- gating ----------------------------------------------------------------


def test_thin_data_produces_no_action(optimizer):
    decision = optimizer.evaluate(window(clicks=4, conversions=0, spend_usd=3))
    assert decision.action is ActionType.NO_ACTION
    assert decision.rule == "learning"


def test_cooldown_blocks_stacked_budget_changes(optimizer):
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    decision = optimizer.evaluate(
        window(clicks=400, conversions=25, spend_usd=300), last_action_at=recent
    )
    assert decision.rule == "cooldown"


def test_cooldown_expires(optimizer):
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    decision = optimizer.evaluate(
        window(clicks=400, conversions=25, spend_usd=300), last_action_at=old
    )
    assert decision.action is ActionType.INCREASE_BUDGET


def test_cooldown_uses_the_injected_clock(optimizer):
    """Back-tests advance simulated time, not wall-clock time."""
    acted = datetime(2026, 3, 1, tzinfo=timezone.utc)
    later = datetime(2026, 3, 5, tzinfo=timezone.utc)
    decision = optimizer.evaluate(
        window(clicks=400, conversions=25, spend_usd=300),
        last_action_at=acted,
        now=later,
    )
    assert decision.action is ActionType.INCREASE_BUDGET


def test_paused_entities_are_skipped(optimizer):
    decision = optimizer.evaluate(
        window(clicks=400, conversions=0, spend_usd=400), is_active=False
    )
    assert decision.rule == "inactive"


# --- creative fatigue ------------------------------------------------------


def test_high_frequency_triggers_new_creative(optimizer):
    decision = optimizer.evaluate(
        window(clicks=200, conversions=7, spend_usd=200, frequency=4.5)
    )
    assert decision.action is ActionType.GENERATE_VARIANTS
    assert decision.rule == "frequency_fatigue"


def test_ctr_decay_triggers_new_creative(optimizer):
    w = window(clicks=100, conversions=4, spend_usd=120, impressions=20_000)
    decision = optimizer.evaluate(w, opening_ctr=0.012)
    assert decision.action is ActionType.GENERATE_VARIANTS
    assert decision.rule == "ctr_decay"


def test_stable_ctr_does_not_trigger_refresh(optimizer):
    w = window(clicks=100, conversions=4, spend_usd=120, impressions=20_000)
    decision = optimizer.evaluate(w, opening_ctr=0.0052)
    assert decision.action is not ActionType.GENERATE_VARIANTS


# --- evidence and auditability ---------------------------------------------


def test_every_decision_records_its_evidence(optimizer):
    for w in (
        window(clicks=400, conversions=25, spend_usd=300),
        window(clicks=250, conversions=0, spend_usd=250),
    ):
        decision = optimizer.evaluate(w)
        assert decision.evidence["roas"] is not None
        assert decision.evidence["clicks"] == w.clicks
        assert decision.reason
        assert decision.rule


def test_lifetime_evidence_is_attached_when_supplied(optimizer):
    decision = optimizer.evaluate(
        window(clicks=45, conversions=0, spend_usd=40),
        lifetime=window(clicks=300, conversions=0, spend_usd=280),
    )
    assert decision.evidence["lifetime"]["clicks"] == 300


# --- priors and allocation -------------------------------------------------


def test_pooled_prior_reflects_the_group_rate():
    windows = [window(clicks=500, conversions=15), window(clicks=500, conversions=25)]
    prior_a, prior_b = pooled_prior(windows, strength=25)
    assert prior_a / (prior_a + prior_b) == pytest.approx(0.04, abs=0.005)


def test_pooled_prior_falls_back_when_there_is_no_data():
    prior_a, prior_b = pooled_prior([window(clicks=3, conversions=0)], strength=25)
    assert prior_a / (prior_a + prior_b) == pytest.approx(0.02, abs=0.005)


def test_prior_shrinks_a_lucky_small_sample():
    lucky = window(clicks=10, conversions=2)
    apply_pooled_prior([lucky, window(clicks=1000, conversions=30)])
    assert lucky.cvr == pytest.approx(0.20)
    assert lucky.cvr_interval().mean < 0.08


def test_allocation_favours_the_proven_winner():
    windows = [
        window(entity_id=1, clicks=500, conversions=25),
        window(entity_id=2, clicks=500, conversions=5),
        window(entity_id=3, clicks=20, conversions=1),
    ]
    allocation = allocate_budget(
        windows, usd_to_micros(100), rng=random.Random(42)
    )
    assert allocation[1] > allocation[2]
    assert allocation[1] > allocation[3]


def test_allocation_keeps_funding_the_unproven():
    """A creative starved of budget never produces the data to justify budget."""
    windows = [
        window(entity_id=1, clicks=500, conversions=25),
        window(entity_id=2, clicks=15, conversions=0),
    ]
    allocation = allocate_budget(windows, usd_to_micros(100), rng=random.Random(1))
    assert allocation[2] >= usd_to_micros(5)


def test_allocation_never_exceeds_the_budget():
    windows = [window(entity_id=i, clicks=200, conversions=i) for i in range(1, 6)]
    total = usd_to_micros(120)
    allocation = allocate_budget(windows, total, rng=random.Random(3))
    assert sum(allocation.values()) <= total


def test_allocation_handles_empty_input():
    assert allocate_budget([], usd_to_micros(100)) == {}
    assert allocate_budget([window()], 0) == {}


def test_comparison_reports_the_probability_the_variant_wins(optimizer):
    result = optimizer.compare(
        window(entity_id=1, clicks=800, conversions=20),
        window(entity_id=2, clicks=800, conversions=40),
    )
    assert result["prob_variant_better"] > 0.95
    assert result["decisive"]


def test_comparison_is_not_decisive_on_thin_data(optimizer):
    result = optimizer.compare(
        window(entity_id=1, clicks=30, conversions=1),
        window(entity_id=2, clicks=30, conversions=2),
    )
    assert not result["decisive"]


def test_policy_reads_from_settings(settings):
    settings.target_roas = 2.5
    settings.scale_step = 0.4
    policy = OptimizerPolicy.from_settings(settings)
    assert policy.target_roas == 2.5
    assert policy.scale_step == 0.4
