"""End-to-end simulation of the whole platform.

Runs the real code path against the sandbox ad platform: seed an offer, launch
a structured test, simulate delivery day by day, feed network postbacks back
through the tracking layer, and let the optimizer manage the account. Nothing
here is mocked except the ad platform itself.

Run it with:

    python -m adgenie.demo --days 21
"""

from __future__ import annotations

import argparse
import random
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .core.launcher import CampaignLauncher, LaunchPlan
from .core.metrics import load_performance
from .core.orchestrator import Orchestrator
from .core.tracking import (
    TrackingContext,
    encode_subid,
    record_click,
    record_conversion,
)
from .db import Base
from .models import (
    AdGroup,
    Campaign,
    ConversionStatus,
    Creative,
    EntityLevel,
    EntityStatus,
    Offer,
    PayoutType,
    Platform,
)
from .money import fmt_usd, usd_to_micros
from .platforms.factory import reset_sandboxes
from .platforms.sandbox import SandboxPlatform

# How long after the click a conversion may still arrive.
LAG_HORIZON_DAYS = 21

# A realistic direct-response delay mix: most convert in-session, the rest
# trail off over days. Weights are relative.
_DELAY_BUCKETS: tuple[tuple[float, float, float], ...] = (
    (0.0, 1.0, 40.0),      # same session
    (1.0, 24.0, 30.0),     # same day
    (24.0, 72.0, 18.0),    # next couple of days
    (72.0, 168.0, 9.0),    # within the week
    (168.0, 504.0, 3.0),   # the long tail
)


def _sample_conversion_delay(rng: random.Random) -> float:
    """Hours between the click and the network reporting the conversion."""
    total = sum(weight for _, _, weight in _DELAY_BUCKETS)
    draw = rng.random() * total
    running = 0.0
    for low, high, weight in _DELAY_BUCKETS:
        running += weight
        if draw <= running:
            return rng.uniform(low, high)
    return _DELAY_BUCKETS[-1][1]


DEMO_OFFER = {
    "name": "CalmLeaf Sleep Support",
    "network": "clickbank",
    "network_offer_id": "calmleaf",
    "vertical": "supplements",
    "destination_url": "https://example-offer.test/calmleaf",
    "payout_type": PayoutType.CPA,
    "payout_micros": usd_to_micros(42.00),
    "expected_reversal_rate": 0.12,
    "product_description": (
        "A magnesium glycinate and L-theanine blend taken 30 minutes before bed, "
        "designed to support a consistent wind-down routine."
    ),
    "target_audience": "Adults 30-55 who keep an irregular schedule and want a simpler evening routine",
    "key_benefits": [
        "wind down without next-morning grogginess",
        "keep a consistent evening routine",
        "skip the guesswork on dosing",
    ],
    "proof_points": [
        "Third-party tested in a US facility",
        "60-day return window",
    ],
    "geo_targets": ["US", "CA"],
    "banned_claims": ["cures insomnia"],
    "is_regulated": True,
}


def seed_offer(session: Session) -> Offer:
    offer = Offer(**DEMO_OFFER)
    session.add(offer)
    session.commit()
    return offer


def simulate_conversions(
    session: Session,
    sandbox: SandboxPlatform,
    offer: Offer,
    day: date,
    rng: random.Random,
) -> int:
    """Turn simulated clicks into tracked clicks and network postbacks.

    This exercises the same tracking and attribution code that a live postback
    would, so the numbers the optimizer sees come through the real path.
    """
    created = 0
    occurred = datetime.combine(day, time(12, 0), tzinfo=timezone.utc)

    for (external_id, row_day), row in list(sandbox.insights.items()):
        if row_day != day or row.clicks <= 0:
            continue
        creative = (
            session.query(Creative).filter(Creative.external_id == external_id).first()
        )
        if creative is None:
            continue
        group = session.get(AdGroup, creative.ad_group_id)
        campaign = session.get(Campaign, group.campaign_id)
        subid = encode_subid(
            TrackingContext(
                offer_id=offer.id,
                campaign_id=campaign.id,
                ad_group_id=group.id,
                creative_id=creative.id,
                platform=campaign.platform,
            )
        )
        # Record every click, then convert the share the auction said converted.
        clicks = []
        for _ in range(row.clicks):
            click, _offer = record_click(
                session,
                subid,
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/605.1",
                ip=f"203.0.113.{rng.randint(1, 254)}",
                query_params={"fbclid": f"sim{rng.getrandbits(48):x}"},
            )
            click.created_at = occurred
            clicks.append(click)

        for click in rng.sample(clicks, min(int(row.conversions), len(clicks))):
            status = (
                ConversionStatus.REVERSED
                if rng.random() < offer.expected_reversal_rate
                else ConversionStatus.APPROVED
            )
            # Conversions do not land the instant the click happens. Simulating
            # them as instant would hide the censoring problem the optimizer has
            # to survive: on a real account the most recent days always look
            # worse than they are.
            delay_hours = _sample_conversion_delay(rng)
            converted_at = occurred + timedelta(hours=delay_hours)
            if converted_at > datetime.combine(day, time(23, 59), tzinfo=timezone.utc) + timedelta(
                days=LAG_HORIZON_DAYS
            ):
                continue
            record_conversion(
                session,
                network=offer.network,
                network_txn_id=f"{click.click_id}-{rng.getrandbits(24):x}",
                click_id=click.click_id,
                revenue_micros=offer.payout_micros,
                sale_amount_micros=usd_to_micros(97.0),
                status=status,
                occurred_at=converted_at,
            )
            created += 1
    session.commit()
    return created


def print_report(
    session: Session, since: date, until: date, as_of: datetime | None = None
) -> None:
    print("\n" + "=" * 96)
    print(f"PERFORMANCE  {since} .. {until}")
    print("=" * 96)
    header = (
        f"{'creative':<44}{'clicks':>7}{'spend':>11}{'conv':>6}"
        f"{'revenue':>11}{'ROAS':>7}{'P(win)':>8}"
    )
    print(header)
    print("-" * 96)

    totals = {"spend": 0, "revenue": 0, "clicks": 0, "conversions": 0}
    for creative in session.query(Creative).order_by(Creative.id):
        window = load_performance(
            session, EntityLevel.CREATIVE, creative.id, since, until, as_of=as_of
        )
        if not window.clicks and not window.spend_micros:
            continue
        totals["spend"] += window.spend_micros
        totals["revenue"] += window.revenue_micros
        totals["clicks"] += window.clicks
        totals["conversions"] += window.conversions
        label = f"{creative.angle:<16} {creative.status.value:<8} #{creative.id}"
        print(
            f"{label:<44}{window.clicks:>7}{fmt_usd(window.spend_micros):>11}"
            f"{window.conversions:>6}{fmt_usd(window.revenue_micros):>11}"
            f"{window.roas:>7.2f}{window.prob_profitable(1.0):>8.0%}"
        )
    print("-" * 96)
    profit = totals["revenue"] - totals["spend"]
    roas = totals["revenue"] / totals["spend"] if totals["spend"] else 0.0
    print(
        f"{'TOTAL':<44}{totals['clicks']:>7}{fmt_usd(totals['spend']):>11}"
        f"{totals['conversions']:>6}{fmt_usd(totals['revenue']):>11}{roas:>7.2f}"
    )
    print(f"{'PROFIT':<44}{fmt_usd(profit):>25}")
    print(
        f"\n  Conversions were simulated with a realistic reporting delay and "
        f"allowed {LAG_HORIZON_DAYS} days to settle,"
    )
    print("  so these totals are mature. The optimizer had to decide without that luxury.")


def run(
    days: int = 21,
    budget: float = 60.0,
    seed: int = 11,
    verbose: bool = True,
    database_url: str | None = None,
) -> dict:
    """Simulate `days` of a campaign against the sandbox.

    The demo builds its own settings and its own throwaway database rather than
    touching the configured ones. Reusing them would mean `adgenie demo` run
    against a real deployment drops that deployment's tables and leaves live
    spend enabled for the rest of the process.
    """
    base = get_settings()
    settings = base.model_copy(
        update={
            # Act on the sandbox, which is the only thing this touches.
            "dry_run": False,
            "database_url": database_url or "sqlite:///./adgenie-demo.db",
            "global_daily_budget_cap_usd": max(
                base.global_daily_budget_cap_usd, budget * 20
            ),
            # The demo's offer is fictional and its destination does not
            # resolve, by design: this runs with no network at all. Auditing
            # it would correctly refuse to launch and the demo would show
            # nothing. The auditor has its own tests.
            "audit_landing_pages": False,
        }
    )
    reset_sandboxes()

    engine = create_engine(
        settings.database_url, connect_args={"check_same_thread": False}
    )
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    rng = random.Random(seed)

    session = factory()
    offer = seed_offer(session)

    sandbox = SandboxPlatform(Platform.META, seed=seed)
    launcher = CampaignLauncher(session, settings=settings, platform_client=sandbox)
    plan = LaunchPlan(
        offer_id=offer.id,
        platform=Platform.META,
        daily_budget_usd=budget,
        angle_count=4,
        creatives_per_angle=2,
        geo_targets=["US"],
        start_paused=False,
    )
    result = launcher.launch(plan)
    if not result.ok:
        # campaign_id 0 is the sentinel for a launch that never happened.
        # Printing it as though something launched sends the reader looking
        # for delivery data that was never going to exist.
        raise RuntimeError(
            "the demo could not launch: " + "; ".join(result.errors or ["unknown"])
        )
    if verbose:
        print(f"Launched campaign {result.campaign_id}: "
              f"{len(result.creative_ids)} creatives live, "
              f"{len(result.blocked_creative_ids)} blocked by policy review.")
        for warning in result.warnings:
            print(f"  warning: {warning}")

    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: sandbox}
    )

    start = date.today() - timedelta(days=days)
    cycles: list[dict] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        sandbox.simulate_day(day)
        simulate_conversions(session, sandbox, offer, day, rng)
        orchestrator.sync_metrics(day, day)

        # Optimize twice a week, once enough history exists to be meaningful.
        if offset >= 6 and offset % 3 == 0:
            cycle = orchestrator.run_cycle(
                lookback_days=7, apply=True, today=day + timedelta(days=1)
            )
            cycles.append(cycle)
            if verbose and cycle["proposed"]:
                print(f"\n--- day {offset}: optimizer run {cycle['run_id']} ---")
                for action in cycle["actions"]:
                    print(
                        f"  {action['action']:<16} {action['level']}#{action['entity_id']:<4} "
                        f"[{action['rule']}]"
                    )
                    print(f"      {action['reason']}")

    # Let the trailing conversions land before reporting. Without this the
    # final table understates revenue for exactly the reason the optimizer has
    # to reason about: the last few days of clicks have not finished converting.
    settle_until = start + timedelta(days=days - 1 + LAG_HORIZON_DAYS)
    if verbose:
        print_report(
            session,
            start,
            settle_until,
            as_of=datetime.combine(settle_until, time(23, 59), tzinfo=timezone.utc),
        )

    summary = {
        "campaign_id": result.campaign_id,
        "creatives": len(result.creative_ids),
        "blocked": len(result.blocked_creative_ids),
        "cycles": len(cycles),
        "actions": sum(c["proposed"] for c in cycles),
        "applied": sum(c["applied"] for c in cycles),
    }
    session.close()
    engine.dispose()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AdGenie Pro simulation.")
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--budget", type=float, default=60.0)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument(
        "--database-url",
        default=None,
        help="where to build the throwaway demo database (it is dropped first)",
    )
    args = parser.parse_args()
    summary = run(
        days=args.days,
        budget=args.budget,
        seed=args.seed,
        database_url=args.database_url,
    )
    print(f"\n{summary}")


if __name__ == "__main__":
    main()
