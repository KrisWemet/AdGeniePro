"""Funnels: pricing a lead before you know what it was worth."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from adgenie.core.ltv import (
    DEFAULT_LEAD_CURVE,
    LeadValueModel,
    fit_lead_value,
    offer_prior_micros,
    realised_value,
)
from adgenie.core.metrics import PerformanceWindow, load_performance
from adgenie.core.optimizer import Optimizer, OptimizerPolicy
from adgenie.core.tracking import (
    TrackingContext,
    encode_subid,
    hash_email,
    record_click,
    record_funnel_event,
    record_lead,
)
from adgenie.models import (
    ActionType,
    ConversionStatus,
    EntityLevel,
    FunnelStep,
    FunnelStepKind,
    Lead,
)
from adgenie.money import usd_to_micros

BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1"
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture
def funnel_offer(session, offer):
    """A lead magnet, a $17 tripwire and the $40 affiliate offer."""
    for index, (key, kind, value) in enumerate(
        [
            ("optin", FunnelStepKind.OPTIN, 0),
            ("tripwire", FunnelStepKind.TRIPWIRE, 17),
            ("core", FunnelStepKind.CORE, 40),
        ]
    ):
        session.add(
            FunnelStep(
                offer_id=offer.id, key=key, name=key.title(), kind=kind,
                position=index, value_micros=usd_to_micros(value),
            )
        )
    session.commit()
    session.refresh(offer)
    return offer


# --- configuration ---------------------------------------------------------


def test_an_offer_knows_whether_it_runs_a_funnel(session, offer, funnel_offer):
    assert funnel_offer.has_funnel
    assert [s.key for s in funnel_offer.funnel_steps] == ["optin", "tripwire", "core"]
    assert funnel_offer.funnel_steps[0].captures_lead
    assert not funnel_offer.funnel_steps[1].captures_lead


def test_a_direct_offer_has_no_funnel(session, offer):
    assert not offer.has_funnel


def test_the_prior_comes_from_the_funnel_economics(session, funnel_offer):
    """Before any lead exists, price one from the steps rather than a constant."""
    prior = offer_prior_micros(session, funnel_offer.id)
    # A $17 tripwire and a $40 core at conservative take rates.
    assert usd_to_micros(1.0) < prior < usd_to_micros(3.0)


def test_the_prior_is_zero_without_a_funnel(session, offer):
    assert offer_prior_micros(session, offer.id) == 0


# --- lead capture ----------------------------------------------------------


def test_an_email_is_stored_only_as_a_hash(settings):
    digest = hash_email("Person@Example.com ", salt=settings.secret_key)
    assert "@" not in digest
    assert len(digest) == 64
    # Case and whitespace must not create two identities for one person.
    assert digest == hash_email("person@example.com", salt=settings.secret_key)


def test_an_optin_is_credited_to_the_ad_that_earned_it(session, funnel_offer):
    click, _ = record_click(
        session,
        encode_subid(TrackingContext(funnel_offer.id, None, None, 7)),
        user_agent=BROWSER_UA,
    )
    session.commit()

    lead, method = record_lead(
        session, offer_id=funnel_offer.id, email="a@b.test", click_id=click.click_id
    )
    session.commit()
    assert method == "click_id"
    assert lead.creative_id == 7
    assert lead.email_hash


def test_the_same_person_twice_is_one_lead(session, funnel_offer):
    record_lead(session, offer_id=funnel_offer.id, email="a@b.test")
    session.commit()
    second, method = record_lead(session, offer_id=funnel_offer.id, email="A@B.test")
    session.commit()

    assert method == "duplicate"
    assert session.query(Lead).count() == 1


def test_an_unattributed_optin_is_still_recorded(session, funnel_offer):
    """A lead with no traceable click is worth having and worth counting."""
    lead, method = record_lead(session, offer_id=funnel_offer.id, email="x@y.test")
    session.commit()
    assert method == "unmatched"
    assert lead.creative_id is None
    assert lead.extra["attribution_method"] == "unmatched"


def test_a_lead_needs_an_address_of_some_kind(session, funnel_offer):
    with pytest.raises(ValueError, match="email"):
        record_lead(session, offer_id=funnel_offer.id)


# --- funnel events ---------------------------------------------------------


def test_a_step_takes_its_value_from_the_funnel(session, funnel_offer):
    conversion, _ = record_funnel_event(
        session,
        offer_id=funnel_offer.id,
        step_key="tripwire",
        network_txn_id="t1",
        email="a@b.test",
    )
    session.commit()
    assert conversion.revenue_micros == usd_to_micros(17)
    assert conversion.step_key == "tripwire"
    assert conversion.lead_id is not None


def test_an_explicit_amount_overrides_the_step_value(session, funnel_offer):
    conversion, _ = record_funnel_event(
        session, offer_id=funnel_offer.id, step_key="core",
        network_txn_id="t2", revenue_micros=usd_to_micros(55),
    )
    session.commit()
    assert conversion.revenue_micros == usd_to_micros(55)


def test_an_unknown_step_is_refused(session, funnel_offer):
    with pytest.raises(ValueError, match="no funnel step"):
        record_funnel_event(
            session, offer_id=funnel_offer.id, step_key="nonsense", network_txn_id="t3"
        )


def test_revenue_accumulates_onto_the_lead(session, funnel_offer):
    record_funnel_event(
        session, offer_id=funnel_offer.id, step_key="tripwire",
        network_txn_id="t1", email="a@b.test",
    )
    record_funnel_event(
        session, offer_id=funnel_offer.id, step_key="core",
        network_txn_id="t2", email="a@b.test",
    )
    session.commit()

    lead = session.query(Lead).one()
    assert lead.realised_value_micros == usd_to_micros(57)
    assert realised_value(session, lead.id) == usd_to_micros(57)


def test_a_pending_step_does_not_credit_the_lead(session, funnel_offer):
    record_funnel_event(
        session, offer_id=funnel_offer.id, step_key="core", network_txn_id="t1",
        email="a@b.test", status=ConversionStatus.PENDING,
    )
    session.commit()
    assert session.query(Lead).one().realised_value_micros == 0


def test_every_step_attributes_back_to_the_same_ad(session, funnel_offer):
    """An opt-in, a tripwire and a commission are one click's work."""
    click, _ = record_click(
        session,
        encode_subid(TrackingContext(funnel_offer.id, None, None, 9)),
        user_agent=BROWSER_UA,
    )
    session.commit()

    for step, txn in (("tripwire", "a"), ("core", "b")):
        conversion, _ = record_funnel_event(
            session, offer_id=funnel_offer.id, step_key=step,
            network_txn_id=txn, click_id=click.click_id, email="a@b.test",
        )
        assert conversion.creative_id == 9
    session.commit()


# --- lead value ------------------------------------------------------------


def _seed_leads(session, offer, count, value_usd, age_days, start_txn=0, spread=True):
    """Seed a lead cohort.

    Real lead value is heavily skewed: most leads buy nothing and a few buy a
    lot. `spread` reproduces that so the interval has something to describe.
    """
    for i in range(count):
        if spread:
            # One lead in five buys, at five times the average.
            value = value_usd * 5 if i % 5 == 0 else 0.0
        else:
            value = value_usd
        session.add(
            Lead(
                offer_id=offer.id,
                creative_id=1,
                email_hash=f"hash{start_txn + i}",
                created_at=(NOW - timedelta(days=age_days)).replace(tzinfo=None),
                realised_value_micros=usd_to_micros(value),
            )
        )
    session.flush()


def test_with_no_leads_the_prior_stands(session, funnel_offer):
    prior = offer_prior_micros(session, funnel_offer.id)
    model = fit_lead_value(session, funnel_offer.id, prior_micros=prior, as_of=NOW)
    assert model.mean_micros == prior
    assert not model.fitted


def test_young_leads_are_projected_not_averaged(session, funnel_offer):
    """A lead captured yesterday has barely earned; averaging it understates all."""
    _seed_leads(session, funnel_offer, 30, value_usd=1.0, age_days=1, spread=False)
    session.commit()

    model = fit_lead_value(session, funnel_offer.id, as_of=NOW)
    assert model.mature_sample_size == 0
    # A day in, roughly a fifth of the revenue has arrived, so the projection
    # must be several times what has been observed.
    assert model.mean_micros > usd_to_micros(3.0)
    assert model.lower_micros < model.mean_micros < model.upper_micros


def test_mature_leads_are_measured(session, funnel_offer):
    _seed_leads(session, funnel_offer, 60, value_usd=4.0, age_days=45, spread=False)
    session.commit()

    model = fit_lead_value(session, funnel_offer.id, as_of=NOW)
    assert model.fitted
    assert model.mature_sample_size == 60
    # With no prior supplied the sample stands on its own; shrinking toward an
    # unstated zero would understate every funnel.
    assert model.mean_micros == pytest.approx(usd_to_micros(4.0), rel=0.05)


def test_an_unstated_prior_does_not_drag_the_estimate_toward_zero(
    session, funnel_offer
):
    _seed_leads(session, funnel_offer, 60, value_usd=4.0, age_days=45, spread=False)
    session.commit()

    unprimed = fit_lead_value(session, funnel_offer.id, as_of=NOW)
    primed = fit_lead_value(
        session, funnel_offer.id, prior_micros=usd_to_micros(1.0), as_of=NOW
    )
    assert unprimed.mean_micros > primed.mean_micros


def test_a_thin_sample_is_shrunk_toward_the_prior(session, funnel_offer):
    """A value fitted on five leads is noise wearing a number."""
    _seed_leads(session, funnel_offer, 5, value_usd=50.0, age_days=45, spread=False)
    session.commit()

    prior = offer_prior_micros(session, funnel_offer.id)
    model = fit_lead_value(session, funnel_offer.id, prior_micros=prior, as_of=NOW)
    assert not model.fitted
    # Five lucky leads must not carry a $50 estimate.
    assert model.mean_micros < usd_to_micros(20.0)
    assert model.mean_micros > prior


def test_a_large_sample_outweighs_the_prior(session, funnel_offer):
    _seed_leads(session, funnel_offer, 400, value_usd=6.0, age_days=45, spread=False)
    session.commit()

    model = fit_lead_value(
        session, funnel_offer.id, prior_micros=usd_to_micros(0.5), as_of=NOW
    )
    assert model.mean_micros == pytest.approx(usd_to_micros(6.0), rel=0.15)


def test_the_lower_bound_is_what_gets_spent_against(session, funnel_offer):
    _seed_leads(session, funnel_offer, 100, value_usd=5.0, age_days=45)
    session.commit()

    model = fit_lead_value(session, funnel_offer.id, as_of=NOW)
    assert model.lower_micros < model.mean_micros
    assert model.value_of(100) < model.value_of(100, conservative=False)


def test_the_curve_shows_revenue_arriving_over_weeks():
    model = LeadValueModel()
    assert model.maturity(0) == 0.0
    assert 0.15 < model.maturity(1) < 0.35
    assert model.maturity(30) > 0.85
    assert model.maturity(60) == 1.0
    assert model.maturity(500) == 1.0


def test_the_curve_is_monotone():
    model = LeadValueModel()
    values = [model.maturity(d) for d in (0, 1, 3, 7, 14, 30, 60)]
    assert values == sorted(values)


# --- what it changes about decisions ---------------------------------------


def _funnel_window(clicks, leads, value_per_lead, spend, realised=0.0):
    w = PerformanceWindow(EntityLevel.CREATIVE, 1, date(2026, 3, 1), date(2026, 3, 7))
    w.clicks = clicks
    w.impressions = clicks * 70
    w.spend_micros = usd_to_micros(spend)
    w.revenue_micros = usd_to_micros(realised)
    w.offer_payout_micros = usd_to_micros(40)
    w.daily_budget_micros = usd_to_micros(40)
    w.maturity = 1.0
    w.effective_clicks = clicks
    w.leads = leads
    w.lead_value_per_lead_micros = usd_to_micros(value_per_lead)
    w.lead_value_micros = usd_to_micros(value_per_lead * leads)
    return w


def test_a_lead_campaign_is_not_judged_on_day_one_revenue():
    """The defect this module exists to prevent.

    $255 of tripwire revenue on $800 of spend reads as a 0.32 return. Counting
    the 300 leads it also bought, the campaign is above breakeven.
    """
    window = _funnel_window(1000, leads=300, value_per_lead=2.10, spend=800, realised=255)
    assert window.roas < 0.5
    assert window.pipeline_roas > 1.0

    decision = Optimizer(OptimizerPolicy()).evaluate(window, lifetime=window)
    assert decision.action is not ActionType.PAUSE


def test_expensive_leads_are_still_killed():
    """The fix must not disarm the kill rule, only stop it misreading a funnel."""
    window = _funnel_window(1000, leads=120, value_per_lead=0.90, spend=800)
    decision = Optimizer(OptimizerPolicy()).evaluate(window, lifetime=window)
    assert decision.action is ActionType.PAUSE
    assert decision.rule == "funnel_unprofitable"
    assert "conservative lead value" in decision.reason


def test_cheap_leads_are_scaled():
    window = _funnel_window(1000, leads=400, value_per_lead=3.50, spend=800)
    decision = Optimizer(OptimizerPolicy()).evaluate(window, lifetime=window)
    assert decision.action is ActionType.INCREASE_BUDGET
    assert decision.rule == "funnel_scale"


def test_too_few_leads_to_price(): 
    window = _funnel_window(200, leads=8, value_per_lead=2.0, spend=160)
    decision = Optimizer(OptimizerPolicy()).evaluate(window, lifetime=window)
    assert decision.action is ActionType.NO_ACTION
    assert decision.rule == "funnel_learning"


def test_a_direct_offer_is_unaffected_by_any_of_this():
    """No leads means the ordinary rules apply, unchanged."""
    window = _funnel_window(600, leads=0, value_per_lead=0, spend=600)
    window.conversions = 6
    window.revenue_micros = 6 * usd_to_micros(40)
    assert not window.has_funnel_value

    decision = Optimizer(OptimizerPolicy()).evaluate(window, lifetime=window)
    assert decision.rule != "funnel_hold"


def test_cost_per_lead_and_pipeline_are_reported():
    window = _funnel_window(1000, leads=250, value_per_lead=2.0, spend=500)
    evidence = window.as_dict()
    assert evidence["leads"] == 250
    assert evidence["cost_per_lead_usd"] == pytest.approx(2.0)
    assert evidence["lead_value_usd"] == pytest.approx(500.0)
    assert evidence["pipeline_roas"] == pytest.approx(1.0)


# --- through the metrics layer ---------------------------------------------


def test_load_performance_prices_the_leads_it_finds(session, funnel_offer):
    from adgenie.models import AdGroup, Campaign, Creative, MetricSnapshot, Platform

    campaign = Campaign(
        offer_id=funnel_offer.id, platform=Platform.META, name="c", external_id="c1"
    )
    session.add(campaign)
    session.flush()
    group = AdGroup(campaign_id=campaign.id, name="g", external_id="g1")
    session.add(group)
    session.flush()
    creative = Creative(ad_group_id=group.id, name="ad", external_id="a1")
    session.add(creative)
    session.flush()

    session.add(
        MetricSnapshot(
            level=EntityLevel.CREATIVE, entity_id=creative.id, day=date(2026, 3, 2),
            impressions=20_000, clicks=400, spend_micros=usd_to_micros(300),
        )
    )
    for i in range(40):
        session.add(
            Lead(
                offer_id=funnel_offer.id, creative_id=creative.id,
                email_hash=f"h{i}", created_at=datetime(2026, 3, 2, 12),
            )
        )
    session.commit()

    window = load_performance(
        session, EntityLevel.CREATIVE, creative.id, date(2026, 3, 1), date(2026, 3, 7)
    )
    assert window.leads == 40
    assert window.lead_value_per_lead_micros > 0
    assert window.pipeline_roas > window.roas
