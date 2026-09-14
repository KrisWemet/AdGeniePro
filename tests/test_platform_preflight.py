from __future__ import annotations

import httpx

from adgenie.config import Settings
from adgenie.models import Platform
from adgenie.platform_preflight import run_preflight


def _ready_settings(**overrides) -> Settings:
    values = dict(
        environment="staging",
        database_url="sqlite:///./test.db",
        public_base_url="https://track.example.test",
        api_key="operator-key",
        secret_key="not-a-placeholder-secret",
        postback_secret="network-postback-secret",
        dry_run=True,
        global_daily_budget_cap_usd=25.0,
        meta_access_token="token",
        meta_ad_account_id="123456",
        meta_page_id="789",
        meta_pixel_id="456",
        meta_api_version="v26.0",
        google_developer_token="developer-token",
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_refresh_token="refresh-token",
        google_customer_id="1234567890",
    )
    values.update(overrides)
    return Settings(**values)


def _check(report, key):
    return next(c for c in report.checks if c.key == key)


def test_configuration_preflight_blocks_local_tracking_url():
    report = run_preflight(
        _ready_settings(public_base_url="http://localhost:8000"),
        platforms=(Platform.META,),
    )

    assert not report.configuration_ready
    assert _check(report, "tracking.public_url").status == "fail"


def test_configuration_preflight_requires_public_api_auth():
    report = run_preflight(
        _ready_settings(api_key=None),
        platforms=(Platform.META,),
    )

    assert not report.configuration_ready
    assert _check(report, "security.api_key").status == "fail"


def test_meta_sales_test_requires_pixel():
    report = run_preflight(
        _ready_settings(meta_pixel_id=None),
        platforms=(Platform.META,),
    )

    assert not report.configuration_ready
    assert _check(report, "meta.pixel").status == "fail"


def test_meta_preflight_rejects_the_old_api_version():
    report = run_preflight(
        _ready_settings(meta_api_version="v21.0"),
        platforms=(Platform.META,),
    )

    assert not report.configuration_ready
    assert _check(report, "meta.api_version").status == "fail"


def test_preflight_rejects_placeholder_postback_secret():
    report = run_preflight(
        _ready_settings(postback_secret="change-me-postback"),
        platforms=(Platform.META,),
    )

    assert not report.configuration_ready
    assert _check(report, "tracking.postback_secret").status == "fail"


def test_preflight_does_not_require_dry_run_but_warns_if_off():
    report = run_preflight(
        _ready_settings(dry_run=False),
        platforms=(Platform.META,),
    )

    assert report.configuration_ready
    dry = _check(report, "safety.dry_run")
    assert dry.status == "warn"
    assert dry.blocking is False


def test_live_preflight_reads_account_page_and_pixel_and_can_pass():
    class LiveMeta:
        def health_check(self):
            return {
                "ok": True,
                "account": "Test Ad Account",
                "currency": "CAD",
            }

        def _request(self, method, path, params=None):
            assert method == "GET"
            if path == "789":
                return {"id": "789", "name": "Test Page"}
            if path == "456":
                return {"id": "456", "name": "Test Pixel"}
            raise AssertionError(path)

    calls = []

    def factory(platform, settings):
        calls.append(platform)
        return LiveMeta()

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"status": "ok"},
            request=request,
        )
    )
    with httpx.Client(transport=transport) as client:
        report = run_preflight(
            _ready_settings(),
            platforms=(Platform.META,),
            live=True,
            platform_factory=factory,
            sandbox_detector=lambda client: False,
            http_client=client,
        )

    assert calls == [Platform.META]
    assert _check(report, "live.meta").status == "pass"
    assert _check(report, "live.meta_page").status == "pass"
    assert _check(report, "live.meta_pixel").status == "pass"
    assert _check(report, "live.public_health").status == "pass"
    assert report.ready_for_live_test


def test_live_preflight_blocks_an_unreadable_pixel():
    class LiveMeta:
        def health_check(self):
            return {"ok": True, "account": "Account", "currency": "CAD"}

        def _request(self, method, path, params=None):
            if path == "789":
                return {"id": "789", "name": "Page"}
            raise RuntimeError("permission denied")

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "ok"}, request=request)
    )
    with httpx.Client(transport=transport) as client:
        report = run_preflight(
            _ready_settings(),
            platforms=(Platform.META,),
            live=True,
            platform_factory=lambda platform, settings: LiveMeta(),
            sandbox_detector=lambda client: False,
            http_client=client,
        )

    assert _check(report, "live.meta_pixel").status == "fail"
    assert not report.ready_for_live_test


def test_live_preflight_refuses_a_sandbox_adapter():
    class PretendSandbox:
        def health_check(self):  # should never be trusted as proof of live access
            return {"ok": True}

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "ok"}, request=request)
    )
    with httpx.Client(transport=transport) as client:
        report = run_preflight(
            _ready_settings(),
            platforms=(Platform.META,),
            live=True,
            platform_factory=lambda platform, settings: PretendSandbox(),
            sandbox_detector=lambda client: True,
            http_client=client,
        )

    assert _check(report, "live.meta").status == "fail"
    assert not report.ready_for_live_test


def test_google_preflight_catches_oauth_fields_factory_does_not_require():
    report = run_preflight(
        _ready_settings(google_client_id=None, google_client_secret=None),
        platforms=(Platform.GOOGLE,),
    )

    assert not report.configuration_ready
    google = _check(report, "google.credentials")
    assert google.status == "fail"
    assert "GOOGLE_CLIENT_ID" in google.detail
    assert "GOOGLE_CLIENT_SECRET" in google.detail
