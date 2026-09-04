"""Command-line interface.

    python -m adgenie.cli init
    python -m adgenie.cli offer-add --name "..." --url "..." --payout 40
    python -m adgenie.cli launch --offer 1 --platform meta --budget 50
    python -m adgenie.cli sync
    python -m adgenie.cli optimize --apply
    python -m adgenie.cli report
    python -m adgenie.cli demo --days 21
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from sqlalchemy import select

from .config import get_settings
from .core.launcher import CampaignLauncher, LaunchPlan
from .core.metrics import default_window, load_performance
from .core.orchestrator import Orchestrator
from .db import init_db, session_scope
from .models import Campaign, Creative, EntityLevel, Offer, PayoutType, Platform
from .money import fmt_usd, usd_to_micros


def cmd_init(args) -> int:
    init_db()
    settings = get_settings()
    print(f"Database ready at {settings.database_url}")
    print(f"Dry run: {settings.dry_run}")
    print(f"Meta: {'connected' if settings.has_meta else 'sandbox'}")
    print(f"Google: {'connected' if settings.has_google else 'sandbox'}")
    print(f"Copywriter: {'claude' if settings.has_copywriter_llm else 'template'}")
    return 0


def cmd_offer_add(args) -> int:
    init_db()
    with session_scope() as session:
        offer = Offer(
            name=args.name,
            destination_url=args.url,
            network=args.network,
            vertical=args.vertical,
            payout_type=PayoutType(args.payout_type),
            payout_micros=usd_to_micros(args.payout),
            expected_reversal_rate=args.reversal_rate,
            product_description=args.description or "",
            target_audience=args.audience or "",
            key_benefits=args.benefit or [],
            proof_points=args.proof or [],
            is_regulated=args.regulated,
        )
        session.add(offer)
        session.flush()
        print(f"Created offer {offer.id}: {offer.name}")
        print(f"  Expected value per conversion: {fmt_usd(offer.expected_value_micros())}")
    return 0


def cmd_offers(args) -> int:
    init_db()
    with session_scope() as session:
        offers = session.execute(select(Offer).order_by(Offer.id)).scalars().all()
        if not offers:
            print("No offers yet. Add one with: adgenie.cli offer-add")
            return 0
        for offer in offers:
            print(
                f"{offer.id:>4}  {offer.name[:40]:<40} {offer.network:<12} "
                f"{fmt_usd(offer.payout_micros):>10}  {offer.vertical}"
            )
    return 0


def cmd_launch(args) -> int:
    init_db()
    settings = get_settings()
    with session_scope() as session:
        result = CampaignLauncher(session, settings=settings).launch(
            LaunchPlan(
                offer_id=args.offer,
                platform=Platform(args.platform),
                daily_budget_usd=args.budget,
                angle_count=args.angles,
                creatives_per_angle=args.per_angle,
                keywords=args.keyword or [],
                geo_targets=args.geo or [],
                start_paused=not args.start_active,
                research_market=args.research,
                research_term=args.research_term,
                generate_media=args.with_media,
            )
        )
    print(json.dumps(result.as_dict(), indent=2))
    if result.warnings:
        print("\nWarnings:", file=sys.stderr)
        for warning in result.warnings:
            print(f"  {warning}", file=sys.stderr)
    return 1 if result.errors else 0


def cmd_sync(args) -> int:
    init_db()
    until = date.today() - timedelta(days=1)
    since = until - timedelta(days=args.days - 1)
    with session_scope() as session:
        summary = Orchestrator(session).sync_metrics(since, until)
    print(json.dumps(summary, indent=2))
    return 1 if summary.get("errors") else 0


def cmd_optimize(args) -> int:
    init_db()
    settings = get_settings()
    if args.apply and settings.dry_run:
        print(
            "Refusing to apply: DRY_RUN is on. Set DRY_RUN=false to let the "
            "optimizer change a live account.",
            file=sys.stderr,
        )
        return 2
    with session_scope() as session:
        result = Orchestrator(session, settings=settings).run_cycle(
            lookback_days=args.days, apply=args.apply
        )

    print(
        f"Run {result['run_id']}: evaluated {result['evaluated']}, "
        f"proposed {result['proposed']}, applied {result['applied']}"
        f"{' (dry run)' if result['dry_run'] else ''}"
    )
    for action in result["actions"]:
        flag = " [needs approval]" if action["requires_approval"] else ""
        print(f"\n  {action['action']} on {action['level']} #{action['entity_id']}{flag}")
        print(f"    rule: {action['rule']}")
        print(f"    {action['reason']}")
    return 0


def cmd_report(args) -> int:
    init_db()
    since, until = default_window(args.days)
    with session_scope() as session:
        creatives = session.execute(select(Creative).order_by(Creative.id)).scalars().all()
        header = (
            f"{'id':>4}  {'angle':<18}{'status':<10}{'clicks':>8}{'spend':>11}"
            f"{'conv':>6}{'revenue':>11}{'ROAS':>7}{'P(win)':>8}"
        )
        print(f"\n{since} .. {until}")
        print(header)
        print("-" * len(header))

        spend = revenue = clicks = conversions = 0
        for creative in creatives:
            window = load_performance(
                session, EntityLevel.CREATIVE, creative.id, since, until
            )
            if not (window.clicks or window.spend_micros):
                continue
            spend += window.spend_micros
            revenue += window.revenue_micros
            clicks += window.clicks
            conversions += window.conversions
            print(
                f"{creative.id:>4}  {creative.angle[:17]:<18}{creative.status.value:<10}"
                f"{window.clicks:>8}{fmt_usd(window.spend_micros):>11}"
                f"{window.conversions:>6}{fmt_usd(window.revenue_micros):>11}"
                f"{window.roas:>7.2f}{window.prob_profitable(1.0):>8.0%}"
            )
        print("-" * len(header))
        roas = revenue / spend if spend else 0.0
        print(
            f"{'':>4}  {'TOTAL':<28}{clicks:>8}{fmt_usd(spend):>11}"
            f"{conversions:>6}{fmt_usd(revenue):>11}{roas:>7.2f}"
        )
        print(f"{'':>4}  {'PROFIT':<28}{fmt_usd(revenue - spend):>25}")
    return 0


def cmd_research(args) -> int:
    init_db()
    settings = get_settings()
    from .research.ad_library import commercial_ads_available
    from .research.service import MarketResearcher

    countries = args.country or settings.ad_library_country_codes
    if not commercial_ads_available(countries):
        print(
            "Warning: none of "
            + ", ".join(countries)
            + " is an EU or UK market, so the archive carries only political and "
            "issue ads there. Add --country GB (or an EU code) to see commercial "
            "competitors.",
            file=sys.stderr,
        )
    if not settings.has_ad_library:
        print(
            "META_ACCESS_TOKEN is not set; the Ad Library needs a token with "
            "ads_read.",
            file=sys.stderr,
        )
        return 2

    with session_scope() as session:
        brief = MarketResearcher(session, settings).research(
            args.term, countries=countries, vertical=args.vertical,
            active_only=not args.include_inactive, max_pages=args.pages,
        )

    print(f"\n{args.term}  ({brief.confidence} confidence)")
    print(
        f"  {brief.ads_seen} ads from {brief.advertisers} advertisers, "
        f"{brief.proven_ads} running {settings.ad_library_proven_days}+ days"
    )
    if brief.angle_ranking:
        print("\n  Arguments surviving longest:")
        for angle, share in brief.angle_ranking:
            bar = "#" * max(1, int(share * 30))
            print(f"    {angle:<20}{share:>6.0%}  {bar}")
    if brief.top_ads:
        print("\n  Longest-running ads:")
        for ad in brief.top_ads[:5]:
            live = "live" if ad["still_running"] else "ended"
            print(
                f"    {ad['days_running']:>4}d {live:<6} {ad['angle']:<18}"
                f"{ad['page_name'][:32]}"
            )
    print("\n  Direction for the copywriter:")
    for note in brief.to_prompt_notes():
        print(f"    - {note}")
    for warning in brief.warnings:
        print(f"\n  Note [{warning['code']}]: {warning['message']}")
    return 0


def cmd_research_sweep(args) -> int:
    init_db()
    settings = get_settings()
    if not settings.has_ad_library:
        print("META_ACCESS_TOKEN is not set.", file=sys.stderr)
        return 2
    from .research.service import MarketResearcher

    with session_scope() as session:
        researcher = MarketResearcher(session, settings)
        updated = researcher.sweep_for_retirements(
            vertical=args.vertical, countries=args.country
        )
        retired = researcher.retired_ads(args.vertical)

    print(f"Updated {updated} observation(s).")
    if retired:
        print("\nAds that stopped quickly (a negative signal):")
        for ad in retired[:15]:
            print(
                f"  {ad['days_running']:>4}d  {ad['angle'] or 'unknown':<18}"
                f"{ad['page_name'][:36]}"
            )
    else:
        print("No short-lived retirements recorded yet.")
    return 0


def cmd_media(args) -> int:
    init_db()
    settings = get_settings()
    from .media.studio import MediaStudio

    with session_scope() as session:
        creative = session.get(Creative, args.creative)
        if creative is None:
            print(f"creative {args.creative} not found", file=sys.stderr)
            return 1
        assets = MediaStudio(session, settings).generate_for_creative(
            creative, placements=args.placement or None, kind=args.kind
        )
        for asset in assets:
            location = asset.local_path or asset.error or ""
            print(
                f"  {asset.status.value:<10}{(asset.extra or {}).get('placement',''):<18}"
                f"{asset.aspect_ratio:<7}{location}"
            )
        return 0 if any(a.status.value == "ready" for a in assets) else 1


def cmd_demo(args) -> int:
    from .demo import run

    # The demo builds and drops its own database, never the configured one.
    run(
        days=args.days,
        budget=args.budget,
        seed=args.seed,
        database_url=args.database_url,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adgenie",
        description="Write, launch and optimize affiliate ads on Meta and Google.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and show configuration").set_defaults(
        func=cmd_init
    )

    add = sub.add_parser("offer-add", help="register an affiliate offer")
    add.add_argument("--name", required=True)
    add.add_argument("--url", required=True, help="the advertiser's landing page")
    add.add_argument("--payout", type=float, required=True, help="payout in USD")
    add.add_argument("--payout-type", default="cpa", choices=[p.value for p in PayoutType])
    add.add_argument("--network", default="manual")
    add.add_argument("--vertical", default="general")
    add.add_argument("--reversal-rate", type=float, default=0.10)
    add.add_argument("--description")
    add.add_argument("--audience")
    add.add_argument("--benefit", action="append", help="repeatable")
    add.add_argument("--proof", action="append", help="repeatable")
    add.add_argument("--regulated", action="store_true")
    add.set_defaults(func=cmd_offer_add)

    sub.add_parser("offers", help="list offers").set_defaults(func=cmd_offers)

    research = sub.add_parser(
        "research", help="scan the Meta Ad Library for what is still running"
    )
    research.add_argument("--term", required=True, help="what to search for")
    research.add_argument(
        "--country", action="append",
        help="repeatable; commercial ads are EU and UK only",
    )
    research.add_argument("--vertical", default="")
    research.add_argument("--pages", type=int, default=3)
    research.add_argument("--include-inactive", action="store_true")
    research.set_defaults(func=cmd_research)

    sweep = sub.add_parser(
        "research-sweep",
        help="re-scan stored searches including stopped ads, so retirements show up",
    )
    sweep.add_argument("--vertical", default="")
    sweep.add_argument("--country", action="append")
    sweep.set_defaults(func=cmd_research_sweep)

    media = sub.add_parser("media", help="generate imagery for a creative")
    media.add_argument("--creative", type=int, required=True)
    media.add_argument("--kind", default="image", choices=["image", "video"])
    media.add_argument("--placement", action="append", help="repeatable")
    media.set_defaults(func=cmd_media)

    launch = sub.add_parser("launch", help="build and launch a structured test")
    launch.add_argument("--offer", type=int, required=True)
    launch.add_argument("--platform", required=True, choices=[p.value for p in Platform])
    launch.add_argument("--budget", type=float, required=True, help="daily budget in USD")
    launch.add_argument("--angles", type=int, default=3)
    launch.add_argument("--per-angle", type=int, default=1)
    launch.add_argument("--keyword", action="append")
    launch.add_argument("--geo", action="append")
    launch.add_argument(
        "--start-active", action="store_true", help="start spending immediately"
    )
    launch.add_argument(
        "--research", action="store_true",
        help="scan the Ad Library first and feed the patterns to the copywriter",
    )
    launch.add_argument("--research-term", default="")
    launch.add_argument(
        "--with-media", action="store_true", help="generate imagery for each ad"
    )
    launch.set_defaults(func=cmd_launch)

    sync = sub.add_parser("sync", help="pull delivery data from the platforms")
    sync.add_argument("--days", type=int, default=7)
    sync.set_defaults(func=cmd_sync)

    optimize = sub.add_parser("optimize", help="evaluate and act")
    optimize.add_argument("--days", type=int, default=None)
    optimize.add_argument(
        "--apply", action="store_true", help="apply decisions instead of proposing them"
    )
    optimize.set_defaults(func=cmd_optimize)

    report = sub.add_parser("report", help="performance by creative")
    report.add_argument("--days", type=int, default=7)
    report.set_defaults(func=cmd_report)

    demo = sub.add_parser("demo", help="run the full pipeline against the simulator")
    demo.add_argument("--days", type=int, default=21)
    demo.add_argument("--budget", type=float, default=60.0)
    demo.add_argument("--seed", type=int, default=11)
    demo.add_argument(
        "--database-url", default=None, help="throwaway demo database location"
    )
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
