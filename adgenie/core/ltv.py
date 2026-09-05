"""What a lead is worth, before you know what it was worth.

A funnel moves most of the revenue weeks past the click. Sending traffic
straight to an offer monetises it once on day one; capturing an address
monetises it repeatedly for months. That is better business and worse
telemetry: on the day the money is being spent, a lead-generation campaign
looks like a campaign with almost no revenue.

An optimizer that waits for the truth cannot optimize, and one that assumes a
lead is worth whatever you hoped will happily scale a list of people who never
buy. The way out is to *measure* what leads have historically been worth and to
act on the pessimistic end of that measurement.

Two things are estimated here:

* **Realised value per lead**, from cohorts old enough to have finished
  earning, so recent cohorts do not drag the average down purely for being
  young.
* **A maturity curve for lead revenue**, the same censoring correction
  `lag.py` applies to conversions, but on a horizon of weeks rather than days.

Everything is reported with an interval, and callers that spend money are
expected to use the lower bound.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Conversion, ConversionStatus, Lead, Offer
from ..money import micros_to_usd
from .stats import Interval

logger = logging.getLogger(__name__)

__all__ = ["LeadValueModel", "fit_lead_value", "DEFAULT_LEAD_CURVE"]

# Share of a lead's eventual revenue collected by N days after opt-in. Email
# funnels earn most of it in the first fortnight, then trail.
DEFAULT_LEAD_CURVE: tuple[tuple[float, float], ...] = (
    (0.0, 0.00),
    (1.0, 0.22),
    (3.0, 0.42),
    (7.0, 0.62),
    (14.0, 0.78),
    (30.0, 0.91),
    (60.0, 1.00),
)

# Pseudo-leads of belief in the prior. A value fitted on eight leads is noise.
_SHRINKAGE_LEADS = 40.0

# Cohorts younger than this contribute to the curve but not to the value
# estimate: they have not finished earning.
_MIN_COHORT_AGE_DAYS = 7


@dataclass
class LeadValueModel:
    """Expected revenue from one lead, and how fast it arrives."""

    mean_micros: int = 0
    lower_micros: int = 0
    upper_micros: int = 0
    sample_size: int = 0
    mature_sample_size: int = 0
    offer_id: int | None = None
    creative_id: int | None = None
    fitted: bool = False
    curve: tuple[tuple[float, float], ...] = DEFAULT_LEAD_CURVE
    horizon_days: int = 60

    def maturity(self, age_days: float) -> float:
        """Share of a lead's eventual revenue that should have arrived by now."""
        if age_days <= 0:
            return 0.0
        points = self.curve
        if age_days >= points[-1][0]:
            return 1.0
        index = bisect_right([d for d, _ in points], age_days)
        if index == 0:
            return 0.0
        low_d, low_v = points[index - 1]
        high_d, high_v = points[index]
        if high_d == low_d:
            return high_v
        return low_v + (age_days - low_d) / (high_d - low_d) * (high_v - low_v)

    def interval(self) -> Interval:
        return Interval(
            lower=micros_to_usd(self.lower_micros),
            mean=micros_to_usd(self.mean_micros),
            upper=micros_to_usd(self.upper_micros),
            level=0.90,
        )

    def value_of(self, leads: float, conservative: bool = True) -> int:
        """What a number of leads is worth.

        Defaults to the lower bound, because this figure is used to justify
        spending: a campaign scaled on an optimistic lead value spends real
        money against revenue that has not been demonstrated.
        """
        per_lead = self.lower_micros if conservative else self.mean_micros
        return int(max(0.0, leads) * per_lead)

    def as_dict(self) -> dict:
        return {
            "fitted": self.fitted,
            "offer_id": self.offer_id,
            "creative_id": self.creative_id,
            "sample_size": self.sample_size,
            "mature_sample_size": self.mature_sample_size,
            "value_per_lead_usd": micros_to_usd(self.mean_micros),
            "lower_usd": micros_to_usd(self.lower_micros),
            "upper_usd": micros_to_usd(self.upper_micros),
            "horizon_days": self.horizon_days,
        }


def fit_lead_value(
    session: Session,
    offer_id: int,
    creative_id: int | None = None,
    prior_micros: int | None = None,
    as_of: datetime | None = None,
) -> LeadValueModel:
    """Measure what this offer's leads have actually been worth.

    Only cohorts old enough to have finished earning count toward the value,
    because a lead captured yesterday has produced almost nothing yet and
    averaging it in would understate every lead.

    `prior_micros` is what a lead is believed to be worth before any has been
    measured, usually `offer_prior_micros`. Pass None to let the sample speak
    for itself: shrinking toward an unstated zero would understate every funnel.
    """
    as_of = _aware(as_of or datetime.now(timezone.utc))
    offer = session.get(Offer, offer_id)
    horizon = offer.lead_value_horizon_days if offer else 60

    query = select(Lead).where(Lead.offer_id == offer_id)
    if creative_id is not None:
        query = query.where(Lead.creative_id == creative_id)
    leads = list(session.execute(query).scalars())

    model = LeadValueModel(
        offer_id=offer_id,
        creative_id=creative_id,
        horizon_days=horizon,
        sample_size=len(leads),
    )
    if not leads:
        model.mean_micros = model.lower_micros = model.upper_micros = prior_micros or 0
        return model

    cutoff = as_of - timedelta(days=_MIN_COHORT_AGE_DAYS)
    mature = [lead for lead in leads if _aware(lead.created_at) <= cutoff]
    model.mature_sample_size = len(mature)

    if not mature:
        # Every lead is too young to have earned. Correct the young cohort for
        # how little of its window has run rather than reporting near-zero.
        observed = sum(lead.realised_value_micros for lead in leads)
        average_age = sum(
            (as_of - _aware(lead.created_at)).days for lead in leads
        ) / len(leads)
        maturity = max(0.05, model.maturity(average_age))
        projected = int(observed / maturity / len(leads))
        model.mean_micros = projected
        # Wide by construction: this is an extrapolation, not a measurement.
        model.lower_micros = int(projected * 0.35)
        model.upper_micros = int(projected * 2.0)
        return model

    values = sorted(lead.realised_value_micros for lead in mature)
    observed_mean = sum(values) / len(values)

    # Shrink toward the prior while the sample is small, exactly as the
    # conversion-rate prior does elsewhere in the optimizer. With no prior
    # supplied there is nothing to shrink toward, and defaulting to zero would
    # silently understate every funnel and bias the optimizer toward killing
    # them, so the sample stands on its own.
    if prior_micros is None:
        prior, weight = observed_mean, 1.0
    else:
        prior = prior_micros
        weight = len(values) / (len(values) + _SHRINKAGE_LEADS)
    mean = weight * observed_mean + (1 - weight) * prior

    # Lead value is heavily skewed: most leads are worth nothing and a few are
    # worth a lot. A percentile interval respects that shape where a symmetric
    # one built from the standard deviation would not.
    model.mean_micros = int(mean)
    model.lower_micros = int(_bootstrap_bound(values, 0.05, prior, weight))
    model.upper_micros = int(_bootstrap_bound(values, 0.95, prior, weight))
    model.fitted = len(values) >= 20
    model.curve = _fit_curve(session, offer_id, mature, as_of) or DEFAULT_LEAD_CURVE

    logger.info(
        "Lead value for offer %s: %s USD per lead (%s-%s) from %s mature leads",
        offer_id,
        f"{micros_to_usd(model.mean_micros):.2f}",
        f"{micros_to_usd(model.lower_micros):.2f}",
        f"{micros_to_usd(model.upper_micros):.2f}",
        len(values),
    )
    return model


def _bootstrap_bound(
    values: list[int], quantile: float, prior: int, weight: float
) -> float:
    """A bound on the *mean*, not on an individual lead.

    Resampling would be more exact; with a sorted sample the interval on the
    mean is well approximated by the sample mean plus a percentile-based
    spread, which costs nothing and does not need a random seed.
    """
    n = len(values)
    if n < 2:
        return weight * (values[0] if values else 0) + (1 - weight) * prior

    mean = sum(values) / n
    # Standard error, then widened for skew by how far the median sits from
    # the mean. A long right tail makes the mean less trustworthy.
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    standard_error = (variance / n) ** 0.5
    median = values[n // 2]
    skew_penalty = 1.0 + min(1.5, abs(mean - median) / (mean or 1.0))
    z = -1.645 if quantile < 0.5 else 1.645

    bound = mean + z * standard_error * skew_penalty
    bound = max(0.0, bound)
    return weight * bound + (1 - weight) * prior


def _fit_curve(
    session: Session, offer_id: int, leads: list[Lead], as_of: datetime
) -> tuple[tuple[float, float], ...] | None:
    """How quickly lead revenue arrives, measured from conversions."""
    lead_ids = [lead.id for lead in leads]
    if len(lead_ids) < 20:
        return None

    rows = session.execute(
        select(Conversion.lead_id, Conversion.occurred_at, Conversion.revenue_micros)
        .where(
            Conversion.lead_id.in_(lead_ids),
            Conversion.status == ConversionStatus.APPROVED,
        )
    ).all()
    if len(rows) < 10:
        return None

    created = {lead.id: _aware(lead.created_at) for lead in leads}
    total = sum(revenue for _, _, revenue in rows) or 1
    checkpoints = [days for days, _ in DEFAULT_LEAD_CURVE]

    curve: list[tuple[float, float]] = []
    previous = 0.0
    for days in checkpoints:
        collected = sum(
            revenue
            for lead_id, occurred, revenue in rows
            if lead_id in created
            and (_aware(occurred) - created[lead_id]).total_seconds() / 86400.0 <= days
        )
        share = collected / total
        previous = max(previous, min(1.0, share))
        curve.append((days, previous))
    if curve[-1][1] < 1.0:
        curve[-1] = (curve[-1][0], 1.0)
    return tuple(curve)


def offer_prior_micros(session: Session, offer_id: int) -> int:
    """A starting guess at lead value, before any lead has been measured.

    Uses the funnel's own economics rather than a constant: a step worth $17
    that one lead in twenty takes is worth a little under a dollar per lead.
    """
    offer = session.get(Offer, offer_id)
    if offer is None:
        return 0
    total = 0
    # Conservative take rates by step type. Deliberately pessimistic: this
    # number only applies before real data exists, and being wrong upward here
    # means funding a list that does not pay.
    assumed_take_rate = {"optin": 0.0, "tripwire": 0.04, "core": 0.03, "upsell": 0.01}
    for step in offer.funnel_steps:
        if not step.is_active:
            continue
        total += int(step.value_micros * assumed_take_rate.get(step.kind.value, 0.01))
    return total


def realised_value(session: Session, lead_id: int) -> int:
    """Revenue a lead has produced across every step."""
    return int(
        session.execute(
            select(func.coalesce(func.sum(Conversion.revenue_micros), 0)).where(
                Conversion.lead_id == lead_id,
                Conversion.status == ConversionStatus.APPROVED,
            )
        ).scalar_one()
        or 0
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
