"""Platform adapters: request shape, error handling and the simulator."""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from adgenie.config import Settings
from adgenie.models import Platform
from adgenie.money import micros_to_cents, usd_to_micros
from adgenie.platforms.base import (
    AdGroupSpec,
    CampaignSpec,
    CreativeSpec,
    PlatformError,
)
from adgenie.platforms.factory import get_platform, is_sandbox, reset_sandboxes
from adgenie.platforms.google import GoogleAdsClient, _id_from_resource, _nested_id
from adgenie.platforms.meta import MetaAdsClient
from adgenie.platforms.sandbox import SandboxPlatform
from adgenie.platforms.specs import get_spec


# --- the simulator ---------------------------------------------------------


def _build(sandbox, headlines, primary=None, budget_usd=50.0):
    campaign = sandbox.create_campaign(
        CampaignSpec(
            name="c",
            objective="OUTCOME_SALES",
            daily_budget_micros=usd_to_micros(budget_usd),
            status="ACTIVE",
        )
    )
    group = sandbox.create_ad_group(
        AdGroupSpec(
            campaign_external_id=campaign,
            name="g",
            daily_budget_micros=usd_to_micros(budget_usd),
            status="ACTIVE",
        )
    )
    return group, sandbox.create_creative(
        CreativeSpec(
            ad_group_external_id=group,
            name="ad",
            final_url="https://track.test/r?s=o1-a1",
            headlines=headlines,
            primary_texts=primary or [],
            status="ACTIVE",
        )
    )


def test_sandbox_rewards_better_copy(sandbox_meta):
    """The simulator must reward good copy, or it tests nothing."""
    _, good = _build(
        sandbox_meta,
        ["A Simpler Evening Routine", "Wind Down In 12 Minutes", "Made For Real Days"],
        ["Third-party tested in a US facility. A 12-minute routine. #ad"],
    )
    _, bad = _build(
        sandbox_meta,
        ["AMAZING DEAL!!!", "CLICK HERE NOW!!!", "BEST EVER!!!"],
        ["ACT NOW!!!"],
    )
    assert sandbox_meta.entities[good].true_ctr > sandbox_meta.entities[bad].true_ctr


def test_sandbox_is_deterministic():
    a, b = SandboxPlatform(Platform.META, seed=5), SandboxPlatform(Platform.META, seed=5)
    _, first = _build(a, ["One Headline Here", "Two Headline Here", "Three Here"])
    _, second = _build(b, ["One Headline Here", "Two Headline Here", "Three Here"])
    assert a.entities[first].true_ctr == b.entities[second].true_ctr

    rows_a = a.simulate_range(date(2026, 1, 1), 5)
    rows_b = b.simulate_range(date(2026, 1, 1), 5)
    assert [r.clicks for r in rows_a] == [r.clicks for r in rows_b]


def test_sandbox_delivery_is_bounded_by_budget(sandbox_meta):
    _build(sandbox_meta, ["A Headline Here", "B Headline Here", "C Here"], budget_usd=20)
    rows = sandbox_meta.simulate_range(date(2026, 1, 1), 7)
    for row in rows:
        assert row.spend_micros <= usd_to_micros(20)
        assert row.clicks <= row.impressions


def test_sandbox_search_converts_better_than_feed():
    """Search intent should out-convert an interrupted feed, as in reality."""
    ctrs = {}
    for platform in (Platform.META, Platform.GOOGLE):
        sandbox = SandboxPlatform(platform, seed=3)
        _, ad = _build(sandbox, ["Natural Sleep Aid", "Compare Options", "See Pricing"])
        ctrs[platform] = sandbox.entities[ad].true_ctr
    assert ctrs[Platform.GOOGLE] > ctrs[Platform.META]


def test_sandbox_paused_entities_do_not_deliver(sandbox_meta):
    group, ad = _build(sandbox_meta, ["A Headline", "B Headline", "C Headline"])
    sandbox_meta.set_status("creative", ad, False)
    assert sandbox_meta.simulate_day(date(2026, 1, 1)) == []


def test_sandbox_splits_budget_across_siblings(sandbox_meta):
    group, _ = _build(sandbox_meta, ["A Headline", "B Headline", "C Headline"])
    sandbox_meta.create_creative(
        CreativeSpec(
            ad_group_external_id=group,
            name="second",
            final_url="https://track.test/r?s=o1-a2",
            headlines=["D Headline", "E Headline", "F Headline"],
            status="ACTIVE",
        )
    )
    rows = sandbox_meta.simulate_day(date(2026, 1, 1))
    assert len(rows) == 2
    assert sum(r.spend_micros for r in rows) <= usd_to_micros(50)


def test_sandbox_validates_input(sandbox_meta):
    with pytest.raises(PlatformError, match="unknown campaign"):
        sandbox_meta.create_ad_group(AdGroupSpec(campaign_external_id="nope", name="g"))

    group, _ = _build(sandbox_meta, ["A", "B", "C"])
    with pytest.raises(PlatformError, match="headline"):
        sandbox_meta.create_creative(
            CreativeSpec(ad_group_external_id=group, name="x", final_url="https://a.test")
        )
    with pytest.raises(PlatformError, match="positive"):
        sandbox_meta.set_budget("ad_group", group, 0)


def test_sandbox_can_simulate_failures():
    sandbox = SandboxPlatform(Platform.META, fail_on={"create_campaign"})
    with pytest.raises(PlatformError, match="simulated failure"):
        sandbox.create_campaign(
            CampaignSpec(name="c", objective="X", daily_budget_micros=1)
        )


def test_sandbox_insights_are_filtered_by_window(sandbox_meta):
    _build(sandbox_meta, ["A Headline", "B Headline", "C Headline"])
    sandbox_meta.simulate_range(date(2026, 1, 1), 10)
    rows = sandbox_meta.fetch_insights(
        "creative", date(2026, 1, 3), date(2026, 1, 5)
    )
    assert {r.day for r in rows} == {
        date(2026, 1, 3), date(2026, 1, 4), date(2026, 1, 5)
    }


# --- the factory -----------------------------------------------------------


def test_factory_falls_back_to_the_sandbox_without_credentials(settings):
    reset_sandboxes()
    client = get_platform(Platform.META, settings)
    assert is_sandbox(client)
    assert client.health_check()["mode"] == "sandbox"


def test_factory_returns_the_live_client_when_configured(settings):
    settings.meta_access_token = "tok"
    settings.meta_ad_account_id = "act_123"
    client = get_platform(Platform.META, settings)
    assert isinstance(client, MetaAdsClient)
    assert client.account_id == "123"


# --- Meta adapter ----------------------------------------------------------


@pytest.fixture
def meta_settings() -> Settings:
    return Settings(
        meta_access_token="tok",
        meta_ad_account_id="123",
        meta_page_id="page1",
        meta_pixel_id="pix1",
        dry_run=False,
    )


def _mock_meta(handler, meta_settings) -> MetaAdsClient:
    transport = httpx.MockTransport(handler)
    return MetaAdsClient(
        meta_settings, client=httpx.Client(transport=transport), dry_run=False
    )


def test_meta_campaign_request_shape(meta_settings):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, json={"id": "camp_1"})

    client = _mock_meta(handler, meta_settings)
    result = client.create_campaign(
        CampaignSpec(
            name="Test", objective="OUTCOME_SALES",
            daily_budget_micros=usd_to_micros(50), status="ACTIVE",
        )
    )
    assert result == "camp_1"
    assert "act_123/campaigns" in seen["url"]
    assert seen["body"]["status"] == "ACTIVE"
    # Required by Meta since 2021, even when empty.
    assert json.loads(seen["body"]["special_ad_categories"]) == []


def test_meta_converts_micros_to_cents(meta_settings):
    seen = {}

    def handler(request):
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"id": "x"})

    client = _mock_meta(handler, meta_settings)
    client.set_budget("ad_group", "adset_1", usd_to_micros(37.50))
    assert seen["daily_budget"] == "3750"
    assert micros_to_cents(usd_to_micros(37.50)) == 3750


def test_meta_creative_creates_both_a_creative_and_an_ad(meta_settings):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"id": f"obj_{len(paths)}"})

    client = _mock_meta(handler, meta_settings)
    ad_id = client.create_creative(
        CreativeSpec(
            ad_group_external_id="adset_1",
            name="ad",
            final_url="https://track.test/r?s=o1-a1",
            headlines=["A Calm Evening", "Second Option"],
            primary_texts=["A 12-minute wind-down. #ad", "Another body"],
            descriptions=["Ships in two days"],
        )
    )
    assert ad_id == "obj_2"
    assert any("adcreatives" in p for p in paths)
    assert any(p.endswith("/ads") for p in paths)


def test_meta_creative_requires_a_page(meta_settings):
    meta_settings.meta_page_id = None
    client = _mock_meta(lambda r: httpx.Response(200, json={}), meta_settings)
    with pytest.raises(PlatformError, match="Page id"):
        client.create_creative(
            CreativeSpec(
                ad_group_external_id="a", name="n", final_url="u", headlines=["h"]
            )
        )


def test_meta_parses_insights_including_purchase_actions(meta_settings):
    payload = {
        "data": [
            {
                "ad_id": "ad_1",
                "date_start": "2026-03-01",
                "impressions": "1000",
                "clicks": "25",
                "spend": "12.34",
                "reach": "900",
                "frequency": "1.11",
                "actions": [
                    {"action_type": "purchase", "value": "3"},
                    {"action_type": "link_click", "value": "25"},
                ],
                "action_values": [{"action_type": "purchase", "value": "120.00"}],
            }
        ]
    }
    client = _mock_meta(lambda r: httpx.Response(200, json=payload), meta_settings)
    rows = client.fetch_insights("creative", date(2026, 3, 1), date(2026, 3, 1))

    assert len(rows) == 1
    row = rows[0]
    assert row.external_id == "ad_1"
    assert row.spend_micros == usd_to_micros("12.34")
    assert row.conversions == 3.0
    assert row.conversion_value_micros == usd_to_micros(120)


def test_meta_paginates_insights(meta_settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"ad_id": "a", "date_start": "2026-03-01", "impressions": "1"}
                    ],
                    "paging": {"next": "https://graph.facebook.com/v21.0/act_123/insights?after=x"},
                },
            )
        return httpx.Response(
            200,
            json={"data": [{"ad_id": "b", "date_start": "2026-03-02", "impressions": "2"}]},
        )

    client = _mock_meta(handler, meta_settings)
    rows = client.fetch_insights("creative", date(2026, 3, 1), date(2026, 3, 2))
    assert {r.external_id for r in rows} == {"a", "b"}


def test_meta_does_not_retry_a_rejected_request(meta_settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            400, json={"error": {"code": 100, "message": "Invalid parameter"}}
        )

    client = _mock_meta(handler, meta_settings)
    with pytest.raises(PlatformError) as exc:
        client.set_status("creative", "ad_1", True)
    assert not exc.value.retryable
    assert calls["n"] == 1


def test_meta_dry_run_sends_nothing(meta_settings):
    def handler(request):
        raise AssertionError("a dry run must not reach the network")

    client = MetaAdsClient(
        meta_settings, client=httpx.Client(transport=httpx.MockTransport(handler)),
        dry_run=True,
    )
    result = client.create_campaign(
        CampaignSpec(name="c", objective="OUTCOME_SALES", daily_budget_micros=1)
    )
    assert result.startswith("dryrun_")


def test_meta_conversions_api_formats_the_click_id(meta_settings):
    seen = {}

    def handler(request):
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"events_received": 1})

    client = _mock_meta(handler, meta_settings)
    sent = client.upload_conversions(
        [{"event_time": 1772000000, "value": 40.0, "fbclid": "IwAR9", "event_id": "e1"}]
    )
    assert sent == 1
    events = json.loads(seen["data"])
    assert events[0]["user_data"]["fbc"].startswith("fb.1.")
    assert events[0]["user_data"]["fbc"].endswith("IwAR9")
    assert events[0]["custom_data"]["value"] == 40.0


def test_meta_skips_conversions_without_an_identifier(meta_settings):
    client = _mock_meta(lambda r: httpx.Response(200, json={}), meta_settings)
    assert client.upload_conversions([{"event_time": 1, "value": 5.0}]) == 0


# --- Google adapter --------------------------------------------------------


@pytest.fixture
def google_settings() -> Settings:
    return Settings(
        google_developer_token="dev",
        google_client_id="cid",
        google_client_secret="secret",
        google_refresh_token="refresh",
        google_customer_id="123-456-7890",
        google_conversion_action_id="555",
        dry_run=False,
    )


def _mock_google(handler, google_settings) -> GoogleAdsClient:
    def wrapped(request):
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return handler(request)

    return GoogleAdsClient(
        google_settings,
        client=httpx.Client(transport=httpx.MockTransport(wrapped)),
        dry_run=False,
    )


def test_google_strips_dashes_from_the_customer_id(google_settings):
    client = _mock_google(lambda r: httpx.Response(200, json={}), google_settings)
    assert client.customer_id == "1234567890"


def test_google_campaign_creates_a_budget_first(google_settings):
    paths, bodies = [], []

    def handler(request):
        paths.append(request.url.path)
        bodies.append(json.loads(request.content))
        name = "campaignBudgets" if "campaignBudgets" in request.url.path else "campaigns"
        return httpx.Response(
            200, json={"results": [{"resourceName": f"customers/1/{name}/9"}]}
        )

    client = _mock_google(handler, google_settings)
    campaign_id = client.create_campaign(
        CampaignSpec(
            name="Search", objective="SEARCH",
            daily_budget_micros=usd_to_micros(40), status="ACTIVE",
        )
    )
    assert campaign_id == "9"
    assert "campaignBudgets:mutate" in paths[0]
    assert bodies[0]["operations"][0]["create"]["amountMicros"] == str(usd_to_micros(40))
    assert bodies[1]["operations"][0]["create"]["status"] == "ENABLED"


def test_google_target_roas_bid_strategy(google_settings):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [{"resourceName": "customers/1/x/9"}]})

    client = _mock_google(handler, google_settings)
    client.create_campaign(
        CampaignSpec(
            name="c", objective="SEARCH", daily_budget_micros=1,
            bid_strategy="TARGET_ROAS", target_roas=2.5,
        )
    )
    assert bodies[1]["operations"][0]["create"]["maximizeConversionValue"] == {
        "targetRoas": 2.5
    }


def test_google_ad_group_attaches_keywords(google_settings):
    bodies = []

    def handler(request):
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"results": [{"resourceName": "customers/1/adGroups/5"}]})

    client = _mock_google(handler, google_settings)
    client.create_ad_group(
        AdGroupSpec(
            campaign_external_id="9", name="g",
            keywords=["natural sleep aid"], negative_keywords=["free"],
            extra={"match_type": "exact"},
        )
    )
    criteria = [b for path, b in bodies if "adGroupCriteria" in path][0]
    ops = criteria["operations"]
    assert ops[0]["create"]["keyword"]["matchType"] == "EXACT"
    assert ops[1]["create"]["negative"] is True


def test_google_enforces_responsive_search_ad_minimums(google_settings):
    client = _mock_google(lambda r: httpx.Response(200, json={}), google_settings)
    with pytest.raises(PlatformError, match="3 headlines"):
        client.create_creative(
            CreativeSpec(
                ad_group_external_id="5", name="ad", final_url="u",
                headlines=["one", "two"], descriptions=["a", "b"],
            )
        )
    with pytest.raises(PlatformError, match="2 descriptions"):
        client.create_creative(
            CreativeSpec(
                ad_group_external_id="5", name="ad", final_url="u",
                headlines=["one", "two", "three"], descriptions=["a"],
            )
        )


def test_google_budgets_are_campaign_level_only(google_settings):
    client = _mock_google(lambda r: httpx.Response(200, json={}), google_settings)
    with pytest.raises(PlatformError, match="campaign level"):
        client.set_budget("ad_group", "5", usd_to_micros(20))


def test_google_parses_search_stream_metrics(google_settings):
    payload = [
        {
            "results": [
                {
                    "adGroupAd": {"ad": {"id": "777"}},
                    "segments": {"date": "2026-03-01"},
                    "metrics": {
                        "impressions": "500",
                        "clicks": "30",
                        "costMicros": "24500000",
                        "conversions": 2.0,
                        "conversionsValue": 80.0,
                    },
                }
            ]
        }
    ]
    client = _mock_google(lambda r: httpx.Response(200, json=payload), google_settings)
    rows = client.fetch_insights("creative", date(2026, 3, 1), date(2026, 3, 1))

    assert rows[0].external_id == "777"
    assert rows[0].spend_micros == 24_500_000
    assert rows[0].conversion_value_micros == usd_to_micros(80)


def test_google_retries_a_server_error(google_settings, monkeypatch):
    monkeypatch.setattr("adgenie.platforms.google.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={"results": [{"resourceName": "customers/1/x/1"}]})

    client = _mock_google(handler, google_settings)
    client.set_status("campaign", "1", True)
    assert calls["n"] == 3


def test_google_offline_conversion_upload(google_settings):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = _mock_google(handler, google_settings)
    sent = client.upload_conversions(
        [{"gclid": "Cj0KC", "event_time": 1772000000, "value": 40.0, "order_id": "t1"}]
    )
    assert sent == 1
    conversion = bodies[0]["conversions"][0]
    assert conversion["gclid"] == "Cj0KC"
    assert conversion["conversionAction"].endswith("conversionActions/555")
    assert bodies[0]["partialFailure"] is True


def test_google_resource_name_helpers():
    assert _id_from_resource("customers/123/campaigns/456") == "456"
    assert _id_from_resource("customers/1/adGroupAds/22~33") == "22~33"
    assert _nested_id({"adGroupAd": {"ad": {"id": "9"}}}, "ad_group_ad.ad.id") == "9"
    assert _nested_id({}, "campaign.id") == ""


# --- specs -----------------------------------------------------------------


def test_specs_encode_real_platform_limits():
    rsa = get_spec(Platform.GOOGLE)
    assert rsa.fields["headlines"].max_chars == 30
    assert rsa.fields["headlines"].min_count == 3
    assert rsa.fields["descriptions"].max_chars == 90

    feed = get_spec(Platform.META)
    assert feed.fields["headlines"].max_chars == 40
    assert "SHOP_NOW" in feed.allowed_ctas


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        get_spec(Platform.META, "carousel_3d")
