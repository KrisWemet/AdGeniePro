"""End-to-end behaviour: launch, measure, attribute, optimize."""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

from adgenie.core.launcher import CampaignLauncher, LaunchPlan
from adgenie.core.metrics import load_performance
from adgenie.core.orchestrator import Orchestrator
from adgenie.core.tracking import (
    TrackingContext,
    encode_subid,
    record_click,
    record_conversion,
)
from adgenie.models import (
    ActionStatus,
    ActionType,
    AdGroup,
    AuditLog,
    Campaign,
    ComplianceVerdict,
    ConversionStatus,
    Creative,
    EntityLevel,
    EntityStatus,
    OptimizationAction,
    Platform,
)
from adgenie.money import usd_to_micros
from adgenie.platforms.sandbox import SandboxPlatform

BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1"


@pytest.fixture
def launched(session, offer, settings, sandbox_meta):
    launcher = CampaignLauncher(session, settings=settings, platform_client=sandbox_meta)
    result = launcher.launch(
        LaunchPlan(
            offer_id=offer.id,
            platform=Platform.META,
            daily_budget_usd=60.0,
            angle_count=3,
            creatives_per_angle=1,
            start_paused=False,
        )
    )
    return result


# --- launch ----------------------------------------------------------------


def test_launch_builds_the_full_hierarchy(launched, session, sandbox_meta):
    assert launched.errors == []
    assert len(launched.ad_group_ids) == 3
    assert len(launched.creative_ids) == 3

    campaign = session.get(Campaign, launched.campaign_id)
    assert campaign.external_id in sandbox_meta.entities
    assert campaign.status is EntityStatus.ACTIVE


def test_launch_gives_each_ad_group_a_distinct_angle(launched, session):
    creatives = session.execute(select(Creative)).scalars().all()
    assert len({c.angle for c in creatives}) == 3


def test_launch_splits_the_budget_across_ad_groups(launched, session):
    groups = session.execute(select(AdGroup)).scalars().all()
    assert sum(g.daily_budget_micros for g in groups) <= usd_to_micros(60)
    assert all(g.daily_budget_micros > 0 for g in groups)


def test_every_creative_gets_its_own_tracking_link(launched, session):
    creatives = session.execute(select(Creative)).scalars().all()
    urls = [c.final_url for c in creatives]
    assert len(set(urls)) == 3
    for creative in creatives:
        assert creative.final_url.startswith("https://track.test/r?s=")
        assert encode_subid(
            TrackingContext(
                offer_id=1, creative_id=creative.id, platform=Platform.META
            )
        ).split("-")[1] in creative.final_url


def test_launch_writes_an_audit_trail(launched, session):
    operations = [
        row.operation for row in session.execute(select(AuditLog)).scalars()
    ]
    assert operations.count("create_campaign") == 1
    assert operations.count("create_ad_group") == 3
    assert operations.count("create_creative") == 3


def test_launch_starts_paused_by_default(session, offer, settings, sandbox_meta):
    result = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta
    ).launch(
        LaunchPlan(offer_id=offer.id, platform=Platform.META, daily_budget_usd=20.0)
    )
    assert session.get(Campaign, result.campaign_id).status is EntityStatus.PAUSED


def test_blocked_copy_never_reaches_the_platform(
    session, offer, settings, sandbox_meta
):
    """A policy-violating creative is stored for review, not launched."""

    class BlockedGenerator:
        name = "stub"

        def generate(self, brief):
            from adgenie.core.copywriter import CreativeDraft

            return CreativeDraft(
                angle="x",
                headlines=["Guaranteed Cure"],
                descriptions=[],
                primary_texts=["Lose 40 pounds guaranteed. Doctors hate this. #ad"],
            )

    from adgenie.core.copywriter import CopyStudio

    launcher = CampaignLauncher(
        session,
        settings=settings,
        studio=CopyStudio(generator=BlockedGenerator(), settings=settings),
        platform_client=sandbox_meta,
    )
    result = launcher.launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META, daily_budget_usd=20.0,
            angle_count=1, start_paused=False,
        )
    )

    assert result.creative_ids == []
    assert len(result.blocked_creative_ids) == 1
    creative = session.get(Creative, result.blocked_creative_ids[0])
    assert creative.compliance_verdict is ComplianceVerdict.BLOCK
    assert creative.status is EntityStatus.PENDING_REVIEW
    assert creative.external_id is None
    assert not any(c[0] == "create_creative" for c in sandbox_meta.calls)


def test_launch_records_a_platform_failure_without_crashing(
    session, offer, settings
):
    sandbox = SandboxPlatform(Platform.META, fail_on={"create_ad_group"})
    result = CampaignLauncher(
        session, settings=settings, platform_client=sandbox
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META, daily_budget_usd=20.0,
            angle_count=2,
        )
    )
    assert len(result.errors) == 2
    assert result.creative_ids == []


def test_launch_rejects_an_unknown_offer(session, settings, sandbox_meta):
    launcher = CampaignLauncher(session, settings=settings, platform_client=sandbox_meta)
    with pytest.raises(ValueError, match="not found"):
        launcher.launch(
            LaunchPlan(offer_id=9999, platform=Platform.META, daily_budget_usd=10.0)
        )


# --- click tracking against real rows --------------------------------------


def test_click_resolves_parents_from_the_creative(launched, session, offer):
    creative_id = launched.creative_ids[0]
    creative = session.get(Creative, creative_id)
    subid = encode_subid(
        TrackingContext(offer.id, None, None, creative_id, Platform.META)
    )
    click, resolved = record_click(session, subid, user_agent=BROWSER_UA)
    session.commit()

    assert resolved.id == offer.id
    assert click.creative_id == creative_id
    assert click.ad_group_id == creative.ad_group_id
    assert click.campaign_id == launched.campaign_id


# --- the measurement loop --------------------------------------------------


def _simulate(session, sandbox, offer, days, rng, start=date(2026, 3, 1)):
    """Deliver, track clicks, and post conversions back, day by day."""
    orchestrator = Orchestrator(
        session, settings=None, platform_clients={Platform.META: sandbox}
    )
    for offset in range(days):
        day = start + timedelta(days=offset)
        sandbox.simulate_day(day)
        occurred = datetime.combine(day, time(12, 0))

        for (external_id, row_day), row in list(sandbox.insights.items()):
            if row_day != day or not row.clicks:
                continue
            creative = session.execute(
                select(Creative).where(Creative.external_id == external_id)
            ).scalar_one_or_none()
            if creative is None:
                continue
            subid = encode_subid(
                TrackingContext(offer.id, None, None, creative.id, Platform.META)
            )
            clicks = []
            for _ in range(row.clicks):
                click, _o = record_click(session, subid, user_agent=BROWSER_UA)
                click.created_at = occurred
                clicks.append(click)
            for click in rng.sample(clicks, min(int(row.conversions), len(clicks))):
                record_conversion(
                    session,
                    network=offer.network,
                    network_txn_id=f"{click.click_id}-1",
                    click_id=click.click_id,
                    revenue_micros=offer.payout_micros,
                    status=ConversionStatus.APPROVED,
                    occurred_at=occurred,
                )
        session.commit()
        orchestrator.sync_metrics(day, day)
    return orchestrator, start + timedelta(days=days - 1)


@pytest.fixture
def simulated(launched, session, offer, sandbox_meta, rng, settings):
    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: sandbox_meta}
    )
    start = date(2026, 3, 1)
    for offset in range(14):
        day = start + timedelta(days=offset)
        sandbox_meta.simulate_day(day)
        occurred = datetime.combine(day, time(12, 0))
        for (external_id, row_day), row in list(sandbox_meta.insights.items()):
            if row_day != day or not row.clicks:
                continue
            creative = session.execute(
                select(Creative).where(Creative.external_id == external_id)
            ).scalar_one_or_none()
            if creative is None:
                continue
            subid = encode_subid(
                TrackingContext(offer.id, None, None, creative.id, Platform.META)
            )
            clicks = []
            for _ in range(row.clicks):
                click, _o = record_click(session, subid, user_agent=BROWSER_UA)
                click.created_at = occurred
                clicks.append(click)
            for click in rng.sample(clicks, min(int(row.conversions), len(clicks))):
                record_conversion(
                    session,
                    network=offer.network,
                    network_txn_id=f"{click.click_id}-1",
                    click_id=click.click_id,
                    revenue_micros=offer.payout_micros,
                    status=ConversionStatus.APPROVED,
                    occurred_at=occurred,
                )
        session.commit()
        orchestrator.sync_metrics(day, day)
    return orchestrator, start, start + timedelta(days=13)


def test_metrics_sync_stores_daily_creative_rows(simulated, session, launched):
    _, since, until = simulated
    window = load_performance(
        session, EntityLevel.CREATIVE, launched.creative_ids[0], since, until
    )
    assert window.impressions > 0
    assert window.clicks > 0
    assert window.spend_micros > 0


def test_rollups_match_the_sum_of_their_children(simulated, session, launched):
    _, since, until = simulated
    campaign = load_performance(
        session, EntityLevel.CAMPAIGN, launched.campaign_id, since, until
    )
    children = [
        load_performance(session, EntityLevel.CREATIVE, cid, since, until)
        for cid in launched.creative_ids
    ]
    assert campaign.clicks == sum(c.clicks for c in children)
    assert campaign.spend_micros == sum(c.spend_micros for c in children)


def test_revenue_comes_from_network_conversions(simulated, session, launched):
    _, since, until = simulated
    total = sum(
        load_performance(session, EntityLevel.CREATIVE, cid, since, until).revenue_micros
        for cid in launched.creative_ids
    )
    assert total > 0


def test_pending_conversions_are_excluded_from_revenue(session, offer, launched):
    creative_id = launched.creative_ids[0]
    subid = encode_subid(
        TrackingContext(offer.id, None, None, creative_id, Platform.META)
    )
    click, _ = record_click(session, subid, user_agent=BROWSER_UA)
    click.created_at = datetime(2026, 3, 5, 9, 0)
    session.commit()
    record_conversion(
        session,
        network="x",
        network_txn_id="pending-1",
        click_id=click.click_id,
        revenue_micros=usd_to_micros(40),
        status=ConversionStatus.PENDING,
        occurred_at=datetime(2026, 3, 5, 12, 0),
    )
    session.commit()

    window = load_performance(
        session, EntityLevel.CREATIVE, creative_id, date(2026, 3, 1), date(2026, 3, 10)
    )
    assert window.revenue_micros == 0
    assert window.pending_conversions == 1


# --- optimization ----------------------------------------------------------


def test_cycle_proposes_actions_with_reasons(simulated, session):
    orchestrator, _, until = simulated
    result = orchestrator.run_cycle(
        lookback_days=7, apply=False, today=until + timedelta(days=1)
    )
    assert result["evaluated"] > 0
    assert result["dry_run"] is True
    for action in result["actions"]:
        assert action["reason"]
        assert action["rule"]


def test_dry_run_changes_nothing_on_the_platform(simulated, session, sandbox_meta):
    orchestrator, _, until = simulated
    before = len(sandbox_meta.calls)
    orchestrator.run_cycle(lookback_days=7, apply=False, today=until + timedelta(days=1))
    assert len(sandbox_meta.calls) == before
    assert all(
        a.status is ActionStatus.PROPOSED
        for a in session.execute(select(OptimizationAction)).scalars()
    )


def test_applying_actions_reaches_the_platform_and_the_database(
    simulated, session, sandbox_meta
):
    orchestrator, _, until = simulated
    result = orchestrator.run_cycle(
        lookback_days=7, apply=True, today=until + timedelta(days=1)
    )
    if not result["applied"]:
        pytest.skip("no auto-applicable action in this simulation")

    applied = [
        a
        for a in session.execute(select(OptimizationAction)).scalars()
        if a.status is ActionStatus.APPLIED
    ]
    assert applied
    assert all(a.applied_at is not None for a in applied)
    assert any(c[0] in ("set_status", "set_budget") for c in sandbox_meta.calls)


def test_a_paused_creative_stops_delivering(simulated, session, sandbox_meta):
    orchestrator, _, until = simulated
    creative = session.execute(select(Creative)).scalars().first()
    orchestrator._set_status(Platform.META, EntityLevel.CREATIVE, creative, active=False)
    session.commit()

    rows = sandbox_meta.simulate_day(until + timedelta(days=1))
    assert creative.external_id not in {r.external_id for r in rows}


def test_run_is_recorded_with_a_summary(simulated, session):
    orchestrator, _, until = simulated
    result = orchestrator.run_cycle(
        lookback_days=7, apply=False, today=until + timedelta(days=1)
    )
    from adgenie.models import OptimizerRun

    run = session.execute(
        select(OptimizerRun).where(OptimizerRun.run_id == result["run_id"])
    ).scalar_one()
    assert run.finished_at is not None
    assert run.summary["window"]["until"] == until.isoformat()


def test_global_budget_cap_stops_runaway_scaling(simulated, session, settings):
    orchestrator, _, until = simulated
    settings.global_daily_budget_cap_usd = 1.0  # already exceeded

    action = OptimizationAction(
        level=EntityLevel.CAMPAIGN,
        entity_id=session.execute(select(Campaign)).scalars().first().id,
        action=ActionType.INCREASE_BUDGET,
        rule="scale_winner",
        reason="test",
        payload={"from_micros": usd_to_micros(60), "to_micros": usd_to_micros(500)},
    )
    session.add(action)
    session.flush()

    assert orchestrator.apply_action(action) is False
    assert action.status is ActionStatus.FAILED
    assert "cap" in (action.error or "").lower()


def test_variant_generation_breeds_children_from_the_parent(
    simulated, session, launched
):
    orchestrator, _, _ = simulated
    parent = session.get(Creative, launched.creative_ids[0])
    action = OptimizationAction(
        level=EntityLevel.CREATIVE,
        entity_id=parent.id,
        action=ActionType.GENERATE_VARIANTS,
        rule="frequency_fatigue",
        reason="test",
        payload={"variants": 3},
    )
    session.add(action)
    session.flush()

    assert orchestrator.apply_action(action)
    session.commit()

    children = session.execute(
        select(Creative).where(Creative.parent_id == parent.id)
    ).scalars().all()
    assert len(children) == 3
    assert all(c.generation == parent.generation + 1 for c in children)
    assert all(c.final_url.startswith("https://track.test/r?s=") for c in children)
    assert len({c.angle for c in children}) == 3


def test_rebalance_proposes_a_split_that_fits_the_budget(simulated, session, launched):
    orchestrator, since, until = simulated
    group_id = session.get(Creative, launched.creative_ids[0]).ad_group_id
    result = orchestrator.rebalance_ad_group(group_id, since, until)
    group = session.get(AdGroup, group_id)
    assert sum(result["allocation_micros"].values()) <= group.daily_budget_micros


def test_conversions_are_pushed_back_to_the_platform(
    simulated, session, offer, launched, sandbox_meta
):
    orchestrator, _, _ = simulated
    subid = encode_subid(
        TrackingContext(offer.id, None, None, launched.creative_ids[0], Platform.META)
    )
    click, _ = record_click(
        session, subid, user_agent=BROWSER_UA, query_params={"fbclid": "IwAR_test"}
    )
    session.commit()
    record_conversion(
        session,
        network="clickbank",
        network_txn_id="push-1",
        click_id=click.click_id,
        revenue_micros=usd_to_micros(40),
        status=ConversionStatus.APPROVED,
    )
    session.commit()

    result = orchestrator.push_conversions(lookback_hours=48)
    assert result["uploaded"] >= 1
    assert any(c["fbclid"] == "IwAR_test" for c in sandbox_meta.uploaded_conversions)


def test_conversions_are_not_uploaded_twice(simulated, session, offer, launched, sandbox_meta):
    orchestrator, _, _ = simulated
    subid = encode_subid(
        TrackingContext(offer.id, None, None, launched.creative_ids[0], Platform.META)
    )
    click, _ = record_click(
        session, subid, user_agent=BROWSER_UA, query_params={"fbclid": "IwAR_once"}
    )
    session.commit()
    record_conversion(
        session, network="cb", network_txn_id="once-1", click_id=click.click_id,
        revenue_micros=usd_to_micros(40), status=ConversionStatus.APPROVED,
    )
    session.commit()

    first = orchestrator.push_conversions()["uploaded"]
    second = orchestrator.push_conversions()["uploaded"]
    assert first >= 1
    assert second == 0
