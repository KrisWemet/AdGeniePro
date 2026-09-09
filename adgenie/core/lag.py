"""Conversion lag: treating recent data as incomplete rather than as failure.

A click does not convert instantly. On a direct-response affiliate offer a
third of conversions land within the hour, most within a day, and a long tail
runs for weeks — trials, upsells and networks that only confirm a sale after a
refund window closes.

That makes recent performance data *right-censored*: the spend has happened,
some of the conversions it bought have not been reported yet. Comparing the two
as though both were complete is the single most expensive mistake an ad
optimizer can make, because it looks exactly like failure. A four-day-old ad
with a real 5% conversion rate shows zero conversions, and a naive kill rule
retires it with high confidence right before it starts paying.

The fix is to weight each day of clicks by how much of its conversion window
has actually elapsed. A click an hour old counts as a fraction of a trial; a
click a month old counts as a whole one. Everything downstream then reasons
about *observed exposure* rather than raw clicks, which widens the credible
interval on young entities exactly as much as the missing data warrants.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Click, Conversion, ConversionStatus

logger = logging.getLogger(__name__)

__all__ = ["LagModel", "DEFAULT_LAG_CURVE", "fit_lag_model"]

# Cumulative fraction of eventual conversions reported by N hours after the
# click. A direct-response default: a third convert in-session, most within a
# day, with a tail for trials and delayed network confirmation.
DEFAULT_LAG_CURVE: tuple[tuple[float, float], ...] = (
    (0.0, 0.00),
    (1.0, 0.35),
    (6.0, 0.55),
    (24.0, 0.70),
    (48.0, 0.80),
    (72.0, 0.85),
    (168.0, 0.93),
    (336.0, 0.97),
    (720.0, 1.00),
)

# Checkpoints an empirical curve is measured at.
_CHECKPOINTS = tuple(hours for hours, _ in DEFAULT_LAG_CURVE)

# How many observed conversions it takes for the fitted curve to outweigh the
# default. The same shrinkage idea used for conversion-rate priors: a curve fit
# on nine conversions is noise dressed as knowledge.
_SHRINKAGE_STRENGTH = 50.0

# Below this, an entity is too young for its numbers to mean anything at all.
MIN_MATURITY_TO_JUDGE = 0.15


@dataclass
class LagModel:
    """How quickly conversions are reported after the click that earned them."""

    curve: tuple[tuple[float, float], ...] = DEFAULT_LAG_CURVE
    sample_size: int = 0
    offer_id: int | None = None
    fitted: bool = False
    median_hours: float | None = None

    def maturity(self, age_hours: float) -> float:
        """Fraction of eventual conversions expected to be reported by now."""
        if age_hours <= 0:
            return 0.0
        points = self.curve
        if age_hours >= points[-1][0]:
            return 1.0

        index = bisect_right([h for h, _ in points], age_hours)
        if index == 0:
            return 0.0
        lower_h, lower_v = points[index - 1]
        upper_h, upper_v = points[index]
        if upper_h == lower_h:
            return upper_v
        span = (age_hours - lower_h) / (upper_h - lower_h)
        return lower_v + span * (upper_v - lower_v)

    def maturity_for_day(self, day: date, as_of: datetime | None = None) -> float:
        """Maturity of a day's clicks, measured from the middle of that day.

        Midday is the honest reference point: a day's clicks arrive throughout
        it, so treating them all as midnight would overstate their age by up to
        a day and understate how much data is still outstanding.
        """
        as_of = _aware(as_of or datetime.now(timezone.utc))
        midpoint = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
        return self.maturity(max(0.0, (as_of - midpoint).total_seconds() / 3600.0))

    def effective_clicks(
        self, daily_clicks: list[tuple[date, int]], as_of: datetime | None = None
    ) -> float:
        """Clicks weighted by how much of their conversion window has elapsed.

        This is the number of trials the posterior should actually see. Passing
        raw clicks instead asserts that every one of them has had its full
        chance to convert, which for anything recent is simply false.
        """
        return sum(
            count * self.maturity_for_day(day, as_of) for day, count in daily_clicks
        )

    def window_maturity(
        self, daily_clicks: list[tuple[date, int]], as_of: datetime | None = None
    ) -> float:
        """Share of this window's clicks that have had their chance to convert."""
        total = sum(count for _, count in daily_clicks)
        if not total:
            return 1.0
        return self.effective_clicks(daily_clicks, as_of) / total

    def project(self, observed: float, maturity: float) -> float:
        """Scale an observed count up to what it is on course to become.

        Useful for reporting and never for spending: projecting revenue and
        then funding against the projection is how a campaign scales on money
        that has not arrived and may never.
        """
        if maturity <= 0.0:
            return 0.0
        return observed / max(maturity, MIN_MATURITY_TO_JUDGE)

    def as_dict(self) -> dict:
        return {
            "fitted": self.fitted,
            "sample_size": self.sample_size,
            "offer_id": self.offer_id,
            "median_hours": self.median_hours,
            "curve": [{"hours": h, "reported": round(v, 4)} for h, v in self.curve],
        }


def _blend(
    empirical: dict[float, float], sample_size: int
) -> tuple[tuple[float, float], ...]:
    """Shrink a fitted curve toward the default in proportion to its evidence."""
    weight = sample_size / (sample_size + _SHRINKAGE_STRENGTH)
    blended: list[tuple[float, float]] = []
    previous = 0.0
    for hours, default_value in DEFAULT_LAG_CURVE:
        value = weight * empirical.get(hours, default_value) + (1 - weight) * default_value
        # A cumulative distribution cannot decrease, and sampling noise can
        # otherwise produce a curve that does.
        previous = max(previous, min(1.0, value))
        blended.append((hours, previous))
    # Anchor the far end so projection stays bounded.
    if blended[-1][1] < 1.0:
        blended[-1] = (blended[-1][0], 1.0)
    return tuple(blended)


def fit_lag_model(
    session: Session,
    offer_id: int | None = None,
    lookback_days: int = 90,
    min_samples: int = 10,
    as_of: datetime | None = None,
) -> LagModel:
    """Measure the click-to-conversion delay from what has already happened.

    Falls back to the default curve when there is too little history, and
    blends toward it in between, so a new offer is never judged on a lag curve
    fitted to nine conversions.
    """
    as_of = _aware(as_of or datetime.now(timezone.utc))
    cutoff = (as_of - timedelta(days=lookback_days)).replace(tzinfo=None)

    query = (
        select(Conversion.occurred_at, Click.created_at)
        .join(Click, Conversion.click_id == Click.click_id)
        .where(
            Conversion.status != ConversionStatus.REVERSED,
            Conversion.created_at >= cutoff,
        )
    )
    if offer_id is not None:
        query = query.where(Conversion.offer_id == offer_id)

    delays: list[float] = []
    for converted_at, clicked_at in session.execute(query).all():
        if converted_at is None or clicked_at is None:
            continue
        hours = (_aware(converted_at) - _aware(clicked_at)).total_seconds() / 3600.0
        if 0.0 <= hours <= DEFAULT_LAG_CURVE[-1][0]:
            delays.append(hours)

    if len(delays) < min_samples:
        logger.info(
            "Only %s conversion delays available for offer %s; using the default "
            "lag curve.",
            len(delays),
            offer_id if offer_id is not None else "all",
        )
        return LagModel(offer_id=offer_id, sample_size=len(delays))

    delays.sort()
    empirical = {
        hours: bisect_right(delays, hours) / len(delays) for hours in _CHECKPOINTS
    }
    model = LagModel(
        curve=_blend(empirical, len(delays)),
        sample_size=len(delays),
        offer_id=offer_id,
        fitted=True,
        median_hours=delays[len(delays) // 2],
    )
    logger.info(
        "Fitted lag curve for offer %s from %s conversions (median %.1fh, "
        "%.0f%% reported within 24h).",
        offer_id if offer_id is not None else "all",
        len(delays),
        model.median_hours,
        model.maturity(24.0) * 100,
    )
    return model


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
