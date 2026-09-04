"""Click redirect and conversion postback endpoints.

These are the only public-facing endpoints. `/r` is what ad clicks hit, and
`/postback` is what affiliate networks call when a sale lands.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.tracking import (
    PLATFORM_CLICK_PARAM,
    build_final_url,
    record_click,
    record_conversion,
    verify_postback_secret,
)
from ..db import get_session
from ..models import Campaign, ConversionStatus, Offer
from ..money import usd_to_micros
from ..schemas import PostbackIn

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tracking"])

_STATUS_MAP = {
    "pending": ConversionStatus.PENDING,
    "approved": ConversionStatus.APPROVED,
    "reversed": ConversionStatus.REVERSED,
}


@router.get("/r", include_in_schema=False)
def redirect_click(
    request: Request,
    s: str = Query(..., description="Encoded sub-id identifying the ad."),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Record the click, then send the visitor on to the offer.

    This runs on every single ad click, so it does the minimum: one insert and
    a 302. Anything heavier belongs in a background job.
    """
    params = dict(request.query_params)
    click, offer = record_click(
        session,
        s,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
        referrer=request.headers.get("referer"),
        country=request.headers.get("cf-ipcountry") or request.headers.get("x-country"),
        query_params=params,
    )
    if offer is None and click.campaign_id:
        # The sub-id was mangled or truncated, but the platform's own macros
        # resolved the creative. A paid click should not be thrown away when
        # the offer is still reachable through the campaign.
        campaign = session.get(Campaign, click.campaign_id)
        if campaign is not None:
            offer = session.get(Offer, campaign.offer_id)
            if offer is not None:
                click.offer_id = offer.id
    if offer is None:
        session.rollback()
        raise HTTPException(404, "unknown offer")

    passthrough = {
        key: params[key] for key in PLATFORM_CLICK_PARAM.values() if key in params
    }
    destination = build_final_url(
        offer.destination_url, click.click_id, extra=passthrough
    )
    session.commit()
    # 302, not 301: a permanent redirect would be cached and the click never
    # recorded again.
    return RedirectResponse(url=destination, status_code=302)


@router.post("/postback")
def conversion_postback(
    payload: PostbackIn,
    session: Session = Depends(get_session),
    x_postback_secret: str | None = Header(default=None),
    secret: str | None = Query(default=None),
) -> dict:
    """Receive a conversion from an affiliate network.

    Authenticated with a shared secret because this endpoint writes the revenue
    numbers the optimizer spends money against. An unauthenticated version of
    this endpoint is a way to make the system scale a losing campaign.
    """
    provided = x_postback_secret or secret
    if not verify_postback_secret(provided):
        logger.warning("Rejected postback with a bad secret for txn %s", payload.transaction_id)
        raise HTTPException(401, "invalid postback secret")

    occurred_at = (
        datetime.fromtimestamp(payload.timestamp, tz=timezone.utc)
        if payload.timestamp
        else datetime.now(timezone.utc)
    )
    conversion, method = record_conversion(
        session,
        network=payload.network,
        network_txn_id=payload.transaction_id,
        click_id=payload.click_id,
        subid=payload.subid,
        revenue_micros=usd_to_micros(payload.revenue),
        sale_amount_micros=usd_to_micros(payload.sale_amount),
        status=_STATUS_MAP[payload.status],
        event_name=payload.event,
        occurred_at=occurred_at.replace(tzinfo=None),
        raw=payload.model_dump(),
    )
    session.commit()

    if method == "unmatched":
        logger.warning(
            "Conversion %s could not be attributed to a click; revenue will not "
            "reach any creative.",
            payload.transaction_id,
        )
    return {
        "conversion_id": conversion.id,
        "attribution": method,
        "creative_id": conversion.creative_id,
        "status": conversion.status.value,
    }


@router.get("/postback", include_in_schema=False)
def conversion_postback_get(
    transaction_id: str,
    session: Session = Depends(get_session),
    click_id: str | None = None,
    subid: str | None = None,
    network: str = "manual",
    revenue: float = 0.0,
    sale_amount: float = 0.0,
    status: str = "approved",
    event: str = "sale",
    timestamp: int | None = None,
    secret: str | None = None,
) -> dict:
    """GET form of the postback.

    Most affiliate networks only support a GET pixel, so both verbs are
    accepted and share the same handler logic.
    """
    if status not in _STATUS_MAP:
        raise HTTPException(422, f"unknown status '{status}'")
    return conversion_postback(
        PostbackIn(
            click_id=click_id,
            subid=subid,
            transaction_id=transaction_id,
            network=network,
            revenue=revenue,
            sale_amount=sale_amount,
            status=status,  # type: ignore[arg-type]
            event=event,
            timestamp=timestamp,
        ),
        session=session,
        x_postback_secret=None,
        secret=secret,
    )
