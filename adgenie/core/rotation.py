"""When an offer needs a new argument, not another wording of the old one.

The optimizer breeds variants from a fatigued creative, and those variants
inherit the parent's angle. That is right when the wording wore out and wrong
when the *argument* did: it produces a family of ads that all fail together,
for the same reason, while the budget keeps going out.

Two distinctions do most of the work here, and both are routinely collapsed.

**An angle is not an execution.** One bad ad for a good argument is the most
common outcome in advertising. Judging an angle on a single creative confounds
the argument with that creative's particular headline, image and hook, and
retiring it throws away a whole line of attack over one bad Tuesday. An angle
is only ever retired on pooled evidence from several distinct executions.

**Fatigue is not failure.** An angle whose click-through has decayed against
its own opening, but whose conversion rate is intact, is worn out *on this
audience* — not wrong. Audiences forget. Such an angle is rested with a return
date, and when it comes back it outranks an untried one, because the argument
is already known to land and only the timing was off. Retiring it instead
deletes a proven asset and replaces it with a coin flip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AdGroup,
    Campaign,
    Creative,
    EntityLevel,
    EntityStatus,
    MetricSnapshot,
    Offer,
    Platform,
)
from ..money import micros_to_usd, safe_div
from .angles import ANGLES, angles_for, get_angle
from .stats import beta_interval, prob_b_beats_a

# Verdicts that mean the angle is genuinely earning its place. "last_resort"
# is deliberately absent: an angle running only because nothing else is left
# must not make a spent offer read as a healthy one.
HEALTHY_VERDICTS = frozenset({"scale", "hold", "unproven"})

__all__ = [
    "HEALTHY_VERDICTS",
    "RotationPolicy",
    "AngleStat",
    "RotationPlan",
    "analyse_rotation",
]


@dataclass(frozen=True)
class RotationPolicy:
    """Thresholds. Every number here is a business decision, not a constant."""

    # An angle cannot be retired on fewer than this many distinct creatives.
    # This is the guard against confusing a bad execution with a bad argument.
    min_executions_to_judge: int = 3
    min_clicks_to_judge: int = 120
    confidence: float = 0.90
    credible_level: float = 0.90
    # Decay against the angle's own opening, not against its siblings, which
    # may have run to different audiences.
    ctr_decay_threshold: float = 0.35
    # How long a fatigued angle sits out before it is worth running again.
    rest_days: int = 30
    # Above this probability of being the weaker argument, a fatigued angle is
    # no longer described as one whose conversion rate held, and it stops
    # outranking an untried angle when the rest is over. Well short of the bar
    # for retiring: this only governs what is claimed about it.
    weaker_argument_probability: float = 0.70
    # Never rotate the offer down to nothing. An empty offer stops earning
    # while the replacements are still in review.
    keep_minimum_live: int = 1
    # How many fresh angles to introduce in one cycle. Introducing six at once
    # splits the budget so thinly that none of them gets a verdict.
    max_new_angles_per_cycle: int = 2
    # And how many may be in flight at all. Adding an angle while several are
    # still unproven makes every one of them slower to conclude: the same
    # spreading-too-thin mistake the portfolio allocator refuses to make with
    # offers. Finish the tests you started before starting more.
    max_angles_in_flight: int = 4


@dataclass
class AngleStat:
    """One argument's record for this offer, pooled over every ad that used it."""

    key: str
    name: str
    creative_ids: list[int] = field(default_factory=list)
    live_creative_ids: list[int] = field(default_factory=list)
    impressions: int = 0
    clicks: int = 0
    spend_micros: int = 0
    conversions: int = 0
    revenue_micros: int = 0
    opening_ctr: float = 0.0
    # Click-through over the most recent delivering days, kept apart from the
    # lifetime rate because only the recent one can show decay.
    recent_ctr: float = 0.0
    last_delivery: date | None = None
    prob_worse: float = 0.0
    verdict: str = "hold"
    reason: str = ""
    rest_until: date | None = None

    @property
    def executions(self) -> int:
        return len(self.creative_ids)

    @property
    def ctr(self) -> float:
        return safe_div(self.clicks, self.impressions)

    @property
    def cvr(self) -> float:
        return safe_div(self.conversions, self.clicks)

    @property
    def roas(self) -> float:
        return safe_div(self.revenue_micros, self.spend_micros)

    @property
    def decay(self) -> float:
        """How far click-through has fallen from this angle's own opening.

        Against its own opening rather than against its siblings, which may
        have run to different audiences at different times.
        """
        if not self.opening_ctr or not self.recent_ctr:
            return 0.0
        return max(0.0, (self.opening_ctr - self.recent_ctr) / self.opening_ctr)

    def as_dict(self) -> dict:
        return {
            "angle": self.key,
            "name": self.name,
            "verdict": self.verdict,
            "reason": self.reason,
            "executions": self.executions,
            "live": len(self.live_creative_ids),
            "clicks": self.clicks,
            "spend_usd": micros_to_usd(self.spend_micros),
            "conversions": self.conversions,
            "cvr": round(self.cvr, 5),
            "roas": round(self.roas, 3),
            "ctr": round(self.ctr, 5),
            "opening_ctr": round(self.opening_ctr, 5),
            "recent_ctr": round(self.recent_ctr, 5),
            "decay": round(self.decay, 3),
            "prob_worse": round(self.prob_worse, 4),
            "rest_until": self.rest_until.isoformat() if self.rest_until else None,
            "creative_ids": self.creative_ids,
        }


@dataclass
class RotationPlan:
    offer_id: int
    offer_name: str
    angles: list[AngleStat] = field(default_factory=list)
    # Angles from the library this offer has never run.
    untested: list[str] = field(default_factory=list)
    # What to run next, best first.
    introduce: list[str] = field(default_factory=list)
    exhausted: bool = False
    recommendation: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "offer_id": self.offer_id,
            "offer": self.offer_name,
            "exhausted": self.exhausted,
            "recommendation": self.recommendation,
            "introduce": self.introduce,
            "untested": self.untested,
            "angles": [a.as_dict() for a in self.angles],
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# gathering the record
# --------------------------------------------------------------------------
def _creatives_for_offer(session: Session, offer_id: int) -> list[Creative]:
    return list(
        session.execute(
            select(Creative)
            .join(AdGroup, Creative.ad_group_id == AdGroup.id)
            .join(Campaign, AdGroup.campaign_id == Campaign.id)
            .where(Campaign.offer_id == offer_id)
        ).scalars()
    )


def _platforms_for_offer(session: Session, offer_id: int) -> set[Platform]:
    return set(
        session.execute(
            select(Campaign.platform).where(Campaign.offer_id == offer_id)
        ).scalars()
    )


def _delivery_by_day(
    session: Session, creative_ids: list[int]
) -> list[tuple[date, int, int, int]]:
    """(day, impressions, clicks, spend) for a set of creatives, summed per day.

    Per day, because several creatives sharing an angle deliver on the same
    date. Keeping them as separate rows would make the opening and closing
    windows below count days more than once.
    """
    if not creative_ids:
        return []
    rows = session.execute(
        select(
            MetricSnapshot.day,
            func.sum(MetricSnapshot.impressions),
            func.sum(MetricSnapshot.clicks),
            func.sum(MetricSnapshot.spend_micros),
        )
        .where(
            MetricSnapshot.level == EntityLevel.CREATIVE,
            MetricSnapshot.entity_id.in_(creative_ids),
        )
        .group_by(MetricSnapshot.day)
        .order_by(MetricSnapshot.day)
    ).all()
    return [
        (day, int(imp or 0), int(clicks or 0), int(spend or 0))
        for day, imp, clicks, spend in rows
        if (imp or 0) > 0
    ]


def _window_ctr(days: list[tuple[date, int, int, int]]) -> float:
    impressions = sum(row[1] for row in days)
    clicks = sum(row[2] for row in days)
    return safe_div(clicks, impressions)


def _conversions_for(
    session: Session, creative_ids: list[int]
) -> tuple[int, int]:
    from ..models import Conversion, ConversionStatus

    if not creative_ids:
        return 0, 0
    row = session.execute(
        select(
            func.count(Conversion.id),
            func.coalesce(func.sum(Conversion.revenue_micros), 0),
        ).where(
            Conversion.creative_id.in_(creative_ids),
            Conversion.status == ConversionStatus.APPROVED,
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


# --------------------------------------------------------------------------
# the analysis
# --------------------------------------------------------------------------
def analyse_rotation(
    session: Session,
    offer: Offer,
    policy: RotationPolicy | None = None,
    now: datetime | None = None,
    opening_days: int = 7,
) -> RotationPlan:
    """Decide, per angle, whether to keep it, rest it, or stop using it."""
    policy = policy or RotationPolicy()
    today = (now or datetime.now(timezone.utc)).date()
    plan = RotationPlan(offer_id=offer.id, offer_name=offer.name)

    creatives = _creatives_for_offer(session, offer.id)
    by_angle: dict[str, list[Creative]] = {}
    for creative in creatives:
        if not creative.angle:
            continue
        by_angle.setdefault(creative.angle, []).append(creative)

    stats: list[AngleStat] = []
    for key, members in sorted(by_angle.items()):
        angle = get_angle(key)
        stat = AngleStat(
            key=key,
            name=angle.name,
            creative_ids=[c.id for c in members],
            live_creative_ids=[
                c.id for c in members if c.status is EntityStatus.ACTIVE
            ],
        )
        days = _delivery_by_day(session, stat.creative_ids)
        stat.impressions = sum(row[1] for row in days)
        stat.clicks = sum(row[2] for row in days)
        stat.spend_micros = sum(row[3] for row in days)
        stat.conversions, stat.revenue_micros = _conversions_for(
            session, stat.creative_ids
        )
        if days:
            stat.last_delivery = days[-1][0]
            stat.opening_ctr = _window_ctr(days[:opening_days])
            # Deliberately the *recent* window rather than the lifetime rate.
            # Lifetime CTR averages in the good opening days and so understates
            # decay — an angle halfway through wearing out would read as
            # healthy right up until it was worthless.
            stat.recent_ctr = _window_ctr(days[-opening_days:])
        stats.append(stat)

    _judge(stats, policy, today)
    _protect_the_last_one_standing(stats, policy)

    plan.angles = sorted(stats, key=lambda s: s.roas, reverse=True)
    plan.untested = _untested_keys(session, offer.id, set(by_angle))
    plan.introduce = _what_to_run_next(plan, policy, today)
    plan.exhausted = not plan.introduce and not any(
        s.verdict in HEALTHY_VERDICTS for s in stats
    )
    plan.recommendation = _recommend(plan, policy, today)
    return plan


def _judge(stats: list[AngleStat], policy: RotationPolicy, today: date) -> None:
    """Assign a verdict to each angle. Order matters."""
    # Testing eight angles at 90% finds a "loser" by chance more often than
    # not. The bar for cutting one of N is raised accordingly, the same
    # adjustment the segment analysis makes.
    tested = max(1, len(stats))
    adjusted = 1.0 - (1.0 - policy.confidence) / tested

    total_clicks = sum(s.clicks for s in stats)
    total_conversions = sum(s.conversions for s in stats)

    for stat in stats:
        # Compared against its peers pooled, leaving itself out. Pooling over a
        # group that includes this angle shrinks it toward its own result and
        # makes a bad angle look like the average it is dragging down.
        peer_clicks = total_clicks - stat.clicks
        peer_conversions = total_conversions - stat.conversions
        if peer_clicks > 0 and stat.clicks > 0:
            stat.prob_worse = prob_b_beats_a(
                stat.conversions, stat.clicks, peer_conversions, peer_clicks
            )

        resting_until = (
            stat.last_delivery + timedelta(days=policy.rest_days)
            if stat.last_delivery
            else None
        )
        judged = (
            stat.executions >= policy.min_executions_to_judge
            and stat.clicks >= policy.min_clicks_to_judge
        )

        if judged and stat.prob_worse >= adjusted:
            stat.verdict = "retire"
            stat.reason = (
                f"{stat.prob_worse:.0%} likely to be genuinely worse than this "
                f"offer's other angles, over {stat.executions} separate ads and "
                f"{stat.clicks} clicks. That is the argument failing, not one ad."
            )
        elif stat.clicks >= policy.min_clicks_to_judge and (
            stat.decay >= policy.ctr_decay_threshold
        ):
            # No execution minimum here, unlike retiring. Fatigue is measured
            # from impressions and click-through, which a single creative
            # establishes perfectly well, and resting is reversible: the angle
            # comes back. Retiring is not, which is why it costs more evidence.
            stat.verdict = "rest"
            stat.rest_until = resting_until
            # Only claim the conversion rate held if it did. An angle that is
            # both wearing out and converting poorly is a weaker case for
            # bringing back, and saying otherwise oversells the rest.
            held = stat.prob_worse < policy.weaker_argument_probability
            aside = (
                "while the conversion rate held"
                if held
                else f"and it is {stat.prob_worse:.0%} likely to be the weaker "
                f"argument too, on evidence short of the bar for cutting it"
            )
            stat.reason = (
                f"Click-through fell from {stat.opening_ctr:.2%} to "
                f"{stat.recent_ctr:.2%}, a {stat.decay:.0%} decay, {aside}. "
                f"Rest until {resting_until:%Y-%m-%d} and judge it then."
            )
        elif not judged:
            stat.verdict = "unproven"
            missing = []
            if stat.executions < policy.min_executions_to_judge:
                missing.append(
                    f"{policy.min_executions_to_judge - stat.executions} more "
                    f"execution(s)"
                )
            if stat.clicks < policy.min_clicks_to_judge:
                missing.append(
                    f"{policy.min_clicks_to_judge - stat.clicks} more clicks"
                )
            stat.reason = (
                "Not enough evidence to judge the argument rather than the ad: "
                "needs " + " and ".join(missing) + "."
            )
        else:
            stat.verdict = "scale"
            interval = beta_interval(
                stat.conversions, stat.clicks, policy.credible_level
            )
            stat.reason = (
                f"Converting at {stat.cvr:.2%} (90% range "
                f"{interval.lower:.2%}-{interval.upper:.2%}) over "
                f"{stat.executions} ads, with click-through holding."
            )


def _protect_the_last_one_standing(
    stats: list[AngleStat], policy: RotationPolicy
) -> None:
    """Never rotate an offer down to nothing.

    Retiring and resting are both correct in isolation and can still, together,
    switch the offer off — while the replacements are drafts waiting on policy
    review. The best of the condemned keeps running until something else is
    live to take over.
    """
    live = [s for s in stats if s.live_creative_ids]
    if not live:
        return
    survivors = [s for s in live if s.verdict in HEALTHY_VERDICTS]
    if len(survivors) >= policy.keep_minimum_live:
        return

    spare = sorted(
        (s for s in live if s.verdict in ("rest", "retire")),
        # Resting beats retiring, then by realised return: the best of a bad
        # set, not merely the least statistically condemned.
        key=lambda s: (s.verdict == "rest", s.roas),
        reverse=True,
    )
    for stat in spare[: policy.keep_minimum_live - len(survivors)]:
        was = stat.verdict
        # Deliberately not "hold". This angle is running because there is
        # nothing else, not because it is working, and calling it a hold would
        # let a spent offer read as a healthy one everywhere downstream.
        stat.verdict = "last_resort"
        stat.rest_until = None
        stat.reason = (
            f"Would {was}, but it is the last angle this offer has live. "
            "Kept running until a replacement is delivering — an offer with no "
            "ads earns nothing while its replacements sit in review. This is a "
            "stopgap, not a verdict in its favour."
        )


def _untested_keys(session: Session, offer_id: int, used: set[str]) -> list[str]:
    """Library angles this offer has never run, in platform priority order."""
    platforms = _platforms_for_offer(session, offer_id) or {Platform.META}
    ordered: list[str] = []
    for platform in sorted(platforms, key=lambda p: p.value):
        for angle in angles_for(platform.value):
            if angle.key not in used and angle.key not in ordered:
                ordered.append(angle.key)
    # Anything the priority lists omit still belongs in the library.
    for angle in ANGLES:
        if angle.key not in used and angle.key not in ordered:
            ordered.append(angle.key)
    return ordered


def _what_to_run_next(
    plan: RotationPlan, policy: RotationPolicy, today: date
) -> list[str]:
    """The angles worth introducing now, best first.

    A proven angle coming off rest goes ahead of an untried one. The argument
    is already known to land on this audience; only the timing was wrong. An
    untried angle is a coin flip, and a coin flip is what you buy when you have
    nothing better.
    """
    returning = [
        stat
        for stat in plan.angles
        if stat.verdict == "rest"
        and stat.rest_until is not None
        and stat.rest_until <= today
        # It only outranks an untried angle if it actually worked. An angle
        # that was fatiguing *and* converting worse than its peers is not a
        # proven asset coming back, it is a worse coin flip.
        and stat.roas > 0
        and stat.prob_worse < policy.weaker_argument_probability
    ]
    returning.sort(key=lambda s: s.roas, reverse=True)

    in_flight = sum(
        1
        for stat in plan.angles
        if stat.verdict in HEALTHY_VERDICTS and stat.live_creative_ids
    )
    slots = max(0, policy.max_angles_in_flight - in_flight)
    if not slots:
        return []

    out = [stat.key for stat in returning]
    out.extend(key for key in plan.untested if key not in out)
    return out[: min(slots, policy.max_new_angles_per_cycle)]


def _recommend(plan: RotationPlan, policy: RotationPolicy, today: date) -> str:
    if not plan.angles:
        return (
            "Nothing has run yet. Launch a structured test across several "
            "angles before any of this means anything."
        )
    if plan.exhausted:
        return (
            "Every angle is spent and the library is used up. More creative is "
            "not the answer here: another variant of a worn-out argument to an "
            "audience that has already rejected it costs money and teaches "
            "nothing. Change the audience, change the platform, or accept that "
            "this offer is finished and put the budget somewhere else."
        )
    resting = [s for s in plan.angles if s.verdict == "rest"]
    unproven = [
        s for s in plan.angles if s.verdict == "unproven" and s.live_creative_ids
    ]
    if not plan.introduce and len(unproven) >= policy.max_angles_in_flight:
        return (
            f"{len(unproven)} angle(s) are still running without enough evidence "
            f"to judge the argument rather than the ad. Adding more now makes "
            f"every one of them slower to conclude. Let these finish first."
        )
    if plan.introduce:
        names = ", ".join(get_angle(key).name for key in plan.introduce)
        returning = [
            s.key for s in plan.angles if s.verdict == "rest" and s.key in plan.introduce
        ]
        note = (
            " Bringing a rested angle back beats trying a new one: you already "
            "know the argument works here."
            if returning
            else ""
        )
        return f"Introduce: {names}.{note}"
    if resting:
        soonest = min(s.rest_until for s in resting if s.rest_until)
        return (
            f"Nothing new to introduce. The next rested angle is available on "
            f"{soonest:%Y-%m-%d}; until then, run what is working."
        )
    return "Keep running what is live. Nothing needs rotating yet."
