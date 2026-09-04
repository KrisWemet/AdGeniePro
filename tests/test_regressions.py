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
