"""Competitor research: coverage honesty, signal extraction, persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from adgenie.config import Settings
from adgenie.models import CompetitorAd, Platform
from adgenie.platforms.base import PlatformError
from adgenie.research.ad_library import (
    EU_UK_COUNTRIES,
    AdLibraryAd,
    AdLibraryClient,
    commercial_ads_available,
)
from adgenie.research.service import MarketResearcher
from adgenie.research.signals import (
    build_market_brief,
    classify_angle,
    count_variants,
    score_staying_power,
)

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def make_ad(ad_id, days, body, *, active=True, page="p1", titles=None, reach=0):
    return AdLibraryAd(
        ad_archive_id=ad_id,
        page_id=page,
        page_name=f"Page {page}",
        bodies=[body],
        titles=titles or ["A Headline"],
        started_at=NOW - timedelta(days=days),
        stopped_at=None if active else NOW,
        eu_total_reach=reach,
    )


# --- coverage honesty ------------------------------------------------------


def test_commercial_ads_are_eu_and_uk_only():
    assert not commercial_ads_available(["US"])
    assert not commercial_ads_available(["US", "CA", "AU"])
    assert commercial_ads_available(["US", "GB"])
    assert commercial_ads_available(["DE"])
    assert "GB" in EU_UK_COUNTRIES and "US" not in EU_UK_COUNTRIES


@pytest.fixture
def library_settings() -> Settings:
    return Settings(meta_access_token="tok", meta_api_version="v21.0")


def _client(handler, settings, **kw):
    return AdLibraryClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(handler)), **kw
    )


def test_a_us_search_warns_that_commercial_ads_are_not_carried(library_settings):
    """An empty US result means 'not carried', not 'no competition'."""
    client = _client(lambda r: httpx.Response(200, json={"data": []}), library_settings)
    ads, warnings = client.search(search_terms="sleep aid", countries=["US"])

    assert ads == []
    codes = {w.code for w in warnings}
    assert "NO_COMMERCIAL_COVERAGE" in codes
    assert any("Digital Services Act" in w.message for w in warnings)


def test_every_search_warns_that_there_is_no_performance_data(library_settings):
    client = _client(lambda r: httpx.Response(200, json={"data": []}), library_settings)
    _, warnings = client.search(search_terms="x", countries=["GB"])
    assert "NO_PERFORMANCE_DATA" in {w.code for w in warnings}


def test_an_eu_search_does_not_warn_about_coverage(library_settings):
    client = _client(lambda r: httpx.Response(200, json={"data": []}), library_settings)
    _, warnings = client.search(search_terms="x", countries=["DE", "FR"])
    assert "NO_COMMERCIAL_COVERAGE" not in {w.code for w in warnings}


# --- request shape ---------------------------------------------------------


def test_search_sends_the_required_parameters(library_settings):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    _client(handler, library_settings).search(
        search_terms="sleep aid", countries=["GB", "IE"], active_only=True
    )
    assert seen["ad_reached_countries"] == "GB,IE"
    assert seen["ad_type"] == "ALL"
    assert seen["ad_active_status"] == "ACTIVE"
    assert "ad_creative_bodies" in seen["fields"]


def test_page_ids_are_batched(library_settings):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    _client(handler, library_settings).search(
        page_ids=[str(i) for i in range(25)], countries=["GB"]
    )
    # The API caps the batch; sending more silently drops them.
    assert len(seen["search_page_ids"].split(",")) == 10


def test_search_requires_a_term_or_page_ids(library_settings):
    client = _client(lambda r: httpx.Response(200, json={"data": []}), library_settings)
    with pytest.raises(ValueError):
        client.search(countries=["GB"])


def test_missing_token_is_a_clear_error():
    with pytest.raises(PlatformError, match="ads_read"):
        AdLibraryClient(Settings(meta_access_token=None))


def test_permission_errors_explain_the_fix(library_settings):
    def handler(request):
        return httpx.Response(
            400, json={"error": {"code": 200, "message": "Permissions error"}}
        )

    client = _client(handler, library_settings)
    with pytest.raises(PlatformError, match="ads_read"):
        client.search(search_terms="x", countries=["GB"])


def test_results_are_parsed_and_paginated(library_settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "123",
                            "page_id": "p1",
                            "page_name": "Sleep Co",
                            "ad_creative_bodies": ["Wind down tonight"],
                            "ad_creative_link_titles": ["CalmLeaf"],
                            "ad_delivery_start_time": "2026-01-01T00:00:00+0000",
                            "publisher_platforms": ["facebook", "instagram"],
                            "eu_total_reach": 55000,
                        }
                    ],
                    "paging": {"next": "https://graph.facebook.com/v21.0/ads_archive?after=X"},
                },
            )
        return httpx.Response(200, json={"data": []})

    ads, _ = _client(handler, library_settings).search(
        search_terms="sleep", countries=["GB"]
    )
    assert len(ads) == 1
    ad = ads[0]
    assert ad.ad_archive_id == "123"
    assert ad.page_name == "Sleep Co"
    assert ad.eu_total_reach == 55000
    assert ad.is_active
    assert calls["n"] == 2


# --- signal extraction -----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Tired of tossing and turning every night?", "problem_solution"),
        ("How it works: a clinically studied magnesium formula", "mechanism"),
        ("Rated 4.8 by 12,000 customers", "social_proof"),
        ("CalmLeaf vs the usual sleep aid", "comparison"),
        ("Sceptical? Here is an honest review", "objection"),
        ("How to fix your evening routine in 3 steps", "how_to"),
        ("50% off today only, free shipping", "offer_led"),
    ],
)
def test_angle_classification(text, expected):
    assert classify_angle([text]) == expected


def test_unclassifiable_copy_is_not_forced_into_an_angle():
    assert classify_angle(["Hello there"]) == "unknown"
    assert classify_angle([]) == "unknown"


def test_staying_power_rises_with_longevity():
    scores = [
        score_staying_power(make_ad("a", days, "x"), as_of=NOW)
        for days in (1, 7, 30, 90, 180)
    ]
    assert scores == sorted(scores)
    assert scores[-1] > scores[0] * 1.5


def test_a_stopped_ad_scores_below_a_live_one_of_the_same_age():
    live = score_staying_power(make_ad("a", 60, "x"), as_of=NOW)
    dead = score_staying_power(make_ad("b", 60, "x", active=False), as_of=NOW)
    assert live > dead


def test_more_variants_raise_the_score():
    one = score_staying_power(make_ad("a", 60, "x"), variant_count=1, as_of=NOW)
    many = score_staying_power(make_ad("a", 60, "x"), variant_count=8, as_of=NOW)
    assert many > one


def test_variants_are_counted_by_shared_opening_copy():
    ads = [
        make_ad("1", 30, "Wind down without grogginess tonight with our blend"),
        make_ad("2", 30, "Wind down without grogginess tonight with our blend"),
        make_ad("3", 30, "A completely different message about something else"),
    ]
    counts = count_variants(ads)
    assert counts["1"] == counts["2"] == 2
    assert counts["3"] == 1


def test_variants_are_scoped_to_the_advertiser():
    ads = [
        make_ad("1", 30, "Same opening words here for both of these ads", page="p1"),
        make_ad("2", 30, "Same opening words here for both of these ads", page="p2"),
    ]
    assert count_variants(ads)["1"] == 1


# --- the brief -------------------------------------------------------------


def test_brief_weights_angles_by_staying_power_not_by_count():
    """One advertiser flooding the archive must not outvote a durable rival."""
    ads = [make_ad(f"n{i}", 2, "50% off today only, free shipping", page="spam") for i in range(20)]
    ads.append(make_ad("d1", 200, "How it works: a clinically studied formula", page="p2"))
    ads.append(make_ad("d2", 180, "The science behind our patented method", page="p3"))

    brief = build_market_brief(ads, "x", proven_days=30, as_of=NOW)
    assert brief.dominant_angles[0] == "mechanism"


def test_brief_ignores_brand_new_ads_when_proven_ones_exist():
    ads = [
        make_ad("old", 120, "How it works: our patented method", page="p1"),
        make_ad("new", 2, "50% off today only", page="p2"),
    ]
    brief = build_market_brief(ads, "x", proven_days=30, as_of=NOW)
    assert brief.proven_ads == 1
    assert "offer_led" not in brief.dominant_angles


def test_confidence_reflects_the_evidence():
    assert build_market_brief([], "x").confidence == "none"

    thin = build_market_brief([make_ad("1", 90, "How it works")], "x", as_of=NOW)
    assert thin.confidence == "low"

    thick = build_market_brief(
        [
            make_ad(f"a{i}", 90, "How it works: our patented method", page=f"p{i % 10}")
            for i in range(25)
        ],
        "x",
        as_of=NOW,
    )
    assert thick.confidence == "high"


def test_prompt_notes_describe_patterns_never_wording():
    ads = [
        make_ad(f"a{i}", 90, "Tired of tossing and turning? Our unique blend helps.", page=f"p{i}")
        for i in range(10)
    ]
    brief = build_market_brief(ads, "sleep aid", as_of=NOW)
    notes = " ".join(brief.to_prompt_notes())

    assert "Tired of tossing and turning" not in notes, "must not carry competitor copy"
    assert "not as copy to imitate" in notes
    assert "no performance data" in notes


def test_empty_brief_produces_no_notes():
    assert build_market_brief([], "x").to_prompt_notes() == []


def test_brief_reports_structural_norms():
    ads = [
        make_ad(f"a{i}", 90, "Is your evening routine a mess? 3 steps to fix it 🙂", page=f"p{i}")
        for i in range(6)
    ]
    brief = build_market_brief(ads, "x", as_of=NOW)
    assert brief.question_hook_rate == 1.0
    assert brief.numeric_claim_rate == 1.0
    assert brief.emoji_usage_rate == 1.0
    assert brief.body_length_p50 > 0


# --- persistence -----------------------------------------------------------


def test_scan_persists_observations(session, library_settings):
    payload = {
        "data": [
            {
                "id": "a1",
                "page_id": "p1",
                "page_name": "Sleep Co",
                "ad_creative_bodies": ["How it works: our patented blend"],
                "ad_delivery_start_time": "2026-01-01T00:00:00+0000",
            }
        ]
    }
    client = _client(lambda r: httpx.Response(200, json=payload), library_settings)
    researcher = MarketResearcher(session, library_settings, client=client)
    researcher.research("sleep aid", countries=["GB"], vertical="supplements")
    session.commit()

    row = session.query(CompetitorAd).one()
    assert row.ad_archive_id == "a1"
    assert row.angle == "mechanism"
    assert row.vertical == "supplements"
    assert row.staying_power > 0


def test_rescanning_updates_rather_than_duplicates(session, library_settings):
    payload = {
        "data": [
            {
                "id": "a1",
                "page_id": "p1",
                "page_name": "Sleep Co",
                "ad_creative_bodies": ["How it works"],
                "ad_delivery_start_time": "2026-01-01T00:00:00+0000",
            }
        ]
    }
    client = _client(lambda r: httpx.Response(200, json=payload), library_settings)
    researcher = MarketResearcher(session, library_settings, client=client)
    researcher.research("x", countries=["GB"])
    session.commit()
    researcher.research("x", countries=["GB"])
    session.commit()

    assert session.query(CompetitorAd).count() == 1


def test_retired_ads_surface_the_negative_signal(session, library_settings):
    session.add(
        CompetitorAd(
            ad_archive_id="dead", page_name="Gone Co", vertical="supplements",
            is_active=False, days_running=9, angle="offer_led",
        )
    )
    session.add(
        CompetitorAd(
            ad_archive_id="alive", page_name="Live Co", vertical="supplements",
            is_active=True, days_running=120, angle="mechanism",
        )
    )
    session.commit()

    retired = MarketResearcher(session, library_settings).retired_ads("supplements")
    assert [r["ad_archive_id"] for r in retired] == ["dead"]


def test_stored_brief_needs_no_api_call(session, library_settings):
    session.add(
        CompetitorAd(
            ad_archive_id="a1", page_id="p1", vertical="supplements",
            bodies=["How it works: our patented method"],
            started_at=datetime(2026, 1, 1), is_active=True, days_running=120,
        )
    )
    session.commit()

    # No client is supplied; building the brief must not try to construct one.
    brief = MarketResearcher(session, library_settings).stored_brief("supplements")
    assert brief.ads_seen == 1
    assert "FROM_CACHE" in {w["code"] for w in brief.warnings}


def test_research_offer_searches_the_category_not_the_brand(session, library_settings, offer):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    client = _client(handler, library_settings)
    MarketResearcher(session, library_settings, client=client).research_offer(offer)
    # The vertical finds competitors; the offer's own name finds only itself.
    assert seen["search_terms"] == offer.vertical
