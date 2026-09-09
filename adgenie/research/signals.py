"""Turning Ad Library observations into creative direction.

The library reports no performance data for commercial ads, so "what is
working" has to be inferred. The inference this module makes is the one
experienced buyers make by hand:

    An advertiser does not fund a losing ad for three months.

So an ad's *staying power* — how long it has run, whether it is still live,
how many near-identical variants exist, and how far it reached — stands in for
profitability. It is a proxy, not a measurement, and everything here is scored
and labelled as such.

What comes out is a `MarketBrief`: which angles dominate, how long the winning
copy runs, the shape of the hooks and calls to action. That feeds the
copywriter as *pattern* guidance. Competitor copy is never reproduced: doing so
risks the advertiser's trademark, and this platform's own policy engine would
block it anyway.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..core.angles import ANGLES, Angle
from .ad_library import AdLibraryAd

__all__ = [
    "ScoredAd",
    "MarketBrief",
    "classify_angle",
    "score_staying_power",
    "count_variants",
    "build_market_brief",
]

# Surface patterns that identify which argument an ad is making. Deliberately
# lexical: an LLM classifier would be better but must not be a hard dependency,
# and a wrong guess here only mis-sorts a competitor, it does not ship an ad.
_ANGLE_PATTERNS: dict[str, tuple[str, ...]] = {
    "problem_solution": (
        r"\btired of\b", r"\bstruggl\w+\b", r"\bsick of\b", r"\bfrustrat\w+\b",
        r"\bfed up\b", r"\bthe problem with\b", r"\bstop\s+\w+ing\b",
    ),
    "mechanism": (
        r"\bhow it works\b", r"\bformula\b", r"\bingredient\w*\b", r"\bpatented\b",
        r"\bclinically\b", r"\bengineered\b", r"\btechnology\b", r"\bmethod\b",
        r"\bthe science\b",
    ),
    "social_proof": (
        r"\b\d[\d,.]*\+?\s*(customers|people|users|reviews|members)\b",
        r"\brated\b", r"\bstars?\b", r"\btestimonial\b", r"\bjoin\s+\d",
        r"\bloved by\b", r"\btrusted by\b", r"\bbestsell\w+\b",
    ),
    "comparison": (
        r"\bvs\.?\b", r"\bversus\b", r"\bcompared to\b", r"\bunlike\b",
        r"\balternative to\b", r"\bswitch(ed)? from\b", r"\bbetter than\b",
    ),
    "objection": (
        r"\bsceptical\b", r"\bskeptical\b", r"\bdoes it work\b", r"\bhonest review\b",
        r"\bwhat they don'?t tell you\b", r"\bis it worth\b", r"\bno catch\b",
    ),
    "cost_of_inaction": (
        r"\bevery day you\b", r"\bcosting you\b", r"\bwaiting\b", r"\badds up\b",
        r"\bthe longer you\b",
    ),
    "identity": (
        r"\bfor (people|women|men|parents|runners|founders)\b", r"\bif you'?re the kind\b",
        r"\bmade for\b", r"\bbuilt for\b",
    ),
    "how_to": (
        r"\bhow to\b", r"\bstep \d\b", r"\b\d+ (steps?|ways?|tips?)\b",
        r"\bguide\b", r"\bhere'?s how\b",
    ),
    "offer_led": (
        r"\b\d+% off\b", r"\bfree shipping\b", r"\bbuy \d+ get\b", r"\bsale\b",
        r"\bdiscount\b", r"\btoday only\b", r"\bmoney[- ]back\b", r"\bfree trial\b",
    ),
    "search_intent": (r"\bofficial site\b", r"\bshop now\b", r"\bbuy online\b"),
}

_COMPILED = {
    key: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    for key, patterns in _ANGLE_PATTERNS.items()
}

_CTA_PATTERNS = (
    "shop now", "learn more", "sign up", "get offer", "order now", "buy now",
    "try free", "download", "book now", "claim", "get started", "see more",
)


@dataclass
class ScoredAd:
    ad: AdLibraryAd
    angle: str
    staying_power: float
    days_running: int
    variant_count: int
    is_proven: bool

    def as_dict(self) -> dict:
        return {
            "ad_archive_id": self.ad.ad_archive_id,
            "page_name": self.ad.page_name,
            "angle": self.angle,
            "days_running": self.days_running,
            "variant_count": self.variant_count,
            "staying_power": round(self.staying_power, 3),
            "is_proven": self.is_proven,
            "still_running": self.ad.is_active,
            "eu_total_reach": self.ad.eu_total_reach,
            "snapshot_url": self.ad.snapshot_url,
        }


@dataclass
class MarketBrief:
    """What the market appears to be doing, and how confident we are."""

    search_term: str
    ads_seen: int
    proven_ads: int
    advertisers: int
    angle_ranking: list[tuple[str, float]] = field(default_factory=list)
    dominant_angles: list[str] = field(default_factory=list)
    median_days_running: int = 0
    longest_running_days: int = 0
    common_ctas: list[str] = field(default_factory=list)
    body_length_p50: int = 0
    headline_length_p50: int = 0
    emoji_usage_rate: float = 0.0
    question_hook_rate: float = 0.0
    numeric_claim_rate: float = 0.0
    top_ads: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        """How much weight this brief deserves.

        A handful of ads from two advertisers is an anecdote; a hundred ads
        from thirty advertisers, most of them long-running, is a pattern.
        """
        if self.proven_ads >= 20 and self.advertisers >= 8:
            return "high"
        if self.proven_ads >= 6 and self.advertisers >= 3:
            return "moderate"
        if self.ads_seen:
            return "low"
        return "none"

    def as_dict(self) -> dict:
        return {
            "search_term": self.search_term,
            "confidence": self.confidence,
            "ads_seen": self.ads_seen,
            "proven_ads": self.proven_ads,
            "advertisers": self.advertisers,
            "dominant_angles": self.dominant_angles,
            "angle_ranking": [
                {"angle": a, "share": round(s, 3)} for a, s in self.angle_ranking
            ],
            "median_days_running": self.median_days_running,
            "longest_running_days": self.longest_running_days,
            "common_ctas": self.common_ctas,
            "body_length_p50": self.body_length_p50,
            "headline_length_p50": self.headline_length_p50,
            "emoji_usage_rate": round(self.emoji_usage_rate, 3),
            "question_hook_rate": round(self.question_hook_rate, 3),
            "numeric_claim_rate": round(self.numeric_claim_rate, 3),
            "top_ads": self.top_ads,
            "warnings": self.warnings,
        }

    def to_prompt_notes(self) -> list[str]:
        """Pattern guidance for the copywriter.

        Describes shape, never wording. The copywriter is told which arguments
        are surviving in this market and how long-running copy is structured,
        and is left to write its own.
        """
        if self.confidence == "none":
            return []
        notes = [
            f"Competitor scan of {self.ads_seen} ads from {self.advertisers} "
            f"advertisers ({self.confidence} confidence). Long-running ads are a "
            "proxy for profitable ones; the archive reports no performance data."
        ]
        if self.dominant_angles:
            named = ", ".join(
                next((a.name for a in ANGLES if a.key == k), k)
                for k in self.dominant_angles
            )
            notes.append(
                f"Arguments surviving longest in this market: {named}. Treat "
                "these as evidence about the audience, not as copy to imitate."
            )
        if self.median_days_running:
            notes.append(
                f"The typical proven ad has run {self.median_days_running} days "
                f"(longest {self.longest_running_days}), so the market rewards a "
                "durable evergreen angle over a topical one."
            )
        if self.body_length_p50:
            notes.append(
                f"Long-running body copy runs about {self.body_length_p50} "
                f"characters and headlines about {self.headline_length_p50}."
            )
        if self.question_hook_rate > 0.35:
            notes.append(
                f"{self.question_hook_rate:.0%} of proven ads open on a question. "
                "Ask about the situation, never about the reader's own health, "
                "finances or identity."
            )
        if self.numeric_claim_rate > 0.4:
            notes.append(
                f"{self.numeric_claim_rate:.0%} of proven ads lead with a concrete "
                "number. Use one only if the brief supplies it."
            )
        if self.emoji_usage_rate > 0.5:
            notes.append("Most proven ads in this market use one or two emoji.")
        elif self.emoji_usage_rate < 0.15:
            notes.append("Proven ads here use no emoji; match that register.")
        if self.common_ctas:
            notes.append(f"Calls to action in use: {', '.join(self.common_ctas)}.")
        return notes


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def classify_angle(texts: list[str]) -> str:
    """Best-guess angle for an ad, by surface pattern."""
    joined = " ".join(texts).lower()
    if not joined.strip():
        return "unknown"
    scores: Counter[str] = Counter()
    for key, patterns in _COMPILED.items():
        for pattern in patterns:
            if pattern.search(joined):
                scores[key] += 1
    if not scores:
        return "unknown"
    return scores.most_common(1)[0][0]


def score_staying_power(
    ad: AdLibraryAd,
    variant_count: int = 1,
    proven_days: int = 30,
    as_of: datetime | None = None,
) -> float:
    """A 0-1 proxy for how well an ad is doing.

    Longevity dominates, on a log scale: the step from 7 days to 30 says far
    more than the step from 90 to 120. Still being live, and the advertiser
    bothering to produce variants, both add to it.
    """
    days = ad.days_running(as_of)
    longevity = math.log1p(max(0, days)) / math.log1p(180)
    score = 0.65 * min(1.0, longevity)
    if ad.is_active:
        score += 0.15
    if days >= proven_days:
        score += 0.10
    # More variants means more committed budget, with fast diminishing returns.
    score += 0.10 * min(1.0, math.log1p(max(0, variant_count - 1)) / math.log1p(9))
    return round(min(1.0, score), 4)


def count_variants(ads: list[AdLibraryAd]) -> dict[str, int]:
    """How many near-identical ads each page is running.

    Keyed by ad id. Two ads count as the same idea when their first dozen
    normalised words match, which catches the usual practice of duplicating one
    creative across audiences.
    """
    buckets: Counter[tuple[str | None, str]] = Counter()
    keys: dict[str, tuple[str | None, str]] = {}
    for ad in ads:
        text = " ".join(ad.all_text()).lower()
        signature = " ".join(re.findall(r"[a-z0-9']+", text)[:12])
        key = (ad.page_id, signature)
        keys[ad.ad_archive_id] = key
        buckets[key] += 1
    return {ad_id: buckets[key] for ad_id, key in keys.items()}


_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")


def build_market_brief(
    ads: list[AdLibraryAd],
    search_term: str = "",
    proven_days: int = 30,
    warnings: list | None = None,
    as_of: datetime | None = None,
) -> MarketBrief:
    """Aggregate a set of observed ads into creative direction."""
    as_of = as_of or datetime.now(timezone.utc)
    warning_dicts = [
        w.as_dict() if hasattr(w, "as_dict") else dict(w) for w in (warnings or [])
    ]
    if not ads:
        return MarketBrief(
            search_term=search_term,
            ads_seen=0,
            proven_ads=0,
            advertisers=0,
            warnings=warning_dicts,
        )

    variants = count_variants(ads)
    scored: list[ScoredAd] = []
    for ad in ads:
        days = ad.days_running(as_of)
        variant_count = variants.get(ad.ad_archive_id, 1)
        scored.append(
            ScoredAd(
                ad=ad,
                angle=classify_angle(ad.all_text()),
                staying_power=score_staying_power(ad, variant_count, proven_days, as_of),
                days_running=days,
                variant_count=variant_count,
                is_proven=days >= proven_days,
            )
        )
    scored.sort(key=lambda s: s.staying_power, reverse=True)

    # Only proven ads shape the guidance. A week-old ad is an experiment, and
    # copying other people's experiments is how you inherit their losses.
    proven = [s for s in scored if s.is_proven] or scored

    # Weight each angle by staying power rather than by count, so one page
    # flooding the archive with new ads cannot outvote a durable competitor.
    weights: Counter[str] = Counter()
    for item in proven:
        if item.angle != "unknown":
            weights[item.angle] += item.staying_power
    total_weight = sum(weights.values()) or 1.0
    ranking = [(k, v / total_weight) for k, v in weights.most_common()]

    bodies = [b for s in proven for b in s.ad.bodies if b]
    titles = [t for s in proven for t in s.ad.titles if t]
    days_list = sorted(s.days_running for s in proven)

    ctas: Counter[str] = Counter()
    for text in (" ".join(s.ad.all_text()).lower() for s in proven):
        for cta in _CTA_PATTERNS:
            if cta in text:
                ctas[cta] += 1

    return MarketBrief(
        search_term=search_term,
        ads_seen=len(ads),
        proven_ads=sum(1 for s in scored if s.is_proven),
        advertisers=len({a.page_id for a in ads if a.page_id}),
        angle_ranking=ranking,
        dominant_angles=[k for k, share in ranking if share >= 0.12][:4],
        median_days_running=_median(days_list),
        longest_running_days=max(days_list) if days_list else 0,
        common_ctas=[c for c, _ in ctas.most_common(4)],
        body_length_p50=_median(sorted(len(b) for b in bodies)),
        headline_length_p50=_median(sorted(len(t) for t in titles)),
        emoji_usage_rate=_rate(proven, lambda s: bool(_EMOJI.search(" ".join(s.ad.all_text())))),
        question_hook_rate=_rate(proven, lambda s: "?" in " ".join(s.ad.all_text()[:1])),
        numeric_claim_rate=_rate(
            proven, lambda s: bool(re.search(r"\d", " ".join(s.ad.all_text())))
        ),
        top_ads=[s.as_dict() for s in scored[:10]],
        warnings=warning_dicts,
    )


def _median(values: list[int]) -> int:
    if not values:
        return 0
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) // 2


def _rate(items: list[ScoredAd], predicate) -> float:
    if not items:
        return 0.0
    return sum(1 for i in items if predicate(i)) / len(items)
