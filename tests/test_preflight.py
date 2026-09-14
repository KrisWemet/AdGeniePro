"""Read-only checks distinguish a simulator, invalid credentials and live access."""

from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import select

from adgenie.cli import build_parser
from adgenie.models import Campaign, EntityStatus, Platform
from adgenie.platforms.base import PlatformError
from adgenie.platforms.factory import get_platform
from adgenie.preflight import run_preflight


def ready_settings(settings):
    settings.api_key = "a" * 32
    settings.secret_key = "b" * 32
    settings.postback_secret = "c" * 32
    settings.cors_origins = ["https://track.test"]
    settings.database_url = "sqlite:////data/adgenie.db"
    settings.audit_landing_pages = True
    settings.clickbank_ins_secret = "CBTEST1234567890"
    settings.clickbank_nickname = "testaff"
    settings.meta_access_token = "TEST_TOKEN_DO_NOT_PRINT"
    settings.meta_ad_account_id = "123"
    settings.meta_page_id = "456"
    settings.meta_pixel_id = "789"
    settings.kie_api_key = "fixture-kie-key"
    return settings


def public_transport(request):
    assert request.method == "GET"
    if request.url.path == "/healthz":
        return httpx.Response(200, json={"status": "ok", "service": "adgenie", "capabilities": ["clickbank_ins_v8"]})
    return httpx.Response(422 if request.url.path == "/r" else 405)


def test_preflight_passes_read_checks_but_never_declares_spend_ready(session, settings, offer):
    settings = ready_settings(settings)
    offer.destination_url = "https://abc.hop.clickbank.net/"
    session.commit()
    client = Mock()
    client.health_check.return_value = {"ok": True, "currency": "USD"}
    client._request.side_effect = lambda method, path, **kwargs: {"id": path}
    with httpx.Client(transport=httpx.MockTransport(public_transport)) as http:
        report = run_preflight(session, settings, Platform.META, offer.id, client=client, http=http, check_destination=False)
    assert report["ready_for_paused_launch"] is True
    assert report["ready_to_spend"] is False
    assert "TEST_TOKEN_DO_NOT_PRINT" not in str(report)
    for call in client._request.call_args_list:
        assert call.args[0] == "GET"
    assert not session.new and not session.dirty


def test_missing_credentials_never_pass_as_live_connection(session, settings, offer):
    with httpx.Client(transport=httpx.MockTransport(public_transport)) as http:
        report = run_preflight(session, settings, Platform.META, offer.id, http=http, check_destination=False)
    assert not report["ready_for_paused_launch"]
    assert any(c["check"] == "ad_account" and c["status"] == "fail" for c in report["checks"])


def test_canadian_dollar_account_does_not_mix_with_usd_revenue(session, settings, offer):
    settings = ready_settings(settings)
    client = Mock()
    client.health_check.return_value = {"ok": True, "currency": "CAD"}
    client._request.side_effect = lambda method, path, **kwargs: {"id": path}
    with httpx.Client(transport=httpx.MockTransport(public_transport)) as http:
        report = run_preflight(session, settings, Platform.META, offer.id, client=client, http=http, check_destination=False)
    assert any(c["check"] == "account_currency" and c["status"] == "fail" for c in report["checks"])


def test_production_fails_closed_instead_of_simulating(settings):
    settings.environment = "prod"
    settings.meta_access_token = None
    with pytest.raises(PlatformError, match="fallback refused"):
        get_platform(Platform.META, settings)


def test_dry_status_request_changes_neither_database_nor_platform(api_client, settings, session, monkeypatch):
    campaign = Campaign(offer_id=1, name="paused", platform=Platform.META,
                        status=EntityStatus.PAUSED, external_id="123")
    session.add(campaign)
    session.commit()
    settings.dry_run = True
    client_method = Mock(side_effect=AssertionError("must not contact adapter"))
    monkeypatch.setattr("adgenie.core.orchestrator.Orchestrator.client", client_method)
    response = api_client.post(f"/api/campaigns/{campaign.id}/status?active=true")
    assert response.status_code == 200
    assert response.json()["applied"] is False
    session.expire_all()
    assert session.scalar(select(Campaign)).status is EntityStatus.PAUSED
    client_method.assert_not_called()


def test_dry_created_campaign_cannot_be_activated_later(api_client, settings, session):
    campaign = Campaign(offer_id=1, name="dry", platform=Platform.META,
                        status=EntityStatus.PAUSED, external_id="dryrun_123")
    session.add(campaign)
    session.commit()
    settings.dry_run = False
    assert api_client.post(f"/api/campaigns/{campaign.id}/status?active=true").status_code == 409


def test_cli_exposes_preflight_and_conversion_upload():
    parser = build_parser()
    assert parser.parse_args(["preflight", "--offer", "1"]).platform == "meta"
    assert parser.parse_args(["push-conversions", "--hours", "72"]).hours == 72


def test_preflight_does_not_create_a_missing_sqlite_database(tmp_path, settings):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    path = tmp_path / "missing.db"
    settings.database_url = "sqlite:///" + str(path)
    with Session(create_engine(settings.database_url)) as session:
        with httpx.Client(transport=httpx.MockTransport(public_transport)) as http:
            report = run_preflight(session, settings, Platform.META, 1, http=http, check_destination=False)
    assert not report["ready_for_paused_launch"]
    assert not path.exists()


def test_activation_checks_total_budget_before_any_platform_call(api_client, settings, session, monkeypatch):
    session.add(Campaign(offer_id=1, name="already running", platform=Platform.META,
        daily_budget_micros=400_000_000, status=EntityStatus.ACTIVE, external_id="live1"))
    paused = Campaign(offer_id=1, name="would exceed cap", platform=Platform.META,
        daily_budget_micros=200_000_000, status=EntityStatus.PAUSED, external_id="live2")
    session.add(paused)
    session.commit()
    calls = Mock(side_effect=AssertionError("must not spend"))
    monkeypatch.setattr("adgenie.core.orchestrator.Orchestrator.client", calls)
    response = api_client.post(f"/api/campaigns/{paused.id}/status?active=true")
    assert response.status_code == 422
    calls.assert_not_called()


def test_production_refuses_an_immediately_active_launch(session, settings, offer, sandbox_meta):
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan
    ready_settings(settings)
    settings.environment = "prod"
    result = CampaignLauncher(session, settings, platform_client=sandbox_meta).launch(
        LaunchPlan(offer_id=offer.id, platform=Platform.META, daily_budget_usd=10,
                   start_paused=False, generate_media=True))
    assert not result.ok
    assert any("created paused" in error for error in result.errors)
    assert sandbox_meta.calls == []


def test_public_health_does_not_require_admin_key_or_query_ad_platforms(api_client, settings, monkeypatch):
    settings.api_key = "a" * 32
    monkeypatch.setattr("adgenie.main.get_platform", Mock(side_effect=AssertionError("no network")))
    response = api_client.get("/healthz")
    assert response.status_code == 200
    assert "clickbank_ins_v8" in response.json()["capabilities"]
    assert "a" * 32 not in response.text


def test_production_boot_refuses_public_api_without_auth(api_client, settings):
    settings.environment = "prod"
    errors = settings.production_errors()
    assert any("API_KEY" in error for error in errors)
    assert any("CORS_ORIGINS" in error for error in errors)
