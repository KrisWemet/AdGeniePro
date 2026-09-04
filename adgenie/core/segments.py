"""Finding the segment that is quietly eating the budget.

Campaign totals hide their own worst parts. An affiliate ad set frequently has
one placement or one age bracket consuming a fifth of the spend at a fraction
of the conversion rate, and because the campaign average still looks acceptable
nobody goes looking. Cutting it is often a larger ROAS move than any creative
test, and it is available immediately rather than after another week of data.

The hard part is not finding the worst segment — sorting does that. It is
knowing whether the worst segment is genuinely bad or merely unlucky, because
with five segments and a 90% credible interval you will find a "significant"
loser somewhere roughly half the time by chance alone. So every segment is
compared against the rest of the entity pooled, on the same Beta posterior the
rest of the optimizer uses, with the number of segments tested accounted for.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from ..models import EntityLevel
from ..money import micros_to_usd, safe_div
from ..platforms.base import BreakdownRow
from .stats import beta_interval, prob_b_beats_a

logger = logging.getLogger(__name__)

__all__ = ["SegmentStat", "SegmentReport", "analyse_segments"]


@dataclass
class SegmentStat:
    dimension: str
    segment: str
    impressions: int = 0
    clicks: int = 0
    spend_micros: int = 0
    conversions: float = 0.0
    payout_micros: int = 0
    # Probability this segment's true conversion rate is worse than the rest.
    prob_worse: float = 0.0
    share_of_spend: float = 0.0
    verdict: str = "keep"
    reason: str = ""

    @property
    def cvr(self) -> float:
        return safe_div(self.conversions, self.clicks)

    @property
    def cpc_micros(self) -> float:
        return safe_div(self.spend_micros, self.clicks)

    @property
    def revenue_micros(self) -> int:
        return int(self.conversions * self.payout_micros)

    @property
    def roas(self) -> float:
        return safe_div(self.revenue_micros, self.spend_micros)

    @property
    def wasted_micros(self) -> int:
        """Money this segment lost, floored at zero."""
        return max(0, self.spend_micros - self.revenue_micros)

    def as_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "segment": self.segment,
            "clicks": self.clicks,
            "spend_usd": micros_to_usd(self.spend_micros),
            "conversions": round(self.conversions, 2),
            "cvr": round(self.cvr, 5),
            "roas": round(self.roas, 3),
            "share_of_spend": round(self.share_of_spend, 4),
            "prob_worse_than_rest": round(self.prob_worse, 4),
            "wasted_usd": micros_to_usd(self.wasted_micros),
            "verdict": self.verdict,
            "reason": self.reason,
        }


@dataclass
class SegmentReport:
    level: EntityLevel
    entity_id: int
    dimension: str
    segments: list[SegmentStat] = field(default_factory=list)
    exclusions: list[SegmentStat] = field(default_factory=list)
    total_spend_micros: int = 0
    recoverable_micros: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "level": self.level.value,
            "entity_id": self.entity_id,
            "dimension": self.dimension,
            "total_spend_usd": micros_to_usd(self.total_spend_micros),
            "recoverable_usd": micros_to_usd(self.recoverable_micros),
            "exclusions": [s.as_dict() for s in self.exclusions],
            "segments": [s.as_dict() for s in self.segments],
            "note": self.note,
        }


def analyse_segments(
    rows: list[BreakdownRow],
    level: EntityLevel,
    entity_id: int,
    dimension: str,
    payout_micros: int,
    *,
    min_clicks: int = 60,
    min_share_of_spend: float = 0.05,
    confidence: float = 0.95,
    max_exclusions: int = 2,
    keep_minimum_segments: int = 2,
) -> SegmentReport:
    """Decide which segments, if any, are worth cutting.

    A segment must clear four bars before it is proposed for exclusion, and
    each one exists to stop a specific way of being wrong:

    * enough clicks, so the comparison is not noise;
    * enough of the budget, so the cut is worth making at all;
    * a high probability of being genuinely worse than its peers, adjusted for
      how many segments were examined;
    * and it must not be the last one standing.
    """
    by_segment: dict[str, SegmentStat] = {}
    for row in rows:
        stat = by_segment.get(row.segment)
        if stat is None:
            stat = SegmentStat(
                dimension=dimension, segment=row.segment, payout_micros=payout_micros
            )
            by_segment[row.segment] = stat
        stat.impressions += row.impressions
        stat.clicks += row.clicks
        stat.spend_micros += row.spend_micros
        stat.conversions += row.conversions

    report = SegmentReport(level=level, entity_id=entity_id, dimension=dimension)
    stats = list(by_segment.values())
    if not stats:
        report.note = "No delivery data for this dimension."
        return report

    total_spend = sum(s.spend_micros for s in stats)
    total_clicks = sum(s.clicks for s in stats)
    total_conversions = sum(s.conversions for s in stats)
    report.total_spend_micros = total_spend

    for stat in stats:
        stat.share_of_spend = safe_div(stat.spend_micros, total_spend)
        # Compared against everything else pooled, not against the best
        # performer: the question is whether this segment drags the entity down,
        # not whether it is the strongest.
        rest_clicks = total_clicks - stat.clicks
        rest_conversions = total_conversions - stat.conversions
        if stat.clicks and rest_clicks:
            stat.prob_worse = prob_b_beats_a(
                stat.conversions, stat.clicks, rest_conversions, rest_clicks
            )

    # With N segments examined, the chance of one looking bad by luck grows with
    # N. Requiring a correspondingly higher bar keeps the false-positive rate at
    # roughly `confidence` across the whole comparison rather than per segment.
    tested = max(1, len(stats))
    adjusted_confidence = 1.0 - (1.0 - confidence) / tested

    candidates: list[SegmentStat] = []
    for stat in sorted(stats, key=lambda s: s.wasted_micros, reverse=True):
        if stat.clicks < min_clicks:
            stat.verdict = "keep"
            stat.reason = (
                f"Only {stat.clicks} clicks; too little to tell it apart from "
                "the rest."
            )
            continue
        if stat.share_of_spend < min_share_of_spend:
            stat.verdict = "keep"
            stat.reason = (
                f"Takes {stat.share_of_spend:.1%} of spend; cutting it would "
                "not move the campaign."
            )
            continue
        if stat.prob_worse < adjusted_confidence:
            stat.verdict = "keep"
            stat.reason = (
                f"{stat.prob_worse:.0%} likely to be genuinely worse than the "
                f"rest, short of the {adjusted_confidence:.0%} bar for cutting "
                f"one of {tested} segments."
            )
            continue

        lower_bound = beta_interval(stat.conversions, stat.clicks, 0.9).upper
        stat.verdict = "exclude"
        stat.reason = (
            f"Takes {stat.share_of_spend:.0%} of spend at a "
            f"{stat.cvr:.2%} conversion rate against "
            f"{safe_div(total_conversions - stat.conversions, total_clicks - stat.clicks):.2%} "
            f"elsewhere, {stat.prob_worse:.0%} likely to be genuinely worse. "
            f"Roughly {micros_to_usd(stat.wasted_micros):.2f} USD lost here; its "
            f"conversion rate is below {lower_bound:.2%} with 95% confidence."
        )
        candidates.append(stat)

    # Never cut so much that nothing is left to deliver on.
    survivors = len(stats) - len(candidates)
    while candidates and survivors < keep_minimum_segments:
        rescued = candidates.pop()
        rescued.verdict = "keep"
        rescued.reason = (
            "Cutting this would leave too few segments for the entity to "
            "deliver; review the whole entity instead."
        )
        survivors += 1

    report.exclusions = candidates[:max_exclusions]
    for extra in candidates[max_exclusions:]:
        extra.verdict = "keep"
        extra.reason = (
            "Held back this cycle: only the worst few segments are cut at a "
            "time so the effect of each is measurable."
        )
    report.recoverable_micros = sum(s.wasted_micros for s in report.exclusions)
    report.segments = sorted(stats, key=lambda s: s.spend_micros, reverse=True)

    if report.exclusions:
        logger.info(
            "%s %s: excluding %s on %s, about %s USD of waste",
            level.value,
            entity_id,
            ", ".join(s.segment for s in report.exclusions),
            dimension,
            f"{micros_to_usd(report.recoverable_micros):.2f}",
        )
    else:
        report.note = "No segment is clearly bad enough to cut yet."
    return report


def group_rows(rows: list[BreakdownRow]) -> dict[str, list[BreakdownRow]]:
    """Split fetched rows by the entity they belong to."""
    grouped: dict[str, list[BreakdownRow]] = defaultdict(list)
    for row in rows:
        grouped[row.external_id].append(row)
    return dict(grouped)
