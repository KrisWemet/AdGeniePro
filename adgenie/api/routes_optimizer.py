"""Optimizer control: sync, run, review and approve."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.metrics import default_window
from ..core.orchestrator import Orchestrator
from ..db import get_session
from ..models import (
    ActionStatus,
    AdGroup,
    AuditLog,
    OptimizationAction,
    OptimizerRun,
)
from ..schemas import ActionOut, OptimizeIn, SyncIn

router = APIRouter(tags=["optimizer"])


def _action_out(action: OptimizationAction) -> ActionOut:
    return ActionOut(
        id=action.id,
        level=action.level.value,
        entity_id=action.entity_id,
        action=action.action.value,
        rule=action.rule,
        reason=action.reason,
        confidence=action.confidence,
        status=action.status.value,
        requires_approval=action.requires_approval,
        payload=action.payload,
    )


@router.post("/optimizer/sync")
def sync(payload: SyncIn, session: Session = Depends(get_session)) -> dict:
    """Pull the latest delivery data from every connected platform."""
    settings = get_settings()
    until = payload.until or (date.today() - timedelta(days=1))
    since = payload.since or (until - timedelta(days=settings.optimizer_lookback_days))
    if since > until:
        raise HTTPException(422, "since must not be after until")
    return Orchestrator(session, settings=settings).sync_metrics(since, until)


@router.post("/optimizer/run")
def run_optimizer(
    payload: OptimizeIn, session: Session = Depends(get_session)
) -> dict:
    """Evaluate every entity and record what should change.

    With `apply` false (the default in a dry-run deployment) this changes
    nothing on the ad platforms; it produces a reviewable set of proposals.
    """
    return Orchestrator(session, settings=get_settings()).run_cycle(
        lookback_days=payload.lookback_days, apply=payload.apply
    )


@router.get("/optimizer/runs")
def list_runs(
    session: Session = Depends(get_session), limit: int = Query(default=20, le=100)
) -> list[dict]:
    runs = session.execute(
        select(OptimizerRun).order_by(OptimizerRun.started_at.desc()).limit(limit)
    ).scalars()
    return [
        {
            "run_id": r.run_id,
            "started_at": r.started_at.isoformat(),
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "dry_run": r.dry_run,
            "evaluated": r.entities_evaluated,
            "proposed": r.actions_proposed,
            "applied": r.actions_applied,
            "summary": r.summary,
        }
        for r in runs
    ]


@router.get("/optimizer/actions", response_model=list[ActionOut])
def list_actions(
    session: Session = Depends(get_session),
    status: ActionStatus | None = None,
    run_id: str | None = None,
    limit: int = Query(default=100, le=500),
) -> list[ActionOut]:
    query = select(OptimizationAction).order_by(OptimizationAction.created_at.desc())
    if status:
        query = query.where(OptimizationAction.status == status)
    if run_id:
        query = query.where(OptimizationAction.run_id == run_id)
    return [_action_out(a) for a in session.execute(query.limit(limit)).scalars()]


@router.post("/optimizer/actions/{action_id}/approve", response_model=ActionOut)
def approve_action(
    action_id: int, session: Session = Depends(get_session)
) -> ActionOut:
    """Approve and immediately apply a proposal that was held for review."""
    action = session.get(OptimizationAction, action_id)
    if action is None:
        raise HTTPException(404, f"action {action_id} not found")
    if action.status not in (ActionStatus.PROPOSED, ActionStatus.APPROVED):
        raise HTTPException(
            409, f"action {action_id} is already {action.status.value}"
        )

    orchestrator = Orchestrator(session, settings=get_settings())
    applied = orchestrator.apply_action(action, actor="human")
    session.commit()
    if not applied:
        raise HTTPException(502, action.error or "failed to apply action")
    return _action_out(action)


@router.post("/optimizer/actions/{action_id}/reject", response_model=ActionOut)
def reject_action(
    action_id: int,
    reason: str = Query(default=""),
    session: Session = Depends(get_session),
) -> ActionOut:
    action = session.get(OptimizationAction, action_id)
    if action is None:
        raise HTTPException(404, f"action {action_id} not found")
    if action.status is ActionStatus.APPLIED:
        raise HTTPException(409, "cannot reject an action that was already applied")
    action.status = ActionStatus.REJECTED
    action.error = reason or "rejected by operator"
    session.commit()
    return _action_out(action)


@router.get("/optimizer/rebalance/{ad_group_id}")
def rebalance(
    ad_group_id: int,
    days: int = Query(default=7, ge=1, le=90),
    session: Session = Depends(get_session),
) -> dict:
    """Propose a Thompson-sampled budget split across an ad group's creatives."""
    if session.get(AdGroup, ad_group_id) is None:
        raise HTTPException(404, f"ad group {ad_group_id} not found")
    since, until = default_window(days)
    return Orchestrator(session, settings=get_settings()).rebalance_ad_group(
        ad_group_id, since, until
    )


@router.post("/optimizer/push-conversions")
def push_conversions(
    hours: int = Query(default=48, ge=1, le=720),
    session: Session = Depends(get_session),
) -> dict:
    """Send network-confirmed sales back to Meta and Google.

    Bidding algorithms can only optimize toward events they can observe, and an
    affiliate sale happens on someone else's domain.
    """
    return Orchestrator(session, settings=get_settings()).push_conversions(hours)


@router.get("/audit")
def audit_log(
    session: Session = Depends(get_session), limit: int = Query(default=100, le=500)
) -> list[dict]:
    """Every mutation this platform has sent to an ad account."""
    rows = session.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    ).scalars()
    return [
        {
            "id": r.id,
            "actor": r.actor,
            "platform": r.platform.value if r.platform else None,
            "operation": r.operation,
            "target": r.target,
            "ok": r.ok,
            "dry_run": r.dry_run,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
