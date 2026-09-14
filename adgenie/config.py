"""Runtime configuration for AdGenie Pro.

In local development, absent credentials select the built-in sandbox so the
full pipeline (generate -> review -> launch -> measure -> optimize) can run.
Production refuses ad-platform simulator fallback.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- core ---
    app_name: str = "AdGenie Pro"
    environment: Literal["dev", "staging", "prod"] = "dev"
    database_url: str = "sqlite:///./adgenie.db"
    secret_key: str = "dev-insecure-change-me"

    # Public base URL used to build affiliate tracking links and postbacks.
    public_base_url: str = "http://localhost:8000"

    # --- access control ---
    # When set, every /api route requires this key in an X-API-Key header.
    # These routes launch campaigns and move budgets, so anything reachable
    # from a network other than localhost must set it.
    api_key: str | None = None
    # Browser origins allowed to call the API. "*" is only safe while api_key
    # is unset and the server is bound to localhost.
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # --- safety rails ---
    # When true no live mutation is sent to Meta/Google; actions are recorded
    # as proposals only. This is the default: spending real money is opt-in.
    dry_run: bool = True
    # Actions above this daily-budget delta (USD) require a human to approve.
    auto_apply_budget_ceiling_usd: float = 50.0
    # Hard cap on total daily spend the optimizer may allocate across accounts.
    global_daily_budget_cap_usd: float = 500.0

    # --- copywriting (Anthropic) ---
    anthropic_api_key: str | None = None
    copywriter_model: str = "claude-opus-5"
    copywriter_max_tokens: int = 16000
    # Opus 5 removed sampling params; thinking depth is tuned with effort.
    copywriter_effort: str = "high"
    copywriter_max_repair_attempts: int = 2

    # --- Meta ---
    meta_access_token: str | None = None
    meta_ad_account_id: str | None = None
    meta_page_id: str | None = None
    meta_pixel_id: str | None = None
    # Keep first contact on a currently-supported Marketing API version. v21
    # was the old prototype default and is no longer usable for ad creation.
    meta_api_version: str = "v26.0"

    # --- Google Ads ---
    google_developer_token: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None
    google_customer_id: str | None = None
    google_login_customer_id: str | None = None
    google_conversion_action_id: str | None = None
    google_api_version: str = "v18"

    # --- destination auditing ---
    # Audit the landing page before a launch sends paid traffic to it. On by
    # default: the destination is what gets an ad account banned, and the check
    # costs one request. Turn it off only where the page cannot be reached from
    # the server, such as an offline test environment.
    audit_landing_pages: bool = True
    # Re-audit destinations no more often than this during a sweep.
    landing_page_recheck_hours: int = 24

    # --- media generation (kie.ai) ---
    kie_api_key: str | None = None
    kie_base_url: str = "https://api.kie.ai"
    kie_image_model: str = "nano-banana-pro"
    kie_video_model: str = "veo-3-1"
    kie_poll_interval_seconds: float = 5.0
    kie_poll_timeout_seconds: float = 600.0
    # Generated asset URLs expire within about a day, so they are downloaded
    # and kept locally as soon as a task finishes.
    media_storage_dir: str = "./media"
    # Optional. Media reaches an ad by being uploaded into the ad account from
    # this local copy; a public address is only needed where a platform fetches
    # imagery by URL rather than owning it.
    media_public_base_url: str | None = None

    # --- competitor research (Meta Ad Library) ---
    # Uses meta_access_token. The library returns commercial ads only for
    # EU and UK delivery; elsewhere it carries political and issue ads only.
    ad_library_country_codes: list[str] = Field(
        default_factory=lambda: ["GB", "IE", "DE", "FR", "NL", "ES", "IT"]
    )
    ad_library_page_size: int = 100
    # An ad still running after this many days is treated as proven.
    ad_library_proven_days: int = 30

    # --- affiliate networks ---
    clickbank_api_key: str | None = None
    clickbank_nickname: str | None = None
    clickbank_ins_secret: str | None = None
    # Deliberately the example value: postbacks are rejected until it is
    # changed, so an unconfigured deployment cannot be fed forged revenue.
    postback_secret: str = "change-me-postback"

    # --- optimizer defaults ---
    optimizer_lookback_days: int = 7
    optimizer_min_clicks: int = 30
    optimizer_credible_level: float = 0.90
    target_roas: float = Field(default=1.30, description="Revenue / spend target.")
    scale_step: float = Field(default=0.20, description="Budget increase per cycle.")
    throttle_step: float = Field(default=0.25, description="Budget cut per cycle.")
    kill_payout_multiple: float = Field(
        default=1.5,
        description="Spend beyond N x offer payout with zero conversions triggers a kill review.",
    )
    action_cooldown_hours: int = 12

    @property
    def requires_api_key(self) -> bool:
        return bool(self.api_key)

    def production_errors(self) -> list[str]:
        """Configuration errors only; this never contacts a provider."""
        from urllib.parse import urlparse
        from .core.tracking import secret_is_placeholder

        errors = []
        origin = urlparse(self.public_base_url)
        if (origin.scheme != "https" or not origin.hostname
                or origin.hostname in {"localhost", "127.0.0.1", "track.yourdomain.com"}
                or origin.username or origin.password or origin.query or origin.fragment
                or origin.path not in {"", "/"}):
            errors.append("PUBLIC_BASE_URL must be your public HTTPS origin")
        for name in ("api_key", "secret_key", "postback_secret"):
            value = getattr(self, name) or ""
            if len(value) < 24 or secret_is_placeholder(value):
                errors.append(f"{name.upper()} must be a non-placeholder secret of at least 24 characters")
        if not self.cors_origins or "*" in self.cors_origins:
            errors.append("CORS_ORIGINS must list explicit origins")
        from pathlib import Path
        from sqlalchemy.engine import make_url
        database = make_url(self.database_url)
        if database.get_backend_name() == "sqlite" and (
                not database.database or database.database == ":memory:"
                or not Path(database.database).is_absolute()):
            errors.append("SQLite DATABASE_URL must use an absolute path on persistent storage")
        if self.global_daily_budget_cap_usd <= 0:
            errors.append("GLOBAL_DAILY_BUDGET_CAP_USD must be positive")
        return errors

    @property
    def has_meta(self) -> bool:
        return bool(self.meta_access_token and self.meta_ad_account_id)

    @property
    def has_google(self) -> bool:
        return bool(
            self.google_developer_token
            and self.google_refresh_token
            and self.google_customer_id
        )

    @property
    def has_copywriter_llm(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_media_generation(self) -> bool:
        return bool(self.kie_api_key)

    @property
    def has_ad_library(self) -> bool:
        # The Ad Library rides on the same token as the Marketing API, but only
        # needs ads_read, so an account with no ad account can still research.
        return bool(self.meta_access_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache (used by tests that patch the environment)."""
    get_settings.cache_clear()
    return get_settings()


__all__ = ["Settings", "get_settings", "reload_settings", "os"]
