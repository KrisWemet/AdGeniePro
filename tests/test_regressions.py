"""Regressions for defects found in review. Each one was live in the code.

These are grouped separately because they document specific mistakes rather
than intended behaviour, and because a fix that silently reverts is worse than
one that was never made.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest
from sqlalchemy import BigInteger, select

from adgenie.config import Settings
from adgenie.core.metrics import PerformanceWindow
from adgenie.core.optimizer import Optimizer, OptimizerPolicy
from adgenie.core.orchestrator import Orchestrator, _to_epoch
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
    Campaign,
    Conversion,
    ConversionStatus,
    Creative,
    EntityLevel,
    EntityStatus,
    MetricSnapshot,
    OptimizationAction,
    Platform,
)
from adgenie.money import usd_to_micros
from adgenie.platforms.base import AdGroupSpec, CampaignSpec, CreativeSpec, PlatformError
from adgenie.platforms.google import GoogleAdsClient
from adgenie.platforms.meta import MetaAdsClient
from adgenie.platforms.sandbox import SandboxPlatform

BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1"


# --- money columns ---------------------------------------------------------


def test_money_columns_are_64_bit():
    """A 32-bit column overflows just past $2,147 of lifetime spend."""
    for model in (Campaign, Creative, Conversion, MetricSnapshot):
        for column in model.__table__.columns:
            if column.name.endswith("_micros"):
                assert isinstance(column.type, BigInteger), (
                    f"{model.__tablename__}.{column.name} must be BigInteger"
                )


def test_large_spend_round_trips(session, offer):
    """$50,000 of lifetime spend must survive a write and read."""
    snapshot = MetricSnapshot(
        level=EntityLevel.CREATIVE,
        entity_id=1,
        day=date(2026, 3, 1),
        spend_micros=usd_to_micros(50_000),
    )
    session.add(snapshot)
    session.commit()
    session.expire_all()
    assert session.get(MetricSnapshot, snapshot.id).spend_micros == usd_to_micros(50_000)


# --- Google id shape -------------------------------------------------------


@pytest.fixture
def google_settings() -> Settings:
    return Settings(
        google_developer_token="dev",
        google_client_id="cid",
        google_client_secret="secret",
        google_refresh_token="refresh",
        google_customer_id="123-456-7890",
        dry_run=False,
    )


def _mock_google(handler, google_settings, dry_run=False) -> GoogleAdsClient:
    def wrapped(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return handler(request)

    return GoogleAdsClient(
        google_settings,
        client=httpx.Client(transport=httpx.MockTransport(wrapped)),
        dry_run=dry_run,
    )


def test_google_insight_ids_match_the_stored_creative_id(google_settings):
    """The ad id alone never matches "{adGroupId}~{adId}", so sync saw nothing."""
    payload = [
        {
            "results": [
                {
                    "adGroup": {"id": "22"},
                    "adGroupAd": {"ad": {"id": "33"}},
                    "segments": {"date": "2026-03-01"},
                    "metrics": {"impressions": "100", "clicks": "5", "costMicros": "1000"},
                }
            ]
        }
    ]
    client = _mock_google(lambda r: httpx.Response(200, json=payload), google_settings)
    rows = client.fetch_insights("creative", date(2026, 3, 1), date(2026, 3, 1))
    assert rows[0].external_id == "22~33"


def test_google_insight_query_asks_for_the_ad_group_id(google_settings):
    seen = {}

    def handler(request):
        seen["query"] = json.loads(request.content)["query"]
        return httpx.Response(200, json=[{"results": []}])

    client = _mock_google(handler, google_settings)
    client.fetch_insights("creative", date(2026, 3, 1), date(2026, 3, 1))
    assert "ad_group.id" in seen["query"]


def test_google_insight_filter_ignores_non_numeric_ids(google_settings):
    """A dry-run id like "dryrun1" used to raise ValueError and 500 the sync."""
    seen = {}

    def handler(request):
        seen["query"] = json.loads(request.content)["query"]
        return httpx.Response(200, json=[{"results": []}])

    client = _mock_google(handler, google_settings)
    client.fetch_insights(
        "creative", date(2026, 3, 1), date(2026, 3, 1), ["dryrun1", "22~33"]
    )
    assert "IN (33)" in seen["query"]


def test_google_dry_run_ad_ids_keep_the_composite_shape(google_settings):
    client = _mock_google(
        lambda r: httpx.Response(500), google_settings, dry_run=True
    )
    ad_id = client.create_creative(
        CreativeSpec(
            ad_group_external_id="5", name="ad", final_url="https://x.test",
            headlines=["a", "b", "c"], descriptions=["d1", "d2"],
        )
    )
    assert "~" in ad_id


def test_google_rejects_a_bare_ad_id_on_status_change(google_settings):
    client = _mock_google(lambda r: httpx.Response(200, json={}), google_settings)
    with pytest.raises(PlatformError, match="adGroupId"):
        client.set_status("creative", "33", False)


# --- Meta pagination -------------------------------------------------------


@pytest.fixture
def meta_settings() -> Settings:
    return Settings(
        meta_access_token="tok", meta_ad_account_id="123",
        meta_page_id="p", meta_pixel_id="px", dry_run=False,
    )


def test_meta_pagination_forwards_the_cursor(meta_settings):
    """The follow-up request used to drop the cursor and loop on page one."""
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.url.params))
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "data": [{"ad_id": "a", "date_start": "2026-03-01", "impressions": "1"}],
                    "paging": {
                        "next": (
                            "https://graph.facebook.com/v21.0/act_123/insights"
                            "?after=CURSOR2&level=ad&time_increment=1"
                        )
                    },
                },
            )
        return httpx.Response(
            200, json={"data": [{"ad_id": "b", "date_start": "2026-03-02", "impressions": "2"}]}
        )

    client = MetaAdsClient(
        meta_settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        dry_run=False,
    )
    rows = client.fetch_insights("creative", date(2026, 3, 1), date(2026, 3, 2))

    assert len(seen) == 2
    assert seen[1]["after"] == "CURSOR2"
    assert {r.external_id for r in rows} == {"a", "b"}


def test_meta_pagination_terminates_on_a_repeating_cursor(meta_settings):
    """A cursor that never advances must not hang the sync forever."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "data": [],
                "paging": {
                    "next": "https://graph.facebook.com/v21.0/act_123/insights?after=SAME"
                },
            },
        )

    client = MetaAdsClient(
        meta_settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        dry_run=False,
    )
    client.fetch_insights("creative", date(2026, 3, 1), date(2026, 3, 2))
    assert calls["n"] <= 500


# --- optimizer budget floor ------------------------------------------------


def _window(clicks, conversions, spend_usd, budget_usd):
    w = PerformanceWindow(EntityLevel.CREATIVE, 1, date(2026, 3, 1), date(2026, 3, 7))
    w.clicks = clicks
    w.conversions = conversions
    w.impressions = clicks * 70
    w.spend_micros = usd_to_micros(spend_usd)
    w.revenue_micros = conversions * usd_to_micros(40)
    w.offer_payout_micros = usd_to_micros(40)
    w.daily_budget_micros = usd_to_micros(budget_usd)
    return w


def test_throttle_never_raises_the_budget():
    """The minimum-budget floor used to turn a $2.50 cut into a $5.00 rise."""
    decision = Optimizer(OptimizerPolicy()).evaluate(
        _window(3000, 78, 3000, budget_usd=2.50)
    )
    assert decision.action is not ActionType.INCREASE_BUDGET
    assert decision.action is ActionType.NO_ACTION
    assert decision.rule.endswith("_at_floor")


def test_throttle_still_works_above_the_floor():
    decision = Optimizer(OptimizerPolicy()).evaluate(
        _window(3000, 78, 3000, budget_usd=40)
    )
    assert decision.action is ActionType.DECREASE_BUDGET
    assert decision.payload["to_micros"] < decision.payload["from_micros"]


# --- orchestrator ----------------------------------------------------------


@pytest.fixture
def google_campaign(session, offer, settings):
    """A Google campaign with three ad groups sharing one campaign budget."""
    campaign = Campaign(
        offer_id=offer.id,
        platform=Platform.GOOGLE,
        name="g",
        external_id="c1",
        daily_budget_micros=usd_to_micros(90),
        max_daily_budget_micros=usd_to_micros(400),
        status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    groups = []
    for i in range(3):
        group = AdGroup(
            campaign_id=campaign.id,
            name=f"g{i}",
            external_id=f"ag{i}",
            daily_budget_micros=usd_to_micros(30),
            status=EntityStatus.ACTIVE,
        )
        session.add(group)
        groups.append(group)
    session.commit()
    return campaign, groups


def test_google_ad_group_scale_moves_the_campaign_by_the_delta(
    session, settings, google_campaign
):
    """Writing the ad group's amount onto the campaign defunded its siblings."""
    campaign, groups = google_campaign
    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.GOOGLE: _NullPlatform()}
    )
    action = OptimizationAction(
        level=EntityLevel.AD_GROUP,
        entity_id=groups[0].id,
        action=ActionType.INCREASE_BUDGET,
        rule="scale_winner",
        reason="t",
        payload={"from_micros": usd_to_micros(30), "to_micros": usd_to_micros(36)},
    )
    session.add(action)
    session.flush()

    assert orchestrator.apply_action(action)
    session.commit()
    # $90 campaign + $6 delta, not $36.
    assert campaign.daily_budget_micros == usd_to_micros(96)
    assert groups[1].daily_budget_micros == usd_to_micros(30)


class _NullPlatform:
    """Accepts every mutation so the test isolates the orchestrator's own maths."""

    platform = Platform.GOOGLE

    def set_budget(self, level, external_id, micros):
        return None

    def set_status(self, level, external_id, active):
        return None

    def create_creative(self, spec):
        return "1~1"

    def upload_conversions(self, conversions):
        return len(conversions)


def test_global_cap_counts_ad_set_budgets(session, settings, offer):
    """The cap used to sum only campaigns, so ad-set budgets escaped it."""
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="c",
        daily_budget_micros=0, status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    for i in range(3):
        session.add(
            AdGroup(
                campaign_id=campaign.id, name=f"g{i}", external_id=f"a{i}",
                daily_budget_micros=usd_to_micros(100), status=EntityStatus.ACTIVE,
            )
        )
    session.commit()

    orchestrator = Orchestrator(session, settings=settings)
    assert orchestrator._committed_daily_micros() == usd_to_micros(300)


def test_committed_spend_does_not_double_count_a_campaign_budget(
    session, settings, offer
):
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="c",
        daily_budget_micros=usd_to_micros(100), status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    session.add(
        AdGroup(
            campaign_id=campaign.id, name="g", external_id="a",
            daily_budget_micros=usd_to_micros(40), status=EntityStatus.ACTIVE,
        )
    )
    session.commit()
    orchestrator = Orchestrator(session, settings=settings)
    assert orchestrator._committed_daily_micros() == usd_to_micros(100)


# --- conversion upload -----------------------------------------------------


def test_epoch_conversion_treats_naive_timestamps_as_utc():
    """`.timestamp()` on a naive datetime reads it in the host's local zone."""
    naive = datetime(2026, 3, 1, 12, 0)
    aware = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    assert _to_epoch(naive) == _to_epoch(aware) == int(aware.timestamp())


def _conversion_for(session, offer, creative_id, platform, click_param, txn):
    click, _ = record_click(
        session,
        encode_subid(TrackingContext(offer.id, None, None, creative_id, platform)),
        user_agent=BROWSER_UA,
        query_params={click_param: f"id-{txn}"},
    )
    session.commit()
    record_conversion(
        session, network="cb", network_txn_id=txn, click_id=click.click_id,
        revenue_micros=usd_to_micros(40), status=ConversionStatus.APPROVED,
    )
    session.commit()


def test_one_platform_failure_does_not_re_upload_the_other(session, settings, offer):
    """A failed Google upload used to reset every Meta conversion too."""
    meta_campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="cm",
        status=EntityStatus.ACTIVE,
    )
    google_campaign = Campaign(
        offer_id=offer.id, platform=Platform.GOOGLE, name="g", external_id="cg",
        status=EntityStatus.ACTIVE,
    )
    session.add_all([meta_campaign, google_campaign])
    session.flush()
    creatives = []
    for campaign in (meta_campaign, google_campaign):
        group = AdGroup(campaign_id=campaign.id, name="g", external_id="a")
        session.add(group)
        session.flush()
        creative = Creative(ad_group_id=group.id, name="c", external_id="ad")
        session.add(creative)
        session.flush()
        creatives.append(creative)
    session.commit()

    _conversion_for(session, offer, creatives[0].id, Platform.META, "fbclid", "t-meta")
    _conversion_for(session, offer, creatives[1].id, Platform.GOOGLE, "gclid", "t-goog")

    class Failing:
        platform = Platform.GOOGLE

        def upload_conversions(self, conversions):
            raise PlatformError("nope", platform=Platform.GOOGLE)

    orchestrator = Orchestrator(
        session,
        settings=settings,
        platform_clients={Platform.META: _NullPlatform(), Platform.GOOGLE: Failing()},
    )
    result = orchestrator.push_conversions()

    assert result["uploaded"] == 1
    assert "google" in result["errors"]
    meta_conversion = session.execute(
        select(Conversion).where(Conversion.network_txn_id == "t-meta")
    ).scalar_one()
    google_conversion = session.execute(
        select(Conversion).where(Conversion.network_txn_id == "t-goog")
    ).scalar_one()
    assert meta_conversion.uploaded_to_platform is True
    assert google_conversion.uploaded_to_platform is False


# --- postback status transitions -------------------------------------------


def test_pending_conversion_gains_its_revenue_on_approval(session, offer):
    """Networks post a pending sale at zero and the real amount on approval."""
    args = dict(network="cb", network_txn_id="t-1", click_id=None)
    record_conversion(session, revenue_micros=0, status=ConversionStatus.PENDING, **args)
    session.commit()

    updated, method = record_conversion(
        session, revenue_micros=usd_to_micros(40),
        status=ConversionStatus.APPROVED, **args
    )
    session.commit()
    assert method == "updated"
    assert updated.status is ConversionStatus.APPROVED
    assert updated.revenue_micros == usd_to_micros(40)


def test_reversal_takes_the_revenue_back(session, offer):
    args = dict(network="cb", network_txn_id="t-2", click_id=None)
    record_conversion(
        session, revenue_micros=usd_to_micros(40),
        status=ConversionStatus.APPROVED, **args
    )
    session.commit()
    updated, _ = record_conversion(
        session, revenue_micros=usd_to_micros(40),
        status=ConversionStatus.REVERSED, **args
    )
    session.commit()
    assert updated.revenue_micros == 0


def test_an_identical_repeat_is_still_a_duplicate(session, offer):
    args = dict(
        network="cb", network_txn_id="t-3", click_id=None,
        revenue_micros=usd_to_micros(40), status=ConversionStatus.APPROVED,
    )
    record_conversion(session, **args)
    session.commit()
    _, method = record_conversion(session, **args)
    assert method == "duplicate"


# --- variant generation ----------------------------------------------------


def test_generated_variants_reach_the_platform(session, settings, offer, sandbox_meta):
    """Variants that only existed in the database never replaced the worn ad."""
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    launched = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META,
            daily_budget_usd=30.0, angle_count=1, start_paused=False,
        )
    )
    parent = session.get(Creative, launched.creative_ids[0])

    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: sandbox_meta}
    )
    action = OptimizationAction(
        level=EntityLevel.CREATIVE, entity_id=parent.id,
        action=ActionType.GENERATE_VARIANTS, rule="frequency_fatigue",
        reason="t", payload={"variants": 2},
    )
    session.add(action)
    session.flush()
    assert orchestrator.apply_action(action)
    session.commit()

    children = session.execute(
        select(Creative).where(Creative.parent_id == parent.id)
    ).scalars().all()
    assert len(children) == 2
    for child in children:
        assert child.external_id is not None
        assert child.external_id in sandbox_meta.entities
        # New creative starts paused so it cannot spend before anyone looks.
        assert child.status is EntityStatus.PAUSED


def test_variant_generation_fails_loudly_when_nothing_can_be_created(
    session, settings, offer
):
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    sandbox = SandboxPlatform(Platform.META)
    launched = CampaignLauncher(
        session, settings=settings, platform_client=sandbox
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META,
            daily_budget_usd=30.0, angle_count=1, start_paused=False,
        )
    )
    sandbox.fail_on = {"create_creative"}
    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: sandbox}
    )
    action = OptimizationAction(
        level=EntityLevel.CREATIVE, entity_id=launched.creative_ids[0],
        action=ActionType.GENERATE_VARIANTS, rule="frequency_fatigue",
        reason="t", payload={"variants": 2},
    )
    session.add(action)
    session.flush()

    assert orchestrator.apply_action(action) is False
    assert action.status is ActionStatus.FAILED


# --- reallocation ----------------------------------------------------------


def test_reallocation_sets_ad_group_budgets(session, settings, offer):
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="c",
        daily_budget_micros=usd_to_micros(90), status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    groups = []
    for i in range(2):
        group = AdGroup(
            campaign_id=campaign.id, name=f"g{i}", external_id=f"a{i}",
            daily_budget_micros=usd_to_micros(45), status=EntityStatus.ACTIVE,
        )
        session.add(group)
        groups.append(group)
    session.commit()

    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: _NullPlatform()}
    )
    action = OptimizationAction(
        level=EntityLevel.CAMPAIGN, entity_id=campaign.id,
        action=ActionType.REALLOCATE, rule="rebalance", reason="t",
        payload={
            "allocation_micros": {
                str(groups[0].id): usd_to_micros(60),
                str(groups[1].id): usd_to_micros(30),
            }
        },
    )
    session.add(action)
    session.flush()

    assert orchestrator.apply_action(action)
    session.commit()
    assert groups[0].daily_budget_micros == usd_to_micros(60)
    assert groups[1].daily_budget_micros == usd_to_micros(30)


def test_reallocation_is_rejected_on_google(session, settings, offer):
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.GOOGLE, name="g", external_id="c",
        daily_budget_micros=usd_to_micros(90), status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.commit()

    orchestrator = Orchestrator(session, settings=settings)
    action = OptimizationAction(
        level=EntityLevel.CAMPAIGN, entity_id=campaign.id,
        action=ActionType.REALLOCATE, rule="rebalance", reason="t",
        payload={"allocation_micros": {}},
    )
    session.add(action)
    session.flush()
    assert orchestrator.apply_action(action) is False
    assert "one budget per campaign" in action.error


# --- API access control ----------------------------------------------------


def test_api_requires_a_key_when_one_is_configured(api_client, settings):
    settings.api_key = "s3cret"
    assert api_client.get("/api/offers").status_code == 401
    assert api_client.get("/api/health").status_code == 401
    assert (
        api_client.get("/api/offers", headers={"X-API-Key": "s3cret"}).status_code == 200
    )
    assert (
        api_client.get("/api/offers", headers={"X-API-Key": "wrong"}).status_code == 401
    )


def test_public_tracking_endpoints_stay_reachable_without_the_key(
    api_client, settings
):
    """Ad clicks are anonymous, so /r must never require the operator's key."""
    settings.api_key = "s3cret"
    assert api_client.get("/r?s=o9999", follow_redirects=False).status_code == 404
    assert api_client.post("/postback", json={"transaction_id": "t"}).status_code == 401


def test_dashboard_escapes_untrusted_text():
    """An offer name is attacker-controlled and lands in innerHTML."""
    from pathlib import Path

    import adgenie

    page = (Path(adgenie.__file__).parent / "static" / "index.html").read_text()
    assert "const esc =" in page
    for unescaped in ("${a.reason}", "${c.name}", "${a.rule}", "${r.name"):
        assert unescaped not in page


# --- second review round ---------------------------------------------------


def test_budget_actions_are_not_emitted_for_individual_ads():
    """Ad-level budgets do not exist, so such an action could only ever fail."""
    winner = _window(400, 25, 300, budget_usd=25)
    decision = Optimizer(OptimizerPolicy()).evaluate(winner, has_own_budget=False)
    assert decision.action is ActionType.NO_ACTION
    assert decision.rule == "winner_hold"

    marginal = _window(3000, 78, 3000, budget_usd=40)
    decision = Optimizer(OptimizerPolicy()).evaluate(marginal, has_own_budget=False)
    assert decision.action is ActionType.NO_ACTION
    assert decision.rule == "marginal_hold"


def test_budget_actions_still_fire_where_a_budget_exists():
    decision = Optimizer(OptimizerPolicy()).evaluate(
        _window(400, 25, 300, budget_usd=25), has_own_budget=True
    )
    assert decision.action is ActionType.INCREASE_BUDGET


def test_creative_refresh_is_not_proposed_for_an_ad_group():
    """An ad group is a container; there is no creative in it to regenerate."""
    w = _window(400, 20, 300, budget_usd=40)
    w.level = EntityLevel.AD_GROUP
    w.frequency = 5.0
    decision = Optimizer(OptimizerPolicy()).evaluate(w)
    assert decision.action is not ActionType.GENERATE_VARIANTS


def test_setting_a_budget_on_an_ad_is_refused(session, settings, offer):
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="c",
        status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    group = AdGroup(campaign_id=campaign.id, name="g", external_id="a")
    session.add(group)
    session.flush()
    creative = Creative(ad_group_id=group.id, name="ad", external_id="x")
    session.add(creative)
    session.flush()

    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: _NullPlatform()}
    )
    action = OptimizationAction(
        level=EntityLevel.CREATIVE, entity_id=creative.id,
        action=ActionType.INCREASE_BUDGET, rule="scale_winner", reason="t",
        payload={"from_micros": usd_to_micros(10), "to_micros": usd_to_micros(12)},
    )
    session.add(action)
    session.flush()

    assert orchestrator.apply_action(action) is False
    assert "individual ad" in action.error


def test_variant_generation_on_an_ad_group_fails_without_aborting_the_run(
    session, settings, offer
):
    """It used to raise AttributeError, which escaped and killed the cycle."""
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="c",
        status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    group = AdGroup(campaign_id=campaign.id, name="g", external_id="a")
    session.add(group)
    session.flush()

    orchestrator = Orchestrator(session, settings=settings)
    action = OptimizationAction(
        level=EntityLevel.AD_GROUP, entity_id=group.id,
        action=ActionType.GENERATE_VARIANTS, rule="frequency_fatigue",
        reason="t", payload={"variants": 2},
    )
    session.add(action)
    session.flush()

    assert orchestrator.apply_action(action) is False
    assert action.status is ActionStatus.FAILED


def test_an_unexpected_error_fails_one_action_not_the_whole_run(
    session, settings, offer
):
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="c",
        status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()

    class Exploding:
        platform = Platform.META

        def set_status(self, *a, **k):
            raise ZeroDivisionError("boom")

    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: Exploding()}
    )
    action = OptimizationAction(
        level=EntityLevel.CAMPAIGN, entity_id=campaign.id,
        action=ActionType.PAUSE, rule="t", reason="t",
    )
    session.add(action)
    session.flush()

    assert orchestrator.apply_action(action) is False
    assert "ZeroDivisionError" in action.error


# --- unattributed revenue --------------------------------------------------


def test_offer_foreign_keys_are_nullable():
    """An unmatched postback must record an unattributed row, not raise."""
    assert Conversion.__table__.c.offer_id.nullable
    from adgenie.models import Click as ClickModel

    assert ClickModel.__table__.c.offer_id.nullable


def test_unmatched_conversion_writes_a_null_offer(session):
    conversion, method = record_conversion(
        session, network="x", network_txn_id="orphan-1", revenue_micros=usd_to_micros(40)
    )
    session.commit()
    assert method == "unmatched"
    assert conversion.offer_id is None


def test_click_with_a_mangled_subid_is_still_recorded(session):
    click, offer = record_click(session, "garbage", user_agent=BROWSER_UA)
    session.commit()
    assert offer is None
    assert click.offer_id is None


# --- sandbox economics -----------------------------------------------------


def test_ad_groups_sharing_a_campaign_budget_do_not_multiply_it():
    """Each group used to fall back to the full campaign budget on its own."""
    sandbox = SandboxPlatform(Platform.GOOGLE, seed=5)
    campaign = sandbox.create_campaign(
        CampaignSpec(
            name="c", objective="SEARCH",
            daily_budget_micros=usd_to_micros(90), status="ACTIVE",
        )
    )
    for i in range(3):
        group = sandbox.create_ad_group(
            AdGroupSpec(
                campaign_external_id=campaign, name=f"g{i}",
                daily_budget_micros=0, status="ACTIVE",
            )
        )
        sandbox.create_creative(
            CreativeSpec(
                ad_group_external_id=group, name=f"ad{i}",
                final_url="https://track.test/r?s=o1",
                headlines=["A Headline Here", "B Headline Here", "C Headline"],
                status="ACTIVE",
            )
        )

    rows = sandbox.simulate_day(date(2026, 1, 1))
    assert sum(r.spend_micros for r in rows) <= usd_to_micros(90)


# --- variant count ---------------------------------------------------------


def test_variant_count_is_honoured_beyond_the_platform_angle_list(offer, settings):
    """Asking for ten used to return only as many angles as the platform had."""
    from adgenie.core.copywriter import CopyStudio, build_brief

    brief = build_brief(offer, Platform.GOOGLE, keyword="sleep aid")
    drafts = CopyStudio(settings=settings).write_variants(brief, count=10)
    assert len(drafts) == 10


# --- Meta conversions API --------------------------------------------------


def test_meta_never_sends_a_hashed_ip(meta_settings):
    """Meta wants client_ip_address raw; a hash is a wrong value, not a safe one."""
    seen = {}

    def handler(request):
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={})

    client = MetaAdsClient(
        meta_settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        dry_run=False,
    )
    client.upload_conversions(
        [{"event_time": 1772000000, "value": 40.0, "fbclid": "x", "ip_hash": "deadbeef"}]
    )
    events = json.loads(seen["data"])
    assert "client_ip_address" not in events[0]["user_data"]


# --- demo isolation --------------------------------------------------------


def test_demo_does_not_touch_the_configured_database_or_settings(tmp_path):
    """`adgenie demo` used to drop the real database and enable live spend."""
    from adgenie.config import get_settings
    from adgenie.demo import run

    before = get_settings()
    configured_url, configured_dry_run = before.database_url, before.dry_run

    run(days=3, budget=20.0, verbose=False,
        database_url=f"sqlite:///{tmp_path / 'demo.db'}")

    after = get_settings()
    assert after.database_url == configured_url
    assert after.dry_run == configured_dry_run
    assert (tmp_path / "demo.db").exists()


# --- third review round ----------------------------------------------------


def test_global_cap_is_checked_against_the_increase_not_the_total(
    session, settings, offer
):
    """Comparing the new absolute budget refused scales that fit comfortably."""
    settings.global_daily_budget_cap_usd = 500.0
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="c",
        daily_budget_micros=usd_to_micros(450), status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    groups = []
    for i in range(3):
        group = AdGroup(
            campaign_id=campaign.id, name=f"g{i}", external_id=f"a{i}",
            daily_budget_micros=usd_to_micros(150), status=EntityStatus.ACTIVE,
        )
        session.add(group)
        groups.append(group)
    session.commit()

    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: _NullPlatform()}
    )
    action = OptimizationAction(
        level=EntityLevel.AD_GROUP, entity_id=groups[0].id,
        action=ActionType.INCREASE_BUDGET, rule="scale_winner", reason="t",
        payload={"from_micros": usd_to_micros(150), "to_micros": usd_to_micros(180)},
    )
    session.add(action)
    session.flush()

    # $450 committed + a $30 increase = $480, comfortably under the $500 cap.
    assert orchestrator.apply_action(action) is True
    assert groups[0].daily_budget_micros == usd_to_micros(180)


def test_global_cap_still_clamps_an_increase_that_exceeds_it(
    session, settings, offer
):
    settings.global_daily_budget_cap_usd = 100.0
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="c",
        daily_budget_micros=usd_to_micros(90), status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    session.add(
        AdGroup(
            campaign_id=campaign.id, name="g", external_id="a",
            daily_budget_micros=usd_to_micros(90), status=EntityStatus.ACTIVE,
        )
    )
    session.commit()

    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: _NullPlatform()}
    )
    action = OptimizationAction(
        level=EntityLevel.CAMPAIGN, entity_id=campaign.id,
        action=ActionType.INCREASE_BUDGET, rule="scale_winner", reason="t",
        payload={"from_micros": usd_to_micros(90), "to_micros": usd_to_micros(200)},
    )
    session.add(action)
    session.flush()

    assert orchestrator.apply_action(action) is True
    assert campaign.daily_budget_micros == usd_to_micros(100)
    assert "Capped at" in action.reason


def test_dry_run_does_not_mark_conversions_as_uploaded(session, settings, offer):
    """Marking them sent while sending nothing loses them permanently."""
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="c",
        status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    group = AdGroup(campaign_id=campaign.id, name="g", external_id="a")
    session.add(group)
    session.flush()
    creative = Creative(ad_group_id=group.id, name="ad", external_id="x")
    session.add(creative)
    session.flush()
    session.commit()
    _conversion_for(session, offer, creative.id, Platform.META, "fbclid", "dry-1")

    settings.dry_run = True
    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: _NullPlatform()}
    )
    result = orchestrator.push_conversions()

    assert result["uploaded"] == 0
    assert result["dry_run"] is True
    conversion = session.execute(
        select(Conversion).where(Conversion.network_txn_id == "dry-1")
    ).scalar_one()
    assert conversion.uploaded_to_platform is False


def test_upload_window_follows_the_last_update_not_creation(session, offer):
    """A sale approved days after it was posted must still be uploaded."""
    args = dict(network="cb", network_txn_id="late-1", click_id=None)
    conversion, _ = record_conversion(
        session, revenue_micros=0, status=ConversionStatus.PENDING, **args
    )
    conversion.created_at = datetime(2026, 1, 1)
    session.commit()

    record_conversion(
        session, revenue_micros=usd_to_micros(40),
        status=ConversionStatus.APPROVED, **args
    )
    session.commit()
    session.refresh(conversion)
    assert conversion.updated_at > conversion.created_at


def test_creative_refresh_does_not_re_fire_after_a_recent_refresh():
    """The parent keeps running, so the rule used to breed ads every cycle."""
    optimizer = Optimizer(OptimizerPolicy())
    fatigued = _window(200, 7, 200, budget_usd=25)
    fatigued.frequency = 4.5

    first = optimizer.evaluate(fatigued, has_own_budget=False)
    assert first.action is ActionType.GENERATE_VARIANTS

    now = datetime(2026, 3, 10, tzinfo=timezone.utc)
    second = optimizer.evaluate(
        fatigued,
        has_own_budget=False,
        last_refresh_at=now - timedelta(days=2),
        now=now,
    )
    assert second.action is not ActionType.GENERATE_VARIANTS


def test_creative_refresh_returns_once_the_cooldown_expires():
    optimizer = Optimizer(OptimizerPolicy())
    fatigued = _window(200, 7, 200, budget_usd=25)
    fatigued.frequency = 4.5
    now = datetime(2026, 3, 10, tzinfo=timezone.utc)
    decision = optimizer.evaluate(
        fatigued,
        has_own_budget=False,
        last_refresh_at=now - timedelta(days=30),
        now=now,
    )
    assert decision.action is ActionType.GENERATE_VARIANTS


def test_a_bad_api_key_is_rejected_not_a_crash(api_client, settings):
    """compare_digest raises TypeError on a non-ASCII str, which would 500.

    HTTP headers decode as latin-1, so an accented character reaches the
    handler intact and is exactly the input that used to crash it.
    """
    settings.api_key = "s3cret"
    # Sent as raw bytes because that is what travels on the wire; Starlette
    # decodes it back to a non-ASCII str before the handler sees it.
    response = api_client.get(
        "/api/offers", headers={"X-API-Key": "café".encode("latin-1")}
    )
    assert response.status_code == 401


def test_a_non_ascii_postback_secret_is_rejected_not_a_crash(api_client, settings):
    """A query parameter carries full UTF-8, so this reaches the handler too."""
    response = api_client.get("/postback?transaction_id=t&secret=caf%C3%A9%E2%98%95")
    assert response.status_code == 401


def test_prior_is_built_from_peers_not_from_the_entity_itself():
    """An ad group of one would otherwise shrink a creative toward its own rate."""
    from adgenie.core.metrics import apply_pooled_prior

    solo = _window(200, 20, 200, budget_usd=25)
    apply_pooled_prior([solo])
    prior_mean = solo.prior_a / (solo.prior_a + solo.prior_b)
    assert solo.cvr == pytest.approx(0.10)
    assert prior_mean < 0.05, "the prior must not echo the creative's own rate"


def test_peers_shape_each_other_but_not_themselves():
    from adgenie.core.metrics import apply_pooled_prior

    hot = _window(200, 20, 200, budget_usd=25)
    hot.entity_id = 1
    cold_a = _window(500, 10, 500, budget_usd=25)
    cold_a.entity_id = 2
    cold_b = _window(500, 10, 500, budget_usd=25)
    cold_b.entity_id = 3
    apply_pooled_prior([hot, cold_a, cold_b])

    hot_prior = hot.prior_a / (hot.prior_a + hot.prior_b)
    cold_prior = cold_a.prior_a / (cold_a.prior_a + cold_a.prior_b)
    # The hot creative is judged against the cold pool, and vice versa.
    assert hot_prior < cold_prior


def test_truncation_keeps_a_word_that_fits_exactly():
    from adgenie.platforms.specs import truncate_to_spec

    assert truncate_to_spec("Sleep Better Tonight Naturally", 20) == "Sleep Better Tonight"
    assert (
        truncate_to_spec("Wind Down Without Grogginess Today", 28)
        == "Wind Down Without Grogginess"
    )


def test_google_macro_fallback_matches_a_composite_creative_id(session, offer):
    """Google's `{creative}` macro is the bare ad id; storage is composite."""
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.GOOGLE, name="g", external_id="c",
        status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    group = AdGroup(campaign_id=campaign.id, name="g", external_id="22")
    session.add(group)
    session.flush()
    creative = Creative(ad_group_id=group.id, name="ad", external_id="22~33")
    session.add(creative)
    session.commit()

    click, _ = record_click(
        session,
        encode_subid(TrackingContext(offer.id)),
        user_agent=BROWSER_UA,
        query_params={"pa": "33", "gclid": "Cj0"},
    )
    session.commit()
    assert click.creative_id == creative.id


def test_an_orphaned_entity_fails_one_action_not_the_run(session, settings, offer):
    """_platform_of used to dereference a missing parent outside the guard."""
    group = AdGroup(campaign_id=9999, name="orphan", external_id="a")
    session.add(group)
    session.flush()

    orchestrator = Orchestrator(session, settings=settings)
    action = OptimizationAction(
        level=EntityLevel.AD_GROUP, entity_id=group.id,
        action=ActionType.PAUSE, rule="t", reason="t",
    )
    session.add(action)
    session.flush()

    assert orchestrator.apply_action(action) is False
    assert action.status is ActionStatus.FAILED
    assert "parent campaign" in action.error


def _launch(api_client, offer_id):
    return api_client.post(
        "/api/campaigns/launch",
        json={
            "offer_id": offer_id, "platform": "meta",
            "daily_budget_usd": 20.0, "angle_count": 1,
        },
    ).json()


@pytest.fixture
def created_offer(api_client) -> dict:
    return api_client.post(
        "/api/offers",
        json={
            "name": "CalmLeaf Sleep Support",
            "destination_url": "https://offer.test/calmleaf",
            "payout_usd": 40.0,
        },
    ).json()


# --- fourth review round ---------------------------------------------------


def test_meta_upload_fails_loudly_without_a_pixel(meta_settings):
    """Returning 0 let the caller mark the sales sent and lose them for good."""
    meta_settings.meta_pixel_id = None
    client = MetaAdsClient(
        meta_settings,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
        dry_run=False,
    )
    with pytest.raises(PlatformError, match="META_PIXEL_ID"):
        client.upload_conversions(
            [{"event_time": 1772000000, "value": 40.0, "fbclid": "x"}]
        )


def test_google_upload_fails_loudly_without_a_conversion_action(google_settings):
    google_settings.google_conversion_action_id = None
    client = _mock_google(lambda r: httpx.Response(200, json={}), google_settings)
    with pytest.raises(PlatformError, match="GOOGLE_CONVERSION_ACTION_ID"):
        client.upload_conversions(
            [{"gclid": "Cj0", "event_time": 1772000000, "value": 40.0}]
        )


def test_a_partial_upload_leaves_everything_queued(session, settings, offer):
    """The adapter cannot say which it accepted, so none may be marked sent."""
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="m", external_id="c",
        status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    group = AdGroup(campaign_id=campaign.id, name="g", external_id="a")
    session.add(group)
    session.flush()
    creative = Creative(ad_group_id=group.id, name="ad", external_id="x")
    session.add(creative)
    session.commit()

    for i in range(2):
        _conversion_for(
            session, offer, creative.id, Platform.META, "fbclid", f"partial-{i}"
        )

    class SilentlyDropping:
        platform = Platform.META

        def upload_conversions(self, conversions):
            return 0  # accepted nothing, raised nothing

    orchestrator = Orchestrator(
        session, settings=settings,
        platform_clients={Platform.META: SilentlyDropping()},
    )
    result = orchestrator.push_conversions()

    assert result["uploaded"] == 0
    assert "meta" in result["errors"]
    assert all(
        c.uploaded_to_platform is False
        for c in session.execute(select(Conversion)).scalars()
    )


def test_a_failed_refresh_does_not_suppress_the_fatigue_rule(
    session, settings, offer
):
    """Failed children used to count as a refresh and silence it for 14 days."""
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    sandbox = SandboxPlatform(Platform.META)
    launched = CampaignLauncher(
        session, settings=settings, platform_client=sandbox
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META,
            daily_budget_usd=30.0, angle_count=1, start_paused=False,
        )
    )
    parent_id = launched.creative_ids[0]

    sandbox.fail_on = {"create_creative"}
    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: sandbox}
    )
    action = OptimizationAction(
        level=EntityLevel.CREATIVE, entity_id=parent_id,
        action=ActionType.GENERATE_VARIANTS, rule="frequency_fatigue",
        reason="t", payload={"variants": 2},
    )
    session.add(action)
    session.flush()
    assert orchestrator.apply_action(action) is False
    session.commit()

    assert orchestrator._last_refresh_at(parent_id) is None


def test_a_successful_refresh_does_suppress_it(session, settings, offer, sandbox_meta):
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    launched = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META,
            daily_budget_usd=30.0, angle_count=1, start_paused=False,
        )
    )
    parent_id = launched.creative_ids[0]
    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: sandbox_meta}
    )
    action = OptimizationAction(
        level=EntityLevel.CREATIVE, entity_id=parent_id,
        action=ActionType.GENERATE_VARIANTS, rule="frequency_fatigue",
        reason="t", payload={"variants": 2},
    )
    session.add(action)
    session.flush()
    assert orchestrator.apply_action(action)
    session.commit()

    assert orchestrator._last_refresh_at(parent_id) is not None


def test_a_mangled_subid_still_redirects_when_the_macro_resolves_it(
    api_client, created_offer
):
    """A paid click must not be thrown away when the offer is still reachable."""
    launched = _launch(api_client, created_offer["id"])
    creative = api_client.get(f"/api/creatives/{launched['creative_ids'][0]}").json()

    response = api_client.get(
        f"/r?s=truncated-garbage&pa={creative['external_id']}",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://offer.test/calmleaf")


def test_a_click_with_no_resolvable_offer_is_still_404(api_client):
    assert api_client.get("/r?s=o9999", follow_redirects=False).status_code == 404


def test_live_platform_clients_are_reused(settings):
    """A new client per call leaks a connection pool and re-authenticates."""
    from adgenie.platforms.factory import get_platform, reset_sandboxes

    reset_sandboxes()
    settings.meta_access_token = "tok"
    settings.meta_ad_account_id = "123"
    first = get_platform(Platform.META, settings)
    second = get_platform(Platform.META, settings)
    assert first is second
    reset_sandboxes()


def test_applying_under_dry_run_is_refused_by_the_api(api_client, settings):
    """The CLI refuses this; the API used to rewrite budgets and send nothing."""
    settings.dry_run = True
    response = api_client.post("/api/optimizer/run", json={"apply": True})
    assert response.status_code == 409
    assert "DRY_RUN" in response.json()["detail"]


def test_approving_under_dry_run_is_refused(api_client, settings, session):
    settings.dry_run = True
    action = OptimizationAction(
        level=EntityLevel.CREATIVE, entity_id=1,
        action=ActionType.PAUSE, rule="t", reason="t",
    )
    session.add(action)
    session.commit()

    response = api_client.post(f"/api/optimizer/actions/{action.id}/approve")
    assert response.status_code == 409
    assert session.get(OptimizationAction, action.id).status is ActionStatus.PROPOSED


def test_shouting_rule_exemptions_can_actually_match():
    """Every exempt token must be long enough for the rule to have fired."""
    import re

    from adgenie.core.compliance import RULES

    rule = next(r for r in RULES if r.code == "EXCESSIVE_CAPS")
    tokens = re.findall(r"[A-Z]+", rule.exempt_pattern)
    assert tokens
    assert all(len(t) >= 5 for t in tokens), (
        "an exemption shorter than the rule's own minimum is dead code"
    )
