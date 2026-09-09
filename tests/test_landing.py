"""Landing page auditing: checking what the platforms actually check."""

from __future__ import annotations

import httpx
import pytest

from adgenie.core.compliance import Severity
from adgenie.core.destination import DestinationMonitor
from adgenie.core.landing import (
    BROWSER_AGENT,
    CRAWLER_AGENTS,
    LandingPageFetcher,
    audit_landing_page,
)
from adgenie.models import ComplianceVerdict, EntityStatus, LandingPageCheck, Platform

BODY = " ".join(
    [
        "CalmLeaf is a magnesium glycinate and L-theanine blend taken thirty",
        "minutes before bed. It is third-party tested in a United States facility",
        "and ships within two working days. We may earn a commission when you buy",
        "through this page. Most people take one capsule with water and keep the",
        "same bedtime for a fortnight before judging whether it suits them. There",
        "is a sixty day return window. Ingredients, sourcing and the testing",
        "certificates are listed in full below for anyone who wants to read them",
        "before ordering. Questions go to our team who reply within one day.",
        "The blend contains three hundred milligrams of magnesium glycinate and",
        "two hundred milligrams of L-theanine per serving, with no added sugar,",
        "no artificial colouring and no stimulants of any kind whatsoever. It is",
        "manufactured in a facility that is inspected annually and each batch",
        "carries a certificate of analysis you can look up by its lot number.",
        "People who take medication or who are pregnant should speak to a doctor",
        "before starting any supplement, including this one, as a matter of course.",
    ]
)

CLEAN_PAGE = f"""<html><head><title>CalmLeaf Sleep Support</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><h1>A simpler evening routine</h1><p>{BODY}</p>
<a href="/privacy">Privacy Policy</a>
<a href="/terms">Terms and Conditions</a>
<a href="/contact">Contact Us</a></body></html>"""


def fetcher_for(handler) -> LandingPageFetcher:
    return LandingPageFetcher(
        client=httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
    )


def serve(html: str, status: int = 200):
    return lambda request: httpx.Response(status, html=html)


def codes(audit) -> set[str]:
    return {f.code for f in audit.findings}


# --- a page that is fine ---------------------------------------------------


def test_a_compliant_page_passes():
    audit = audit_landing_page(
        "https://lp.test/calmleaf", fetcher=fetcher_for(serve(CLEAN_PAGE))
    )
    assert audit.verdict is not ComplianceVerdict.BLOCK
    assert audit.passed
    assert "MISSING_PRIVACY_LINK" not in codes(audit)
    assert "THIN_CONTENT" not in codes(audit)


def test_the_snapshot_records_what_was_fetched():
    audit = audit_landing_page(
        "https://lp.test/x", fetcher=fetcher_for(serve(CLEAN_PAGE)), check_cloaking=False
    )
    assert audit.snapshot.title == "CalmLeaf Sleep Support"
    assert audit.snapshot.has_viewport
    assert audit.content_hash
    assert "Privacy Policy" not in audit.snapshot.text or audit.snapshot.links


# --- availability ----------------------------------------------------------


def test_an_unreachable_page_blocks():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    audit = audit_landing_page("https://lp.test/gone", fetcher=fetcher_for(handler))
    assert audit.verdict is ComplianceVerdict.BLOCK
    assert "PAGE_UNREACHABLE" in codes(audit)
    assert "paid for and sent nowhere" in audit.findings[0].suggestion


def test_an_error_status_blocks():
    audit = audit_landing_page(
        "https://lp.test/404", fetcher=fetcher_for(serve("<html></html>", 404))
    )
    assert audit.verdict is ComplianceVerdict.BLOCK
    assert "PAGE_ERROR_STATUS" in codes(audit)


def test_a_dead_page_is_not_also_reported_for_thin_content():
    """One real problem, not a cascade of consequences of it."""
    audit = audit_landing_page(
        "https://lp.test/404", fetcher=fetcher_for(serve("", 500))
    )
    assert len(audit.findings) == 1


# --- required links --------------------------------------------------------


def test_a_missing_privacy_policy_blocks():
    page = "<html><body><p>" + BODY + "</p><a href='/terms'>Terms</a>"
    page += "<a href='/contact'>Contact</a></body></html>"
    audit = audit_landing_page("https://lp.test/x", fetcher=fetcher_for(serve(page)))
    assert "MISSING_PRIVACY_LINK" in codes(audit)
    assert audit.verdict is ComplianceVerdict.BLOCK


def test_missing_terms_or_contact_only_warns():
    page = f"<html><body><p>{BODY}</p><a href='/privacy'>Privacy</a></body></html>"
    audit = audit_landing_page("https://lp.test/x", fetcher=fetcher_for(serve(page)))
    assert {"MISSING_TERMS_LINK", "MISSING_CONTACT_LINK"} <= codes(audit)
    assert "MISSING_PRIVACY_LINK" not in codes(audit)


# --- transport security ----------------------------------------------------


def test_plain_http_warns():
    audit = audit_landing_page(
        "http://lp.test/x", fetcher=fetcher_for(serve(CLEAN_PAGE))
    )
    assert "NOT_HTTPS" in codes(audit)


def test_plain_http_blocks_when_the_page_takes_card_details():
    page = CLEAN_PAGE.replace(
        "</body>",
        "<form action='/pay'><input name='cardnumber'><input name='cvv'></form></body>",
    )
    audit = audit_landing_page("http://lp.test/x", fetcher=fetcher_for(serve(page)))
    assert audit.verdict is ComplianceVerdict.BLOCK
    blocking = {f.code for f in audit.blocking}
    assert "NOT_HTTPS" in blocking


def test_a_form_posting_over_http_blocks():
    page = CLEAN_PAGE.replace(
        "</body>", "<form action='http://insecure.test/x'><input name='email'></form></body>"
    )
    audit = audit_landing_page("https://lp.test/x", fetcher=fetcher_for(serve(page)))
    assert "INSECURE_FORM_ACTION" in codes(audit)


# --- redirects -------------------------------------------------------------


def test_the_redirect_chain_is_recorded():
    hops = {
        "/a": "https://lp.test/b",
        "/b": "https://lp.test/c",
    }

    def handler(request):
        target = hops.get(request.url.path)
        if target:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, html=CLEAN_PAGE)

    audit = audit_landing_page(
        "https://lp.test/a", fetcher=fetcher_for(handler), check_cloaking=False
    )
    assert audit.snapshot.redirect_chain == ["https://lp.test/b", "https://lp.test/c"]
    assert audit.snapshot.final_url == "https://lp.test/c"


def test_a_long_redirect_chain_warns():
    def handler(request):
        depth = int(request.url.path.strip("/") or 0)
        if depth < 4:
            return httpx.Response(
                302, headers={"location": f"https://lp.test/{depth + 1}"}
            )
        return httpx.Response(200, html=CLEAN_PAGE)

    audit = audit_landing_page(
        "https://lp.test/0", fetcher=fetcher_for(handler), check_cloaking=False
    )
    assert "LONG_REDIRECT_CHAIN" in codes(audit)


def test_a_redirect_loop_is_reported_not_followed_forever():
    handler = lambda r: httpx.Response(302, headers={"location": "https://lp.test/loop"})
    audit = audit_landing_page("https://lp.test/loop", fetcher=fetcher_for(handler))
    assert "PAGE_UNREACHABLE" in codes(audit)
    assert "redirects" in audit.findings[0].message


def test_dropping_to_http_mid_chain_warns():
    def handler(request):
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://lp.test/final"})
        return httpx.Response(200, html=CLEAN_PAGE)

    audit = audit_landing_page(
        "https://lp.test/a", fetcher=fetcher_for(handler), check_cloaking=False
    )
    assert "INSECURE_REDIRECT_HOP" in codes(audit)


def test_a_meta_refresh_redirect_warns():
    page = CLEAN_PAGE.replace(
        "<head>", "<head><meta http-equiv='refresh' content='0;url=/elsewhere'>"
    )
    audit = audit_landing_page("https://lp.test/x", fetcher=fetcher_for(serve(page)))
    assert "META_REFRESH_REDIRECT" in codes(audit)


# --- cloaking --------------------------------------------------------------


def _cloaking_handler(browser_html: str, crawler_html: str):
    def handler(request):
        agent = request.headers.get("user-agent", "")
        is_crawler = any(
            token in agent for token in ("facebookexternalhit", "AdsBot-Google")
        )
        return httpx.Response(200, html=crawler_html if is_crawler else browser_html)

    return handler


def test_serving_reviewers_different_content_blocks():
    """The check that matters most: cloaking is a permanent ban."""
    other = CLEAN_PAGE.replace(BODY, "An unrelated neutral encyclopedia article " * 20)
    audit = audit_landing_page(
        "https://lp.test/x", fetcher=fetcher_for(_cloaking_handler(CLEAN_PAGE, other))
    )
    assert audit.verdict is ComplianceVerdict.BLOCK
    assert "POSSIBLE_CLOAKING" in codes(audit)


def test_cloaking_is_reported_for_every_platform_checked():
    other = CLEAN_PAGE.replace(BODY, "An unrelated neutral encyclopedia article " * 20)
    audit = audit_landing_page(
        "https://lp.test/x", fetcher=fetcher_for(_cloaking_handler(CLEAN_PAGE, other))
    )
    cloaking = [f for f in audit.findings if f.code == "POSSIBLE_CLOAKING"]
    platforms = {"meta", "google"}
    assert platforms <= {p for f in cloaking for p in platforms if p in f.message}


def test_minor_page_variation_is_not_called_cloaking():
    """Timestamps and rotating testimonials must not trip the alarm."""
    varied = CLEAN_PAGE.replace(
        "<h1>", "<span>Last updated 4 minutes ago. 312 people viewing.</span><h1>"
    )
    audit = audit_landing_page(
        "https://lp.test/x", fetcher=fetcher_for(_cloaking_handler(CLEAN_PAGE, varied))
    )
    assert "POSSIBLE_CLOAKING" not in codes(audit)


def test_blocking_the_reviewer_blocks():
    def handler(request):
        agent = request.headers.get("user-agent", "")
        if "facebookexternalhit" in agent or "AdsBot" in agent:
            return httpx.Response(403, html="denied")
        return httpx.Response(200, html=CLEAN_PAGE)

    audit = audit_landing_page("https://lp.test/x", fetcher=fetcher_for(handler))
    assert "CRAWLER_BLOCKED" in codes(audit)
    assert audit.verdict is ComplianceVerdict.BLOCK


def test_sending_the_reviewer_somewhere_else_blocks():
    def handler(request):
        agent = request.headers.get("user-agent", "")
        crawler = "facebookexternalhit" in agent or "AdsBot" in agent
        if crawler and request.url.path == "/x":
            return httpx.Response(302, headers={"location": "https://lp.test/safe"})
        return httpx.Response(200, html=CLEAN_PAGE)

    audit = audit_landing_page("https://lp.test/x", fetcher=fetcher_for(handler))
    assert "CRAWLER_REDIRECTED_ELSEWHERE" in codes(audit)


def test_cloaking_checks_can_be_skipped():
    other = CLEAN_PAGE.replace(BODY, "Different words entirely " * 30)
    audit = audit_landing_page(
        "https://lp.test/x",
        fetcher=fetcher_for(_cloaking_handler(CLEAN_PAGE, other)),
        check_cloaking=False,
    )
    assert "POSSIBLE_CLOAKING" not in codes(audit)
    assert audit.crawler_snapshots == {}


def test_the_crawler_agents_are_the_real_ones():
    assert "facebookexternalhit" in CRAWLER_AGENTS["meta"]
    assert "AdsBot-Google" in CRAWLER_AGENTS["google"]
    assert "Mozilla" in BROWSER_AGENT


# --- page quality ----------------------------------------------------------


def test_a_bridge_page_is_flagged_as_thin():
    page = "<html><body><a href='/privacy'>Privacy</a><a href='/terms'>Terms</a>"
    page += "<a href='/contact'>Contact</a><h1>Click here</h1></body></html>"
    audit = audit_landing_page("https://lp.test/x", fetcher=fetcher_for(serve(page)))
    assert "THIN_CONTENT" in codes(audit)


def test_a_desktop_only_page_warns():
    audit = audit_landing_page(
        "https://lp.test/x",
        fetcher=fetcher_for(serve(CLEAN_PAGE.replace('<meta name="viewport"', "<meta name='x'"))),
    )
    assert "NOT_MOBILE_READY" in codes(audit)


def test_a_page_with_no_disclosure_warns():
    audit = audit_landing_page(
        "https://lp.test/x",
        fetcher=fetcher_for(serve(CLEAN_PAGE.replace("We may earn a commission", "You will love"))),
    )
    assert "NO_AFFILIATE_DISCLOSURE_ON_PAGE" in codes(audit)


@pytest.mark.parametrize(
    "markup,code",
    [
        ("<audio autoplay src='x.mp3'>", "AUTOPLAY_MEDIA"),
        ("<script>window.onbeforeunload = function(){}</script>", "BACK_BUTTON_TRAP"),
        ("<p>As seen on CNN and Forbes</p>", "UNSUBSTANTIATED_ENDORSEMENT"),
    ],
)
def test_dark_patterns_are_flagged(markup, code):
    audit = audit_landing_page(
        "https://lp.test/x",
        fetcher=fetcher_for(serve(CLEAN_PAGE.replace("</body>", markup + "</body>"))),
    )
    assert code in codes(audit)


def test_a_countdown_warns_but_does_not_refuse_the_launch():
    """A timer to a real deadline is ordinary, and the markup cannot tell.

    Blocking on a suspicion the page may well not deserve is worse than
    saying so and letting the operator look.
    """
    markup = "<script>setInterval(function(){ tick(countdown) }, 1000)</script>"
    audit = audit_landing_page(
        "https://lp.test/x",
        fetcher=fetcher_for(serve(CLEAN_PAGE.replace("</body>", markup + "</body>"))),
    )
    assert "SCRIPTED_COUNTDOWN" in codes(audit)
    finding = next(f for f in audit.findings if f.code == "SCRIPTED_COUNTDOWN")
    assert finding.severity is Severity.WARN
    assert audit.verdict is not ComplianceVerdict.BLOCK


def test_a_back_button_trap_still_blocks():
    """The counterweight: what the markup proves is a block, not a warning."""
    markup = "<script>window.onbeforeunload = function(){}</script>"
    audit = audit_landing_page(
        "https://lp.test/x",
        fetcher=fetcher_for(serve(CLEAN_PAGE.replace("</body>", markup + "</body>"))),
    )
    assert audit.verdict is ComplianceVerdict.BLOCK


def test_page_text_is_held_to_the_same_claim_rules_as_the_ad():
    """A guaranteed-cure promise is no safer one click away from the ad."""
    page = CLEAN_PAGE.replace(BODY, BODY + " Guaranteed results, this miracle cure works.")
    audit = audit_landing_page("https://lp.test/x", fetcher=fetcher_for(serve(page)))
    assert any(c.startswith("PAGE_") for c in codes(audit))
    assert audit.verdict is ComplianceVerdict.BLOCK


def test_ad_format_rules_are_not_applied_to_a_web_page():
    audit = audit_landing_page("https://lp.test/x", fetcher=fetcher_for(serve(CLEAN_PAGE)))
    assert not any(
        c in codes(audit) for c in ("PAGE_OVER_CHAR_LIMIT", "PAGE_TOO_FEW_ASSETS")
    )


# --- ad-to-page consistency ------------------------------------------------


def test_a_page_that_does_not_deliver_the_ad_is_flagged():
    audit = audit_landing_page(
        "https://lp.test/x",
        fetcher=fetcher_for(serve(CLEAN_PAGE)),
        ad_texts=[
            "Cryptocurrency trading signals with automated portfolio rebalancing",
            "Leverage arbitrage across decentralised exchanges every morning",
        ],
        check_cloaking=False,
    )
    assert "AD_PAGE_MISMATCH" in codes(audit)


def test_a_matching_page_is_not_flagged():
    audit = audit_landing_page(
        "https://lp.test/x",
        fetcher=fetcher_for(serve(CLEAN_PAGE)),
        ad_texts=[
            "CalmLeaf magnesium glycinate blend, third-party tested",
            "A simpler evening routine before bed with theanine",
        ],
        check_cloaking=False,
    )
    assert "AD_PAGE_MISMATCH" not in codes(audit)


def test_consistency_is_skipped_when_the_ad_text_is_too_short():
    audit = audit_landing_page(
        "https://lp.test/x",
        fetcher=fetcher_for(serve(CLEAN_PAGE)),
        ad_texts=["Buy now"],
        check_cloaking=False,
    )
    assert "AD_PAGE_MISMATCH" not in codes(audit)


# --- monitoring over time --------------------------------------------------


def test_a_check_is_recorded_against_the_offer(session, offer):
    monitor = DestinationMonitor(session, fetcher=fetcher_for(serve(CLEAN_PAGE)))
    check = monitor.check_offer(offer)
    session.commit()

    stored = session.query(LandingPageCheck).one()
    assert stored.id == check.id
    assert stored.offer_id == offer.id
    assert stored.content_hash
    assert not stored.content_changed


def test_a_page_changing_after_approval_is_detected(session, offer):
    """The whole point: the page was fine when the ad was approved."""
    monitor = DestinationMonitor(session, fetcher=fetcher_for(serve(CLEAN_PAGE)))
    monitor.check_offer(offer)
    session.commit()

    swapped = CLEAN_PAGE.replace(BODY, "The advertiser replaced this page entirely " * 12)
    monitor.fetcher = fetcher_for(serve(swapped))
    second = monitor.check_offer(offer)
    session.commit()

    assert second.content_changed


def test_an_unchanged_page_is_not_reported_as_changed(session, offer):
    monitor = DestinationMonitor(session, fetcher=fetcher_for(serve(CLEAN_PAGE)))
    monitor.check_offer(offer)
    session.commit()
    second = monitor.check_offer(offer)
    session.commit()
    assert not second.content_changed


def test_a_sweep_skips_pages_checked_recently(session, offer, settings):
    from adgenie.models import Campaign

    session.add(
        Campaign(
            offer_id=offer.id, platform=Platform.META, name="c",
            external_id="c1", status=EntityStatus.ACTIVE,
        )
    )
    session.commit()

    monitor = DestinationMonitor(session, fetcher=fetcher_for(serve(CLEAN_PAGE)))
    first = monitor.sweep(max_age_hours=24)
    assert first["checked"] == 1

    second = monitor.sweep(max_age_hours=24)
    assert second["checked"] == 0
    assert second["skipped"] == 1


def test_a_sweep_reports_a_destination_that_now_fails(session, offer, settings):
    from adgenie.models import Campaign

    session.add(
        Campaign(
            offer_id=offer.id, platform=Platform.META, name="c",
            external_id="c1", status=EntityStatus.ACTIVE,
        )
    )
    session.commit()

    monitor = DestinationMonitor(session, fetcher=fetcher_for(serve("", 410)))
    summary = monitor.sweep()
    assert summary["blocking"]
    assert summary["blocking"][0]["offer_id"] == offer.id


def _blocking_campaign(session, offer):
    from adgenie.models import Campaign

    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="c",
        external_id="c1", status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.commit()
    return campaign


def test_offenders_can_be_paused(session, offer, settings):
    campaign = _blocking_campaign(session, offer)

    monitor = DestinationMonitor(
        session, fetcher=fetcher_for(serve("", 410)), settings=settings
    )
    summary = monitor.sweep()
    result = monitor.pause_offenders(summary)

    assert result["applied"] is True
    assert campaign.id in result["campaign_ids"]
    assert campaign.status is EntityStatus.PAUSED
    assert "landing page" in campaign.last_error


def test_a_dry_run_sweep_pauses_nothing(session, offer, settings):
    """A sweep against live credentials must not stop production by accident."""
    campaign = _blocking_campaign(session, offer)
    dry = settings.model_copy(update={"dry_run": True})

    monitor = DestinationMonitor(
        session, fetcher=fetcher_for(serve("", 410)), settings=dry
    )
    summary = monitor.sweep()

    class Exploding:
        def client(self, platform):  # pragma: no cover - must never be called
            raise AssertionError("a dry run reached the platform")

    result = monitor.pause_offenders(summary, orchestrator=Exploding())

    assert result["applied"] is False
    # It still says which campaigns it would have stopped.
    assert campaign.id in result["campaign_ids"]
    assert campaign.status is EntityStatus.ACTIVE
    assert campaign.last_error is None


def test_pause_offenders_defaults_to_the_safe_side(session, offer):
    """No settings passed means the ambient ones, which default to a dry run."""
    campaign = _blocking_campaign(session, offer)
    monitor = DestinationMonitor(session, fetcher=fetcher_for(serve("", 410)))
    assert monitor.settings.dry_run is True

    result = monitor.pause_offenders(monitor.sweep())

    assert result["applied"] is False
    assert campaign.status is EntityStatus.ACTIVE


def test_a_sweep_survives_a_timezone_aware_stored_check(session, offer, settings):
    """The stored timestamp may be aware or naive depending on the session.

    `checked_at` is a naive column with a timezone-aware default, so whether a
    row reads back aware depends on whether it is still in the identity map.
    Comparing the two directly raises TypeError and takes the sweep with it.
    """
    from datetime import datetime, timezone

    from adgenie.models import Campaign

    session.add(
        Campaign(
            offer_id=offer.id, platform=Platform.META, name="c",
            external_id="c1", status=EntityStatus.ACTIVE,
        )
    )
    session.commit()

    monitor = DestinationMonitor(
        session, fetcher=fetcher_for(serve(CLEAN_PAGE)), settings=settings
    )
    check = monitor.check_offer(offer)
    check.checked_at = datetime.now(timezone.utc)
    session.commit()

    summary = monitor.sweep(max_age_hours=24)
    assert summary["skipped"] == 1
    assert summary["checked"] == 0


# --- refusing to launch ----------------------------------------------------


def test_a_failing_destination_stops_the_launch(session, offer, settings, sandbox_meta):
    """No campaign at all, rather than paused wreckage in the ad account."""
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan
    from adgenie.models import Campaign

    settings.audit_landing_pages = True
    result = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta,
        destination_monitor=DestinationMonitor(
            session, fetcher=fetcher_for(serve("", 404))
        ),
    ).launch(
        LaunchPlan(offer_id=offer.id, platform=Platform.META, daily_budget_usd=20.0)
    )

    assert result.errors
    assert result.campaign_id == 0
    assert session.query(Campaign).count() == 0
    assert sandbox_meta.calls == []


def test_a_good_destination_lets_the_launch_proceed(
    session, offer, settings, sandbox_meta
):
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    settings.audit_landing_pages = True
    result = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta,
        destination_monitor=DestinationMonitor(
            session, fetcher=fetcher_for(serve(CLEAN_PAGE))
        ),
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META,
            daily_budget_usd=20.0, angle_count=1,
        )
    )
    assert result.creative_ids
    assert result.landing_page["verdict"] != "block"


def test_the_operator_can_override_a_failing_destination(
    session, offer, settings, sandbox_meta
):
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    settings.audit_landing_pages = True
    result = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta,
        destination_monitor=DestinationMonitor(
            session, fetcher=fetcher_for(serve("", 404))
        ),
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META, daily_budget_usd=20.0,
            angle_count=1, ignore_landing_page_findings=True,
        )
    )
    assert result.creative_ids


def test_an_audit_that_errors_does_not_stop_a_launch(
    session, offer, settings, sandbox_meta
):
    """A tool that cannot check should not become a tool that blocks work."""
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    class Broken:
        def check_offer(self, offer, **kwargs):
            raise RuntimeError("auditor exploded")

    settings.audit_landing_pages = True
    result = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta,
        destination_monitor=Broken(),
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META,
            daily_budget_usd=20.0, angle_count=1,
        )
    )
    assert result.creative_ids
