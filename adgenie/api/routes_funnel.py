"""Funnel configuration, lead capture and lead economics."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.ltv import fit_lead_value, offer_prior_micros
from ..core.tracking import (
    record_funnel_event,
    record_lead,
    secret_is_placeholder,
    verify_postback_secret,
)
from ..db import get_session
from ..models import ConversionStatus, FunnelStep, FunnelStepKind, Lead, Offer
from ..money import micros_to_usd, usd_to_micros

router = APIRouter(tags=["funnel"])


class FunnelStepIn(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    name: str = ""
    kind: FunnelStepKind = FunnelStepKind.OTHER
    position: int = 0
    value_usd: float = Field(default=0.0, ge=0)
    url: str | None = None


class OptInIn(BaseModel):
    offer_id: int
    email: str
    click_id: str | None = None
    subid: str | None = None
    step: str = "optin"


class FunnelEventIn(BaseModel):
    offer_id: int
    step: str
    transaction_id: str
    click_id: str | None = None
    subid: str | None = None
    email: str | None = None
    revenue: float | None = None
    status: str = "approved"
    timestamp: int | None = None


_STATUS = {
    "pending": ConversionStatus.PENDING,
    "approved": ConversionStatus.APPROVED,
    "reversed": ConversionStatus.REVERSED,
}


def _step_out(step: FunnelStep) -> dict:
    return {
        "id": step.id,
        "key": step.key,
        "name": step.name,
        "kind": step.kind.value,
        "position": step.position,
        "value_usd": micros_to_usd(step.value_micros),
        "url": step.url,
        "captures_lead": step.captures_lead,
        "is_active": step.is_active,
    }


@router.put("/offers/{offer_id}/funnel")
def set_funnel(
    offer_id: int,
    steps: list[FunnelStepIn],
    session: Session = Depends(get_session),
) -> dict:
    """Define the steps between the ad click and the money.

    Replaces the whole funnel, so this is the one place its shape is decided.
    """
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise HTTPException(404, f"offer {offer_id} not found")

    keys = [s.key for s in steps]
    if len(keys) != len(set(keys)):
        raise HTTPException(422, "step keys must be unique within a funnel")

    for existing in list(offer.funnel_steps):
        session.delete(existing)
    session.flush()

    for index, payload in enumerate(steps):
        session.add(
            FunnelStep(
                offer_id=offer_id,
                key=payload.key,
                name=payload.name or payload.key.replace("_", " ").title(),
                kind=payload.kind,
                position=payload.position or index,
                value_micros=usd_to_micros(payload.value_usd),
                url=payload.url,
            )
        )
    session.commit()
    session.refresh(offer)
    return {
        "offer_id": offer_id,
        "steps": [_step_out(s) for s in offer.funnel_steps],
        "assumed_value_per_lead_usd": micros_to_usd(
            offer_prior_micros(session, offer_id)
        ),
        "note": (
            "That per-lead figure is an assumption from the step values, used "
            "only until real leads have been measured."
        ),
    }


@router.get("/offers/{offer_id}/funnel")
def get_funnel(offer_id: int, session: Session = Depends(get_session)) -> dict:
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise HTTPException(404, f"offer {offer_id} not found")
    return {
        "offer_id": offer_id,
        "has_funnel": offer.has_funnel,
        "steps": [_step_out(s) for s in offer.funnel_steps],
    }


@router.get("/offers/{offer_id}/lead-value")
def lead_value(
    offer_id: int,
    creative_id: int | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """What this offer's leads have actually been worth.

    The optimizer spends against the lower bound, not the mean: a list scaled
    on an optimistic lead value is a list funded by a forecast.
    """
    if session.get(Offer, offer_id) is None:
        raise HTTPException(404, f"offer {offer_id} not found")
    model = fit_lead_value(
        session,
        offer_id,
        creative_id=creative_id,
        prior_micros=offer_prior_micros(session, offer_id),
    )
    return {
        **model.as_dict(),
        "note": (
            "Only cohorts older than a week count toward the value; younger "
            "leads have not finished earning and would drag the average down."
        ),
    }


@router.post("/funnel/optin")
def capture_lead(
    payload: OptInIn,
    session: Session = Depends(get_session),
    x_postback_secret: str | None = Header(default=None),
    secret: str | None = Query(default=None),
) -> dict:
    """Record an opt-in and credit it to the ad that earned it.

    Authenticated like the conversion postback: this writes the lead counts the
    optimizer spends against.
    """
    _require_secret(x_postback_secret or secret)
    if session.get(Offer, payload.offer_id) is None:
        raise HTTPException(404, f"offer {payload.offer_id} not found")

    lead, method = record_lead(
        session,
        offer_id=payload.offer_id,
        email=payload.email,
        click_id=payload.click_id,
        subid=payload.subid,
        source_step=payload.step,
    )
    session.commit()
    return {
        "lead_id": lead.id,
        "attribution": method,
        "creative_id": lead.creative_id,
    }


@router.post("/funnel/event")
def funnel_event(
    payload: FunnelEventIn,
    session: Session = Depends(get_session),
    x_postback_secret: str | None = Header(default=None),
    secret: str | None = Query(default=None),
) -> dict:
    """Record a completed funnel step: a tripwire sale, a core purchase, an upsell."""
    _require_secret(x_postback_secret or secret)
    if payload.status not in _STATUS:
        raise HTTPException(422, f"unknown status '{payload.status}'")

    occurred = (
        datetime.fromtimestamp(payload.timestamp, tz=timezone.utc)
        if payload.timestamp
        else datetime.now(timezone.utc)
    )
    try:
        conversion, method = record_funnel_event(
            session,
            offer_id=payload.offer_id,
            step_key=payload.step,
            network_txn_id=payload.transaction_id,
            click_id=payload.click_id,
            subid=payload.subid,
            email=payload.email,
            revenue_micros=(
                usd_to_micros(payload.revenue) if payload.revenue is not None else None
            ),
            status=_STATUS[payload.status],
            occurred_at=occurred.replace(tzinfo=None),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    session.commit()
    return {
        "conversion_id": conversion.id,
        "step": payload.step,
        "attribution": method,
        "creative_id": conversion.creative_id,
        "lead_id": conversion.lead_id,
        "revenue_usd": micros_to_usd(conversion.revenue_micros),
    }


@router.get("/funnel/leads")
def list_leads(
    offer_id: int | None = None,
    creative_id: int | None = None,
    limit: int = Query(default=50, le=500),
    session: Session = Depends(get_session),
) -> dict:
    query = select(Lead).order_by(Lead.created_at.desc())
    if offer_id:
        query = query.where(Lead.offer_id == offer_id)
    if creative_id:
        query = query.where(Lead.creative_id == creative_id)
    leads = list(session.execute(query.limit(limit)).scalars())
    return {
        "count": len(leads),
        "leads": [
            {
                "id": lead.id,
                "offer_id": lead.offer_id,
                "creative_id": lead.creative_id,
                "source_step": lead.source_step,
                "realised_value_usd": micros_to_usd(lead.realised_value_micros),
                "created_at": lead.created_at.isoformat(),
                "attribution": (lead.extra or {}).get("attribution_method"),
            }
            for lead in leads
        ],
    }


def _require_secret(provided: str | None) -> None:
    if verify_postback_secret(provided):
        return
    if secret_is_placeholder(get_settings().postback_secret):
        raise HTTPException(
            503,
            "POSTBACK_SECRET is not configured; funnel events cannot be accepted "
            "until it is set to a real value.",
        )
    raise HTTPException(401, "invalid postback secret")
