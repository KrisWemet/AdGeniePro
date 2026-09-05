"""Checking destinations, and noticing when they change.

A landing page audit run once at launch answers the wrong question. The page
was compliant *then*. Affiliate networks rotate creatives, advertisers edit
copy, and a page that passed in March can be collecting card details over HTTP
by May. So every audit is stored with a content hash, and a later audit that
finds different content says so.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AdGroup,
    Campaign,
    ComplianceVerdict,
    Creative,
    EntityStatus,
    LandingPageCheck,
    Offer,
    Platform,
)
from .landing import LandingPageAudit, LandingPageFetcher, audit_landing_page

logger = logging.getLogger(__name__)

__all__ = ["DestinationMonitor"]


class DestinationMonitor:
    def __init__(
        self,
        session: Session,
        fetcher: LandingPageFetcher | None = None,
    ) -> None:
        self.session = session
        self.fetcher = fetcher or LandingPageFetcher()

    # ------------------------------------------------------------------
    def check_offer(
        self,
        offer: Offer,
        ad_texts: list[str] | None = None,
        check_cloaking: bool = True,
    ) -> LandingPageCheck:
        """Audit an offer's destination and record the result."""
        audit = audit_landing_page(
            offer.destination_url,
            fetcher=self.fetcher,
            ad_texts=ad_texts,
            offer=offer,
            check_cloaking=check_cloaking,
        )
        return self._persist(audit, offer_id=offer.id)

    def check_creative(self, creative: Creative) -> LandingPageCheck:
        """Audit where one creative actually sends people.

        Uses the creative's own copy, so a page that does not deliver what this
        particular ad promised is caught rather than averaged away.
        """
        group = self.session.get(AdGroup, creative.ad_group_id)
        campaign = self.session.get(Campaign, group.campaign_id) if group else None
        offer = self.session.get(Offer, campaign.offer_id) if campaign else None
        if offer is None:
            raise ValueError(f"creative {creative.id} has no reachable offer")

        audit = audit_landing_page(
            offer.destination_url,
            fetcher=self.fetcher,
            ad_texts=creative.headlines + creative.primary_texts,
            offer=offer,
        )
        return self._persist(audit, offer_id=offer.id, creative_id=creative.id)

    # ------------------------------------------------------------------
    def sweep(
        self, max_age_hours: int = 24, only_active: bool = True
    ) -> dict:
        """Re-check the destinations of everything currently running.

        Worth running on a schedule. The point is not the first audit but the
        second: a page that changed since it was approved is the case this
        catches and a launch-time check never will.
        """
        query = select(Offer).where(Offer.status == EntityStatus.ACTIVE)
        offers = list(self.session.execute(query).scalars())
        if only_active:
            live = {
                campaign.offer_id
                for campaign in self.session.execute(
                    select(Campaign).where(Campaign.status == EntityStatus.ACTIVE)
                ).scalars()
            }
            offers = [o for o in offers if o.id in live]

        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            hours=max_age_hours
        )
        summary = {"checked": 0, "changed": [], "blocking": [], "skipped": 0}

        for offer in offers:
            last = self._latest(offer.id)
            if last is not None and last.checked_at > cutoff:
                summary["skipped"] += 1
                continue

            check = self.check_offer(offer)
            summary["checked"] += 1
            if check.content_changed:
                summary["changed"].append(
                    {"offer_id": offer.id, "name": offer.name, "url": check.url}
                )
            if check.verdict is ComplianceVerdict.BLOCK:
                summary["blocking"].append(
                    {
                        "offer_id": offer.id,
                        "name": offer.name,
                        "findings": [
                            f["code"] for f in check.report.get("findings", [])
                            if f["severity"] == "block"
                        ],
                    }
                )
        self.session.commit()

        if summary["blocking"]:
            logger.warning(
                "%s live offer(s) now have a blocking landing page problem: %s",
                len(summary["blocking"]),
                ", ".join(str(b["offer_id"]) for b in summary["blocking"]),
            )
        return summary

    def pause_offenders(self, summary: dict, orchestrator=None) -> list[int]:
        """Stop spending on offers whose destination now fails.

        Deliberately separate from `sweep`: noticing and acting are different
        decisions, and an operator may want the first without the second.
        """
        paused: list[int] = []
        for entry in summary.get("blocking", []):
            campaigns = list(
                self.session.execute(
                    select(Campaign).where(
                        Campaign.offer_id == entry["offer_id"],
                        Campaign.status == EntityStatus.ACTIVE,
                    )
                ).scalars()
            )
            for campaign in campaigns:
                if orchestrator is not None and campaign.external_id:
                    try:
                        orchestrator.client(campaign.platform).set_status(
                            "campaign", campaign.external_id, False
                        )
                    except Exception as exc:
                        logger.error(
                            "Could not pause campaign %s on the platform: %s",
                            campaign.id,
                            exc,
                        )
                        continue
                campaign.status = EntityStatus.PAUSED
                campaign.last_error = (
                    "Paused: the landing page now has a blocking policy problem."
                )
                paused.append(campaign.id)
        self.session.commit()
        return paused

    # ------------------------------------------------------------------
    def _latest(self, offer_id: int) -> LandingPageCheck | None:
        return self.session.execute(
            select(LandingPageCheck)
            .where(LandingPageCheck.offer_id == offer_id)
            .order_by(LandingPageCheck.checked_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _persist(
        self, audit: LandingPageAudit, offer_id: int, creative_id: int | None = None
    ) -> LandingPageCheck:
        previous = self._latest(offer_id)
        changed = bool(
            previous
            and previous.content_hash
            and audit.content_hash
            and previous.content_hash != audit.content_hash
        )
        if changed:
            logger.warning(
                "The landing page for offer %s has changed since %s. Re-read it: a "
                "page that was compliant when approved may not be now.",
                offer_id,
                previous.checked_at.date(),
            )

        check = LandingPageCheck(
            offer_id=offer_id,
            creative_id=creative_id,
            url=audit.url,
            final_url=audit.snapshot.final_url if audit.snapshot else None,
            status_code=audit.snapshot.status_code if audit.snapshot else 0,
            redirect_hops=len(audit.snapshot.redirect_chain) if audit.snapshot else 0,
            verdict=audit.verdict,
            score=audit.score,
            content_hash=audit.content_hash,
            content_changed=changed,
            report=audit.as_dict(),
        )
        self.session.add(check)
        self.session.flush()
        return check
