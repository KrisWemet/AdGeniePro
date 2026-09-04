"""Tracking is where affiliate revenue becomes measurable. It has to be exact."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from adgenie.core.tracking import (
    TrackingContext,
    attribution_window_ok,
    build_final_url,
    build_tracking_url,
    decode_subid,
    encode_subid,
    hash_ip,
    looks_like_bot,
    new_click_id,
    record_click,
    record_conversion,
    sign_payload,
    verify_postback_secret,
    verify_signature,
)
from adgenie.models import Click, ConversionStatus, Platform
from adgenie.money import usd_to_micros

BROWSER_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/605.1"


# --- sub-id round trip -----------------------------------------------------


@pytest.mark.parametrize(
    "ctx",
    [
        TrackingContext(7, None, None, 56, Platform.META),
        TrackingContext(1, None, None, 3, Platform.GOOGLE),
        TrackingContext(9),
        TrackingContext(100, None, None, 400, Platform.GOOGLE),
        TrackingContext(3, 4, 5, None, Platform.GOOGLE),
    ],
)
def test_subid_round_trips(ctx):
    assert decode_subid(encode_subid(ctx)) == ctx


def test_subid_carries_the_creative_and_drops_derivable_parents():
    """The creative determines its ad group and campaign, so they are omitted."""
    decoded = decode_subid(
        encode_subid(TrackingContext(7, 12, 34, 56, Platform.META))
    )
    assert (decoded.offer_id, decoded.creative_id) == (7, 56)
    assert decoded.campaign_id is None


@pytest.mark.parametrize("value", [99_999, 9_999_999, 2_000_000_000])
def test_subid_stays_short_enough_for_clickbank(value):
    """ClickBank truncates its tracking id at 24 characters."""
    token = encode_subid(TrackingContext(value, value, value, value, Platform.META))
    assert len(token) <= 24, token


@pytest.mark.parametrize("junk", ["", "!!!", "garbage", "o", "pm", "---", "o-"])
def test_malformed_subid_degrades_instead_of_raising(junk):
    assert decode_subid(junk).offer_id == 0


def test_click_ids_are_unique_and_url_safe():
    ids = {new_click_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(i.replace("-", "").replace("_", "").isalnum() for i in ids)


# --- URL construction ------------------------------------------------------


def test_tracking_url_carries_platform_macros(settings):
    url = build_tracking_url(
        TrackingContext(1, 2, 3, 4, Platform.GOOGLE), settings=settings
    )
    assert url.startswith("https://track.test/r?s=")
    assert "pc={campaignid}" in url
    assert "pk={keyword}" in url


def test_meta_macros_use_double_braces(settings):
    url = build_tracking_url(
        TrackingContext(1, 2, 3, 4, Platform.META), settings=settings
    )
    assert "{{campaign.id}}" in url


def test_final_url_preserves_existing_query():
    url = build_final_url("https://offer.test/lp?utm_source=x", "abc123")
    assert "utm_source=x" in url
    assert "subid=abc123" in url


def test_final_url_overwrites_a_stale_subid():
    url = build_final_url("https://offer.test/lp?subid=old", "new123")
    assert "subid=new123" in url
    assert "old" not in url


# --- postback security -----------------------------------------------------


def test_signed_payload_round_trips(settings):
    token = sign_payload({"conversion": 5}, secret=settings.postback_secret)
    assert verify_signature(token, settings.postback_secret)["conversion"] == 5


@pytest.mark.parametrize("mutate", [lambda t: t[:-1] + "0", lambda t: "x" + t, lambda t: "nodot"])
def test_tampered_tokens_are_rejected(settings, mutate):
    token = sign_payload({"a": 1}, secret=settings.postback_secret)
    assert verify_signature(mutate(token), settings.postback_secret) is None


def test_expired_token_is_rejected(settings):
    token = sign_payload({"a": 1}, secret=settings.postback_secret, ttl=-10)
    assert verify_signature(token, settings.postback_secret) is None


def test_wrong_secret_is_rejected(settings):
    token = sign_payload({"a": 1}, secret="right")
    assert verify_signature(token, "wrong") is None


def test_postback_secret_comparison(settings, monkeypatch):
    import adgenie.core.tracking as tracking

    monkeypatch.setattr(tracking, "get_settings", lambda: settings)
    assert verify_postback_secret("test-secret")
    assert not verify_postback_secret("nope")
    assert not verify_postback_secret(None)


# --- bot and privacy handling ----------------------------------------------


@pytest.mark.parametrize(
    "ua", ["python-requests/2.31", "Googlebot/2.1", "curl/8.0", "", None]
)
def test_bots_are_flagged(ua):
    assert looks_like_bot(ua)


def test_real_browsers_are_not_flagged():
    assert not looks_like_bot(BROWSER_UA)


def test_ip_is_hashed_not_stored(settings):
    hashed = hash_ip("203.0.113.7", salt=settings.secret_key)
    assert "203.0.113.7" not in hashed
    assert hashed == hash_ip("203.0.113.7", salt=settings.secret_key)
    assert hashed != hash_ip("203.0.113.8", salt=settings.secret_key)
    assert hash_ip(None) is None


# --- click recording -------------------------------------------------------


def test_click_records_the_full_entity_chain(session, offer):
    ctx = TrackingContext(offer.id, 4, 5, 6, Platform.META)
    click, resolved = record_click(
        session,
        encode_subid(ctx),
        user_agent=BROWSER_UA,
        ip="203.0.113.9",
        query_params={"fbclid": "IwAR123"},
    )
    session.commit()

    assert resolved.id == offer.id
    # The parents are resolved from the creative, which does not exist here.
    assert click.creative_id == 6
    assert click.platform is Platform.META
    assert click.platform_click_id == "IwAR123"
    assert not click.is_bot


def test_click_infers_platform_from_the_click_parameter(session, offer):
    click, _ = record_click(
        session,
        encode_subid(TrackingContext(offer.id, 1, 2, 3)),
        user_agent=BROWSER_UA,
        query_params={"gclid": "Cj0KC"},
    )
    assert click.platform is Platform.GOOGLE


def test_unknown_offer_returns_none(session):
    click, offer = record_click(session, encode_subid(TrackingContext(9999)))
    assert offer is None


# --- conversion attribution ------------------------------------------------


def _click(session, offer, **kwargs):
    click, _ = record_click(
        session,
        encode_subid(TrackingContext(offer.id, 1, 2, 3, Platform.META)),
        user_agent=BROWSER_UA,
        **kwargs,
    )
    session.commit()
    return click


def test_conversion_attributes_to_the_creative(session, offer):
    click = _click(session, offer)
    conversion, method = record_conversion(
        session,
        network="clickbank",
        network_txn_id="txn-1",
        click_id=click.click_id,
        revenue_micros=usd_to_micros(40),
        status=ConversionStatus.APPROVED,
    )
    session.commit()
    assert method == "click_id"
    assert conversion.creative_id == 3
    assert conversion.revenue_micros == usd_to_micros(40)


def test_duplicate_postback_is_idempotent(session, offer):
    click = _click(session, offer)
    args = dict(
        network="clickbank",
        network_txn_id="txn-dup",
        click_id=click.click_id,
        revenue_micros=usd_to_micros(40),
        status=ConversionStatus.APPROVED,
    )
    first, _ = record_conversion(session, **args)
    session.commit()
    second, method = record_conversion(session, **args)
    session.commit()

    assert method == "duplicate"
    assert second.id == first.id


def test_refund_postback_updates_the_existing_conversion(session, offer):
    click = _click(session, offer)
    args = dict(
        network="clickbank", network_txn_id="txn-r", click_id=click.click_id,
        revenue_micros=usd_to_micros(40),
    )
    record_conversion(session, status=ConversionStatus.APPROVED, **args)
    session.commit()
    updated, method = record_conversion(session, status=ConversionStatus.REVERSED, **args)
    session.commit()

    assert method == "updated"
    assert updated.status is ConversionStatus.REVERSED


def test_conversion_falls_back_to_the_subid(session, offer):
    conversion, method = record_conversion(
        session,
        network="clickbank",
        network_txn_id="txn-sub",
        subid=encode_subid(TrackingContext(offer.id, 7, 8, 9, Platform.GOOGLE)),
        revenue_micros=usd_to_micros(40),
    )
    session.commit()
    assert method == "subid"
    assert conversion.creative_id == 9


def test_unmatched_conversion_is_recorded_and_labelled(session, offer):
    conversion, method = record_conversion(
        session, network="x", network_txn_id="txn-none", revenue_micros=1
    )
    session.commit()
    assert method == "unmatched"
    assert conversion.creative_id is None
    assert conversion.raw["attribution_method"] == "unmatched"


def test_conversion_outside_the_attribution_window_is_not_credited(session, offer):
    click = _click(session, offer)
    click.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=45)
    session.commit()

    conversion, method = record_conversion(
        session,
        network="x",
        network_txn_id="txn-old",
        click_id=click.click_id,
        revenue_micros=usd_to_micros(40),
        attribution_days=30,
    )
    session.commit()
    assert method == "click_id_expired"
    assert conversion.creative_id is None


def test_attribution_window_boundaries():
    click = Click(
        click_id="x",
        offer_id=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert attribution_window_ok(click, datetime(2026, 1, 20, tzinfo=timezone.utc), 30)
    assert not attribution_window_ok(click, datetime(2026, 3, 1, tzinfo=timezone.utc), 30)
    assert not attribution_window_ok(click, datetime(2025, 12, 1, tzinfo=timezone.utc), 30)
