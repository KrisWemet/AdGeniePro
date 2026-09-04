"""The contract every ad platform adapter implements.

Meta and Google model the world differently (ad set vs. ad group, cents vs.
micros, one ad object vs. an asset-based responsive ad). The optimizer should
not care. This interface is the narrow waist: everything above it speaks in
`AdGenie` terms, everything below translates.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date

from ..models import Platform


class PlatformError(RuntimeError):
    """A call to an ad platform failed.

    `retryable` distinguishes a rate limit or transient outage (retry) from a
    rejected creative or invalid targeting (do not retry, surface to a human).
    """

    def __init__(
        self,
        message: str,
        *,
        platform: Platform | None = None,
        code: str | int | None = None,
        retryable: bool = False,
        payload: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.platform = platform
        self.code = code
        self.retryable = retryable
        self.payload = payload or {}


@dataclass
class CampaignSpec:
    name: str
    objective: str
    daily_budget_micros: int
    status: str = "PAUSED"
    bid_strategy: str = "LOWEST_COST"
    target_roas: float | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class AdGroupSpec:
    campaign_external_id: str
    name: str
    daily_budget_micros: int = 0
    bid_micros: int = 0
    status: str = "PAUSED"
    targeting: dict = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class CreativeSpec:
    ad_group_external_id: str
    name: str
    final_url: str
    headlines: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    primary_texts: list[str] = field(default_factory=list)
    call_to_action: str = "LEARN_MORE"
    media_urls: list[str] = field(default_factory=list)
    display_url_path: list[str] = field(default_factory=list)
    status: str = "PAUSED"
    extra: dict = field(default_factory=dict)


# Dimensions delivery can be split along. Meta and Google name them
# differently; these are the platform-neutral names the optimizer reasons in.
BREAKDOWN_PLACEMENT = "placement"
BREAKDOWN_DEVICE = "device"
BREAKDOWN_AGE_GENDER = "age_gender"
BREAKDOWN_REGION = "region"
BREAKDOWN_HOUR = "hour"

SUPPORTED_BREAKDOWNS = (
    BREAKDOWN_PLACEMENT,
    BREAKDOWN_DEVICE,
    BREAKDOWN_AGE_GENDER,
    BREAKDOWN_REGION,
    BREAKDOWN_HOUR,
)


@dataclass
class BreakdownRow:
    """Delivery for one entity, one day, sliced along one dimension.

    Exclusions are where most of the easy ROAS lives. An affiliate campaign
    frequently has one placement or one age bracket quietly consuming a third
    of the budget at a fraction of the conversion rate, and the campaign total
    hides it completely.
    """

    external_id: str
    day: date
    dimension: str
    segment: str
    impressions: int = 0
    clicks: int = 0
    spend_micros: int = 0
    conversions: float = 0.0
    conversion_value_micros: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class InsightRow:
    """One entity's delivery for one day, already normalised to micros."""

    external_id: str
    day: date
    impressions: int = 0
    clicks: int = 0
    spend_micros: int = 0
    conversions: float = 0.0
    conversion_value_micros: int = 0
    frequency: float = 0.0
    reach: int = 0
    video_views: int = 0
    raw: dict = field(default_factory=dict)


class AdPlatform(abc.ABC):
    """Create, mutate and measure ads on one platform."""

    platform: Platform

    # -- creation --
    @abc.abstractmethod
    def create_campaign(self, spec: CampaignSpec) -> str:
        """Return the platform's campaign id."""

    @abc.abstractmethod
    def create_ad_group(self, spec: AdGroupSpec) -> str:
        """Return the platform's ad set / ad group id."""

    @abc.abstractmethod
    def create_creative(self, spec: CreativeSpec) -> str:
        """Return the platform's ad id."""

    # -- mutation --
    @abc.abstractmethod
    def set_status(self, level: str, external_id: str, active: bool) -> None:
        """Pause or resume. `level` is campaign, ad_group or creative."""

    @abc.abstractmethod
    def set_budget(self, level: str, external_id: str, daily_budget_micros: int) -> None:
        """Change a daily budget."""

    @abc.abstractmethod
    def set_bid(self, level: str, external_id: str, bid_micros: int) -> None:
        """Change a bid or target."""

    # -- measurement --
    @abc.abstractmethod
    def fetch_insights(
        self, level: str, since: date, until: date, external_ids: list[str] | None = None
    ) -> list[InsightRow]:
        """Daily delivery rows for the window, inclusive of both endpoints."""

    def fetch_breakdowns(
        self,
        level: str,
        since: date,
        until: date,
        dimension: str,
        external_ids: list[str] | None = None,
    ) -> list["BreakdownRow"]:
        """Delivery split along one dimension.

        Optional: an adapter that cannot slice this way returns nothing, and
        the optimizer simply has one fewer lever.
        """
        return []

    def apply_exclusion(
        self, level: str, external_id: str, dimension: str, segment: str
    ) -> None:
        """Stop delivering to one segment.

        Optional. Raising `PlatformError` is the honest answer where the
        platform offers no such control.
        """
        raise PlatformError(
            f"{self.platform.value} cannot exclude {dimension} segments here",
            platform=self.platform,
            code="UNSUPPORTED",
        )

    # -- conversions back to the platform --
    def upload_conversions(self, conversions: list[dict]) -> int:
        """Send network conversions back so the platform can optimise on them.

        Affiliate conversions happen off-site, so without this the bidding
        algorithm is blind to the only outcome that matters. Optional: adapters
        that cannot do this return 0.
        """
        return 0

    def health_check(self) -> dict:
        return {"platform": self.platform.value, "ok": True}
