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
from ..platforms.base import AdPlatform, CreativeSpec, InsightRow, PlatformError
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
                    # Neither platform funds an individual ad.
                    has_own_budget=False,
                    is_active=creative.status is EntityStatus.ACTIVE,
                    compliance_blocked=creative.compliance_verdict
                    is ComplianceVerdict.BLOCK,
                    last_action_at=self._last_action_at(
                        EntityLevel.CREATIVE, creative.id
                    ),
                    last_refresh_at=self._last_refresh_at(creative.id),
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
                # Meta funds ad sets directly; on Google the change is applied
                # to the parent campaign by delta.
                has_own_budget=True,
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

    def _last_refresh_at(self, creative_id: int) -> datetime | None:
        """When this creative last had replacements bred from it.

        Read from the children themselves rather than from the action log, so
        the answer stays right even if actions are pruned.
        """
        return self.session.execute(
            select(Creative.created_at)
            .where(Creative.parent_id == creative_id)
            .order_by(Creative.created_at.desc())
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

        platform: Platform | None = None
        try:
            # Resolving the parent chain can fail on an orphaned row, so it has
            # to sit inside the guard or it would abort the whole run.
            platform = self._platform_of(action.level, entity)
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
        except Exception as exc:  # one bad action must not abort the whole run
            action.status = ActionStatus.FAILED
            action.error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "Unexpected failure applying %s on %s %s",
                action.action.value,
                action.level.value,
                action.entity_id,
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
            campaign = self.session.get(Campaign, entity.campaign_id)
        else:
            group = self.session.get(AdGroup, entity.ad_group_id)
            campaign = (
                self.session.get(Campaign, group.campaign_id) if group else None
            )
        if campaign is None:
            raise PlatformError(
                f"{level.value} {getattr(entity, 'id', '?')} has no parent campaign",
                code="ORPHANED",
            )
        return campaign.platform

    def _set_status(
        self, platform: Platform, level: EntityLevel, entity, active: bool
    ) -> None:
        if entity.external_id:
            self.client(platform).set_status(level.value, entity.external_id, active)
        entity.status = EntityStatus.ACTIVE if active else EntityStatus.PAUSED

    def _set_budget(
        self, platform: Platform, action: OptimizationAction, entity
    ) -> None:
        if action.level is EntityLevel.CREATIVE:
            raise PlatformError(
                "neither Meta nor Google exposes a budget on an individual ad; "
                "fund the ad set or campaign instead",
                platform=platform,
                code="INVALID_LEVEL",
            )
        target = int(action.payload["to_micros"])

        # Portfolio guard: no combination of individually sensible increases may
        # push total committed daily spend past the global cap. The comparison
        # is on the *increase*, not the entity's new absolute budget, because
        # the money it already spends is part of the committed total either way.
        if action.action is ActionType.INCREASE_BUDGET:
            current = int(action.payload["from_micros"])
            headroom = self._budget_headroom_micros()
            delta = target - current
            if delta > headroom:
                target = current + max(0, headroom)
                action.reason += (
                    f" Capped at {micros_to_usd(target):.2f} USD by the global "
                    "daily spend limit."
                )
                if target <= current:
                    raise PlatformError(
                        "global daily budget cap reached; no headroom to scale",
                        platform=platform,
                        code="BUDGET_CAP",
                    )

        cap = getattr(entity, "max_daily_budget_micros", 0)
        if cap and target > cap:
            target = cap

        if action.level is EntityLevel.AD_GROUP and platform is Platform.GOOGLE:
            # Google holds the budget on the campaign, shared by every ad group
            # under it. Writing this ad group's new amount onto the campaign
            # would silently defund its siblings, so the campaign moves by the
            # delta instead.
            campaign = self.session.get(Campaign, entity.campaign_id)
            if campaign is None:
                raise PlatformError(
                    f"ad group {entity.id} has no campaign",
                    platform=platform,
                    code="NOT_FOUND",
                )
            delta = target - entity.daily_budget_micros
            campaign_target = max(1, campaign.daily_budget_micros + delta)
            if campaign.max_daily_budget_micros:
                campaign_target = min(campaign_target, campaign.max_daily_budget_micros)
            if campaign.external_id:
                self.client(platform).set_budget(
                    "campaign", campaign.external_id, campaign_target
                )
            campaign.daily_budget_micros = campaign_target
            action.payload = {
                **action.payload,
                "campaign_budget_micros": campaign_target,
            }
        elif entity.external_id:
            self.client(platform).set_budget(
                action.level.value, entity.external_id, target
            )
        entity.daily_budget_micros = target
        action.payload = {**action.payload, "applied_micros": target}

    def _committed_daily_micros(
        self, exclude_level: EntityLevel | None = None, exclude_id: int | None = None
    ) -> int:
        """Total daily spend currently committed across active campaigns.

        A campaign either carries its own budget (campaign budget optimisation)
        or delegates to its ad sets. Summing both double-counts; summing only
        campaigns misses every ad-set budget. So each campaign contributes the
        larger of the two, which is the most it can actually spend in a day.
        """
        committed = 0
        campaigns = list(
            self.session.execute(
                select(Campaign).where(Campaign.status == EntityStatus.ACTIVE)
            ).scalars()
        )
        for campaign in campaigns:
            if exclude_level is EntityLevel.CAMPAIGN and campaign.id == exclude_id:
                continue
            group_total = 0
            for group in self.session.execute(
                select(AdGroup).where(
                    AdGroup.campaign_id == campaign.id,
                    AdGroup.status == EntityStatus.ACTIVE,
                )
            ).scalars():
                if exclude_level is EntityLevel.AD_GROUP and group.id == exclude_id:
                    continue
                group_total += group.daily_budget_micros
            committed += max(campaign.daily_budget_micros, group_total)
        return committed

    def _budget_headroom_micros(self) -> int:
        """How much more daily spend the portfolio may commit."""
        cap = int(self.settings.global_daily_budget_cap_usd * 1_000_000)
        return max(0, cap - self._committed_daily_micros())

    def _generate_variants(self, action: OptimizationAction, entity) -> None:
        """Breed fresh creatives from a fatigued one.

        The parent stays live until the children have delivered, and lineage is
        recorded so a family of creatives can be traced back to the angle that
        started it.
        """
        if action.level is not EntityLevel.CREATIVE or not isinstance(entity, Creative):
            raise PlatformError(
                "creative variants can only be bred from a creative",
                code="INVALID_LEVEL",
            )
        group = self.session.get(AdGroup, entity.ad_group_id)
        campaign = self.session.get(Campaign, group.campaign_id) if group else None
        offer = self.session.get(Offer, campaign.offer_id) if campaign else None
        if offer is None:
            raise PlatformError(
                f"creative {entity.id} has no reachable offer", code="NOT_FOUND"
            )
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

            # A variant that only exists in the database does not replace a worn
            # out ad, so the fatigue rule would fire again on every cycle and
            # accumulate orphan rows. Push it to the platform, paused, and let
            # the operator or the next cycle turn it on.
            spec = CreativeSpec(
                ad_group_external_id=group.external_id or "",
                name=child.name,
                final_url=child.final_url,
                headlines=child.headlines,
                descriptions=child.descriptions,
                primary_texts=child.primary_texts,
                call_to_action=child.call_to_action,
                status="PAUSED",
            )
            try:
                child.external_id = self.client(campaign.platform).create_creative(spec)
                child.status = EntityStatus.PAUSED
                created.append(child.id)
            except PlatformError as exc:
                child.status = EntityStatus.FAILED
                child.last_error = str(exc)
                logger.error("Variant %s could not be created: %s", child.name, exc)

        if not created:
            raise PlatformError(
                "no usable variants were produced",
                platform=campaign.platform,
                code="NO_VARIANTS",
            )
        action.payload = {**action.payload, "created_creative_ids": created}

    def _reallocate(self, action: OptimizationAction, entity: Campaign) -> None:
        """Redistribute a campaign's budget across its ad groups.

        Reallocation happens at the ad-group level because that is the lowest
        level either platform actually exposes a budget on. Individual ads share
        their parent's budget and are steered by pausing, not by funding.
        """
        if action.level is not EntityLevel.CAMPAIGN:
            raise PlatformError(
                "budget reallocation applies to a campaign's ad groups",
                platform=self._platform_of(action.level, entity),
                code="INVALID_LEVEL",
            )
        platform = entity.platform
        if platform is Platform.GOOGLE:
            raise PlatformError(
                "Google holds one budget per campaign, so its ad groups cannot "
                "be funded separately",
                platform=platform,
                code="UNSUPPORTED",
            )

        allocation = action.payload.get("allocation_micros", {})
        applied: dict[str, int] = {}
        for group_id, budget_micros in allocation.items():
            group = self.session.get(AdGroup, int(group_id))
            if group is None or group.campaign_id != entity.id:
                continue
            budget_micros = int(budget_micros)
            if group.external_id:
                self.client(platform).set_budget(
                    "ad_group", group.external_id, budget_micros
                )
            group.daily_budget_micros = budget_micros
            applied[str(group.id)] = budget_micros
        action.payload = {**action.payload, "applied_micros": applied}

    # ------------------------------------------------------------------
    def rebalance_ad_group(self, ad_group_id: int, since: date, until: date) -> dict:
        """Advisory: how an ad group's budget would split across its creatives.

        Neither platform lets you fund an individual ad, so this is guidance for
        deciding which creatives to keep running, not something that can be
        applied directly. `rebalance_campaign` is the applicable version.
        """
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
            return {"ad_group_id": ad_group_id, "allocation": {}, "allocation_micros": {}}

        windows = [
            load_performance(self.session, EntityLevel.CREATIVE, c.id, since, until)
            for c in creatives
        ]
        allocation = allocate_budget(windows, group.daily_budget_micros)
        return {
            "ad_group_id": ad_group_id,
            "applicable": False,
            "note": (
                "Advisory only: ad-level budgets do not exist on Meta or Google. "
                "Use it to decide which creatives stay active."
            ),
            "daily_budget_usd": micros_to_usd(group.daily_budget_micros),
            "allocation": {str(k): micros_to_usd(v) for k, v in allocation.items()},
            "allocation_micros": allocation,
        }

    def rebalance_campaign(self, campaign_id: int, since: date, until: date) -> dict:
        """Split a campaign's budget across its ad groups. Applicable on Meta."""
        campaign = self.session.get(Campaign, campaign_id)
        if campaign is None:
            raise ValueError(f"campaign {campaign_id} not found")
        groups = list(
            self.session.execute(
                select(AdGroup).where(
                    AdGroup.campaign_id == campaign_id,
                    AdGroup.status == EntityStatus.ACTIVE,
                )
            ).scalars()
        )
        if not groups:
            return {"campaign_id": campaign_id, "allocation": {}, "allocation_micros": {}}

        windows = [
            load_performance(self.session, EntityLevel.AD_GROUP, g.id, since, until)
            for g in groups
        ]
        total = campaign.daily_budget_micros or sum(
            g.daily_budget_micros for g in groups
        )
        allocation = allocate_budget(windows, total)
        return {
            "campaign_id": campaign_id,
            "applicable": campaign.platform is Platform.META,
            "daily_budget_usd": micros_to_usd(total),
            "allocation": {str(k): micros_to_usd(v) for k, v in allocation.items()},
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
                    # A conversion often arrives pending and is approved days
                    # later. Windowing on creation would skip it forever, so the
                    # window follows the last update instead.
                    Conversion.updated_at >= cutoff,
                )
            ).scalars()
        )
        if not pending:
            return {"uploaded": 0}
        if self.settings.dry_run:
            # Marking these as uploaded while sending nothing would filter them
            # out permanently: once dry run is switched off they would never be
            # retried and the platforms would never learn about those sales.
            logger.info(
                "Dry run: %s conversion(s) ready to upload, sending none.",
                len(pending),
            )
            return {"uploaded": 0, "pending": len(pending), "dry_run": True}

        # Track which conversions belong to which platform so one platform's
        # failure cannot force a re-upload of what another already accepted.
        by_platform: dict[Platform, list[dict]] = defaultdict(list)
        conversions_by_platform: dict[Platform, list[Conversion]] = defaultdict(list)
        for conversion in pending:
            click = self.session.execute(
                select(Click).where(Click.click_id == conversion.click_id)
            ).scalar_one_or_none()
            if click is None or not click.platform:
                continue
            entry = {
                "event_time": _to_epoch(conversion.occurred_at),
                "click_time": _to_epoch(click.created_at),
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
            conversions_by_platform[click.platform].append(conversion)

        uploaded = 0
        succeeded: list[str] = []
        failed: dict[str, str] = {}
        for platform, entries in by_platform.items():
            try:
                uploaded += self.client(platform).upload_conversions(entries)
            except PlatformError as exc:
                logger.error("Conversion upload failed for %s: %s", platform.value, exc)
                failed[platform.value] = str(exc)
                continue
            for conversion in conversions_by_platform[platform]:
                conversion.uploaded_to_platform = True
            succeeded.append(platform.value)
        self.session.commit()
        return {"uploaded": uploaded, "platforms": succeeded, "errors": failed}


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


def _to_epoch(value: datetime) -> int:
    """Unix seconds from a stored timestamp.

    Timestamps are persisted naive but mean UTC. Calling `.timestamp()` on a
    naive datetime makes Python read it in the host's local zone, which shifts
    every uploaded conversion by the server's offset and misattributes it to
    the wrong click.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


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
