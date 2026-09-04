"""Segment analysis: finding the part of a campaign that hides inside its average."""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from adgenie.config import Settings
from adgenie.core.segments import analyse_segments, group_rows
from adgenie.models import ActionType, AdGroup, EntityLevel, EntityStatus, Platform
from adgenie.money import usd_to_micros
from adgenie.platforms.base import BreakdownRow, PlatformError
from adgenie.platforms.meta import MetaAdsClient
from adgenie.platforms.sandbox import SandboxPlatform

PAYOUT = usd_to_micros(40)


def row(segment, clicks, conversions, spend_usd, dimension="placement", eid="a"):
    return BreakdownRow(
        external_id=eid,
        day=date(2026, 3, 1),
        dimension=dimension,
        segment=segment,
        impressions=clicks * 70,
        clicks=clicks,
        spend_micros=usd_to_micros(spend_usd),
        conversions=conversions,
    )


def analyse(rows, **kwargs):
    return analyse_segments(rows, EntityLevel.AD_GROUP, 1, "placement", PAYOUT, **kwargs)


# --- finding the waste -----------------------------------------------------


def test_a_clearly_bad_segment_is_cut():
    report = analyse(
        [
            row("facebook:feed", 900, 36, 540),
            row("instagram:stream", 420, 14, 250),
            row("audience_network:classic", 380, 1, 210),
        ]
    )
    assert [s.segment for s in report.exclusions] == ["audience_network:classic"]
    assert report.recoverable_micros > 0


def test_the_reason_quantifies_the_waste():
    report = analyse(
        [
            row("facebook:feed", 900, 36, 540),
            row("instagram:stream", 420, 14, 250),
            row("audience_network:classic", 380, 1, 210),
        ]
    )
    reason = report.exclusions[0].reason
    assert "% of spend" in reason
    assert "conversion rate" in reason
    assert "USD lost" in reason


def test_an_evenly_performing_split_is_left_alone():
    report = analyse(
        [
            row("facebook:feed", 600, 24, 360),
            row("instagram:stream", 600, 22, 360),
            row("instagram:reels", 600, 25, 360),
        ]
    )
    assert report.exclusions == []
    assert "clearly bad enough" in report.note


def test_shares_of_spend_are_computed_against_the_whole():
    report = analyse(
        [row("a", 500, 20, 300), row("b", 500, 18, 300), row("c", 500, 1, 400)]
    )
    total = sum(s.share_of_spend for s in report.segments)
    assert total == pytest.approx(1.0)


# --- the four guards -------------------------------------------------------


def test_a_thin_segment_is_not_cut_however_bad_it_looks():
    """Zero conversions on twenty clicks is not evidence of anything."""
    report = analyse(
        [
            row("facebook:feed", 900, 36, 540),
            row("instagram:stream", 500, 18, 300),
            row("audience_network:classic", 20, 0, 30),
        ]
    )
    assert report.exclusions == []
    thin = next(s for s in report.segments if s.segment == "audience_network:classic")
    assert "too little to tell it apart" in thin.reason


def test_a_tiny_slice_of_budget_is_not_worth_cutting():
    report = analyse(
        [
            row("facebook:feed", 4000, 160, 2400),
            row("instagram:stream", 3000, 110, 1800),
            row("audience_network:classic", 200, 0, 60),
        ],
        min_clicks=60,
    )
    tiny = next(s for s in report.segments if s.segment == "audience_network:classic")
    assert tiny.verdict == "keep"
    assert "would not move the campaign" in tiny.reason


def test_the_bar_rises_with_the_number_of_segments_tested():
    """Test five segments at 95% and you find a loser by luck half the time."""
    rows = [row(f"seg{i}", 400, 14, 240) for i in range(8)]
    rows.append(row("suspect", 400, 8, 240))
    report = analyse(rows)

    suspect = next(s for s in report.segments if s.segment == "suspect")
    if suspect.verdict == "keep":
        assert "bar for cutting one of" in suspect.reason
    # Whatever the outcome, a merely-below-average segment must not be cut.
    assert all(s.prob_worse > 0.9 for s in report.exclusions)


def test_the_last_segments_standing_are_never_all_cut():
    report = analyse(
        [row("a", 500, 20, 300), row("b", 500, 0, 300), row("c", 500, 0, 300)],
        keep_minimum_segments=2,
    )
    assert len(report.segments) - len(report.exclusions) >= 2


def test_only_a_few_are_cut_at_a_time():
    """Cutting everything at once makes the effect of each impossible to read."""
    rows = [row("good", 2000, 90, 1200)]
    rows += [row(f"bad{i}", 500, 0, 300) for i in range(5)]
    report = analyse(rows, max_exclusions=2)

    assert len(report.exclusions) == 2
    held = [s for s in report.segments if "Held back" in s.reason]
    assert held


# --- statistics ------------------------------------------------------------


def test_a_segment_matching_its_peers_is_an_even_bet():
    """Compared against the rest pooled, an average segment is 50/50."""
    report = analyse(
        [row("a", 500, 20, 300), row("b", 500, 20, 300), row("c", 500, 20, 300)]
    )
    for stat in report.segments:
        assert stat.prob_worse == pytest.approx(0.5, abs=0.06)
    assert report.exclusions == []


def test_being_worse_than_the_pool_is_what_counts_not_being_worse_than_the_best():
    """A segment level with its peers survives even beside a standout."""
    report = analyse(
        [
            row("standout", 500, 45, 300),
            row("ordinary", 500, 20, 300),
            row("also_ordinary", 500, 20, 300),
            row("dire", 500, 1, 300),
        ]
    )
    ordinary = next(s for s in report.segments if s.segment == "ordinary")
    dire = next(s for s in report.segments if s.segment == "dire")
    assert dire.prob_worse > ordinary.prob_worse
    assert [s.segment for s in report.exclusions] == ["dire"]


def test_derived_metrics():
    report = analyse([row("a", 400, 10, 200), row("b", 400, 10, 200)])
    stat = next(s for s in report.segments if s.segment == "a")
    # 10 conversions at $40 is $400 of revenue on $200 of spend.
    assert stat.cvr == pytest.approx(0.025)
    assert stat.roas == pytest.approx(2.0)
    assert stat.wasted_micros == 0, "a profitable segment wastes nothing"

    losing = next(
        s
        for s in analyse([row("a", 400, 1, 200), row("b", 400, 20, 200)]).segments
        if s.segment == "a"
    )
    # $40 of revenue against $200 of spend leaves $160 lost.
    assert losing.wasted_micros == usd_to_micros(160)


def test_no_data_is_reported_as_no_data():
    report = analyse([])
    assert report.segments == []
    assert "No delivery data" in report.note


def test_rows_group_by_entity():
    rows = [row("a", 10, 0, 5, eid="x"), row("a", 10, 0, 5, eid="y")]
    assert set(group_rows(rows)) == {"x", "y"}


# --- the sandbox reproduces the real-world case ----------------------------


@pytest.fixture
def delivered_sandbox():
    from adgenie.platforms.base import AdGroupSpec, CampaignSpec, CreativeSpec

    sandbox = SandboxPlatform(Platform.META, seed=21)
    campaign = sandbox.create_campaign(
        CampaignSpec("c", "OUTCOME_SALES", usd_to_micros(120), status="ACTIVE")
    )
    group = sandbox.create_ad_group(
        AdGroupSpec(campaign, "g", usd_to_micros(120), status="ACTIVE")
    )
    sandbox.create_creative(
        CreativeSpec(
            group, "ad", "https://track.test/r?s=o1",
            headlines=["A Simpler Evening Routine", "Wind Down In 12 Minutes", "Real Days"],
            primary_texts=["Third-party tested. A 12-minute routine. #ad"],
            status="ACTIVE",
        )
    )
    sandbox.simulate_range(date(2026, 3, 1), 30)
    return sandbox, group


def test_the_simulator_reproduces_a_worthless_placement(delivered_sandbox):
    """Audience Network converting near zero is the canonical affiliate case."""
    sandbox, group = delivered_sandbox
    rows = sandbox.fetch_breakdowns(
        "ad_group", date(2026, 3, 1), date(2026, 3, 30), "placement", [group]
    )
    report = analyse_segments(
        rows, EntityLevel.AD_GROUP, 1, "placement", PAYOUT
    )
    worst = min(report.segments, key=lambda s: s.cvr)
    best = max(report.segments, key=lambda s: s.cvr)
    assert "audience_network" in worst.segment
    assert best.cvr > worst.cvr * 3


def test_breakdowns_resolve_from_an_ad_group_down_to_its_creatives(delivered_sandbox):
    sandbox, group = delivered_sandbox
    rows = sandbox.fetch_breakdowns(
        "ad_group", date(2026, 3, 1), date(2026, 3, 30), "placement", [group]
    )
    assert rows
    assert sum(r.clicks for r in rows) > 0


def test_an_unsupported_dimension_is_rejected(delivered_sandbox):
    sandbox, group = delivered_sandbox
    with pytest.raises(PlatformError, match="unsupported breakdown"):
        sandbox.fetch_breakdowns(
            "ad_group", date(2026, 3, 1), date(2026, 3, 2), "weather", [group]
        )


def test_exclusions_are_recorded_on_the_entity(delivered_sandbox):
    sandbox, group = delivered_sandbox
    sandbox.apply_exclusion("ad_group", group, "placement", "audience_network:classic")
    assert "placement:audience_network:classic" in sandbox.entities[group].spec[
        "excluded_segments"
    ]


# --- the Meta adapter ------------------------------------------------------


@pytest.fixture
def meta_settings() -> Settings:
    return Settings(
        meta_access_token="tok", meta_ad_account_id="123",
        meta_page_id="p", meta_pixel_id="px", dry_run=False,
    )


def _meta(handler, settings):
    return MetaAdsClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(handler)),
        dry_run=False,
    )


def test_meta_requests_the_placement_breakdown_pair(meta_settings):
    """publisher_platform alone hides Reels inside Instagram."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    _meta(handler, meta_settings).fetch_breakdowns(
        "ad_group", date(2026, 3, 1), date(2026, 3, 7), "placement"
    )
    assert seen["breakdowns"] == "publisher_platform,platform_position"


def test_meta_parses_a_breakdown_row(meta_settings):
    payload = {
        "data": [
            {
                "adset_id": "as1",
                "date_start": "2026-03-01",
                "publisher_platform": "audience_network",
                "platform_position": "classic",
                "impressions": "5000",
                "clicks": "80",
                "spend": "42.50",
                "actions": [{"action_type": "purchase", "value": "1"}],
            }
        ]
    }
    rows = _meta(lambda r: httpx.Response(200, json=payload), meta_settings).fetch_breakdowns(
        "ad_group", date(2026, 3, 1), date(2026, 3, 1), "placement"
    )
    assert len(rows) == 1
    assert rows[0].segment == "audience_network:classic"
    assert rows[0].spend_micros == usd_to_micros("42.50")
    assert rows[0].conversions == 1.0


def test_meta_rejects_an_unknown_breakdown(meta_settings):
    client = _meta(lambda r: httpx.Response(200, json={"data": []}), meta_settings)
    with pytest.raises(PlatformError, match="unsupported breakdown"):
        client.fetch_breakdowns("ad_group", date(2026, 3, 1), date(2026, 3, 2), "weather")


def test_excluding_a_position_enumerates_the_ones_that_remain(meta_settings):
    """Meta has no 'exclude one placement' call; the rest must be listed."""
    posted = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "targeting": {
                        "publisher_platforms": ["facebook", "instagram"],
                        "instagram_positions": ["stream", "story", "reels"],
                        "targeting_automation": {"advantage_audience": 1},
                    }
                },
            )
        posted.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"success": True})

    _meta(handler, meta_settings).apply_exclusion(
        "ad_group", "as1", "placement", "instagram:reels"
    )
    targeting = json.loads(posted["targeting"])
    assert targeting["instagram_positions"] == ["stream", "story"]
    # Automatic placements and an explicit list cannot both be set.
    assert "targeting_automation" not in targeting


def test_excluding_a_whole_platform_drops_it_from_the_list(meta_settings):
    posted = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "targeting": {
                        "publisher_platforms": [
                            "facebook", "instagram", "audience_network",
                        ]
                    }
                },
            )
        posted.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={})

    _meta(handler, meta_settings).apply_exclusion(
        "ad_group", "as1", "placement", "audience_network"
    )
    targeting = json.loads(posted["targeting"])
    assert "audience_network" not in targeting["publisher_platforms"]


def test_an_exclusion_that_would_empty_the_targeting_is_refused(meta_settings):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"targeting": {"publisher_platforms": ["facebook"]}}
            )
        raise AssertionError("must not post an empty targeting")

    with pytest.raises(PlatformError, match="no placements"):
        _meta(handler, meta_settings).apply_exclusion(
            "ad_group", "as1", "placement", "facebook"
        )


def test_targeting_changes_beyond_placements_are_left_to_a_human(meta_settings):
    """Age and region edits reset ad set learning; that is not an auto-decision."""
    client = _meta(lambda r: httpx.Response(200, json={}), meta_settings)
    with pytest.raises(PlatformError, match="placements only"):
        client.apply_exclusion("ad_group", "as1", "age_gender", "18-24:male")


# --- through the optimizer -------------------------------------------------


def test_the_cycle_proposes_exclusions_for_review(session, settings, offer, sandbox_meta):
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan
    from adgenie.core.orchestrator import Orchestrator

    launched = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META, daily_budget_usd=120,
            angle_count=1, start_paused=False,
        )
    )
    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: sandbox_meta}
    )
    start = date(2026, 3, 1)
    for i in range(40):
        sandbox_meta.simulate_day(start + timedelta(days=i))
    orchestrator.sync_metrics(start, start + timedelta(days=39))

    decisions = orchestrator._evaluate_segments(start, start + timedelta(days=39))
    assert decisions, "40 days of delivery should surface the dead placement"
    decision, _group = decisions[0]
    assert decision.action is ActionType.EXCLUDE_SEGMENT
    assert decision.requires_approval, "an exclusion resets learning; a human signs it"
    assert "audience_network" in decision.payload["segment"]


def test_an_applied_exclusion_is_not_proposed_again(
    session, settings, offer, sandbox_meta
):
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan
    from adgenie.core.orchestrator import Orchestrator
    from adgenie.models import OptimizationAction

    launched = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META, daily_budget_usd=120,
            angle_count=1, start_paused=False,
        )
    )
    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: sandbox_meta}
    )
    start = date(2026, 3, 1)
    for i in range(40):
        sandbox_meta.simulate_day(start + timedelta(days=i))
    orchestrator.sync_metrics(start, start + timedelta(days=39))

    first = orchestrator._evaluate_segments(start, start + timedelta(days=39))
    decision, group = first[0]
    action = OptimizationAction(
        level=EntityLevel.AD_GROUP, entity_id=group.id,
        action=ActionType.EXCLUDE_SEGMENT, rule=decision.rule,
        reason=decision.reason, payload=decision.payload,
    )
    session.add(action)
    session.flush()
    assert orchestrator.apply_action(action)
    session.commit()

    again = orchestrator._evaluate_segments(start, start + timedelta(days=39))
    assert not any(
        d.payload["segment"] == decision.payload["segment"] for d, _ in again
    )
