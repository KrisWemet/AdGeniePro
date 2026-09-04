"""Conversion lag: treating incomplete data as incomplete, not as failure."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from adgenie.core.lag import (
    DEFAULT_LAG_CURVE,
    MIN_MATURITY_TO_JUDGE,
    LagModel,
    fit_lag_model,
)
from adgenie.core.metrics import PerformanceWindow, load_performance
from adgenie.core.optimizer import Optimizer, OptimizerPolicy
from adgenie.core.tracking import TrackingContext, encode_subid, record_click, record_conversion
from adgenie.models import ConversionStatus, EntityLevel, MetricSnapshot
from adgenie.money import usd_to_micros

NOW = datetime(2026, 3, 10, 12, tzinfo=timezone.utc)
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1"


# --- the curve -------------------------------------------------------------


def test_maturity_is_monotone_and_bounded():
    model = LagModel()
    previous = -1.0
    for hours in (0, 0.5, 1, 6, 24, 48, 72, 168, 336, 720, 5000):
        value = model.maturity(hours)
        assert 0.0 <= value <= 1.0
        assert value >= previous
        previous = value


def test_a_click_with_no_elapsed_time_counts_for_nothing():
    assert LagModel().maturity(0) == 0.0
    assert LagModel().maturity(-5) == 0.0


def test_a_month_old_click_is_fully_matured():
    assert LagModel().maturity(720) == 1.0
    assert LagModel().maturity(10_000) == 1.0


def test_maturity_interpolates_between_checkpoints():
    model = LagModel()
    at_1, at_6 = model.maturity(1.0), model.maturity(6.0)
    midpoint = model.maturity(3.5)
    assert at_1 < midpoint < at_6


def test_the_default_curve_reflects_direct_response_behaviour():
    model = LagModel()
    assert 0.3 <= model.maturity(1) <= 0.4, "a third convert in-session"
    assert 0.65 <= model.maturity(24) <= 0.75, "most within a day"
    assert model.maturity(168) >= 0.9, "nearly all within a week"


# --- weighting clicks ------------------------------------------------------


def test_recent_clicks_count_for_less_than_old_ones():
    model = LagModel()
    old = model.effective_clicks([(date(2026, 1, 1), 100)], NOW)
    new = model.effective_clicks([(date(2026, 3, 10), 100)], NOW)
    assert old == pytest.approx(100.0)
    assert new < 50.0


def test_window_maturity_is_the_weighted_share():
    model = LagModel()
    daily = [(date(2026, 3, 10), 100), (date(2026, 1, 1), 100)]
    maturity = model.window_maturity(daily, NOW)
    # Today's clicks contribute nothing yet; January's contribute in full.
    assert maturity == pytest.approx(0.5)
    assert maturity == pytest.approx(
        model.effective_clicks(daily, NOW) / 200, abs=1e-9
    )


def test_a_window_with_no_clicks_is_treated_as_mature():
    """Nothing outstanding means nothing to wait for."""
    assert LagModel().window_maturity([], NOW) == 1.0


def test_a_day_is_measured_from_its_middle():
    """Clicks arrive across a day; treating them as midnight overstates their age."""
    model = LagModel()
    from_midday = model.maturity_for_day(date(2026, 3, 10), NOW)
    assert from_midday == pytest.approx(model.maturity(0.0))


def test_projection_scales_observed_up_to_expected():
    model = LagModel()
    assert model.project(10, 0.5) == pytest.approx(20.0)
    assert model.project(0, 0.5) == 0.0
    # A near-zero maturity must not produce an unbounded projection.
    assert model.project(1, 0.0001) <= 1 / MIN_MATURITY_TO_JUDGE


# --- fitting ---------------------------------------------------------------


def _record_conversion_with_delay(session, offer, hours, txn):
    click, _ = record_click(
        session,
        encode_subid(TrackingContext(offer.id, None, None, 1)),
        user_agent=BROWSER_UA,
    )
    click.created_at = datetime(2026, 2, 1, 12)
    session.flush()
    record_conversion(
        session,
        network="cb",
        network_txn_id=txn,
        click_id=click.click_id,
        revenue_micros=usd_to_micros(40),
        status=ConversionStatus.APPROVED,
        occurred_at=datetime(2026, 2, 1, 12) + timedelta(hours=hours),
    )


def test_a_thin_history_falls_back_to_the_default(session, offer):
    for i in range(3):
        _record_conversion_with_delay(session, offer, 2, f"t{i}")
    session.commit()

    model = fit_lag_model(session, offer.id, as_of=datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert not model.fitted
    assert model.curve == DEFAULT_LAG_CURVE


def test_a_fast_converting_offer_learns_a_faster_curve(session, offer):
    """Everything converts within an hour, so a day-old click is nearly done."""
    for i in range(120):
        _record_conversion_with_delay(session, offer, 0.5, f"fast{i}")
    session.commit()

    model = fit_lag_model(session, offer.id, as_of=datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert model.fitted
    assert model.sample_size == 120
    assert model.median_hours == pytest.approx(0.5, abs=0.1)
    assert model.maturity(24) > LagModel().maturity(24)


def test_a_slow_converting_offer_learns_a_slower_curve(session, offer):
    """A trial offer confirms days later; a day-old click proves almost nothing."""
    for i in range(120):
        _record_conversion_with_delay(session, offer, 200, f"slow{i}")
    session.commit()

    model = fit_lag_model(session, offer.id, as_of=datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert model.fitted
    assert model.maturity(24) < LagModel().maturity(24)


def test_a_fitted_curve_stays_monotone(session, offer):
    for i in range(80):
        _record_conversion_with_delay(session, offer, i % 50, f"m{i}")
    session.commit()

    model = fit_lag_model(session, offer.id, as_of=datetime(2026, 3, 1, tzinfo=timezone.utc))
    values = [v for _, v in model.curve]
    assert values == sorted(values)
    assert values[-1] == 1.0


def test_reversed_conversions_do_not_shape_the_curve(session, offer):
    """A refund is not evidence about how quickly sales are reported."""
    click, _ = record_click(
        session, encode_subid(TrackingContext(offer.id, None, None, 1)),
        user_agent=BROWSER_UA,
    )
    click.created_at = datetime(2026, 2, 1, 12)
    session.flush()
    record_conversion(
        session, network="cb", network_txn_id="rev", click_id=click.click_id,
        revenue_micros=0, status=ConversionStatus.REVERSED,
        occurred_at=datetime(2026, 2, 1, 12),
    )
    session.commit()

    model = fit_lag_model(session, offer.id, min_samples=1)
    assert model.sample_size == 0


# --- what it changes about decisions ---------------------------------------


def _window(clicks, conversions, spend_usd, maturity, level=EntityLevel.CREATIVE):
    w = PerformanceWindow(level, 1, date(2026, 3, 1), date(2026, 3, 4))
    w.clicks = clicks
    w.conversions = conversions
    w.impressions = clicks * 70
    w.spend_micros = usd_to_micros(spend_usd)
    w.revenue_micros = conversions * usd_to_micros(40)
    w.offer_payout_micros = usd_to_micros(40)
    w.daily_budget_micros = usd_to_micros(40)
    w.maturity = maturity
    w.effective_clicks = clicks * maturity
    return w


def test_a_young_ad_with_no_conversions_yet_is_not_killed():
    """The defect this module exists to fix: killing winners mid-flight."""
    young = _window(200, 0, 160, maturity=0.35)
    decision = Optimizer(OptimizerPolicy()).evaluate(young, lifetime=young)
    assert decision.action.value != "pause"


def test_a_mature_ad_with_no_conversions_is_still_killed():
    """The fix must not disarm the kill rule, only delay it until it is fair."""
    mature = _window(600, 0, 480, maturity=0.97)
    decision = Optimizer(OptimizerPolicy()).evaluate(mature, lifetime=mature)
    assert decision.action.value == "pause"
    assert decision.rule == "zero_conversion_kill"


def test_barely_started_entities_report_why_they_are_being_left_alone():
    infant = _window(60, 0, 50, maturity=0.08)
    decision = Optimizer(OptimizerPolicy()).evaluate(infant, lifetime=infant)
    assert decision.rule == "awaiting_conversions"
    assert "in flight" in decision.reason


def test_scaling_waits_for_the_data_to_mature():
    """Never fund a forecast: a wrong scale spends money a wrong kill only forgoes."""
    policy = OptimizerPolicy()
    young_winner = _window(400, 22, 300, maturity=0.42)
    mature_winner = _window(400, 22, 300, maturity=0.95)

    assert Optimizer(policy).evaluate(young_winner).action.value != "increase_budget"
    assert Optimizer(policy).evaluate(mature_winner).action.value == "increase_budget"


def test_the_rate_is_estimated_on_matured_clicks_but_applied_to_all_of_them():
    """Scaling by matured clicks would write off the outstanding ones."""
    half_mature = _window(400, 10, 200, maturity=0.5)
    interval = half_mature.roas_interval(0.9)

    # 10 conversions on 200 matured clicks is a 5% rate; across all 400 clicks
    # that is 20 conversions at $40 on $200 of spend, so ROAS heads for 4.
    assert interval.mean == pytest.approx(4.0, rel=0.35)
    assert interval.mean > half_mature.roas


def test_immature_data_widens_the_interval():
    confident = _window(400, 12, 300, maturity=1.0)
    uncertain = _window(400, 12, 300, maturity=0.3)
    width = lambda w: w.cvr_interval(0.9).upper - w.cvr_interval(0.9).lower
    assert width(uncertain) > width(confident)


def test_a_decayed_winner_is_throttled_rather_than_retired():
    """One bad week does not undo a proven angle."""
    bad_week = _window(300, 0, 240, maturity=0.95)
    proven = _window(2000, 60, 1500, maturity=0.99)

    decision = Optimizer(OptimizerPolicy()).evaluate(bad_week, lifetime=proven)
    assert decision.action.value != "pause"
    assert decision.rule.startswith("decayed_winner")


def test_an_ad_that_never_worked_is_still_retired():
    bad_week = _window(300, 0, 240, maturity=0.95)
    never_worked = _window(2000, 0, 1600, maturity=0.99)
    decision = Optimizer(OptimizerPolicy()).evaluate(bad_week, lifetime=never_worked)
    assert decision.action.value == "pause"


def test_evidence_records_the_maturity_behind_the_decision():
    window = _window(300, 5, 240, maturity=0.55)
    evidence = window.as_dict()
    assert evidence["maturity"] == pytest.approx(0.55)
    assert evidence["effective_clicks"] == pytest.approx(165.0)
    assert evidence["projected_conversions"] > evidence["conversions"]
    assert evidence["projected_roas"] > evidence["roas"]


# --- through the metrics layer ---------------------------------------------


def test_load_performance_applies_the_lag_model(session, offer):
    for offset, clicks in ((0, 100), (5, 100)):
        session.add(
            MetricSnapshot(
                level=EntityLevel.CREATIVE,
                entity_id=1,
                day=date(2026, 3, 10) - timedelta(days=offset),
                impressions=clicks * 70,
                clicks=clicks,
                spend_micros=usd_to_micros(80),
            )
        )
    session.commit()

    window = load_performance(
        session, EntityLevel.CREATIVE, 1, date(2026, 3, 1), date(2026, 3, 10),
        as_of=NOW,
    )
    assert window.clicks == 200
    assert window.effective_clicks < 200
    assert 0.0 < window.maturity < 1.0
    # The five-day-old clicks are nearly done; today's have not started, so the
    # 200 raw clicks are worth roughly 89 trials of evidence.
    assert window.effective_clicks == pytest.approx(89.0, abs=3.0)
