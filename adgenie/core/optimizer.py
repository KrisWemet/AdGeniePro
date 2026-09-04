"""The optimizer: decide what to do with each entity, and say why.

Design commitments, in priority order:

1. **Never act on a point estimate.** Every kill and every scale is gated on a
   credible interval, not on the observed rate. Three unlucky days should not
   kill a winner and one lucky day should not scale a loser.
2. **Every decision carries its evidence.** Each action records the rule that
   fired, the metrics it saw and the confidence it had. An optimizer you cannot
   audit is one you will eventually turn off.
3. **Bounded blast radius.** Budget moves are capped per cycle, respect a
   cooldown, and anything above a configured size is proposed for human
   approval rather than applied.
4. **The safe direction is asymmetric.** Pausing a good ad costs opportunity;
   scaling a bad one costs cash. So the bar for scaling is higher than the bar
   for pausing.

The engine is pure: it reads `PerformanceWindow` objects and returns
`Decision` objects. Nothing here touches an ad account, which is what makes it
testable.
"""

from __future__ import annotations

import logging
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import Settings, get_settings
from ..models import ActionType, EntityLevel
from ..money import micros_to_usd
from .metrics import PerformanceWindow, apply_pooled_prior
from .stats import prob_b_beats_a, thompson_sample_beta

logger = logging.getLogger(__name__)

__all__ = ["Decision", "OptimizerPolicy", "Optimizer", "allocate_budget"]


@dataclass
class Decision:
    level: EntityLevel
    entity_id: int
    action: ActionType
    rule: str
    reason: str
    confidence: float = 0.0
    evidence: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)
    requires_approval: bool = False

    def as_dict(self) -> dict:
        data = asdict(self)
        data["level"] = self.level.value
        data["action"] = self.action.value
        return data


@dataclass
class OptimizerPolicy:
    """Thresholds. Every number here is a business decision, not a constant."""

    target_roas: float = 1.30
    # Below this the entity is losing money after the affiliate's own margin.
    floor_roas: float = 1.00
    credible_level: float = 0.90
    min_clicks_to_judge: int = 30
    min_spend_to_judge_micros: int = 10_000_000  # $10
    # Zero-conversion kill: spend beyond N x payout with nothing to show.
    kill_payout_multiple: float = 1.5
    # Confidence that the true rate clears breakeven, below which we stop.
    kill_confidence: float = 0.10
    # Confidence required before increasing spend.
    scale_confidence: float = 0.80
    scale_step: float = 0.20
    throttle_step: float = 0.25
    max_daily_budget_micros: int = 0  # 0 means "no explicit cap"
    min_daily_budget_micros: int = 5_000_000  # $5
    cooldown_hours: int = 12
    # Meta-specific: audience saturation.
    frequency_ceiling: float = 3.2
    # A creative whose CTR has decayed this far from its own opening week.
    ctr_decay_threshold: float = 0.35
    auto_apply_budget_ceiling_micros: int = 50_000_000

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "OptimizerPolicy":
        settings = settings or get_settings()
        return cls(
            target_roas=settings.target_roas,
            credible_level=settings.optimizer_credible_level,
            min_clicks_to_judge=settings.optimizer_min_clicks,
            kill_payout_multiple=settings.kill_payout_multiple,
            scale_step=settings.scale_step,
            throttle_step=settings.throttle_step,
            cooldown_hours=settings.action_cooldown_hours,
            auto_apply_budget_ceiling_micros=int(
                settings.auto_apply_budget_ceiling_usd * 1_000_000
            ),
        )


class Optimizer:
    """Evaluates entities and emits decisions."""

    def __init__(
        self,
        policy: OptimizerPolicy | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.policy = policy or OptimizerPolicy()
        self.rng = rng or random.Random()

    # ------------------------------------------------------------------
    # single-entity evaluation
    # ------------------------------------------------------------------
    def evaluate(
        self,
        window: PerformanceWindow,
        *,
        lifetime: PerformanceWindow | None = None,
        is_active: bool = True,
        compliance_blocked: bool = False,
        last_action_at: datetime | None = None,
        opening_ctr: float | None = None,
        now: datetime | None = None,
    ) -> Decision:
        """Return exactly one decision. Rules are ordered by severity.

        `window` is the recent window that scaling and throttling read, because
        recent performance is what predicts tomorrow. `lifetime` is everything
        the entity has ever done, and the zero-conversion kill reads that
        instead: an ad that has burned money for three weeks without a single
        sale should not get a fresh start every Monday because a rolling window
        forgot the evidence.
        """
        p = self.policy
        evidence = window.as_dict()
        if lifetime is not None:
            evidence["lifetime"] = {
                "clicks": lifetime.clicks,
                "spend_usd": micros_to_usd(lifetime.spend_micros),
                "conversions": lifetime.conversions,
                "revenue_usd": micros_to_usd(lifetime.revenue_micros),
                "roas": round(lifetime.roas, 4),
            }

        # 1. Compliance overrides everything. A policy-violating ad is an
        #    account risk, not a performance question.
        if compliance_blocked and is_active:
            return Decision(
                level=window.level,
                entity_id=window.entity_id,
                action=ActionType.PAUSE,
                rule="compliance_block",
                reason=(
                    "Creative has a blocking policy finding. Pausing to protect "
                    "the ad account."
                ),
                confidence=1.0,
                evidence=evidence,
            )

        if not is_active:
            return self._no_action(window, "inactive", "Entity is not running.")

        # 2. Cooldown. Budget changes need time to take effect before the next
        #    one; stacking them produces oscillation, not learning.
        if self._in_cooldown(last_action_at, now):
            return self._no_action(
                window,
                "cooldown",
                f"Acted within the last {p.cooldown_hours}h; waiting for delivery.",
                evidence=evidence,
            )

        # 3. Not enough data to say anything.
        if window.spend_micros < p.min_spend_to_judge_micros and window.clicks < p.min_clicks_to_judge:
            return self._no_action(
                window,
                "learning",
                (
                    f"Still learning: {window.clicks} clicks and "
                    f"{micros_to_usd(window.spend_micros):.2f} USD spent."
                ),
                evidence=evidence,
            )

        # 4. Zero-conversion kill. The classic affiliate money pit: an ad that
        #    gets clicks and never converts. Judged on lifetime evidence so a
        #    rolling window cannot keep resetting the case against it, and
        #    gated on the posterior so a cheap offer with few clicks is not
        #    killed prematurely.
        basis = lifetime if lifetime is not None else window
        if basis.conversions == 0 and basis.offer_payout_micros:
            spend_multiple = basis.spend_micros / basis.offer_payout_micros
            probability = basis.prob_profitable(p.floor_roas)
            confidence = 1.0 - probability
            if spend_multiple >= p.kill_payout_multiple and probability <= p.kill_confidence:
                scope = "lifetime" if lifetime is not None else "this window"
                return Decision(
                    level=window.level,
                    entity_id=window.entity_id,
                    action=ActionType.PAUSE,
                    rule="zero_conversion_kill",
                    reason=(
                        f"Spent {spend_multiple:.1f}x the offer payout across "
                        f"{basis.clicks} clicks ({scope}) with no conversions. "
                        f"Probability of reaching breakeven is {probability:.1%}."
                    ),
                    confidence=round(confidence, 4),
                    evidence=evidence,
                )

        # 5. Losing money with enough evidence to be sure.
        if window.clicks >= p.min_clicks_to_judge:
            roas_ci = window.roas_interval(p.credible_level)
            if window.roas < p.floor_roas and roas_ci.upper < p.floor_roas:
                return Decision(
                    level=window.level,
                    entity_id=window.entity_id,
                    action=ActionType.PAUSE,
                    rule="unprofitable_kill",
                    reason=(
                        f"ROAS {window.roas:.2f} with a {int(p.credible_level * 100)}% "
                        f"upper bound of {roas_ci.upper:.2f}, both below breakeven. "
                        f"Lost {micros_to_usd(-window.profit_micros):.2f} USD in this window."
                    ),
                    confidence=round(p.credible_level, 4),
                    evidence=evidence,
                )

            # 6. Scale. The bar is deliberately high: the lower bound of the
            #    credible interval must clear breakeven, not just the mean.
            if (
                window.roas >= p.target_roas
                and roas_ci.lower >= p.floor_roas
                and window.prob_profitable(p.target_roas) >= p.scale_confidence
            ):
                return self._budget_decision(
                    window,
                    direction=1,
                    rule="scale_winner",
                    reason=(
                        f"ROAS {window.roas:.2f} against a {p.target_roas:.2f} target, "
                        f"with a {int(p.credible_level * 100)}% lower bound of "
                        f"{roas_ci.lower:.2f}. Profit "
                        f"{micros_to_usd(window.profit_micros):.2f} USD."
                    ),
                    confidence=round(window.prob_profitable(p.target_roas), 4),
                    evidence=evidence,
                )

            # 7. Marginal. Profitable but under target: cut spend rather than
            #    kill, because the angle may still be worth keeping alive.
            if p.floor_roas <= window.roas < p.target_roas and roas_ci.upper < p.target_roas:
                return self._budget_decision(
                    window,
                    direction=-1,
                    rule="throttle_marginal",
                    reason=(
                        f"ROAS {window.roas:.2f} sits between breakeven and the "
                        f"{p.target_roas:.2f} target, and the upper bound "
                        f"{roas_ci.upper:.2f} does not reach it. Reducing exposure."
                    ),
                    confidence=round(p.credible_level, 4),
                    evidence=evidence,
                )

        # 8. Creative fatigue. Delivery is fine and economics are fine, but the
        #    audience has seen it too often. Refresh rather than pause.
        if window.frequency >= p.frequency_ceiling:
            return Decision(
                level=window.level,
                entity_id=window.entity_id,
                action=ActionType.GENERATE_VARIANTS,
                rule="frequency_fatigue",
                reason=(
                    f"Frequency {window.frequency:.1f} is above the "
                    f"{p.frequency_ceiling:.1f} ceiling. The audience is saturated; "
                    "new creative will cost less per click than more budget."
                ),
                confidence=0.7,
                evidence=evidence,
                payload={"variants": 3},
            )

        if (
            opening_ctr
            and window.ctr > 0
            and (opening_ctr - window.ctr) / opening_ctr >= p.ctr_decay_threshold
            and window.clicks >= p.min_clicks_to_judge
        ):
            return Decision(
                level=window.level,
                entity_id=window.entity_id,
                action=ActionType.GENERATE_VARIANTS,
                rule="ctr_decay",
                reason=(
                    f"Click-through rate fell from {opening_ctr:.2%} to "
                    f"{window.ctr:.2%}, a "
                    f"{(opening_ctr - window.ctr) / opening_ctr:.0%} decay. "
                    "The creative is wearing out."
                ),
                confidence=0.65,
                evidence=evidence,
                payload={"variants": 3},
            )

        return self._no_action(
            window,
            "hold",
            (
                f"ROAS {window.roas:.2f} is within tolerance and the evidence is "
                "not yet strong enough to move budget."
            ),
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _in_cooldown(
        self, last_action_at: datetime | None, now: datetime | None = None
    ) -> bool:
        """`now` is injectable so back-tests advance simulated time, not wall time."""
        if last_action_at is None:
            return False
        if last_action_at.tzinfo is None:
            last_action_at = last_action_at.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now - last_action_at < timedelta(hours=self.policy.cooldown_hours)

    def _no_action(
        self,
        window: PerformanceWindow,
        rule: str,
        reason: str,
        evidence: dict | None = None,
    ) -> Decision:
        return Decision(
            level=window.level,
            entity_id=window.entity_id,
            action=ActionType.NO_ACTION,
            rule=rule,
            reason=reason,
            evidence=evidence or {},
        )

    def _budget_decision(
        self,
        window: PerformanceWindow,
        direction: int,
        rule: str,
        reason: str,
        confidence: float,
        evidence: dict,
    ) -> Decision:
        p = self.policy
        current = window.daily_budget_micros or p.min_daily_budget_micros
        step = p.scale_step if direction > 0 else -p.throttle_step
        proposed = int(current * (1 + step))

        cap = p.max_daily_budget_micros
        if direction > 0:
            if cap:
                proposed = min(proposed, cap)
            proposed = max(proposed, p.min_daily_budget_micros)
        else:
            # The floor must never turn a throttle into an increase. An entity
            # already at or below the minimum has nothing left to cut, so the
            # decision collapses to no action rather than a budget rise.
            proposed = max(proposed, p.min_daily_budget_micros)
            if proposed >= current:
                return self._no_action(
                    window,
                    f"{rule}_at_floor",
                    f"{reason} Budget is already at the minimum, so there is "
                    "nothing to reduce.",
                    evidence=evidence,
                )

        if proposed == current:
            return self._no_action(
                window,
                f"{rule}_capped",
                f"{reason} Budget is already at its limit.",
                evidence=evidence,
            )

        delta = proposed - current
        requires_approval = abs(delta) > p.auto_apply_budget_ceiling_micros

        return Decision(
            level=window.level,
            entity_id=window.entity_id,
            action=ActionType.INCREASE_BUDGET
            if direction > 0
            else ActionType.DECREASE_BUDGET,
            rule=rule,
            reason=(
                f"{reason} Moving the daily budget from "
                f"{micros_to_usd(current):.2f} to {micros_to_usd(proposed):.2f} USD."
            ),
            confidence=confidence,
            evidence=evidence,
            payload={
                "from_micros": current,
                "to_micros": proposed,
                "delta_micros": delta,
            },
            requires_approval=requires_approval,
        )

    # ------------------------------------------------------------------
    # portfolio-level allocation
    # ------------------------------------------------------------------
    def rank_creatives(
        self, windows: list[PerformanceWindow]
    ) -> list[tuple[PerformanceWindow, float]]:
        """Rank by a Thompson sample rather than by observed conversion rate.

        Ranking by the observed rate hands the whole budget to whichever
        creative got lucky first. Sampling from each posterior lets a creative
        with less data but a wide interval still win occasionally, which is
        what turns a ranking into an actual exploration policy.
        """
        scored = []
        for window in windows:
            sample = thompson_sample_beta(
                window.conversions,
                window.clicks,
                prior_a=window.prior_a,
                prior_b=window.prior_b,
                rng=self.rng,
            )
            value = sample * window.offer_payout_micros
            scored.append((window, value))
        return sorted(scored, key=lambda pair: pair[1], reverse=True)

    def compare(
        self, control: PerformanceWindow, variant: PerformanceWindow
    ) -> dict:
        """Probability the variant genuinely beats the control."""
        probability = prob_b_beats_a(
            control.conversions,
            control.clicks,
            variant.conversions,
            variant.clicks,
            prior_a=control.prior_a,
            prior_b=control.prior_b,
            rng=self.rng,
        )
        return {
            "control_id": control.entity_id,
            "variant_id": variant.entity_id,
            "control_cvr": round(control.cvr, 5),
            "variant_cvr": round(variant.cvr, 5),
            "prob_variant_better": round(probability, 4),
            "decisive": probability >= 0.95 or probability <= 0.05,
        }


def allocate_budget(
    windows: list[PerformanceWindow],
    total_budget_micros: int,
    *,
    min_share: float = 0.05,
    exploration_floor_micros: int = 5_000_000,
    rng: random.Random | None = None,
    samples: int = 500,
    use_pooled_prior: bool = True,
) -> dict[int, int]:
    """Split a budget across creatives by probability of being the best.

    Each creative's share is the fraction of Monte Carlo draws in which its
    posterior sample is the highest. A guaranteed floor keeps unproven
    creatives funded long enough to actually be measured, because a creative
    starved of budget never produces the data that would justify funding it.
    """
    if not windows or total_budget_micros <= 0:
        return {}
    rng = rng or random.Random()
    if use_pooled_prior:
        apply_pooled_prior(windows)

    wins = {w.entity_id: 0 for w in windows}
    for _ in range(samples):
        best_id, best_value = None, -1.0
        for window in windows:
            sample = thompson_sample_beta(
                window.conversions,
                window.clicks,
                prior_a=window.prior_a,
                prior_b=window.prior_b,
                rng=rng,
            )
            value = sample * max(1, window.offer_payout_micros)
            if value > best_value:
                best_id, best_value = window.entity_id, value
        if best_id is not None:
            wins[best_id] += 1

    floor_total = min(
        total_budget_micros, exploration_floor_micros * len(windows)
    )
    discretionary = total_budget_micros - floor_total
    floor_each = floor_total // len(windows)

    allocation: dict[int, int] = {}
    for window in windows:
        share = wins[window.entity_id] / samples
        share = max(share, min_share)
        allocation[window.entity_id] = floor_each + int(discretionary * share)

    # Normalise so rounding never overspends the budget.
    total = sum(allocation.values())
    if total > total_budget_micros and total > 0:
        scale = total_budget_micros / total
        allocation = {k: int(v * scale) for k, v in allocation.items()}
    return allocation
