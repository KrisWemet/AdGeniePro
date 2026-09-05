"""Turn an offer into a running, structured test.

The structure this builds is opinionated, because how a test is structured
determines whether it can be read. One ad group per angle, several creatives
inside it, a shared tracking link per creative. That way when the numbers come
back you can tell which *argument* worked, not just which ad did.

Nothing reaches an ad account without passing compliance first. A creative with
a blocking policy finding is persisted with its report and left in
`pending_review` rather than launched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import (
    AdGroup,
    AuditLog,
    Campaign,
    ComplianceVerdict,
    Creative,
    EntityStatus,
    Offer,
    Platform,
)
from ..money import micros_to_usd, usd_to_micros
from ..platforms.base import (
    AdGroupSpec,
    AdPlatform,
    CampaignSpec,
    CreativeSpec,
    PlatformError,
)
from ..platforms.factory import get_platform
from ..platforms.specs import DEFAULT_FORMAT
from .angles import angles_for
from .copywriter import CopyStudio, build_brief
from .tracking import TrackingContext, build_tracking_url

logger = logging.getLogger(__name__)

__all__ = ["LaunchPlan", "LaunchResult", "CampaignLauncher"]


@dataclass
class LaunchPlan:
    offer_id: int
    platform: Platform
    daily_budget_usd: float
    name: str | None = None
    objective: str | None = None
    # One ad group per angle. Fewer, larger ad groups learn faster; more,
    # smaller ones isolate the variable better. Three is the usual compromise.
    angles: list[str] = field(default_factory=list)
    angle_count: int = 3
    creatives_per_angle: int = 1
    keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    targeting: dict = field(default_factory=dict)
    geo_targets: list[str] = field(default_factory=list)
    start_paused: bool = True
    max_daily_budget_usd: float | None = None
    ad_format: str | None = None
    # Scan the Ad Library for what is still running in this market and feed the
    # patterns to the copywriter. Off by default: it costs an API round trip
    # and returns nothing outside the EU and UK.
    research_market: bool = False
    research_term: str = ""
    # Audit the destination before sending paid traffic to it. None defers to
    # the deployment's `audit_landing_pages` setting, which defaults to on.
    check_landing_page: bool | None = None
    # Launch anyway when the page fails. Off by default, and saying yes means
    # knowingly pointing paid traffic at a page the platforms will object to.
    ignore_landing_page_findings: bool = False
    # Generate imagery for each creative. A Meta ad without an image is not an
    # ad; Google search ads carry no imagery and skip this.
    generate_media: bool = False
    media_kind: str = "image"
    media_placements: list[str] = field(default_factory=list)


@dataclass
class LaunchResult:
    campaign_id: int
    campaign_external_id: str | None
    ad_group_ids: list[int] = field(default_factory=list)
    creative_ids: list[int] = field(default_factory=list)
    blocked_creative_ids: list[int] = field(default_factory=list)
    media_asset_ids: list[int] = field(default_factory=list)
    market_brief: dict | None = None
    landing_page: dict | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = True

    def as_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "campaign_external_id": self.campaign_external_id,
            "ad_group_ids": self.ad_group_ids,
            "creative_ids": self.creative_ids,
            "blocked_creative_ids": self.blocked_creative_ids,
            "launched": len(self.creative_ids),
            "blocked": len(self.blocked_creative_ids),
            "media_asset_ids": self.media_asset_ids,
            "market_brief": self.market_brief,
            "landing_page": self.landing_page,
            "warnings": self.warnings,
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


DEFAULT_OBJECTIVE = {
    Platform.META: "OUTCOME_SALES",
    Platform.GOOGLE: "SEARCH",
}


class CampaignLauncher:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        studio: CopyStudio | None = None,
        platform_client: AdPlatform | None = None,
        media_studio=None,
        researcher=None,
        destination_monitor=None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.studio = studio or CopyStudio(settings=self.settings)
        self._platform_client = platform_client
        self._media_studio = media_studio
        self._researcher = researcher
        self._monitor = destination_monitor
        self._landing_report: dict | None = None

    def _media(self):
        if self._media_studio is None:
            from ..media.studio import MediaStudio

            self._media_studio = MediaStudio(self.session, self.settings)
        return self._media_studio

    def _client(self, platform: Platform) -> AdPlatform:
        return self._platform_client or get_platform(platform, self.settings)

    # ------------------------------------------------------------------
    def launch(self, plan: LaunchPlan) -> LaunchResult:
        offer = self.session.get(Offer, plan.offer_id)
        if offer is None:
            raise ValueError(f"offer {plan.offer_id} not found")

        client = self._client(plan.platform)
        ad_format = plan.ad_format or DEFAULT_FORMAT[plan.platform]
        budget_micros = usd_to_micros(plan.daily_budget_usd)
        status = "PAUSED" if plan.start_paused else "ACTIVE"

        self._media_asset_ids: list[int] = []
        should_check = (
            self.settings.audit_landing_pages
            if plan.check_landing_page is None
            else plan.check_landing_page
        )
        if should_check:
            blocked = self._check_destination(offer, plan)
            if blocked is not None:
                return blocked

        market_notes: list[str] = []
        market_brief: dict | None = None
        if plan.research_market:
            market_notes, market_brief = self._research(offer, plan)

        campaign = self._create_campaign(offer, plan, client, budget_micros, status)
        result = LaunchResult(
            campaign_id=campaign.id,
            campaign_external_id=campaign.external_id,
            dry_run=self.settings.dry_run,
        )
        result.market_brief = market_brief
        if campaign.status is EntityStatus.FAILED:
            result.errors.append(campaign.last_error or "campaign creation failed")
            return result

        angle_keys = plan.angles or [
            a.key for a in angles_for(plan.platform.value, plan.angle_count)
        ]
        # Split the campaign budget evenly so no angle is starved before it can
        # be measured.
        per_group_micros = max(1_000_000, budget_micros // max(1, len(angle_keys)))

        for angle_key in angle_keys:
            group = self._create_ad_group(
                campaign, plan, client, angle_key, per_group_micros, status
            )
            if group.status is EntityStatus.FAILED:
                result.errors.append(f"{angle_key}: {group.last_error}")
                continue
            result.ad_group_ids.append(group.id)

            for index in range(plan.creatives_per_angle):
                creative = self._create_creative(
                    offer, campaign, group, plan, client, angle_key, ad_format,
                    index, status, market_notes,
                )
                if creative.compliance_verdict is ComplianceVerdict.BLOCK:
                    result.blocked_creative_ids.append(creative.id)
                    result.warnings.append(
                        f"{creative.name}: blocked by policy review "
                        f"({len(creative.compliance_report.get('findings', []))} findings)"
                    )
                elif creative.status is EntityStatus.FAILED:
                    result.errors.append(f"{creative.name}: {creative.last_error}")
                else:
                    result.creative_ids.append(creative.id)
                    if creative.compliance_verdict is ComplianceVerdict.WARN:
                        result.warnings.append(
                            f"{creative.name}: launched with policy warnings"
                        )

        result.media_asset_ids = self._media_asset_ids
        result.landing_page = self._landing_report
        self.session.commit()
        logger.info(
            "Launched campaign %s (%s): %s creatives live, %s blocked",
            campaign.id,
            campaign.name,
            len(result.creative_ids),
            len(result.blocked_creative_ids),
        )
        return result

    # ------------------------------------------------------------------
    def _create_campaign(
        self,
        offer: Offer,
        plan: LaunchPlan,
        client: AdPlatform,
        budget_micros: int,
        status: str,
    ) -> Campaign:
        name = plan.name or f"{offer.name} | {plan.platform.value} | auto"
        objective = plan.objective or DEFAULT_OBJECTIVE[plan.platform]
        max_budget = (
            usd_to_micros(plan.max_daily_budget_usd)
            if plan.max_daily_budget_usd
            else budget_micros * 4
        )

        campaign = Campaign(
            offer_id=offer.id,
            platform=plan.platform,
            name=name,
            objective=objective,
            daily_budget_micros=budget_micros,
            max_daily_budget_micros=max_budget,
            target_roas=self.settings.target_roas,
            status=EntityStatus.DRAFT,
            settings={
                "geo_targets": plan.geo_targets or offer.geo_targets,
                "angles": plan.angles,
            },
        )
        self.session.add(campaign)
        self.session.flush()

        spec = CampaignSpec(
            name=name,
            objective=objective,
            daily_budget_micros=budget_micros,
            status=status,
            extra={
                "channel": "search" if plan.platform is Platform.GOOGLE else "feed",
                "campaign_budget_optimization": plan.platform is Platform.GOOGLE,
                "special_ad_categories": [],
            },
        )
        try:
            campaign.external_id = client.create_campaign(spec)
            campaign.status = (
                EntityStatus.PAUSED if plan.start_paused else EntityStatus.ACTIVE
            )
        except PlatformError as exc:
            campaign.status = EntityStatus.FAILED
            campaign.last_error = str(exc)
            logger.error("Campaign creation failed: %s", exc)
        self._audit(
            plan.platform, "create_campaign", campaign.name, spec.__dict__, campaign
        )
        self.session.flush()
        return campaign

    def _create_ad_group(
        self,
        campaign: Campaign,
        plan: LaunchPlan,
        client: AdPlatform,
        angle_key: str,
        budget_micros: int,
        status: str,
    ) -> AdGroup:
        name = f"{campaign.name} | {angle_key}"
        targeting = dict(plan.targeting)
        if plan.geo_targets:
            targeting.setdefault("geo_locations", {"countries": plan.geo_targets})

        group = AdGroup(
            campaign_id=campaign.id,
            name=name,
            daily_budget_micros=budget_micros,
            max_daily_budget_micros=budget_micros * 4,
            targeting=targeting,
            keywords=plan.keywords,
            negative_keywords=plan.negative_keywords,
            status=EntityStatus.DRAFT,
        )
        self.session.add(group)
        self.session.flush()

        spec = AdGroupSpec(
            campaign_external_id=campaign.external_id or "",
            name=name,
            daily_budget_micros=budget_micros
            if campaign.platform is Platform.META
            else 0,
            status=status,
            targeting=targeting,
            keywords=plan.keywords,
            negative_keywords=plan.negative_keywords,
            extra={"match_type": "phrase"},
        )
        try:
            group.external_id = client.create_ad_group(spec)
            group.status = (
                EntityStatus.PAUSED if plan.start_paused else EntityStatus.ACTIVE
            )
        except PlatformError as exc:
            group.status = EntityStatus.FAILED
            group.last_error = str(exc)
            logger.error("Ad group creation failed: %s", exc)
        self._audit(campaign.platform, "create_ad_group", name, spec.__dict__, group)
        self.session.flush()
        return group

    def _create_creative(
        self,
        offer: Offer,
        campaign: Campaign,
        group: AdGroup,
        plan: LaunchPlan,
        client: AdPlatform,
        angle_key: str,
        ad_format: str,
        index: int,
        status: str,
        market_notes: list[str] | None = None,
    ) -> Creative:
        brief = build_brief(
            offer,
            platform=campaign.platform,
            ad_format=ad_format,
            angle_key=angle_key,
            keyword=(plan.keywords[0] if plan.keywords else ""),
            market_notes=market_notes,
        )
        draft = self.studio.write(brief, offer=offer)

        creative = Creative(
            ad_group_id=group.id,
            name=f"{group.name} | v{index + 1}",
            angle=draft.angle or angle_key,
            headlines=draft.headlines,
            descriptions=draft.descriptions,
            primary_texts=draft.primary_texts,
            call_to_action=draft.call_to_action,
            image_prompt=draft.image_prompt,
            generator=draft.generator,
            generator_meta=draft.generator_meta,
            compliance_verdict=(
                draft.compliance.verdict if draft.compliance else ComplianceVerdict.UNREVIEWED
            ),
            compliance_report=draft.compliance.as_dict() if draft.compliance else {},
            status=EntityStatus.DRAFT,
        )
        self.session.add(creative)
        self.session.flush()

        # The tracking link needs the creative's own id, so it is built after
        # the row exists and written back before the ad is created.
        creative.final_url = build_tracking_url(
            TrackingContext(
                offer_id=offer.id,
                campaign_id=campaign.id,
                ad_group_id=group.id,
                creative_id=creative.id,
                platform=campaign.platform,
            ),
            settings=self.settings,
        )

        if creative.compliance_verdict is ComplianceVerdict.BLOCK:
            creative.status = EntityStatus.PENDING_REVIEW
            creative.last_error = "Blocked by policy review; not sent to the platform."
            logger.warning(
                "Creative %s blocked: %s",
                creative.name,
                "; ".join(
                    f["code"] for f in creative.compliance_report.get("findings", [])[:4]
                ),
            )
            self.session.flush()
            return creative

        if plan.generate_media:
            self._media_asset_ids.extend(
                self._attach_media(creative, plan, campaign.platform, ad_format)
            )

        spec = CreativeSpec(
            ad_group_external_id=group.external_id or "",
            name=creative.name,
            final_url=creative.final_url,
            headlines=creative.headlines,
            descriptions=creative.descriptions,
            primary_texts=creative.primary_texts,
            call_to_action=creative.call_to_action,
            display_url_path=creative.display_url_path,
            media_urls=creative.media_urls,
            status=status,
        )
        try:
            creative.external_id = client.create_creative(spec)
            creative.status = (
                EntityStatus.PAUSED if plan.start_paused else EntityStatus.ACTIVE
            )
        except PlatformError as exc:
            creative.status = EntityStatus.FAILED
            creative.last_error = str(exc)
            logger.error("Creative creation failed: %s", exc)
        self._audit(
            campaign.platform, "create_creative", creative.name, spec.__dict__, creative
        )
        self.session.flush()
        return creative

    # ------------------------------------------------------------------
    def _check_destination(self, offer: Offer, plan: LaunchPlan) -> LaunchResult | None:
        """Audit the page before paying to send anyone to it.

        Returns a failed result when the page should stop the launch, so the
        campaign is never created. Building the ads first and then discovering
        the destination is broken leaves paused wreckage in the ad account.
        """
        from .destination import DestinationMonitor

        try:
            monitor = self._monitor or DestinationMonitor(self.session)
            check = monitor.check_offer(offer)
        except Exception as exc:
            # A page that cannot be audited is not a page that should stop a
            # launch on its own; report it and continue.
            logger.warning("Landing page audit failed, launching without it: %s", exc)
            return None

        self.session.commit()
        report = check.report
        if check.verdict is not ComplianceVerdict.BLOCK:
            self._landing_report = report
            return None

        codes = [
            f["code"] for f in report.get("findings", []) if f["severity"] == "block"
        ]
        if plan.ignore_landing_page_findings:
            logger.warning(
                "Launching despite blocking landing page findings: %s",
                ", ".join(codes),
            )
            self._landing_report = report
            return None

        logger.error(
            "Refusing to launch: %s has blocking landing page findings (%s)",
            offer.destination_url,
            ", ".join(codes),
        )
        return LaunchResult(
            campaign_id=0,
            campaign_external_id=None,
            landing_page=report,
            errors=[
                "The destination page has blocking policy findings, so no campaign "
                "was created: "
                + "; ".join(
                    f["message"]
                    for f in report.get("findings", [])
                    if f["severity"] == "block"
                )
            ],
            dry_run=self.settings.dry_run,
        )

    def _research(self, offer: Offer, plan: LaunchPlan) -> tuple[list[str], dict | None]:
        """Scan the Ad Library for what is still running in this market."""
        try:
            researcher = self._researcher
            if researcher is None:
                from ..research.service import MarketResearcher

                researcher = MarketResearcher(self.session, self.settings)
            brief = researcher.research_offer(
                offer, search_term=plan.research_term or None
            )
        except Exception as exc:
            # Research is an optional prior. Losing it should never stop a
            # launch, so the failure is reported and the launch continues.
            logger.warning("Market research failed, launching without it: %s", exc)
            return [], {"error": str(exc)}
        return brief.to_prompt_notes(), brief.as_dict()

    def _attach_media(
        self,
        creative: Creative,
        plan: LaunchPlan,
        platform: Platform,
        ad_format: str,
    ) -> list[int]:
        try:
            assets = self._media().generate_for_creative(
                creative,
                placements=plan.media_placements or None,
                kind=plan.media_kind,
                platform=platform,
                ad_format=ad_format,
            )
        except Exception as exc:
            logger.error("Media generation failed for %s: %s", creative.name, exc)
            creative.generator_meta = {
                **creative.generator_meta,
                "media_error": str(exc),
            }
            return []
        ids = [a.id for a in assets]
        creative.generator_meta = {**creative.generator_meta, "media_asset_ids": ids}
        return ids

    def _audit(
        self, platform: Platform, operation: str, target: str, request: dict, entity
    ) -> None:
        self.session.add(
            AuditLog(
                actor="launcher",
                platform=platform,
                operation=operation,
                target=target,
                request={k: str(v)[:500] for k, v in request.items()},
                response={
                    "external_id": getattr(entity, "external_id", None),
                    "status": getattr(entity, "status").value,
                },
                ok=getattr(entity, "status") is not EntityStatus.FAILED,
                dry_run=self.settings.dry_run,
            )
        )
