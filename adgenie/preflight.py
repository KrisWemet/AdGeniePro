"""Read-only readiness checks. Passing these never authorizes ad spending."""

from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from .config import Settings
from .models import ClickBankReceipt, Offer, Platform
from .networks.clickbank import configured
from .platforms.factory import get_platform, is_sandbox


def run_preflight(session: Session, settings: Settings, platform: Platform,
                  offer_id: int | None = None, *, client=None, http=None,
                  check_destination: bool = True) -> dict:
    checks = []

    def add(name, ok, detail, *, warning=False):
        checks.append({"check": name, "status": "pass" if ok else ("warning" if warning else "fail"),
                       "detail": detail})

    errors = settings.production_errors()
    add("production_config", not errors, "; ".join(errors) or "Public URL, API authentication, secrets and cap configured")
    add("spending_mode", True, "Dry run: no ad mutations" if settings.dry_run else "Live writes enabled; create only a paused campaign")
    add("landing_audit_enabled", settings.audit_landing_pages, "AUDIT_LANDING_PAGES must remain enabled for the first test")
    db_ready = False
    try:
        actual_url = session.get_bind().url
        if actual_url.get_backend_name() == "sqlite" and actual_url.database not in {None, "", ":memory:"}:
            if not Path(actual_url.database).is_file():
                raise FileNotFoundError("Run init before preflight")
        session.execute(text("SELECT 1"))
        tables = set(inspect(session.get_bind()).get_table_names())
        missing = {"offers", "campaigns", "clicks", "conversions", "clickbank_receipts"} - tables
        db_ready = not missing
        add("database_schema", db_ready, "Schema readable" if db_ready else "Run init; missing tables: " + ", ".join(sorted(missing)))
        url = make_url(settings.database_url)
        if url.get_backend_name() == "sqlite":
            persistent = bool(url.database and url.database != ":memory:" and Path(url.database).is_absolute())
            add("database_storage", persistent, "SQLite requires an absolute path on a persistent mounted disk", warning=True)
        else:
            add("database_storage", True, "External database configured; verify its backup and restore separately")
    except Exception:
        session.rollback()
        add("database_schema", False, "Database unavailable or schema inspection failed; credentials are omitted")

    offer = session.get(Offer, offer_id) if db_ready and offer_id else None
    add("offer", offer is not None, "Offer loaded" if offer else "Provide --offer with an existing offer ID")
    if offer:
        add("offer_value", offer.expected_value_micros() > 0, "Expected affiliate payout must be positive")
        if offer.network.lower() == "clickbank":
            add("clickbank_ins", configured(settings), "Configure affiliate nickname and a 1–16 character uppercase alphanumeric INS secret")
            host = urlparse(offer.destination_url).hostname or ""
            add("clickbank_hoplink", urlparse(offer.destination_url).scheme == "https" and
                (host == "hop.clickbank.net" or host.endswith(".hop.clickbank.net")),
                "The first test requires an HTTPS ClickBank HopLink; decode the checkout affiliate manually")
            count = session.query(ClickBankReceipt).count()
            add("clickbank_live_evidence", count > 0,
                f"{count} receipt ledger(s) present; reconcile them with ClickBank before trusting revenue", warning=True)

    owned_http = http is None
    http = http or httpx.Client(timeout=20, follow_redirects=False)
    try:
        # No /r query is sent: probing routing must not create synthetic clicks.
        origin = settings.public_base_url.rstrip("/")
        if urlparse(origin).scheme == "https" and not errors:
            try:
                response = http.get(origin + "/healthz")
                body = response.json()
                add("public_service", response.status_code == 200 and body.get("service") == "adgenie"
                    and "clickbank_ins_v8" in body.get("capabilities", []),
                    "HTTPS service must expose the ClickBank-capable build")
                response = http.get(origin + "/r")
                add("public_redirect_route", response.status_code == 422,
                    "Tracking route responds without creating a test click")
                response = http.get(origin + "/postback/clickbank")
                add("public_ins_route", response.status_code == 405,
                    "INS route is POST-only; use ClickBank Test URL to verify delivery and decryption")
            except Exception:
                add("public_service", False, "Public service probe failed; no secrets are included in this report")
        else:
            add("public_service", False, "Fix production configuration before probing the public service")

        configured_platform = settings.has_meta if platform is Platform.META else settings.has_google
        if not configured_platform:
            add("ad_account", False, f"{platform.value} credentials missing; simulator is not a live connection")
        else:
            try:
                client = client or get_platform(platform, settings)
                health = client.health_check()
                add("ad_account", bool(health.get("ok")) and not is_sandbox(client),
                    "Live account read succeeded" if health.get("ok") else "Live account read failed; check token, permissions and API version")
                add("account_currency", health.get("currency") == "USD",
                    "Current revenue and spend accounting requires a USD ad account; no currency conversion is implemented")
                if platform is Platform.META:
                    for name, object_id in (("page_access", settings.meta_page_id), ("pixel_access", settings.meta_pixel_id)):
                        if not object_id:
                            add(name, False, "Configure the corresponding Meta object ID")
                            continue
                        try:
                            body = client._request("GET", object_id, params={"fields": "id"})
                            add(name, body.get("id") == object_id, "Read access verified; assignment and write permissions still require a paused launch")
                        except Exception:
                            add(name, False, "Object read failed; check ID and token access")
                else:
                    add("google_oauth", bool(settings.google_client_id and settings.google_client_secret), "OAuth client ID and secret required")
                    add("google_conversions", bool(settings.google_conversion_action_id), "Configure the offline conversion action")
            except Exception:
                add("ad_account", False, "Account probe failed; verify credentials and installed dependencies")
    finally:
        if owned_http:
            http.close()

    if offer and check_destination:
        from .core.landing import LandingPageFetcher, audit_landing_page
        from .core.tracking import TrackingContext, build_prelanding_url
        try:
            landing_url = build_prelanding_url(
                TrackingContext(offer_id=offer.id), settings=settings
            )
            with LandingPageFetcher() as fetcher:
                audit = audit_landing_page(landing_url, fetcher=fetcher, offer=offer)
                vendor_audit = audit_landing_page(
                    offer.destination_url, fetcher=fetcher, offer=offer
                )
            add("destination_audit", audit.passed,
                "Controlled pre-landing page passed" if audit.passed else ", ".join(f.code for f in audit.blocking))
            add(
                "vendor_destination_audit",
                vendor_audit.passed,
                (
                    "Vendor destination passed automated review"
                    if vendor_audit.passed
                    else "Manual review required: "
                    + ", ".join(f.code for f in vendor_audit.blocking)
                ),
                warning=True,
            )
        except Exception:
            add("destination_audit", False, "Could not complete destination audit")
    if platform is Platform.META:
        add("media_provider", settings.has_media_generation,
            "KIE_API_KEY required by the generated-image launch path; real generation still needs verification")
    add("live_verification", False,
        "Still required: affiliate checkout decode, ClickBank Test URL, paused ad review, and a reconciled real sale", warning=True)
    return {"ready_for_paused_launch": not any(c["status"] == "fail" for c in checks),
            "ready_to_spend": False, "platform": platform.value,
            "dry_run": settings.dry_run, "checks": checks}
