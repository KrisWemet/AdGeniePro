from __future__ import annotations

import httpx

from adgenie.config import Settings
from adgenie.money import usd_to_micros
from adgenie.platforms.base import CampaignSpec
from adgenie.platforms.factory import get_platform, reset_sandboxes
from adgenie.platforms.meta import MetaAdsClient
from adgenie.platforms.meta_current import CurrentMetaAdsClient


def _settings(**overrides) -> Settings:
    values = dict(
        meta_access_token="tok",
        meta_ad_account_id="123",
        meta_page_id="page1",
        meta_api_version="v26.0",
        dry_run=False,
    )
    values.update(overrides)
    return Settings(**values)


def test_live_factory_uses_current_meta_adapter():
    reset_sandboxes()
    client = get_platform(Platform.META, _settings(dry_run=True))
    try:
        assert isinstance(client, CurrentMetaAdsClient)
        assert isinstance(client, MetaAdsClient)
    finally:
        reset_sandboxes()


def test_current_meta_marks_adset_budget_sharing_off():
    seen = {}

    def handler(request):
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"id": "camp_1"})

    client = CurrentMetaAdsClient(
        _settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        dry_run=False,
    )
    try:
        result = client.create_campaign(
            CampaignSpec(
                name="Friday smoke test",
                objective="OUTCOME_SALES",
                daily_budget_micros=usd_to_micros(10),
                status="PAUSED",
            )
        )
    finally:
        client._client.close()

    assert result == "camp_1"
    assert seen["is_adset_budget_sharing_enabled"] == "false"
    assert "daily_budget" not in seen


def test_campaign_level_budget_does_not_send_adset_sharing_flag():
    seen = {}

    def handler(request):
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"id": "camp_2"})

    client = CurrentMetaAdsClient(
        _settings(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        dry_run=False,
    )
    try:
        client.create_campaign(
            CampaignSpec(
                name="CBO",
                objective="OUTCOME_SALES",
                daily_budget_micros=usd_to_micros(10),
                status="PAUSED",
                extra={"campaign_budget_optimization": True},
            )
        )
    finally:
        client._client.close()

    assert "is_adset_budget_sharing_enabled" not in seen
    assert seen["daily_budget"] == "1000"
