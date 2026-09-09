"""Competitor research, persisted.

Storing what was observed matters more than it first appears. A single scan is
a snapshot; scans over time show which ads *stopped* running, which is the only
negative signal the Ad Library offers. An ad that vanished after three weeks
was probably not working, and knowing that is as useful as knowing which ones
survived.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import CompetitorAd, Offer
from .ad_library import (
    EU_UK_COUNTRIES,
    AdLibraryAd,
    AdLibraryClient,
    CoverageWarning,
)
from .signals import MarketBrief, build_market_brief, classify_angle, count_variants, score_staying_power

logger = logging.getLogger(__name__)

__all__ = ["MarketResearcher"]


class MarketResearcher:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        client: AdLibraryClient | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._client = client

    def client(self) -> AdLibraryClient:
        if self._client is None:
            self._client = AdLibraryClient(self.settings)
        return self._client

    # ------------------------------------------------------------------
    def research(
        self,
        search_term: str,
        *,
        countries: list[str] | None = None,
        vertical: str = "",
        active_only: bool = True,
        max_pages: int = 3,
        persist: bool = True,
    ) -> MarketBrief:
        """Scan the archive for one search term and summarise what is running."""
        ads, warnings = self.client().search(
            search_terms=search_term,
            countries=countries,
            active_only=active_only,
            max_pages=max_pages,
        )
        if persist:
            self._persist(ads, search_term=search_term, vertical=vertical)

        brief = build_market_brief(
            ads,
            search_term=search_term,
            proven_days=self.settings.ad_library_proven_days,
            warnings=warnings,
        )
        logger.info(
            "Market brief for %r: %s confidence, %s proven ads, angles %s",
            search_term,
            brief.confidence,
            brief.proven_ads,
            brief.dominant_angles,
        )
        return brief

    def research_offer(
        self,
        offer: Offer,
        search_term: str | None = None,
        countries: list[str] | None = None,
    ) -> MarketBrief:
        """Research the market an offer competes in.

        Searching the offer's own name would find that offer's own advertisers,
        which is a much narrower and less useful picture than the category it
        sits in.
        """
        term = search_term or offer.vertical or offer.name
        return self.research(
            term,
            countries=countries or self._research_countries(offer),
            vertical=offer.vertical or "",
        )

    def _research_countries(self, offer: Offer) -> list[str]:
        """Which markets to scan for an offer targeting given countries.

        The offer's own geos are the right answer only where the archive
        carries commercial ads. A US-targeted offer scanned against US would
        read the political and issue archive and hand those patterns to the
        copywriter as if they were commercial market intelligence, so the
        configured EU and UK markets are added to give it something real to
        learn from.
        """
        targets = [c.upper() for c in (offer.geo_targets or [])]
        covered = [c for c in targets if c in EU_UK_COUNTRIES]
        if covered:
            return covered
        fallback = self.settings.ad_library_country_codes
        if targets:
            logger.info(
                "Offer targets %s, where the archive carries no commercial ads. "
                "Scanning %s instead; the audience differs, so treat the result "
                "as directional.",
                ", ".join(targets),
                ", ".join(fallback),
            )
        return list(fallback)

    # ------------------------------------------------------------------
    def _persist(
        self, ads: list[AdLibraryAd], search_term: str, vertical: str
    ) -> list[CompetitorAd]:
        """Upsert observations, so repeated scans build a history."""
        if not ads:
            return []
        now = datetime.now(timezone.utc)
        variants = count_variants(ads)

        existing = {
            row.ad_archive_id: row
            for row in self.session.execute(
                select(CompetitorAd).where(
                    CompetitorAd.ad_archive_id.in_([a.ad_archive_id for a in ads])
                )
            ).scalars()
        }

        rows: list[CompetitorAd] = []
        seen: set[str] = set()
        for ad in ads:
            # Paging can return the same ad twice; inserting it twice would
            # violate the unique constraint and lose the whole scan.
            if ad.ad_archive_id in seen:
                continue
            seen.add(ad.ad_archive_id)
            variant_count = variants.get(ad.ad_archive_id, 1)
            row = existing.get(ad.ad_archive_id)
            if row is None:
                row = CompetitorAd(
                    ad_archive_id=ad.ad_archive_id,
                    first_seen_at=now,
                    search_term=search_term,
                    vertical=vertical,
                )
                self.session.add(row)
                existing[ad.ad_archive_id] = row

            row.page_id = ad.page_id
            row.page_name = ad.page_name
            row.countries = ad.countries
            row.publisher_platforms = ad.publisher_platforms
            row.languages = ad.languages
            row.bodies = ad.bodies
            row.titles = ad.titles
            row.descriptions = ad.descriptions
            row.captions = ad.captions
            row.snapshot_url = ad.snapshot_url
            row.started_at = _naive(ad.started_at)
            row.stopped_at = _naive(ad.stopped_at)
            row.is_active = ad.is_active
            row.days_running = ad.days_running(now)
            row.eu_total_reach = ad.eu_total_reach
            row.variant_count = variant_count
            row.angle = classify_angle(ad.all_text())
            row.staying_power = score_staying_power(
                ad, variant_count, self.settings.ad_library_proven_days, now
            )
            row.last_seen_at = now
            row.raw = ad.raw
            rows.append(row)

        self.session.flush()
        return rows

    # ------------------------------------------------------------------
    def stored_brief(self, vertical: str = "", search_term: str = "") -> MarketBrief:
        """Rebuild a brief from what was already scanned, with no API call."""
        query = select(CompetitorAd)
        if vertical:
            query = query.where(CompetitorAd.vertical == vertical)
        if search_term:
            query = query.where(CompetitorAd.search_term == search_term)
        rows = list(self.session.execute(query).scalars())

        ads = [
            AdLibraryAd(
                ad_archive_id=row.ad_archive_id,
                page_id=row.page_id,
                page_name=row.page_name,
                bodies=row.bodies or [],
                titles=row.titles or [],
                descriptions=row.descriptions or [],
                snapshot_url=row.snapshot_url,
                started_at=row.started_at,
                stopped_at=row.stopped_at,
                countries=row.countries or [],
                eu_total_reach=row.eu_total_reach,
            )
            for row in rows
        ]
        return build_market_brief(
            ads,
            search_term=search_term or vertical,
            proven_days=self.settings.ad_library_proven_days,
            warnings=[
                CoverageWarning(
                    "FROM_CACHE",
                    "Built from previously stored observations, not a fresh scan.",
                )
            ],
        )

    def sweep_for_retirements(
        self, vertical: str = "", countries: list[str] | None = None
    ) -> int:
        """Re-scan including inactive ads, so stopped ones can be observed.

        A scan restricted to live ads can never see an ad stop: the row simply
        stops being returned and keeps its stale `is_active`. This pass is what
        makes `retired_ads` mean anything, and it is worth running on a
        schedule rather than at launch time.
        """
        terms = {
            row.search_term
            for row in self.session.execute(
                select(CompetitorAd).where(
                    CompetitorAd.vertical == vertical if vertical else True
                )
            ).scalars()
            if row.search_term
        }
        updated = 0
        for term in terms:
            ads, _ = self.client().search(
                search_terms=term, countries=countries, active_only=False
            )
            updated += len(self._persist(ads, search_term=term, vertical=vertical))
        self.session.flush()
        return updated

    def retired_ads(self, vertical: str = "", max_days: int = 21) -> list[dict]:
        """Ads that stopped running quickly: the archive's only negative signal.

        An advertiser who pulled a creative inside three weeks was almost
        certainly not making money on it, which is worth knowing before writing
        something similar. Populated by `sweep_for_retirements`, since a
        live-only scan never observes a stop.
        """
        query = select(CompetitorAd).where(
            CompetitorAd.is_active.is_(False),
            CompetitorAd.days_running <= max_days,
        )
        if vertical:
            query = query.where(CompetitorAd.vertical == vertical)
        return [
            {
                "ad_archive_id": row.ad_archive_id,
                "page_name": row.page_name,
                "angle": row.angle,
                "days_running": row.days_running,
            }
            for row in self.session.execute(query).scalars()
        ]


def _naive(value: datetime | None) -> datetime | None:
    """Timestamps are stored naive but mean UTC, matching the rest of the schema."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
