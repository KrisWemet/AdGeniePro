"""Campaign launch, inspection and manual control."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.launcher import CampaignLauncher, LaunchPlan
from ..core.metrics import load_performance
from ..core.orchestrator import Orchestrator
from ..db import get_session
from ..models import (
    AdGroup,
    Campaign,
    Creative,
    EntityLevel,
    EntityStatus,
    Offer,
    Platform,
)
from ..money import micros_to_usd
from ..schemas import CampaignOut, CreativeOut, LaunchIn, PerformanceOut

router = APIRouter(tags=["campaigns"])


def _campaign_out(campaign: Campaign) -> CampaignOut:
    return CampaignOut(
        id=campaign.id,
        offer_id=campaign.offer_id,
        platform=campaign.platform,
        name=campaign.name,
        external_id=campaign.external_id,
        objective=campaign.objective,
        daily_budget_usd=micros_to_usd(campaign.daily_budget_micros),
        status=campaign.status.value,
        created_at=campaign.created_at,
    )


def _creative_out(creative: Creative) -> CreativeOut:
    return CreativeOut(
        id=creative.id,
        ad_group_id=creative.ad_group_id,
        name=creative.name,
        angle=creative.angle,
        external_id=creative.external_id,
        headlines=creative.headlines,
        descriptions=creative.descriptions,
        primary_texts=creative.primary_texts,
        call_to_action=creative.call_to_action,
        final_url=creative.final_url,
        status=creative.status.value,
        compliance_verdict=creative.compliance_verdict.value,
        generation=creative.generation,
        generator=creative.generator,
    )


@router.post("/campaigns/launch")
def launch_campaign(payload: LaunchIn, session: Session = Depends(get_session)) -> dict:
    """Generate copy, review it against policy, and build the campaign.

    Campaigns start paused by default. Turning on spend is a separate,
    deliberate call.
    """
    settings = get_settings()
    if session.get(Offer, payload.offer_id) is None:
        raise HTTPException(404, f"offer {payload.offer_id} not found")
    if payload.daily_budget_usd > settings.global_daily_budget_cap_usd:
        raise HTTPException(
            422,
            f"daily budget {payload.daily_budget_usd} exceeds the configured "
            f"global cap of {settings.global_daily_budget_cap_usd}",
        )

    plan = LaunchPlan(**payload.model_dump())
    result = CampaignLauncher(session, settings=settings).launch(plan)
    return result.as_dict()


@router.get("/campaigns", response_model=list[CampaignOut])
def list_campaigns(
    session: Session = Depends(get_session),
    platform: Platform | None = None,
    status: EntityStatus | None = None,
) -> list[CampaignOut]:
    query = select(Campaign).order_by(Campaign.created_at.desc())
    if platform:
        query = query.where(Campaign.platform == platform)
    if status:
        query = query.where(Campaign.status == status)
    return [_campaign_out(c) for c in session.execute(query).scalars()]


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, session: Session = Depends(get_session)) -> dict:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    groups = list(
        session.execute(
            select(AdGroup).where(AdGroup.campaign_id == campaign_id)
        ).scalars()
    )
    return {
        "campaign": _campaign_out(campaign).model_dump(),
        "ad_groups": [
            {
                "id": g.id,
                "name": g.name,
                "external_id": g.external_id,
                "status": g.status.value,
                "daily_budget_usd": micros_to_usd(g.daily_budget_micros),
                "keywords": g.keywords,
                "creatives": [
                    _creative_out(c).model_dump()
                    for c in session.execute(
                        select(Creative).where(Creative.ad_group_id == g.id)
                    ).scalars()
                ],
            }
            for g in groups
        ],
    }


@router.post("/campaigns/{campaign_id}/status")
def set_campaign_status(
    campaign_id: int,
    active: bool = Query(...),
    session: Session = Depends(get_session),
) -> dict:
    """Turn a campaign on or off, including on the ad platform itself."""
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    orchestrator = Orchestrator(session, settings=get_settings())
    if campaign.external_id:
        orchestrator.client(campaign.platform).set_status(
            "campaign", campaign.external_id, active
        )
    campaign.status = EntityStatus.ACTIVE if active else EntityStatus.PAUSED

    # Children follow the parent so the account state matches what is stored.
    for group in session.execute(
        select(AdGroup).where(AdGroup.campaign_id == campaign_id)
    ).scalars():
        if group.status in (EntityStatus.ACTIVE, EntityStatus.PAUSED):
            group.status = EntityStatus.ACTIVE if active else EntityStatus.PAUSED
            if group.external_id:
                orchestrator.client(campaign.platform).set_status(
                    "ad_group", group.external_id, active
                )
        for creative in session.execute(
            select(Creative).where(Creative.ad_group_id == group.id)
        ).scalars():
            if creative.status in (EntityStatus.ACTIVE, EntityStatus.PAUSED):
                creative.status = EntityStatus.ACTIVE if active else EntityStatus.PAUSED
                if creative.external_id:
                    orchestrator.client(campaign.platform).set_status(
                        "creative", creative.external_id, active
                    )
    session.commit()
    return {"campaign_id": campaign_id, "status": campaign.status.value}


@router.get("/creatives/{creative_id}")
def get_creative(creative_id: int, session: Session = Depends(get_session)) -> dict:
    creative = session.get(Creative, creative_id)
    if creative is None:
        raise HTTPException(404, f"creative {creative_id} not found")
    return {
        **_creative_out(creative).model_dump(),
        "compliance_report": creative.compliance_report,
        "generator_meta": creative.generator_meta,
        "parent_id": creative.parent_id,
    }


@router.get("/performance", response_model=list[PerformanceOut])
def performance(
    session: Session = Depends(get_session),
    level: EntityLevel = EntityLevel.CREATIVE,
    days: int = Query(default=7, ge=1, le=180),
    campaign_id: int | None = None,
) -> list[PerformanceOut]:
    """Performance with credible intervals, not just point estimates."""
    until = date.today() - timedelta(days=1)
    since = until - timedelta(days=days - 1)

    names: dict[int, str] = {}
    if level is EntityLevel.CREATIVE:
        query = select(Creative)
        if campaign_id:
            query = query.join(AdGroup, Creative.ad_group_id == AdGroup.id).where(
                AdGroup.campaign_id == campaign_id
            )
        entities = list(session.execute(query).scalars())
        names = {c.id: f"{c.angle} #{c.id}" for c in entities}
    elif level is EntityLevel.AD_GROUP:
        query = select(AdGroup)
        if campaign_id:
            query = query.where(AdGroup.campaign_id == campaign_id)
        entities = list(session.execute(query).scalars())
        names = {g.id: g.name for g in entities}
    else:
        query = select(Campaign)
        if campaign_id:
            query = query.where(Campaign.id == campaign_id)
        entities = list(session.execute(query).scalars())
        names = {c.id: c.name for c in entities}

    out: list[PerformanceOut] = []
    for entity in entities:
        window = load_performance(session, level, entity.id, since, until)
        if not (window.impressions or window.spend_micros or window.clicks):
            continue
        data = window.as_dict()
        out.append(
            PerformanceOut(
                level=data["level"],
                entity_id=data["entity_id"],
                name=names.get(entity.id, ""),
                since=data["since"],
                until=data["until"],
                impressions=data["impressions"],
                clicks=data["clicks"],
                spend_usd=data["spend_usd"],
                conversions=data["conversions"],
                revenue_usd=data["revenue_usd"],
                profit_usd=data["profit_usd"],
                ctr=data["ctr"],
                cpc_usd=data["cpc_usd"],
                cvr=data["cvr"],
                cpa_usd=data["cpa_usd"],
                epc_usd=data["epc_usd"],
                roas=data["roas"],
                roas_lower=data["roas_lower"],
                roas_upper=data["roas_upper"],
                prob_profitable=data["prob_profitable"],
                attribution_gap=data["attribution_gap"],
            )
        )
    return sorted(out, key=lambda r: r.spend_usd, reverse=True)
