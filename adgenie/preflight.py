"""Production-readiness checks for the first real ad-account test.

The preflight is deliberately read-only. With ``--live`` it talks to configured
platforms and the deployed AdGenie health endpoint, but it never creates,
updates, pauses or enables an ad object. Its job is to prove the plumbing before
``DRY_RUN`` is ever turned off.

Run the first Meta check with::

    python -m adgenie.preflight --platform meta --live

Without ``--live`` only configuration is inspected, so it is safe to run on a
laptop with no network access.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Literal
from urllib.parse import urlparse

import httpx

from .config import Settings, get_settings
from .core.tracking import secret_is_placeholder
from .models import Platform
from .platforms.factory import get_platform, is_sandbox

Status = Literal["pass", "warn", "fail", "skip"]


@dataclass(frozen=True)
class PreflightCheck:
    key: str
    status: Status
    detail: str
    blocking: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]
    live_requested: bool

    @property
    def configuration_ready(self) -> bool:
        return not any(c.status == "fail" and c.blocking for c in self.checks)

    @property
    def live_verified(self) -> bool:
        if not self.live_requested:
            return False
        live_checks = [c for c in self.checks if c.key.startswith("live.")]
        return bool(live_checks) and all(c.status == "pass" for c in live_checks)

    @property
    def ready_for_live_test(self) -> bool:
        return self.configuration_ready and self.live_verified

    def as_dict(self) -> dict:
        return {
            "configuration_ready": self.configuration_ready,
            "live_requested": self.live_requested,
            "live_verified": self.live_verified,
            "ready_for_live_test": self.ready_for_live_test,
            "checks": [c.as_dict() for c in self.checks],
        }


def _public_origin_check(settings: Settings) -> PreflightCheck:
    parsed = urlparse(settings.public_base_url)
    host = (parsed.hostname or "").lower()
    local_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    if parsed.scheme != "https" or not host or host in local_hosts:
        return PreflightCheck(
            "tracking.public_url",
            "fail",
            "PUBLIC_BASE_URL must be a public HTTPS origin before paid traffic can be tracked.",
        )
    return PreflightCheck(
        "tracking.public_url", "pass", f"Public tracking origin is {settings.public_base_url.rstrip('/')}"
    )


def _security_checks(settings: Settings) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    checks.append(_public_origin_check(settings))

    if settings.api_key:
        checks.append(PreflightCheck("security.api_key", "pass", "API routes require X-API-Key."))
    else:
        checks.append(
            PreflightCheck(
                "security.api_key",
                "fail",
                "API_KEY is unset. A public deployment would expose campaign and budget controls.",
            )
        )

    insecure_secret_keys = {
        "",
        "dev-insecure-change-me",
        "change-me",
        "change-me-to-something-random",
        "changeme",
        "secret",
    }
    if (settings.secret_key or "").strip().lower() in insecure_secret_keys:
        checks.append(
            PreflightCheck(
                "security.secret_key",
                "fail",
                "SECRET_KEY is still a development/example value.",
            )
        )
    else:
        checks.append(PreflightCheck("security.secret_key", "pass", "SECRET_KEY is non-placeholder."))

    if secret_is_placeholder(settings.postback_secret):
        checks.append(
            PreflightCheck(
                "tracking.postback_secret",
                "fail",
                "POSTBACK_SECRET is still an example value; conversion revenue would be rejected.",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "tracking.postback_secret", "pass", "POSTBACK_SECRET is non-placeholder."
            )
        )

    if settings.global_daily_budget_cap_usd <= 0:
        checks.append(
            PreflightCheck(
                "safety.daily_cap",
                "fail",
                "GLOBAL_DAILY_BUDGET_CAP_USD must be positive for a controlled live test.",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "safety.daily_cap",
                "pass",
                f"Global daily spend cap is ${settings.global_daily_budget_cap_usd:.2f}.",
            )
        )

    if settings.dry_run:
        checks.append(
            PreflightCheck(
                "safety.dry_run",
                "pass",
                "DRY_RUN is on. Preflight can contact live accounts without allowing mutations.",
                blocking=False,
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "safety.dry_run",
                "warn",
                "DRY_RUN is off. Preflight is read-only, but other commands can mutate live accounts.",
                blocking=False,
            )
        )

    if settings.database_url.startswith("sqlite"):
        checks.append(
            PreflightCheck(
                "database.production",
                "warn",
                "SQLite is acceptable for a single-instance smoke test, but not for multi-instance production.",
                blocking=False,
            )
        )
    else:
        checks.append(
            PreflightCheck("database.production", "pass", "Database is not SQLite.", blocking=False)
        )
    return checks


def _meta_config_checks(settings: Settings) -> list[PreflightCheck]:
    missing = [
        name
        for name, value in (
            ("META_ACCESS_TOKEN", settings.meta_access_token),
            ("META_AD_ACCOUNT_ID", settings.meta_ad_account_id),
            ("META_PAGE_ID", settings.meta_page_id),
        )
        if not value
    ]
    checks: list[PreflightCheck] = []
    if missing:
        checks.append(
            PreflightCheck(
                "meta.credentials",
                "fail",
                "Missing " + ", ".join(missing) + ".",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "meta.credentials",
                "pass",
                "Meta token, ad account and Page id are configured.",
            )
        )
    if settings.meta_pixel_id:
        checks.append(
            PreflightCheck(
                "meta.pixel",
                "pass",
                "META_PIXEL_ID is configured for sending affiliate conversions back to Meta.",
                blocking=False,
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "meta.pixel",
                "warn",
                "META_PIXEL_ID is unset. A paused campaign can be tested, but Conversions API upload cannot work yet.",
                blocking=False,
            )
        )
    return checks


def _google_config_checks(settings: Settings) -> list[PreflightCheck]:
    required = (
        ("GOOGLE_DEVELOPER_TOKEN", settings.google_developer_token),
        ("GOOGLE_CLIENT_ID", settings.google_client_id),
        ("GOOGLE_CLIENT_SECRET", settings.google_client_secret),
        ("GOOGLE_REFRESH_TOKEN", settings.google_refresh_token),
        ("GOOGLE_CUSTOMER_ID", settings.google_customer_id),
    )
    missing = [name for name, value in required if not value]
    if missing:
        return [
            PreflightCheck(
                "google.credentials",
                "fail",
                "Missing " + ", ".join(missing) + ".",
            )
        ]
    return [
        PreflightCheck(
            "google.credentials",
            "pass",
            "Google developer token, OAuth credentials and customer id are configured.",
        )
    ]


def _platform_live_check(
    platform: Platform,
    settings: Settings,
    platform_factory: Callable,
    sandbox_detector: Callable,
) -> PreflightCheck:
    key = f"live.{platform.value}"
    try:
        client = platform_factory(platform, settings)
        if sandbox_detector(client):
            return PreflightCheck(
                key,
                "fail",
                f"{platform.value.title()} resolved to the simulator instead of a live adapter.",
            )
        status = client.health_check()
    except Exception as exc:
        return PreflightCheck(key, "fail", f"Read-only {platform.value} check failed: {exc}")

    if not status.get("ok"):
        return PreflightCheck(
            key,
            "fail",
            f"Read-only {platform.value} check failed: {status.get('error') or status}",
        )
    account = status.get("account") or "account accessible"
    currency = status.get("currency") or "currency unknown"
    return PreflightCheck(
        key,
        "pass",
        f"Read-only API call succeeded: {account} ({currency}).",
    )


def _public_health_check(
    settings: Settings, http_client: httpx.Client | None = None
) -> PreflightCheck:
    key = "live.public_health"
    parsed = urlparse(settings.public_base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return PreflightCheck(key, "fail", "Public health check skipped because PUBLIC_BASE_URL is invalid.")

    client = http_client or httpx.Client(timeout=10.0, follow_redirects=True)
    owns_client = http_client is None
    headers = {"X-API-Key": settings.api_key} if settings.api_key else {}
    try:
        response = client.get(settings.public_base_url.rstrip("/") + "/api/health", headers=headers)
        if response.status_code != 200:
            return PreflightCheck(
                key,
                "fail",
                f"Public /api/health returned HTTP {response.status_code}.",
            )
        body = response.json()
        if body.get("status") != "ok":
            return PreflightCheck(key, "fail", f"Public health endpoint returned {body}.")
        return PreflightCheck(
            key,
            "pass",
            f"Public AdGenie health endpoint is reachable at {settings.public_base_url.rstrip('/')}.",
        )
    except Exception as exc:
        return PreflightCheck(key, "fail", f"Public health endpoint is not reachable: {exc}")
    finally:
        if owns_client:
            client.close()


def run_preflight(
    settings: Settings | None = None,
    *,
    platforms: Iterable[Platform] = (Platform.META, Platform.GOOGLE),
    live: bool = False,
    platform_factory: Callable | None = None,
    sandbox_detector: Callable | None = None,
    http_client: httpx.Client | None = None,
) -> PreflightReport:
    """Run configuration checks and optionally read-only live calls."""

    settings = settings or get_settings()
    selected = tuple(platforms)
    checks = _security_checks(settings)

    if Platform.META in selected:
        checks.extend(_meta_config_checks(settings))
    if Platform.GOOGLE in selected:
        checks.extend(_google_config_checks(settings))

    if live:
        factory = platform_factory or get_platform
        detector = sandbox_detector or is_sandbox
        for platform in selected:
            credential_key = f"{platform.value}.credentials"
            credential_failed = any(
                c.key == credential_key and c.status == "fail" for c in checks
            )
            if credential_failed:
                checks.append(
                    PreflightCheck(
                        f"live.{platform.value}",
                        "fail",
                        "Live check cannot run until required credentials are configured.",
                    )
                )
            else:
                checks.append(_platform_live_check(platform, settings, factory, detector))
        checks.append(_public_health_check(settings, http_client=http_client))

    return PreflightReport(tuple(checks), live_requested=live)


def _selected_platforms(value: str) -> tuple[Platform, ...]:
    if value == "meta":
        return (Platform.META,)
    if value == "google":
        return (Platform.GOOGLE,)
    return (Platform.META, Platform.GOOGLE)


def _print_report(report: PreflightReport) -> None:
    icons = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    for check in report.checks:
        print(f"{icons[check.status]:<4}  {check.key:<26} {check.detail}")
    print()
    if not report.live_requested:
        state = "CONFIG READY" if report.configuration_ready else "CONFIG NOT READY"
        print(f"{state}. Run again with --live to verify real accounts and the public endpoint.")
    else:
        print(
            "READY FOR LIVE TEST: " + ("YES" if report.ready_for_live_test else "NO")
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="adgenie-preflight",
        description="Prove AdGenie is ready for a live test without mutating an ad account.",
    )
    parser.add_argument(
        "--platform",
        choices=("meta", "google", "all"),
        default="all",
        help="which live integration must be ready",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="make read-only calls to the selected ad platform(s) and the public health endpoint",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args(argv)

    report = run_preflight(platforms=_selected_platforms(args.platform), live=args.live)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        _print_report(report)

    if args.live:
        return 0 if report.ready_for_live_test else 1
    return 0 if report.configuration_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
