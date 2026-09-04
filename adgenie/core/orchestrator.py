"""The control loop.

One cycle:

    sync metrics -> roll up -> evaluate -> record decisions -> apply

Everything is recorded before anything is applied, so a run that fails halfway
leaves a complete record of what it intended to do. Applying is gated three
ways: global dry-run, a per-action approval flag for large budget moves, and a
portfolio-wide daily spend cap that no sequence of individually-reasonable
increases can breach.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import (
    ActionStatus,
    ActionType,
    AdGroup,
    AuditLog,
    Campaign,
    Click,
    ComplianceVerdict,
    Conversion,
    ConversionStatus,
    Creative,
    EntityLevel,
    EntityStatus,
    MetricSnapshot,
    Offer,
    OptimizationAction,
    OptimizerRun,
    Platform,
)
from ..money import micros_to_usd
from ..platforms.base import AdPlatform, InsightRow, PlatformError
from ..platforms.factory import get_platform
from .copywriter import CopyStudio, build_brief
from .metrics import (
    PerformanceWindow,
    apply_pooled_prior,
    default_window,
    load_performance,
)
from .optimizer import Decision, Optimizer, OptimizerPolicy, allocate_budget
from .tracking import TrackingContext, build_tracking_url

logger = logging.getLogger(__name__)

__all__ = ["Orchestrator", "sync_metrics", "run_cycle"]

# Lower bound for "lifetime" queries. Earlier than any plausible campaign.
_EPOCH = date(2000, 1, 1)

_LEVEL_TO_ATTR = {
    EntityLevel.CAMPAIGN: Campaign,
    EntityLevel.AD_GROUP: AdGroup,
    EntityLevel.CREATIVE: Creative,
}


class Orchestrator:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        optimizer: Optimizer | None = None,
        studio: CopyStudio | None = None,
        platform_clients: dict[Platform, AdPlatform] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.optimizer = optimizer or Optimizer(
            OptimizerPolicy.from_settings(self.settings)
        )
        self.studio = studio or CopyStudio(settings=self.settings)
        self._clients = platform_clients or {}

    def client(self, platform: Platform) -> AdPlatform:
        if platform not in self._clients:
            self._clients[platform] = get_platform(platform, self.settings)
        return self._clients[platform]

    # ------------------------------------------------------------------
    # metric sync
    # ------------------------------------------------------------------
    def sync_metrics(self, since: date, until: date) -> dict:
        """Pull creative-level delivery, then roll it up.

        Only the creative level is fetched from the platform. Higher levels are
        summed from it so a campaign total can never disagree with the ads
        inside it, which is a real and confusing failure mode when both are
        fetched independently.
        """
        summary = {"creatives": 0, "rows": 0, "platforms": [], "errors": []}

        for platform in Platform:
            creatives = list(
                self.session.execute(
                    select(Creative)
                    .join(AdGroup, Creative.ad_group_id == AdGroup.id)
                    .join(Campaign, AdGroup.campaign_id == Campaign.id)
                    .where(
                        Campaign.platform == platform,
                        Creative.external_id.is_not(None),
                    )
                ).scalars()
            )
            if not creatives:
                continue
            summary["platforms"].append(platform.value)

            by_external = {c.external_id: c for c in creatives}
            try:
                rows = self.client(platform).fetch_insights(
                    "creative", since, until, list(by_external)
                )
            except PlatformError as exc:
                logger.error("Insight sync failed for %s: %s", platform.value, exc)
                summary["errors"].append(f"{platform.value}: {exc}")
                continue

            for row in rows:
                creative = by_external.get(row.external_id)
                if creative is None:
                    continue
                self._upsert_snapshot(EntityLevel.CREATIVE, creative.id, row)
                summary["rows"] += 1
            summary["creatives"] += len(creatives)

        self.session.flush()
        self._rollup(since, until)
        self.session.commit()
        return summary

    def _upsert_snapshot(
        self, level: EntityLevel, entity_id: int, row: InsightRow
    ) -> None:
        snapshot = self.session.execute(
            select(MetricSnapshot).where(
                MetricSnapshot.level == level,
                MetricSnapshot.entity_id == entity_id,
                MetricSnapshot.day == row.day,
            )
        ).scalar_one_or_none()
        if snapshot is None:
            snapshot = MetricSnapshot(level=level, entity_id=entity_id, day=row.day)
            self.session.add(snapshot)
        snapshot.impressions = row.impressions
        snapshot.clicks = row.clicks
        snapshot.spend_micros = row.spend_micros
        snapshot.platform_conversions = row.conversions
        snapshot.platform_conversion_value_micros = row.conversion_value_micros
        snapshot.frequency = row.frequency
        snapshot.reach = row.reach
        snapshot.video_views = row.video_views
        snapshot.raw = row.raw
        snapshot.synced_at = datetime.now(timezone.utc)

    def _rollup(self, since: date, until: date) -> None:
        """Sum creative rows into ad-group and campaign rows."""
        creative_parents = dict(
            self.session.execute(
                select(Creative.id, Creative.ad_group_id)
            ).all()
        )
        group_parents = dict(
            self.session.execute(select(AdGroup.id, AdGroup.campaign_id)).all()
        )

        rows = list(
            self.session.execute(
                select(MetricSnapshot).where(
                    MetricSnapshot.level == EntityLevel.CREATIVE,
                    MetricSnapshot.day >= since,
                    MetricSnapshot.day <= until,
                )
            ).scalars()
        )

        group_agg: dict[tuple[int, date], InsightRow] = {}
        campaign_agg: dict[tuple[int, date], InsightRow] = {}
        for row in rows:
            group_id = creative_parents.get(row.entity_id)
            if group_id is None:
                continue
            campaign_id = group_parents.get(group_id)
            for bucket, key in (
                (group_agg, (group_id, row.day)),
                (campaign_agg, (campaign_id, row.day) if campaign_id else None),
            ):
                if key is None:
                    continue
                agg = bucket.get(key)
                if agg is None:
                    agg = InsightRow(external_id=str(key[0]), day=row.day)
                    bucket[key] = agg
                agg.impressions += row.impressions
                agg.clicks += row.clicks
                agg.spend_micros += row.spend_micros
                agg.conversions += row.platform_conversions
                agg.conversion_value_micros += row.platform_conversion_value_micros
                agg.reach += row.reach
                agg.video_views += row.video_views
                agg.frequency = max(agg.frequency, row.frequency)

        for (entity_id, _day), agg in group_agg.items():
            self._upsert_snapshot(EntityLevel.AD_GROUP, entity_id, agg)
        for (entity_id, _day), agg in campaign_agg.items():
            self._upsert_snapshot(EntityLevel.CAMPAIGN, entity_id, agg)
        self.session.flush()

    # ------------------------------------------------------------------
    # optimization cycle
    # ------------------------------------------------------------------
    def run_cycle(
        self,
        *,
        lookback_days: int | None = None,
        apply: bool | None = None,
        today: date | None = None,
        now: datetime | None = None,
    ) -> dict:
        lookback_days = lookback_days or self.settings.optimizer_lookback_days
        apply = (not self.settings.dry_run) if apply is None else apply
        since, until = default_window(lookback_days, today)

        run_id = uuid.uuid4().hex[:16]
        run = OptimizerRun(run_id=run_id, dry_run=not apply)
        self.session.add(run)
        self.session.flush()

        # Back-tests pass simulated time so cooldowns advance with the
        # simulation rather than with the wall clock.
        now = now or (
            datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
            if today
            else datetime.now(timezone.utc)
        )

        decisions: list[tuple[Decision, object]] = []
        decisions += self._evaluate_creatives(since, until, now)
        decisions += self._evaluate_ad_groups(since, until, now)

        actions: list[OptimizationAction] = []
        for decision, _entity in decisions:
            if decision.action is ActionType.NO_ACTION:
                continue
            action = OptimizationAction(
                run_id=run_id,
                level=decision.level,
                entity_id=decision.entity_id,
                action=decision.action,
                rule=decision.rule,
                reason=decision.reason,
                confidence=decision.confidence,
                evidence=decision.evidence,
                payload=decision.payload,
                requires_approval=decision.requires_approval,
                status=ActionStatus.PROPOSED,
            )
            self.session.add(action)
            actions.append(action)
        self.session.flush()

        applied = 0
        if apply:
            for action in actions:
                if action.requires_approval:
                    logger.info(
                        "Action %s on %s %s needs approval (%s)",
                        action.action.value,
                        action.level.value,
                        action.entity_id,
                        action.reason,
                    )
                    continue
                if self.apply_action(action, now=now):
                    applied += 1

        run.finished_at = datetime.now(timezone.utc)
        run.entities_evaluated = len(decisions)
        run.actions_proposed = len(actions)
        run.actions_applied = applied
        run.summary = {
            "window": {"since": since.isoformat(), "until": until.isoformat()},
            "by_rule": _count_by(actions, lambda a: a.rule),
            "by_action": _count_by(actions, lambda a: a.action.value),
            "needs_approval": sum(1 for a in actions if a.requires_approval),
        }
        self.session.commit()

        logger.info(
            "Optimizer run %s: evaluated %s, proposed %s, applied %s",
            run_id,
            len(decisions),
            len(actions),
            applied,
        )
        return {
            "run_id": run_id,
            "evaluated": len(decisions),
            "proposed": len(actions),
            "applied": applied,
            "dry_run": not apply,
            "summary": run.summary,
            "actions": [_action_dict(a) for a in actions],
        }

    # -- evaluation ------------------------------------------------------
    def _evaluate_creatives(
        self, since: date, until: date, now: datetime | None = None
    ) -> list[tuple[Decision, Creative]]:
        creatives = list(
            self.session.execute(
                select(Creative).where(
                    Creative.status.in_(
                        [EntityStatus.ACTIVE, EntityStatus.PAUSED, EntityStatus.PENDING_REVIEW]
                    )
                )
            ).scalars()
        )
        # Group by ad group so the shrinkage prior is pooled over comparable ads.
        by_group: dict[int, list[Creative]] = defaultdict(list)
        for creative in creatives:
            by_group[creative.ad_group_id].append(creative)

        out: list[tuple[Decision, Creative]] = []
        for group_id, members in by_group.items():
            windows = {
                c.id: load_performance(
                    self.session,
                    EntityLevel.CREATIVE,
                    c.id,
                    since,
                    until,
                    self.settings.optimizer_credible_level,
                )
                for c in members
            }
            apply_pooled_prior(list(windows.values()))
            lifetimes = {
                c.id: load_performance(
                    self.session,
                    EntityLevel.CREATIVE,
                    c.id,
                    _EPOCH,
                    until,
                    self.settings.optimizer_credible_level,
                )
                for c in members
            }
            apply_pooled_prior(list(lifetimes.values()))

            for creative in members:
                window = windows[creative.id]
                decision = self.optimizer.evaluate(
                    window,
                    lifetime=lifetimes[creative.id],
                    now=now,
                    is_active=creative.status is EntityStatus.ACTIVE,
                    compliance_blocked=creative.compliance_verdict
                    is ComplianceVerdict.BLOCK,
                    last_action_at=self._last_action_at(
                        EntityLevel.CREATIVE, creative.id
                    ),
                    opening_ctr=self._opening_ctr(creative.id),
                )
                out.append((decision, creative))
        return out

    def _evaluate_ad_groups(
        self, since: date, until: date, now: datetime | None = None
    ) -> list[tuple[Decision, AdGroup]]:
        groups = list(
            self.session.execute(
                select(AdGroup).where(AdGroup.status == EntityStatus.ACTIVE)
            ).scalars()
        )
        windows = [
            load_performance(
                self.session,
                EntityLevel.AD_GROUP,
                g.id,
                since,
                until,
                self.settings.optimizer_credible_level,
            )
            for g in groups
        ]
        apply_pooled_prior(windows)
        lifetimes = [
            load_performance(
                self.session, EntityLevel.AD_GROUP, g.id, _EPOCH, until
            )
            for g in groups
        ]
        apply_pooled_prior(lifetimes)
        out = []
        for group, window, lifetime in zip(groups, windows, lifetimes):
            decision = self.optimizer.evaluate(
                window,
                lifetime=lifetime,
                now=now,
                is_active=True,
                last_action_at=self._last_action_at(EntityLevel.AD_GROUP, group.id),
            )
            out.append((decision, group))
        return out

    def _last_action_at(self, level: EntityLevel, entity_id: int) -> datetime | None:
        return self.session.execute(
            select(OptimizationAction.applied_at)
            .where(
                OptimizationAction.level == level,
                OptimizationAction.entity_id == entity_id,
                OptimizationAction.status == ActionStatus.APPLIED,
            )
            .order_by(OptimizationAction.applied_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _opening_ctr(self, creative_id: int) -> float | None:
        """CTR over the creative's first seven delivering days.

        Comparing an ad to its own opening week is a fairer fatigue test than
        comparing it to its siblings, which may target different audiences.
        """
        rows = list(
            self.session.execute(
                select(MetricSnapshot)
                .where(
                    MetricSnapshot.level == EntityLevel.CREATIVE,
                    MetricSnapshot.entity_id == creative_id,
                    MetricSnapshot.impressions > 0,
                )
                .order_by(MetricSnapshot.day)
                .limit(7)
            ).scalars()
        )
        impressions = sum(r.impressions for r in rows)
        if impressions < 1000 or len(rows) < 3:
            return None
        return sum(r.clicks for r in rows) / impressions

    # ------------------------------------------------------------------
    # applying
    # ------------------------------------------------------------------
    def apply_action(
        self,
        action: OptimizationAction,
        actor: str = "optimizer",
        now: datetime | None = None,
    ) -> bool:
        entity = self.session.get(_LEVEL_TO_ATTR[action.level], action.entity_id)
        if entity is None:
            action.status = ActionStatus.FAILED
            action.error = "entity no longer exists"
            return False

        platform = self._platform_of(action.level, entity)
        try:
            if action.action is ActionType.PAUSE:
                self._set_status(platform, action.level, entity, active=False)
            elif action.action is ActionType.RESUME:
                self._set_status(platform, action.level, entity, active=True)
            elif action.action in (
                ActionType.INCREASE_BUDGET,
                ActionType.DECREASE_BUDGET,
            ):
                self._set_budget(platform, action, entity)
            elif action.action is ActionType.GENERATE_VARIANTS:
                self._generate_variants(action, entity)
            elif action.action is ActionType.REALLOCATE:
                self._reallocate(action, entity)
            else:
                action.status = ActionStatus.REJECTED
                action.error = f"no handler for {action.action.value}"
                return False
        except PlatformError as exc:
            action.status = ActionStatus.FAILED
            action.error = str(exc)
            logger.error(
                "Failed to apply %s on %s %s: %s",
                action.action.value,
                action.level.value,
                action.entity_id,
                exc,
            )
            self.session.flush()
            return False

        action.status = ActionStatus.APPLIED
        action.applied_at = now or datetime.now(timezone.utc)
        self.session.add(
            AuditLog(
                actor=actor,
                platform=platform,
                operation=action.action.value,
                target=f"{action.level.value}:{action.entity_id}",
                request=action.payload,
                response={"rule": action.rule},
                ok=True,
                dry_run=self.settings.dry_run,
            )
        )
        self.session.flush()
        return True

    def _platform_of(self, level: EntityLevel, entity) -> Platform:
        if level is EntityLevel.CAMPAIGN:
            return entity.platform
        if level is EntityLevel.AD_GROUP:
            return self.session.get(Campaign, entity.campaign_id).platform
        group = self.session.get(AdGroup, entity.ad_group_id)
        return self.session.get(Campaign, group.campaign_id).platform

    def _set_status(
        self, platform: Platform, level: EntityLevel, entity, active: bool
    ) -> None:
        if entity.external_id:
            self.client(platform).set_status(level.value, entity.external_id, active)
        entity.status = EntityStatus.ACTIVE if active else EntityStatus.PAUSED

    def _set_budget(
        self, platform: Platform, action: OptimizationAction, entity
    ) -> None:
        target = int(action.payload["to_micros"])

        # Portfolio guard: no combination of individually sensible increases may
        # push total committed daily spend past the global cap.
        if action.action is ActionType.INCREASE_BUDGET:
            headroom = self._budget_headroom_micros(exclude_entity=entity, level=action.level)
            if target > headroom:
                target = max(int(action.payload["from_micros"]), headroom)
                action.reason += (
                    f" Capped at {micros_to_usd(target):.2f} USD by the global "
                    "daily spend limit."
                )
                if target <= action.payload["from_micros"]:
                    raise PlatformError(
                        "global daily budget cap reached; no headroom to scale",
                        platform=platform,
                        code="BUDGET_CAP",
                    )

        cap = getattr(entity, "max_daily_budget_micros", 0)
        if cap and target > cap:
            target = cap

        if action.level is EntityLevel.AD_GROUP and platform is Platform.GOOGLE:
            # Google budgets live on the campaign, so scale the parent instead.
            campaign = self.session.get(Campaign, entity.campaign_id)
            if campaign and campaign.external_id:
                self.client(platform).set_budget(
                    "campaign", campaign.external_id, target
                )
                campaign.daily_budget_micros = target
        elif entity.external_id:
            self.client(platform).set_budget(
                action.level.value, entity.external_id, target
            )
        entity.daily_budget_micros = target
        action.payload = {**action.payload, "applied_micros": target}

    def _budget_headroom_micros(self, exclude_entity, level: EntityLevel) -> int:
        cap = int(self.settings.global_daily_budget_cap_usd * 1_000_000)
        committed = 0
        for campaign in self.session.execute(
            select(Campaign).where(Campaign.status == EntityStatus.ACTIVE)
        ).scalars():
            if level is EntityLevel.CAMPAIGN and campaign.id == getattr(
                exclude_entity, "id", None
            ):
                continue
            committed += campaign.daily_budget_micros
        return max(0, cap - committed)

    def _generate_variants(self, action: OptimizationAction, entity: Creative) -> None:
        """Breed fresh creatives from a fatigued one.

        The parent stays live until the children have delivered, and lineage is
        recorded so a family of creatives can be traced back to the angle that
        started it.
        """
        group = self.session.get(AdGroup, entity.ad_group_id)
        campaign = self.session.get(Campaign, group.campaign_id)
        offer = self.session.get(Offer, campaign.offer_id)
        count = int(action.payload.get("variants", 2))

        brief = build_brief(
            offer,
            platform=campaign.platform,
            angle_key=entity.angle or None,
            keyword=(group.keywords[0] if group.keywords else ""),
        )
        drafts = self.studio.write_variants(brief, count=count, offer=offer)

        created: list[int] = []
        for index, draft in enumerate(drafts):
            if draft.compliance and draft.compliance.verdict is ComplianceVerdict.BLOCK:
                continue
            child = Creative(
                ad_group_id=group.id,
                name=f"{entity.name} > gen{entity.generation + 1}.{index + 1}",
                angle=draft.angle,
                headlines=draft.headlines,
                descriptions=draft.descriptions,
                primary_texts=draft.primary_texts,
                call_to_action=draft.call_to_action,
                image_prompt=draft.image_prompt,
                parent_id=entity.id,
                generation=entity.generation + 1,
                generator=draft.generator,
                generator_meta=draft.generator_meta,
                compliance_verdict=draft.compliance.verdict
                if draft.compliance
                else ComplianceVerdict.UNREVIEWED,
                compliance_report=draft.compliance.as_dict() if draft.compliance else {},
                status=EntityStatus.DRAFT,
            )
            self.session.add(child)
            self.session.flush()
            child.final_url = build_tracking_url(
                TrackingContext(
                    offer_id=offer.id,
                    campaign_id=campaign.id,
                    ad_group_id=group.id,
                    creative_id=child.id,
                    platform=campaign.platform,
                ),
                settings=self.settings,
            )
            created.append(child.id)
        action.payload = {**action.payload, "created_creative_ids": created}

    def _reallocate(self, action: OptimizationAction, entity: AdGroup) -> None:
        """Redistribute an ad group's budget across its creatives."""
        allocation = action.payload.get("allocation", {})
        platform = self._platform_of(EntityLevel.AD_GROUP, entity)
        for creative_id, budget_micros in allocation.items():
            creative = self.session.get(Creative, int(creative_id))
            if creative and creative.external_id:
                self.client(platform).set_budget(
                    "creative", creative.external_id, int(budget_micros)
                )

    # ------------------------------------------------------------------
    def rebalance_ad_group(self, ad_group_id: int, since: date, until: date) -> dict:
        """Propose a Thompson-sampled budget split across an ad group's creatives."""
        group = self.session.get(AdGroup, ad_group_id)
        if group is None:
            raise ValueError(f"ad group {ad_group_id} not found")
        creatives = list(
            self.session.execute(
                select(Creative).where(
                    Creative.ad_group_id == ad_group_id,
                    Creative.status == EntityStatus.ACTIVE,
                )
            ).scalars()
        )
        if not creatives:
            return {"ad_group_id": ad_group_id, "allocation": {}}

        windows = [
            load_performance(self.session, EntityLevel.CREATIVE, c.id, since, until)
            for c in creatives
        ]
        allocation = allocate_budget(windows, group.daily_budget_micros)
        return {
            "ad_group_id": ad_group_id,
            "daily_budget_usd": micros_to_usd(group.daily_budget_micros),
            "allocation": {
                str(k): micros_to_usd(v) for k, v in allocation.items()
            },
            "allocation_micros": allocation,
        }

    # ------------------------------------------------------------------
    def push_conversions(self, lookback_hours: int = 48) -> dict:
        """Echo network conversions back to the platforms.

        Smart Bidding and Advantage+ can only optimize toward events they can
        see. An affiliate sale happens on someone else's domain, so unless it is
        uploaded the algorithm is optimizing for the wrong thing.
        """
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            hours=lookback_hours
        )
        pending = list(
            self.session.execute(
                select(Conversion).where(
                    Conversion.uploaded_to_platform.is_(False),
                    Conversion.status == ConversionStatus.APPROVED,
                    Conversion.created_at >= cutoff,
                )
            ).scalars()
        )
        if not pending:
            return {"uploaded": 0}

        by_platform: dict[Platform, list[dict]] = defaultdict(list)
        for conversion in pending:
            click = self.session.execute(
                select(Click).where(Click.click_id == conversion.click_id)
            ).scalar_one_or_none()
            if click is None or not click.platform:
                continue
            entry = {
                "event_time": int(conversion.occurred_at.timestamp()),
                "click_time": int(click.created_at.timestamp()),
                "value": micros_to_usd(conversion.revenue_micros),
                "currency": "USD",
                "event_id": f"adgenie-{conversion.id}",
                "order_id": conversion.network_txn_id,
                "event_name": "Purchase",
            }
            if click.platform is Platform.META:
                entry["fbclid"] = click.platform_click_id
                if click.ip_hash:
                    entry["ip_hash"] = click.ip_hash
            else:
                entry["gclid"] = click.platform_click_id
            if not (entry.get("fbclid") or entry.get("gclid")):
                continue
            by_platform[click.platform].append(entry)
            conversion.uploaded_to_platform = True

        uploaded = 0
        for platform, entries in by_platform.items():
            try:
                uploaded += self.client(platform).upload_conversions(entries)
            except PlatformError as exc:
                logger.error("Conversion upload failed for %s: %s", platform.value, exc)
                for conversion in pending:
                    conversion.uploaded_to_platform = False
        self.session.commit()
        return {"uploaded": uploaded, "platforms": [p.value for p in by_platform]}


# --------------------------------------------------------------------------
# module-level conveniences
# --------------------------------------------------------------------------


def sync_metrics(session: Session, since: date, until: date, **kwargs) -> dict:
    return Orchestrator(session, **kwargs).sync_metrics(since, until)


def run_cycle(session: Session, **kwargs) -> dict:
    orchestrator_kwargs = {
        k: kwargs.pop(k)
        for k in ("settings", "optimizer", "studio", "platform_clients")
        if k in kwargs
    }
    return Orchestrator(session, **orchestrator_kwargs).run_cycle(**kwargs)


def _count_by(items, key) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        counts[key(item)] += 1
    return dict(counts)


def _action_dict(action: OptimizationAction) -> dict:
    return {
        "id": action.id,
        "level": action.level.value,
        "entity_id": action.entity_id,
        "action": action.action.value,
        "rule": action.rule,
        "reason": action.reason,
        "confidence": action.confidence,
        "status": action.status.value,
        "requires_approval": action.requires_approval,
        "payload": action.payload,
    }
