"""Offer and creative-generation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.angles import ANGLES
from ..core.compliance import review_texts
from ..core.copywriter import CopyStudio, build_brief
from ..db import get_session
from ..models import Offer, PayoutType
from ..money import micros_to_usd, usd_to_micros
from ..schemas import (
    ComplianceOut,
    CopyOut,
    GenerateCopyIn,
    OfferIn,
    OfferOut,
    ReviewCopyIn,
)

router = APIRouter(tags=["offers"])


def _to_out(offer: Offer) -> OfferOut:
    return OfferOut(
        id=offer.id,
        name=offer.name,
        network=offer.network,
        vertical=offer.vertical,
        destination_url=offer.destination_url,
        payout_type=offer.payout_type,
        payout_usd=micros_to_usd(offer.payout_micros),
        expected_value_usd=micros_to_usd(offer.expected_value_micros()),
        is_regulated=offer.is_regulated,
        created_at=offer.created_at,
    )


@router.get("/offers", response_model=list[OfferOut])
def list_offers(
    session: Session = Depends(get_session),
    limit: int = Query(default=100, le=500),
) -> list[OfferOut]:
    offers = session.execute(
        select(Offer).order_by(Offer.created_at.desc()).limit(limit)
    ).scalars()
    return [_to_out(o) for o in offers]


@router.post("/offers", response_model=OfferOut, status_code=201)
def create_offer(payload: OfferIn, session: Session = Depends(get_session)) -> OfferOut:
    data = payload.model_dump()
    data["payout_micros"] = usd_to_micros(data.pop("payout_usd"))
    data["average_order_value_micros"] = usd_to_micros(
        data.pop("average_order_value_usd")
    )
    offer = Offer(**data)
    if offer.payout_type in (PayoutType.CPS, PayoutType.REVSHARE) and not (
        offer.payout_percent and offer.average_order_value_micros
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Revenue-share offers need both payout_percent and "
                "average_order_value_usd; without them expected value per "
                "conversion cannot be computed and the optimizer cannot judge ROAS."
            ),
        )
    session.add(offer)
    session.commit()
    return _to_out(offer)


@router.get("/offers/{offer_id}", response_model=OfferOut)
def get_offer(offer_id: int, session: Session = Depends(get_session)) -> OfferOut:
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise HTTPException(404, f"offer {offer_id} not found")
    return _to_out(offer)


@router.get("/angles")
def list_angles() -> list[dict]:
    return [
        {"key": a.key, "name": a.name, "thesis": a.thesis, "guidance": a.guidance}
        for a in ANGLES
    ]


@router.post("/copy/generate", response_model=list[CopyOut])
def generate_copy(
    payload: GenerateCopyIn, session: Session = Depends(get_session)
) -> list[CopyOut]:
    """Generate ad copy for an offer, already reviewed against ad policy."""
    offer = session.get(Offer, payload.offer_id)
    if offer is None:
        raise HTTPException(404, f"offer {payload.offer_id} not found")

    studio = CopyStudio(settings=get_settings())
    brief = build_brief(
        offer,
        platform=payload.platform,
        ad_format=payload.ad_format,
        angle_key=payload.angle,
        keyword=payload.keyword,
    )
    drafts = (
        [studio.write(brief, offer=offer)]
        if payload.angle
        else studio.write_variants(brief, count=payload.variants, offer=offer)
    )
    return [
        CopyOut(
            angle=d.angle,
            headlines=d.headlines,
            descriptions=d.descriptions,
            primary_texts=d.primary_texts,
            call_to_action=d.call_to_action,
            image_prompt=d.image_prompt,
            rationale=d.rationale,
            generator=d.generator,
            compliance=ComplianceOut(**d.compliance.as_dict()) if d.compliance else None,
        )
        for d in drafts
    ]


@router.post("/copy/review", response_model=ComplianceOut)
def review_copy(
    payload: ReviewCopyIn, session: Session = Depends(get_session)
) -> ComplianceOut:
    """Check ad text against Meta and Google policy before it goes live."""
    offer = session.get(Offer, payload.offer_id) if payload.offer_id else None
    report = review_texts(
        {
            "headlines": payload.headlines,
            "descriptions": payload.descriptions,
            "primary_texts": payload.primary_texts,
        },
        platform=payload.platform,
        ad_format=payload.ad_format,
        offer=offer,
    )
    return ComplianceOut(**report.as_dict())
