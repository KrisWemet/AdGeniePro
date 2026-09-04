"""Performance aggregation.

Two sources are joined here, and keeping them distinct matters:

* `MetricSnapshot` - what the ad platform charged you and what it delivered.
* `Conversion` - what the affiliate network actually paid you.

ROAS is computed from the second divided by the first. Using the platform's own
reported conversion value instead is the most common way an affiliate media
buyer talks themselves into scaling a losing campaign, because the pixel counts
landing-page events it can see and misses the sales it cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AdGroup,
    Campaign,
    Click,
    Conversion,
    ConversionStatus,
    Creative,
    EntityLevel,
    MetricSnapshot,
    Offer,
)
from ..money import micros_to_usd, safe_div
from .stats import Interval, beta_interval, prob_rate_above

__all__ = ["PerformanceWindow", "load_performance", "load_many"]


@dataclass
class PerformanceWindow:
    """Everything the optimizer needs to judge one entity."""

    level: EntityLevel
    entity_id: int
    since: date
    until: date

    impressions: int = 0
    clicks: int = 0
    spend_micros: int = 0
    # Network-confirmed outcomes.
    conversions: int = 0
    pending_conversions: int = 0
    reversed_conversions: int = 0
    revenue_micros: int = 0
    # Platform-reported, kept only for diagnostics and discrepancy detection.
    platform_conversions: float = 0.0
    platform_conversion_value_micros: int = 0
    frequency: float = 0.0
    days_with_delivery: int = 0
    first_day: date | None = None
    last_day: date | None = None
    # Offer economics, needed to compute a breakeven point.
    offer_payout_micros: int = 0
    daily_budget_micros: int = 0
    credible_level: float = 0.90
    # Prior on the conversion rate, in pseudo-clicks. A flat Beta(1, 1) says a
    # 50% conversion rate is as plausible as a 2% one, which badly inflates
    # small samples; `pooled_prior` replaces it with the account's own history.
    prior_a: float = 1.0
    prior_b: float = 1.0
    daily: list[dict] = field(default_factory=list)

    # -- rates -----------------------------------------------------------
    @property
    def ctr(self) -> float:
        return safe_div(self.clicks, self.impressions)

    @property
    def cpc_micros(self) -> float:
        return safe_div(self.spend_micros, self.clicks)

    @property
    def cpm_micros(self) -> float:
        return safe_div(self.spend_micros * 1000, self.impressions)

    @property
    def cvr(self) -> float:
        return safe_div(self.conversions, self.clicks)

    @property
    def cpa_micros(self) -> float:
        return safe_div(self.spend_micros, self.conversions)

    @property
    def epc_micros(self) -> float:
        """Earnings per click. The single most useful affiliate number."""
        return safe_div(self.revenue_micros, self.clicks)

    @property
    def roas(self) -> float:
        return safe_div(self.revenue_micros, self.spend_micros)

    @property
    def profit_micros(self) -> int:
        return self.revenue_micros - self.spend_micros

    @property
    def platform_roas(self) -> float:
        return safe_div(self.platform_conversion_value_micros, self.spend_micros)

    @property
    def attribution_gap(self) -> float:
        """How far the platform's conversion count sits from the network's.

        A large persistent gap means tracking is broken, and every decision
        downstream is being made on bad data.
        """
        if not self.platform_conversions:
            return 0.0
        return (self.platform_conversions - self.conversions) / self.platform_conversions

    # -- uncertainty -----------------------------------------------------
    @property
    def breakeven_cvr(self) -> float:
        """The conversion rate at which revenue equals spend.

        Below this the ad loses money no matter how good the click volume is.
        """
        if not self.offer_payout_micros or not self.clicks:
            return 0.0
        return min(1.0, self.cpc_micros / self.offer_payout_micros)

    def cvr_interval(self, level: float | None = None) -> Interval:
        return beta_interval(
            self.conversions,
            self.clicks,
            level or self.credible_level,
            prior_a=self.prior_a,
            prior_b=self.prior_b,
        )

    def prob_profitable(self, target_roas: float = 1.0) -> float:
        """P(true conversion rate clears the rate needed for `target_roas`)."""
        if not self.clicks or not self.offer_payout_micros:
            return 0.0
        needed = self.breakeven_cvr * target_roas
        if needed <= 0:
            return 0.0
        if needed >= 1:
            return 0.0
        return prob_rate_above(
            needed,
            self.conversions,
            self.clicks,
            prior_a=self.prior_a,
            prior_b=self.prior_b,
        )

    def roas_interval(self, level: float | None = None) -> Interval:
        """Credible interval on ROAS, derived from the interval on CVR.

        Revenue per conversion is treated as the offer's expected payout, so
        the uncertainty that matters is entirely in the conversion rate. That
        holds for fixed-payout CPA offers, which is most affiliate inventory.
        """
        if not self.clicks or not self.spend_micros or not self.offer_payout_micros:
            return Interval(0.0, 0.0, 0.0, level or self.credible_level)
        interval = self.cvr_interval(level)
        scale = self.clicks * self.offer_payout_micros / self.spend_micros
        return Interval(
            lower=interval.lower * scale,
            mean=interval.mean * scale,
            upper=interval.upper * scale,
            level=interval.level,
        )

    # -- serialization ---------------------------------------------------
    def as_dict(self) -> dict:
        roas_ci = self.roas_interval()
        return {
            "level": self.level.value,
            "entity_id": self.entity_id,
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "impressions": self.impressions,
            "clicks": self.clicks,
            "spend_usd": micros_to_usd(self.spend_micros),
            "conversions": self.conversions,
            "pending_conversions": self.pending_conversions,
            "reversed_conversions": self.reversed_conversions,
            "revenue_usd": micros_to_usd(self.revenue_micros),
            "profit_usd": micros_to_usd(self.profit_micros),
            "ctr": round(self.ctr, 5),
            "cpc_usd": round(micros_to_usd(int(self.cpc_micros)), 4),
            "cvr": round(self.cvr, 5),
            "cpa_usd": round(micros_to_usd(int(self.cpa_micros)), 2),
            "epc_usd": round(micros_to_usd(int(self.epc_micros)), 4),
            "roas": round(self.roas, 4),
            "roas_lower": round(roas_ci.lower, 4),
            "roas_upper": round(roas_ci.upper, 4),
            "breakeven_cvr": round(self.breakeven_cvr, 5),
            "prob_profitable": round(self.prob_profitable(), 4),
            "platform_conversions": self.platform_conversions,
            "platform_roas": round(self.platform_roas, 4),
            "attribution_gap": round(self.attribution_gap, 4),
            "frequency": round(self.frequency, 2),
            "days_with_delivery": self.days_with_delivery,
            "daily_budget_usd": micros_to_usd(self.daily_budget_micros),
        }


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

_CONVERSION_FK = {
    EntityLevel.CAMPAIGN: Conversion.campaign_id,
    EntityLevel.AD_GROUP: Conversion.ad_group_id,
    EntityLevel.CREATIVE: Conversion.creative_id,
}

_CLICK_FK = {
    EntityLevel.CAMPAIGN: Click.campaign_id,
    EntityLevel.AD_GROUP: Click.ad_group_id,
    EntityLevel.CREATIVE: Click.creative_id,
}


def _descendant_creative_ids(
    session: Session, level: EntityLevel, entity_id: int
) -> list[int]:
    if level is EntityLevel.CREATIVE:
        return [entity_id]
    if level is EntityLevel.AD_GROUP:
        return list(
            session.execute(
                select(Creative.id).where(Creative.ad_group_id == entity_id)
            ).scalars()
        )
    return list(
        session.execute(
            select(Creative.id)
            .join(AdGroup, Creative.ad_group_id == AdGroup.id)
            .where(AdGroup.campaign_id == entity_id)
        ).scalars()
    )


def _offer_for(session: Session, level: EntityLevel, entity_id: int) -> Offer | None:
    campaign_id: int | None = None
    if level is EntityLevel.CAMPAIGN:
        campaign_id = entity_id
    elif level is EntityLevel.AD_GROUP:
        group = session.get(AdGroup, entity_id)
        campaign_id = group.campaign_id if group else None
    else:
        creative = session.get(Creative, entity_id)
        group = session.get(AdGroup, creative.ad_group_id) if creative else None
        campaign_id = group.campaign_id if group else None
    campaign = session.get(Campaign, campaign_id) if campaign_id else None
    return session.get(Offer, campaign.offer_id) if campaign else None


def _budget_for(session: Session, level: EntityLevel, entity_id: int) -> int:
    if level is EntityLevel.CAMPAIGN:
        campaign = session.get(Campaign, entity_id)
        return campaign.daily_budget_micros if campaign else 0
    if level is EntityLevel.AD_GROUP:
        group = session.get(AdGroup, entity_id)
        if group is None:
            return 0
        if group.daily_budget_micros:
            return group.daily_budget_micros
        campaign = session.get(Campaign, group.campaign_id)
        return campaign.daily_budget_micros if campaign else 0
    creative = session.get(Creative, entity_id)
    return _budget_for(session, EntityLevel.AD_GROUP, creative.ad_group_id) if creative else 0


def load_performance(
    session: Session,
    level: EntityLevel,
    entity_id: int,
    since: date,
    until: date,
    credible_level: float = 0.90,
    count_pending_as_revenue: bool = False,
) -> PerformanceWindow:
    """Aggregate delivery and revenue for one entity over a date window.

    Pending conversions are excluded from revenue by default. Counting money
    the network has not approved yet is how a campaign gets scaled on refunds.
    """
    window = PerformanceWindow(
        level=level,
        entity_id=entity_id,
        since=since,
        until=until,
        credible_level=credible_level,
    )

    rows = list(
        session.execute(
            select(MetricSnapshot)
            .where(
                MetricSnapshot.level == level,
                MetricSnapshot.entity_id == entity_id,
                MetricSnapshot.day >= since,
                MetricSnapshot.day <= until,
            )
            .order_by(MetricSnapshot.day)
        ).scalars()
    )
    for row in rows:
        window.impressions += row.impressions
        window.clicks += row.clicks
        window.spend_micros += row.spend_micros
        window.platform_conversions += row.platform_conversions
        window.platform_conversion_value_micros += row.platform_conversion_value_micros
        window.frequency = max(window.frequency, row.frequency)
        if row.impressions:
            window.days_with_delivery += 1
            window.first_day = window.first_day or row.day
            window.last_day = row.day
        window.daily.append(
            {
                "day": row.day.isoformat(),
                "impressions": row.impressions,
                "clicks": row.clicks,
                "spend_usd": micros_to_usd(row.spend_micros),
            }
        )

    # Revenue comes from the network side, joined on the same window.
    start_dt = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(until, datetime.max.time(), tzinfo=timezone.utc)
    fk = _CONVERSION_FK[level]
    conversion_rows = session.execute(
        select(Conversion.status, func.count(Conversion.id), func.sum(Conversion.revenue_micros))
        .where(
            fk == entity_id,
            Conversion.occurred_at >= start_dt.replace(tzinfo=None),
            Conversion.occurred_at <= end_dt.replace(tzinfo=None),
        )
        .group_by(Conversion.status)
    ).all()
    for status, count, revenue in conversion_rows:
        revenue = int(revenue or 0)
        if status is ConversionStatus.APPROVED:
            window.conversions += count
            window.revenue_micros += revenue
        elif status is ConversionStatus.PENDING:
            window.pending_conversions += count
            if count_pending_as_revenue:
                window.conversions += count
                window.revenue_micros += revenue
        elif status is ConversionStatus.REVERSED:
            window.reversed_conversions += count

    offer = _offer_for(session, level, entity_id)
    if offer is not None:
        window.offer_payout_micros = offer.expected_value_micros()
    window.daily_budget_micros = _budget_for(session, level, entity_id)
    return window


def load_many(
    session: Session,
    level: EntityLevel,
    entity_ids: list[int],
    since: date,
    until: date,
    credible_level: float = 0.90,
) -> dict[int, PerformanceWindow]:
    return {
        entity_id: load_performance(
            session, level, entity_id, since, until, credible_level
        )
        for entity_id in entity_ids
    }


def pooled_prior(
    windows: list[PerformanceWindow], strength: float = 25.0, fallback_cvr: float = 0.02
) -> tuple[float, float]:
    """Empirical-Bayes prior from the pooled conversion rate of a group.

    `strength` is how many pseudo-clicks of belief the prior carries. At 25 it
    dominates a creative with 10 clicks and is irrelevant to one with 500,
    which is exactly the behaviour a shrinkage prior should have.
    """
    total_clicks = sum(w.clicks for w in windows)
    total_conversions = sum(w.conversions for w in windows)
    rate = (total_conversions / total_clicks) if total_clicks >= 50 else fallback_cvr
    rate = min(0.5, max(0.0005, rate))
    return max(0.5, rate * strength), max(0.5, (1.0 - rate) * strength)


def apply_pooled_prior(
    windows: list[PerformanceWindow], strength: float = 25.0
) -> tuple[float, float]:
    """Give each window a prior built from its *peers*, not from itself.

    Pooling over a group that includes the entity being judged means a creative
    is shrunk toward its own result, which tightens its interval with pseudo-
    observations of itself and defeats the point of the prior. That is most
    acute where it matters most: an ad group holding a single creative would
    otherwise have that creative's own rate treated as 25 extra clicks of
    independent evidence, right before the kill and scale gates read it.

    Returns the prior computed over the whole group, for reporting.
    """
    group_prior = pooled_prior(windows, strength=strength)
    for window in windows:
        peers = [w for w in windows if w is not window]
        window.prior_a, window.prior_b = (
            pooled_prior(peers, strength=strength) if peers else pooled_prior([], strength=strength)
        )
    return group_prior


def default_window(lookback_days: int, today: date | None = None) -> tuple[date, date]:
    """Yesterday-anchored window. Today's data is always partial."""
    today = today or datetime.now(timezone.utc).date()
    until = today - timedelta(days=1)
    return until - timedelta(days=lookback_days - 1), until
