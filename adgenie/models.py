"""Domain model for AdGenie Pro.

Hierarchy mirrors both ad platforms so one optimizer can drive either:

    Offer  ->  Campaign  ->  AdGroup       ->  Creative
    (the      (Meta        (Meta ad set /     (Meta ad /
     thing     campaign /   Google ad group)   Google responsive ad)
     you sell) Google campaign)

Measurement is two-sided: `MetricSnapshot` holds what the *platform* reports
(impressions, clicks, spend) while `Click` + `Conversion` hold what the
*affiliate network* reports back through the tracking link. Revenue always
comes from the network side, because platform pixels under-report affiliate
conversions that fire on someone else's domain.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------


class Platform(str, enum.Enum):
    META = "meta"
    GOOGLE = "google"


class EntityStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"
    REJECTED = "rejected"
    FAILED = "failed"


class PayoutType(str, enum.Enum):
    CPA = "cpa"  # fixed amount per conversion
    CPS = "cps"  # percentage of sale value
    CPL = "cpl"  # per lead
    REVSHARE = "revshare"  # recurring percentage


class ComplianceVerdict(str, enum.Enum):
    UNREVIEWED = "unreviewed"
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


class ActionType(str, enum.Enum):
    PAUSE = "pause"
    RESUME = "resume"
    INCREASE_BUDGET = "increase_budget"
    DECREASE_BUDGET = "decrease_budget"
    SET_BID = "set_bid"
    ROTATE_CREATIVE = "rotate_creative"
    GENERATE_VARIANTS = "generate_variants"
    REALLOCATE = "reallocate"
    EXCLUDE_SEGMENT = "exclude_segment"
    NO_ACTION = "no_action"


class ActionStatus(str, enum.Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    APPLIED = "applied"
    REJECTED = "rejected"
    FAILED = "failed"


class ConversionStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REVERSED = "reversed"  # refund / chargeback


class MediaKind(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"


class MediaStatus(str, enum.Enum):
    PENDING = "pending"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"
    REJECTED = "rejected"  # blocked by the image policy pre-screen


class EntityLevel(str, enum.Enum):
    CAMPAIGN = "campaign"
    AD_GROUP = "ad_group"
    CREATIVE = "creative"


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------


class Offer(Base):
    """An affiliate offer: the product, its payout and its promotion rules.

    Money columns are `BigInteger`: at a million micros to the dollar, a 32-bit
    column overflows just past $2,147, which a lifetime spend total reaches
    quickly. SQLite would hide the problem; Postgres would not.
    """

    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    network: Mapped[str] = mapped_column(String(80), default="manual")
    network_offer_id: Mapped[str | None] = mapped_column(String(120))
    vertical: Mapped[str] = mapped_column(String(80), default="general")

    destination_url: Mapped[str] = mapped_column(Text, nullable=False)
    payout_type: Mapped[PayoutType] = mapped_column(
        Enum(PayoutType), default=PayoutType.CPA
    )
    payout_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    payout_percent: Mapped[float] = mapped_column(Float, default=0.0)
    average_order_value_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    # Fraction of gross conversions the network later reverses (refunds).
    expected_reversal_rate: Mapped[float] = mapped_column(Float, default=0.10)

    # Selling context handed to the copywriter.
    product_description: Mapped[str] = mapped_column(Text, default="")
    target_audience: Mapped[str] = mapped_column(Text, default="")
    key_benefits: Mapped[list] = mapped_column(JSON, default=list)
    proof_points: Mapped[list] = mapped_column(JSON, default=list)
    landing_page_copy: Mapped[str] = mapped_column(Text, default="")

    # Compliance context.
    geo_targets: Mapped[list] = mapped_column(JSON, default=lambda: ["US"])
    banned_claims: Mapped[list] = mapped_column(JSON, default=list)
    required_disclosures: Mapped[list] = mapped_column(JSON, default=list)
    is_regulated: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[EntityStatus] = mapped_column(
        Enum(EntityStatus), default=EntityStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    campaigns: Mapped[list["Campaign"]] = relationship(
        back_populates="offer", cascade="all, delete-orphan"
    )

    def expected_value_micros(self) -> int:
        """Net expected revenue per approved conversion after reversals."""
        if self.payout_type in (PayoutType.CPA, PayoutType.CPL):
            gross = self.payout_micros
        else:
            gross = int(self.average_order_value_micros * self.payout_percent)
        return int(gross * (1.0 - self.expected_reversal_rate))


class PlatformAccount(Base):
    """A connected Meta ad account or Google Ads customer."""

    __tablename__ = "platform_accounts"
    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_account"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[Platform] = mapped_column(Enum(Platform), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    timezone_name: Mapped[str] = mapped_column(String(64), default="UTC")
    # Never store raw secrets here in production; this holds a reference/alias
    # into the secret manager plus non-sensitive metadata.
    credentials_ref: Mapped[str | None] = mapped_column(String(200))
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("platform_accounts.id"))
    platform: Mapped[Platform] = mapped_column(Enum(Platform), nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)
    objective: Mapped[str] = mapped_column(String(80), default="OUTCOME_SALES")
    bid_strategy: Mapped[str] = mapped_column(String(80), default="LOWEST_COST")
    daily_budget_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    lifetime_budget_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    target_roas: Mapped[float] = mapped_column(Float, default=1.30)
    # Optimizer will not push the daily budget past this.
    max_daily_budget_micros: Mapped[int] = mapped_column(BigInteger, default=0)

    status: Mapped[EntityStatus] = mapped_column(
        Enum(EntityStatus), default=EntityStatus.DRAFT
    )
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    offer: Mapped[Offer] = relationship(back_populates="campaigns")
    ad_groups: Mapped[list["AdGroup"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class AdGroup(Base):
    """A Meta ad set or a Google ad group."""

    __tablename__ = "ad_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)

    daily_budget_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    max_daily_budget_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    bid_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    # Meta: age/gender/geo/interests. Google: keywords/match types/audiences.
    targeting: Mapped[dict] = mapped_column(JSON, default=dict)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    negative_keywords: Mapped[list] = mapped_column(JSON, default=list)

    status: Mapped[EntityStatus] = mapped_column(
        Enum(EntityStatus), default=EntityStatus.DRAFT
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    campaign: Mapped[Campaign] = relationship(back_populates="ad_groups")
    creatives: Mapped[list["Creative"]] = relationship(
        back_populates="ad_group", cascade="all, delete-orphan"
    )


class Creative(Base):
    """One ad. Copy is stored as lists so a Google responsive search ad and a
    Meta ad with multiple text variants share a single representation."""

    __tablename__ = "creatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_group_id: Mapped[int] = mapped_column(ForeignKey("ad_groups.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)

    angle: Mapped[str] = mapped_column(String(120), default="")
    headlines: Mapped[list] = mapped_column(JSON, default=list)
    descriptions: Mapped[list] = mapped_column(JSON, default=list)
    primary_texts: Mapped[list] = mapped_column(JSON, default=list)
    call_to_action: Mapped[str] = mapped_column(String(60), default="LEARN_MORE")
    display_url_path: Mapped[list] = mapped_column(JSON, default=list)
    image_prompt: Mapped[str] = mapped_column(Text, default="")
    media_urls: Mapped[list] = mapped_column(JSON, default=list)
    final_url: Mapped[str] = mapped_column(Text, default="")

    compliance_verdict: Mapped[ComplianceVerdict] = mapped_column(
        Enum(ComplianceVerdict), default=ComplianceVerdict.UNREVIEWED
    )
    compliance_report: Mapped[dict] = mapped_column(JSON, default=dict)

    # Lineage: which creative this was bred from, and how it was produced.
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("creatives.id"))
    generation: Mapped[int] = mapped_column(Integer, default=0)
    generator: Mapped[str] = mapped_column(String(60), default="template")
    generator_meta: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[EntityStatus] = mapped_column(
        Enum(EntityStatus), default=EntityStatus.DRAFT
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    ad_group: Mapped[AdGroup] = relationship(back_populates="creatives")
    parent: Mapped["Creative | None"] = relationship(remote_side=[id])


class MetricSnapshot(Base):
    """Daily platform-reported delivery for one entity."""

    __tablename__ = "metric_snapshots"
    __table_args__ = (
        UniqueConstraint("level", "entity_id", "day", name="uq_metric_day"),
        Index("ix_metric_lookup", "level", "entity_id", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[EntityLevel] = mapped_column(Enum(EntityLevel), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)

    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    spend_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    # What the ad platform's own pixel saw. Kept for diagnostics; revenue and
    # ROAS are computed from network conversions instead.
    platform_conversions: Mapped[float] = mapped_column(Float, default=0.0)
    platform_conversion_value_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    frequency: Mapped[float] = mapped_column(Float, default=0.0)
    reach: Mapped[int] = mapped_column(Integer, default=0)
    video_views: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Click(Base):
    """A click on a tracking link, recorded before the redirect to the offer."""

    __tablename__ = "clicks"
    __table_args__ = (Index("ix_click_creative_ts", "creative_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    click_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Nullable: a click whose sub-id was mangled still has to be recorded, and
    # a non-nullable column would make the redirect fail instead.
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    ad_group_id: Mapped[int | None] = mapped_column(ForeignKey("ad_groups.id"))
    creative_id: Mapped[int | None] = mapped_column(
        ForeignKey("creatives.id"), index=True
    )
    platform: Mapped[Platform | None] = mapped_column(Enum(Platform))

    # Platform-side click identifier (fbclid / gclid) for offline uploads.
    platform_click_id: Mapped[str | None] = mapped_column(String(255))
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(8))
    referrer: Mapped[str | None] = mapped_column(Text)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Conversion(Base):
    """A conversion reported by the affiliate network via server postback."""

    __tablename__ = "conversions"
    __table_args__ = (
        UniqueConstraint("network", "network_txn_id", name="uq_network_txn"),
        Index("ix_conv_creative_ts", "creative_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    click_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Nullable for the same reason as `Click.offer_id`: an unmatched postback is
    # recorded as unattributed revenue rather than dropped or 500'd, so the
    # network stops retrying and the gap stays visible.
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"))
    ad_group_id: Mapped[int | None] = mapped_column(ForeignKey("ad_groups.id"))
    creative_id: Mapped[int | None] = mapped_column(
        ForeignKey("creatives.id"), index=True
    )

    network: Mapped[str] = mapped_column(String(80), default="manual")
    network_txn_id: Mapped[str] = mapped_column(String(160), nullable=False)
    revenue_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    sale_amount_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[ConversionStatus] = mapped_column(
        Enum(ConversionStatus), default=ConversionStatus.PENDING
    )
    event_name: Mapped[str] = mapped_column(String(80), default="sale")
    # Whether this conversion has been echoed back to Meta CAPI / Google offline.
    uploaded_to_platform: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Networks approve or reverse a sale long after they first post it, so the
    # upload job windows on this rather than on creation.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, index=True
    )


class OptimizationAction(Base):
    """A decision the optimizer made, why it made it, and what happened."""

    __tablename__ = "optimization_actions"
    __table_args__ = (Index("ix_action_entity", "level", "entity_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    level: Mapped[EntityLevel] = mapped_column(Enum(EntityLevel), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[ActionType] = mapped_column(Enum(ActionType), nullable=False)
    status: Mapped[ActionStatus] = mapped_column(
        Enum(ActionStatus), default=ActionStatus.PROPOSED
    )

    rule: Mapped[str] = mapped_column(String(80), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # Snapshot of the metrics the decision was based on, for auditability.
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)

    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class OptimizerRun(Base):
    __tablename__ = "optimizer_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    entities_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    actions_proposed: Mapped[int] = mapped_column(Integer, default=0)
    actions_applied: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class MediaAsset(Base):
    """An image or video generated for a creative.

    The provider's result URLs expire within about a day, so `local_path` is
    the durable copy and `remote_url` is kept only for provenance.
    """

    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    creative_id: Mapped[int | None] = mapped_column(
        ForeignKey("creatives.id"), index=True
    )
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id"), index=True)
    kind: Mapped[MediaKind] = mapped_column(Enum(MediaKind), default=MediaKind.IMAGE)

    provider: Mapped[str] = mapped_column(String(60), default="kie")
    model: Mapped[str] = mapped_column(String(120), default="")
    task_id: Mapped[str | None] = mapped_column(String(120), index=True)

    prompt: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="1:1")
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    remote_url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    public_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    bytes: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[MediaStatus] = mapped_column(
        Enum(MediaStatus), default=MediaStatus.PENDING
    )
    compliance_report: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class CompetitorAd(Base):
    """One ad observed in the Meta Ad Library.

    The library carries no click-through rate, conversion or spend data for
    commercial ads, so `days_running` and whether it is still live are the
    evidence this platform reasons from: an advertiser does not fund a losing
    ad for three months.
    """

    __tablename__ = "competitor_ads"
    __table_args__ = (
        UniqueConstraint("ad_archive_id", name="uq_competitor_ad"),
        Index("ix_competitor_vertical", "vertical", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_archive_id: Mapped[str] = mapped_column(String(64), nullable=False)
    page_id: Mapped[str | None] = mapped_column(String(64), index=True)
    page_name: Mapped[str] = mapped_column(String(200), default="")

    search_term: Mapped[str] = mapped_column(String(200), default="")
    vertical: Mapped[str] = mapped_column(String(80), default="", index=True)
    countries: Mapped[list] = mapped_column(JSON, default=list)
    publisher_platforms: Mapped[list] = mapped_column(JSON, default=list)
    languages: Mapped[list] = mapped_column(JSON, default=list)

    bodies: Mapped[list] = mapped_column(JSON, default=list)
    titles: Mapped[list] = mapped_column(JSON, default=list)
    descriptions: Mapped[list] = mapped_column(JSON, default=list)
    captions: Mapped[list] = mapped_column(JSON, default=list)
    snapshot_url: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    days_running: Mapped[int] = mapped_column(Integer, default=0)
    # EU ads carry a reach figure; commercial ads carry no spend at all.
    eu_total_reach: Mapped[int] = mapped_column(BigInteger, default=0)
    # How many near-identical variants the same page is running. An advertiser
    # producing fifteen versions of one idea is scaling it.
    variant_count: Mapped[int] = mapped_column(Integer, default=1)

    # What this platform inferred from the copy.
    angle: Mapped[str | None] = mapped_column(String(80), index=True)
    staying_power: Mapped[float] = mapped_column(Float, default=0.0)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class AuditLog(Base):
    """Append-only record of every mutation sent to an ad platform."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80), default="system")
    platform: Mapped[Platform | None] = mapped_column(Enum(Platform))
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    target: Mapped[str] = mapped_column(String(160), default="")
    request: Mapped[dict] = mapped_column(JSON, default=dict)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
