"""An in-memory ad platform that simulates a real auction.

This exists because you cannot iterate on an optimizer against live ad spend.
Every creative is assigned a hidden true click-through and conversion rate,
derived deterministically from its text and the seed, and each simulated day
draws observed delivery from those rates. The optimizer therefore faces the
actual problem it faces in production: inferring a latent rate from a noisy,
budget-limited sample.

Latent quality is a function of the copy, not a coin flip. Length, specificity,
angle and policy findings all move it, so a better ad really does win here, and
a test that passes against the sandbox is testing the decision logic rather
than a mock.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..models import Platform
from .base import (
    AdGroupSpec,
    AdPlatform,
    CampaignSpec,
    CreativeSpec,
    InsightRow,
    PlatformError,
)

__all__ = ["SandboxPlatform", "SandboxEntity", "MarketModel"]


@dataclass
class SandboxEntity:
    external_id: str
    level: str
    name: str
    parent_id: str | None = None
    active: bool = False
    daily_budget_micros: int = 0
    bid_micros: int = 0
    spec: dict = field(default_factory=dict)
    # Hidden truth the optimizer is trying to discover.
    true_ctr: float = 0.0
    true_cvr: float = 0.0
    created_on: date | None = None


@dataclass
class MarketModel:
    """Auction economics for the simulated market."""

    base_cpm_micros: int = 12_000_000  # $12 CPM
    cpm_volatility: float = 0.18
    # A better ad is rewarded with cheaper impressions, as in a real auction.
    quality_cpm_discount: float = 0.45
    daily_impression_ceiling: int = 400_000
    # Frequency-driven decay: the same audience seeing an ad repeatedly.
    fatigue_per_day: float = 0.012
    weekend_lift: float = 0.08


class SandboxPlatform(AdPlatform):
    """A complete, deterministic stand-in for Meta or Google."""

    def __init__(
        self,
        platform: Platform = Platform.META,
        seed: int = 1337,
        market: MarketModel | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self.platform = platform
        self.seed = seed
        self.market = market or MarketModel()
        self.entities: dict[str, SandboxEntity] = {}
        self.insights: dict[tuple[str, date], InsightRow] = {}
        self.calls: list[tuple[str, dict]] = []
        self.uploaded_conversions: list[dict] = []
        # Operation names that should raise, for exercising error handling.
        self.fail_on = fail_on or set()
        self._counter = 0

    # -- helpers ---------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{self.platform.value[:1]}{prefix}{self._counter:06d}"

    def _guard(self, operation: str) -> None:
        if operation in self.fail_on:
            raise PlatformError(
                f"simulated failure on {operation}",
                platform=self.platform,
                code="SIMULATED",
                retryable=operation.endswith("_retryable"),
            )

    def _rng_for(self, key: str) -> random.Random:
        digest = hashlib.sha256(f"{self.seed}:{key}".encode()).hexdigest()
        return random.Random(int(digest[:16], 16))

    # -- latent quality --------------------------------------------------
    def _latent_quality(self, spec: CreativeSpec) -> tuple[float, float]:
        """Derive hidden CTR and CVR from the copy itself.

        The signal is intentionally simple but not arbitrary: specificity and
        readable length help, shouting and empty superlatives hurt. That means
        the copywriter's output quality actually shows up in the metrics the
        optimizer sees.
        """
        text = " ".join(spec.headlines + spec.descriptions + spec.primary_texts)
        lowered = text.lower()
        rng = self._rng_for(f"quality:{spec.name}:{text[:400]}")

        # Baselines differ by platform: search intent converts far better than
        # interrupted feed browsing.
        if self.platform is Platform.GOOGLE:
            ctr, cvr = 0.045, 0.035
        else:
            ctr, cvr = 0.012, 0.018

        score = 0.0
        if spec.headlines:
            avg_len = sum(len(h) for h in spec.headlines) / len(spec.headlines)
            score += 0.25 if 18 <= avg_len <= 32 else -0.10
        score += 0.10 * min(3, len(set(h.lower() for h in spec.headlines)) / 4)
        if any(ch.isdigit() for ch in text):
            score += 0.15  # concrete numbers earn attention
        if len(text) > 60:
            score += 0.10
        for weak in ("amazing", "incredible", "best ever", "click here", "act now"):
            if weak in lowered:
                score -= 0.18
        letters = [c for c in text if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.35:
            score -= 0.25
        if text.count("!") > 2:
            score -= 0.15

        # Idiosyncratic creative luck: the part no heuristic predicts, and the
        # reason testing exists at all.
        luck = rng.lognormvariate(0.0, 0.42)
        ctr *= max(0.15, (1.0 + score)) * luck
        cvr *= max(0.20, (1.0 + score * 0.6)) * self._rng_for(
            f"cvr:{text[:200]}"
        ).lognormvariate(0.0, 0.35)
        return min(0.35, ctr), min(0.45, cvr)

    # -- creation --------------------------------------------------------
    def create_campaign(self, spec: CampaignSpec) -> str:
        self._guard("create_campaign")
        eid = self._next_id("camp")
        self.entities[eid] = SandboxEntity(
            external_id=eid,
            level="campaign",
            name=spec.name,
            active=spec.status.upper() == "ACTIVE",
            daily_budget_micros=spec.daily_budget_micros,
            spec=spec.__dict__.copy(),
        )
        self.calls.append(("create_campaign", {"id": eid, "name": spec.name}))
        return eid

    def create_ad_group(self, spec: AdGroupSpec) -> str:
        self._guard("create_ad_group")
        if spec.campaign_external_id not in self.entities:
            raise PlatformError(
                f"unknown campaign {spec.campaign_external_id}",
                platform=self.platform,
                code="NOT_FOUND",
            )
        eid = self._next_id("adg")
        self.entities[eid] = SandboxEntity(
            external_id=eid,
            level="ad_group",
            name=spec.name,
            parent_id=spec.campaign_external_id,
            active=spec.status.upper() == "ACTIVE",
            daily_budget_micros=spec.daily_budget_micros,
            bid_micros=spec.bid_micros,
            spec=spec.__dict__.copy(),
        )
        self.calls.append(("create_ad_group", {"id": eid, "name": spec.name}))
        return eid

    def create_creative(self, spec: CreativeSpec) -> str:
        self._guard("create_creative")
        if spec.ad_group_external_id not in self.entities:
            raise PlatformError(
                f"unknown ad group {spec.ad_group_external_id}",
                platform=self.platform,
                code="NOT_FOUND",
            )
        if not spec.headlines:
            raise PlatformError(
                "at least one headline is required",
                platform=self.platform,
                code="MISSING_ASSET",
            )
        if not spec.final_url:
            raise PlatformError(
                "final_url is required", platform=self.platform, code="MISSING_URL"
            )
        ctr, cvr = self._latent_quality(spec)
        eid = self._next_id("ad")
        self.entities[eid] = SandboxEntity(
            external_id=eid,
            level="creative",
            name=spec.name,
            parent_id=spec.ad_group_external_id,
            active=spec.status.upper() == "ACTIVE",
            spec=spec.__dict__.copy(),
            true_ctr=ctr,
            true_cvr=cvr,
        )
        self.calls.append(("create_creative", {"id": eid, "ctr": ctr, "cvr": cvr}))
        return eid

    # -- mutation --------------------------------------------------------
    def _require(self, external_id: str) -> SandboxEntity:
        entity = self.entities.get(external_id)
        if entity is None:
            raise PlatformError(
                f"unknown entity {external_id}",
                platform=self.platform,
                code="NOT_FOUND",
            )
        return entity

    def set_status(self, level: str, external_id: str, active: bool) -> None:
        self._guard("set_status")
        self._require(external_id).active = active
        self.calls.append(("set_status", {"id": external_id, "active": active}))

    def set_budget(self, level: str, external_id: str, daily_budget_micros: int) -> None:
        self._guard("set_budget")
        if daily_budget_micros <= 0:
            raise PlatformError(
                "daily budget must be positive",
                platform=self.platform,
                code="INVALID_BUDGET",
            )
        self._require(external_id).daily_budget_micros = daily_budget_micros
        self.calls.append(
            ("set_budget", {"id": external_id, "budget": daily_budget_micros})
        )

    def set_bid(self, level: str, external_id: str, bid_micros: int) -> None:
        self._guard("set_bid")
        self._require(external_id).bid_micros = bid_micros
        self.calls.append(("set_bid", {"id": external_id, "bid": bid_micros}))

    def upload_conversions(self, conversions: list[dict]) -> int:
        self._guard("upload_conversions")
        self.uploaded_conversions.extend(conversions)
        return len(conversions)

    # -- simulation ------------------------------------------------------
    def simulate_day(self, day: date) -> list[InsightRow]:
        """Run one day of auction for every active creative."""
        rows: list[InsightRow] = []
        for ad in [e for e in self.entities.values() if e.level == "creative"]:
            group = self.entities.get(ad.parent_id or "")
            if group is None:
                continue
            campaign = self.entities.get(group.parent_id or "")
            if not (ad.active and group.active and (campaign is None or campaign.active)):
                # Still record a zero row so "no delivery" is distinguishable
                # from "never ran".
                continue
            if ad.created_on is None:
                ad.created_on = day

            budget = group.daily_budget_micros or (
                campaign.daily_budget_micros if campaign else 0
            )
            siblings = [
                e
                for e in self.entities.values()
                if e.level == "creative" and e.parent_id == group.external_id and e.active
            ]
            share = 1.0 / max(1, len(siblings))
            rows.append(self._simulate_creative(ad, day, int(budget * share)))
        for row in rows:
            self.insights[(row.external_id, row.day)] = row
        return rows

    def simulate_range(self, start: date, days: int) -> list[InsightRow]:
        out: list[InsightRow] = []
        for offset in range(days):
            out.extend(self.simulate_day(start + timedelta(days=offset)))
        return out

    def _simulate_creative(
        self, ad: SandboxEntity, day: date, budget_micros: int
    ) -> InsightRow:
        rng = self._rng_for(f"day:{ad.external_id}:{day.isoformat()}")

        quality = ad.true_ctr / (0.045 if self.platform is Platform.GOOGLE else 0.012)
        cpm = self.market.base_cpm_micros
        cpm *= 1.0 - self.market.quality_cpm_discount * max(-0.5, min(1.0, quality - 1.0))
        cpm *= rng.lognormvariate(0.0, self.market.cpm_volatility)
        if day.weekday() >= 5:
            cpm *= 1.0 - self.market.weekend_lift
        cpm = max(1_000_000, cpm)

        if budget_micros <= 0:
            return InsightRow(external_id=ad.external_id, day=day)

        available = min(
            self.market.daily_impression_ceiling,
            int(budget_micros / cpm * 1000),
        )
        # Delivery rarely spends the full budget on day one.
        days_live = (day - (ad.created_on or day)).days
        ramp = min(1.0, 0.55 + 0.15 * days_live)
        impressions = max(0, int(available * ramp * rng.uniform(0.85, 1.05)))
        if impressions <= 0:
            return InsightRow(external_id=ad.external_id, day=day)

        fatigue = math.exp(-self.market.fatigue_per_day * max(0, days_live))
        ctr = max(0.0005, ad.true_ctr * fatigue)
        clicks = _binomial(rng, impressions, ctr)

        spend = int(impressions / 1000 * cpm)
        spend = min(spend, budget_micros)

        conversions = float(_binomial(rng, clicks, ad.true_cvr))

        return InsightRow(
            external_id=ad.external_id,
            day=day,
            impressions=impressions,
            clicks=clicks,
            spend_micros=spend,
            conversions=conversions,
            conversion_value_micros=0,
            frequency=round(1.0 + days_live * 0.09, 2),
            reach=int(impressions / max(1.0, 1.0 + days_live * 0.09)),
            video_views=int(impressions * 0.22),
            raw={"cpm_micros": int(cpm), "true_ctr": ad.true_ctr, "true_cvr": ad.true_cvr},
        )

    # -- measurement -----------------------------------------------------
    def fetch_insights(
        self, level: str, since: date, until: date, external_ids: list[str] | None = None
    ) -> list[InsightRow]:
        self._guard("fetch_insights")
        wanted = set(external_ids) if external_ids else None
        out = [
            row
            for (eid, day), row in self.insights.items()
            if since <= day <= until and (wanted is None or eid in wanted)
        ]
        return sorted(out, key=lambda r: (r.day, r.external_id))

    def health_check(self) -> dict:
        return {
            "platform": self.platform.value,
            "ok": True,
            "mode": "sandbox",
            "entities": len(self.entities),
        }


def _binomial(rng: random.Random, n: int, p: float) -> int:
    """Binomial draw. Exact for small n, normal approximation for large n."""
    if n <= 0 or p <= 0:
        return 0
    p = min(1.0, p)
    if n <= 1000:
        return sum(1 for _ in range(n) if rng.random() < p)
    mean = n * p
    sd = math.sqrt(n * p * (1 - p))
    return max(0, min(n, int(round(rng.gauss(mean, sd)))))
