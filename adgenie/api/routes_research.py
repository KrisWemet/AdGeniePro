"""Competitor research and media generation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..media.prompts import build_image_prompt, build_video_prompt
from ..media.specs import MEDIA_SPECS, default_placements
from ..media.studio import MediaStudio
from ..models import Creative, MediaAsset, MediaStatus, Offer, Platform
from ..research.ad_library import EU_UK_COUNTRIES, commercial_ads_available
from ..research.service import MarketResearcher

router = APIRouter(tags=["research"])


@router.get("/research/coverage")
def coverage(countries: str = Query(default="US,GB")) -> dict:
    """What the Ad Library will actually return for these countries."""
    codes = [c.strip().upper() for c in countries.split(",") if c.strip()]
    has_commercial = commercial_ads_available(codes)
    return {
        "countries": codes,
        "commercial_ads_available": has_commercial,
        "eu_uk_countries_in_request": sorted(set(codes) & EU_UK_COUNTRIES),
        "explanation": (
            "Ordinary product ads are archived under the Digital Services Act, "
            "which covers the EU and UK. Elsewhere the archive carries political "
            "and issue ads only, plus the US special categories."
            if not has_commercial
            else "Commercial ads are archived for these markets."
        ),
        "performance_data": (
            "The archive reports no click-through rate, conversion or spend data "
            "for commercial ads. How long an ad has run and whether it is still "
            "live are the available signals."
        ),
    }


@router.post("/research/scan")
def scan_market(
    search_term: str = Query(..., min_length=2),
    countries: str | None = Query(default=None),
    vertical: str = Query(default=""),
    active_only: bool = Query(default=True),
    max_pages: int = Query(default=3, ge=1, le=20),
    session: Session = Depends(get_session),
) -> dict:
    """Scan the Ad Library and summarise what is still running."""
    settings = get_settings()
    if not settings.has_ad_library:
        raise HTTPException(
            503,
            "The Ad Library needs a Meta access token with ads_read "
            "(set META_ACCESS_TOKEN).",
        )
    codes = (
        [c.strip().upper() for c in countries.split(",") if c.strip()]
        if countries
        else None
    )
    brief = MarketResearcher(session, settings).research(
        search_term,
        countries=codes,
        vertical=vertical,
        active_only=active_only,
        max_pages=max_pages,
    )
    session.commit()
    return brief.as_dict()


@router.get("/research/brief")
def stored_brief(
    vertical: str = Query(default=""),
    search_term: str = Query(default=""),
    session: Session = Depends(get_session),
) -> dict:
    """Rebuild a brief from stored observations, without calling the API."""
    return MarketResearcher(session, get_settings()).stored_brief(
        vertical=vertical, search_term=search_term
    ).as_dict()


@router.get("/research/retired")
def retired_ads(
    vertical: str = Query(default=""),
    max_days: int = Query(default=21, ge=1, le=365),
    session: Session = Depends(get_session),
) -> dict:
    """Competitor ads that stopped quickly: the archive's only negative signal."""
    rows = MarketResearcher(session, get_settings()).retired_ads(vertical, max_days)
    return {
        "count": len(rows),
        "note": (
            "An advertiser who pulled a creative within a few weeks was probably "
            "not making money on it."
        ),
        "ads": rows,
    }


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------


@router.get("/media/placements")
def placements(platform: Platform | None = None) -> dict:
    specs = {
        name: {
            "aspect_ratio": spec.aspect_ratio,
            "width": spec.width,
            "height": spec.height,
            "kind": spec.kind,
            "notes": spec.notes,
        }
        for name, spec in MEDIA_SPECS.items()
    }
    result: dict = {"placements": specs}
    if platform:
        result["defaults"] = {
            "image": default_placements(platform, "image"),
            "video": default_placements(platform, "video"),
        }
        if platform is Platform.GOOGLE:
            result["note"] = (
                "Google search ads carry no imagery; these placements are for "
                "Demand Gen and Display."
            )
    return result


@router.post("/media/preview-prompt")
def preview_prompt(
    offer_id: int,
    angle: str = Query(default="mechanism"),
    placement: str = Query(default="meta_feed"),
    extra_direction: str = Query(default=""),
    session: Session = Depends(get_session),
) -> dict:
    """Build and screen a media prompt without generating anything.

    Screening costs nothing; a generation costs money and a minute.
    """
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise HTTPException(404, f"offer {offer_id} not found")
    try:
        spec = MEDIA_SPECS[placement]
    except KeyError:
        raise HTTPException(422, f"unknown placement '{placement}'")

    plan = (
        build_video_prompt(offer, angle, placement, extra_direction=extra_direction)
        if spec.kind == "video"
        else build_image_prompt(offer, angle, placement, extra_direction=extra_direction)
    )
    return {**plan.as_dict(), "would_generate": plan.is_safe}


@router.post("/media/generate/{creative_id}")
def generate_media(
    creative_id: int,
    kind: str = Query(default="image", pattern="^(image|video)$"),
    placements: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    """Generate the imagery a creative needs."""
    creative = session.get(Creative, creative_id)
    if creative is None:
        raise HTTPException(404, f"creative {creative_id} not found")

    wanted = (
        [p.strip() for p in placements.split(",") if p.strip()] if placements else None
    )
    assets = MediaStudio(session, get_settings()).generate_for_creative(
        creative, placements=wanted, kind=kind
    )
    session.commit()
    return {
        "creative_id": creative_id,
        "generated": sum(1 for a in assets if a.status is MediaStatus.READY),
        "rejected": sum(1 for a in assets if a.status is MediaStatus.REJECTED),
        "failed": sum(1 for a in assets if a.status is MediaStatus.FAILED),
        "assets": [_asset_out(a) for a in assets],
    }


@router.get("/media/assets")
def list_assets(
    creative_id: int | None = None,
    offer_id: int | None = None,
    limit: int = Query(default=50, le=200),
    session: Session = Depends(get_session),
) -> list[dict]:
    query = select(MediaAsset).order_by(MediaAsset.created_at.desc())
    if creative_id:
        query = query.where(MediaAsset.creative_id == creative_id)
    if offer_id:
        query = query.where(MediaAsset.offer_id == offer_id)
    return [_asset_out(a) for a in session.execute(query.limit(limit)).scalars()]


def _asset_out(asset: MediaAsset) -> dict:
    return {
        "id": asset.id,
        "creative_id": asset.creative_id,
        "kind": asset.kind.value,
        "status": asset.status.value,
        "provider": asset.provider,
        "model": asset.model,
        "placement": (asset.extra or {}).get("placement"),
        "aspect_ratio": asset.aspect_ratio,
        "width": asset.width,
        "height": asset.height,
        "local_path": asset.local_path,
        "public_url": asset.public_url,
        "bytes": asset.bytes,
        "prompt": asset.prompt,
        "compliance": asset.compliance_report,
        "error": asset.error,
    }
