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
                check_landing_page=False if args.skip_landing_check else None,
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


def cmd_landing(args) -> int:
    init_db()
    from .core.destination import DestinationMonitor
    from .core.landing import audit_landing_page

    if args.sweep:
        with session_scope() as session:
            monitor = DestinationMonitor(session)
            summary = monitor.sweep(max_age_hours=args.max_age_hours)
            print(
                f"Checked {summary['checked']} destination(s), "
                f"skipped {summary['skipped']} still fresh."
            )
            for entry in summary["changed"]:
                print(f"  CHANGED  offer {entry['offer_id']}  {entry['name']}")
            for entry in summary["blocking"]:
                print(
                    f"  BLOCKING offer {entry['offer_id']}  {entry['name']}: "
                    + ", ".join(entry["findings"])
                )
            if args.pause and summary["blocking"]:
                from .core.orchestrator import Orchestrator

                paused = monitor.pause_offenders(
                    summary, orchestrator=Orchestrator(session)
                )
                print(f"  Paused {len(paused)} campaign(s).")
            if not summary["changed"] and not summary["blocking"]:
                print("  Nothing changed and nothing blocking.")
        return 0

    if args.offer:
        with session_scope() as session:
            offer = session.get(Offer, args.offer)
            if offer is None:
                print(f"offer {args.offer} not found", file=sys.stderr)
                return 1
            check = DestinationMonitor(session).check_offer(offer)
            report = check.report
            changed = check.content_changed
    elif args.url:
        report = audit_landing_page(args.url).as_dict()
        changed = False
    else:
        print("give --offer, --url or --sweep", file=sys.stderr)
        return 2

    print(f"\n{report['url']}")
    if report.get("final_url") and report["final_url"] != report["url"]:
        print(f"  lands on {report['final_url']} after {report['redirect_hops']} hop(s)")
    print(f"  {report['verdict'].upper()}  score {report['score']}")
    if changed:
        print("  The page has changed since the last check.")

    for severity in ("block", "warn", "info"):
        rows = [f for f in report["findings"] if f["severity"] == severity]
        if not rows:
            continue
        print(f"\n  {severity.upper()}")
        for finding in rows:
            print(f"    {finding['code']}: {finding['message']}")
            if finding.get("suggestion"):
                print(f"      {finding['suggestion']}")
    if not report["findings"]:
        print("  Nothing to flag.")
    return 0 if report["verdict"] != "block" else 1


def cmd_funnel(args) -> int:
    init_db()
    from .core.ltv import fit_lead_value, offer_prior_micros
    from .models import FunnelStep, FunnelStepKind

    with session_scope() as session:
        offer = session.get(Offer, args.offer)
        if offer is None:
            print(f"offer {args.offer} not found", file=sys.stderr)
            return 1

        if args.step:
            # Parse everything before deleting anything. This replaces the whole
            # funnel, so failing halfway would leave it truncated and the error
            # message would be the only sign.
            parsed: list[tuple[str, FunnelStepKind, float]] = []
            for raw in args.step:
                parts = raw.split(":")
                if len(parts) < 2:
                    print(
                        f"--step needs 'key:kind[:value]', got '{raw}'", file=sys.stderr
                    )
                    return 2
                try:
                    step_kind = FunnelStepKind(parts[1])
                except ValueError:
                    print(
                        f"unknown step kind '{parts[1]}'; expected one of "
                        + ", ".join(k.value for k in FunnelStepKind),
                        file=sys.stderr,
                    )
                    return 2
                try:
                    value = float(parts[2]) if len(parts) > 2 else 0.0
                except ValueError:
                    print(f"step value must be a number, got '{parts[2]}'", file=sys.stderr)
                    return 2
                parsed.append((parts[0], step_kind, value))

            keys = [key for key, _, _ in parsed]
            if len(keys) != len(set(keys)):
                print("step keys must be unique within a funnel", file=sys.stderr)
                return 2

            for existing in list(offer.funnel_steps):
                session.delete(existing)
            session.flush()
            for index, (key, step_kind, value) in enumerate(parsed):
                session.add(
                    FunnelStep(
                        offer_id=offer.id, key=key,
                        name=key.replace("_", " ").title(), kind=step_kind,
                        position=index, value_micros=usd_to_micros(value),
                    )
                )
            session.flush()
            session.refresh(offer)

        if not offer.funnel_steps:
            print(f"{offer.name} sends traffic straight to the offer (no funnel).")
            return 0

        print(f"\n{offer.name}")
        for step in offer.funnel_steps:
            lead = " captures lead" if step.captures_lead else ""
            print(
                f"  {step.position}. {step.key:<14}{step.kind.value:<10}"
                f"{fmt_usd(step.value_micros):>10}{lead}"
            )

        model = fit_lead_value(
            session, offer.id, prior_micros=offer_prior_micros(session, offer.id)
        )
        print(
            f"\n  Value per lead: {fmt_usd(model.mean_micros)} "
            f"({fmt_usd(model.lower_micros)} to {fmt_usd(model.upper_micros)})"
        )
        if model.fitted:
            print(
                f"  Measured from {model.mature_sample_size} leads old enough to "
                "have finished earning."
            )
        else:
            print(
                f"  Assumed from the step values; only {model.mature_sample_size} "
                "mature leads so far. The optimizer spends against the lower bound."
            )
    return 0


def cmd_segments(args) -> int:
    init_db()
    from .core.orchestrator import Orchestrator
    from .models import AdGroup

    since, until = default_window(args.days)
    with session_scope() as session:
        groups = (
            [session.get(AdGroup, args.ad_group)]
            if args.ad_group
            else list(session.query(AdGroup).all())
        )
        for group in [g for g in groups if g is not None]:
            try:
                report = Orchestrator(session).segment_report(
                    group.id, since, until, args.dimension
                )
            except Exception as exc:
                print(f"ad group {group.id}: {exc}", file=sys.stderr)
                continue
            if not report["segments"]:
                continue

            print(f"\nad group {group.id}  {group.name[:48]}  ({args.dimension})")
            header = f"  {'segment':<30}{'clicks':>7}{'spend':>10}{'cvr':>8}{'roas':>7}{'':>4}"
            print(header)
            print("  " + "-" * (len(header) - 2))
            for seg in report["segments"]:
                mark = "CUT" if seg["verdict"] == "exclude" else ""
                print(
                    f"  {seg['segment'][:29]:<30}{seg['clicks']:>7}"
                    f"{seg['spend_usd']:>10.2f}{seg['cvr']:>8.2%}"
                    f"{seg['roas']:>7.2f}{mark:>4}"
                )
            if report["exclusions"]:
                print(f"  Recoverable: ${report['recoverable_usd']:.2f}/window")
                for seg in report["exclusions"]:
                    print(f"    {seg['segment']}: {seg['reason']}")
            elif report.get("note"):
                print(f"  {report['note']}")
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
        "--skip-landing-check", action="store_true",
        help="do not audit the destination before launching",
    )
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

    landing = sub.add_parser(
        "landing", help="audit the page the ads send people to"
    )
    landing.add_argument("--offer", type=int, help="audit and record this offer's page")
    landing.add_argument("--url", help="audit one URL without storing anything")
    landing.add_argument(
        "--sweep", action="store_true", help="re-check every live destination"
    )
    landing.add_argument("--max-age-hours", type=int, default=24)
    landing.add_argument(
        "--pause", action="store_true",
        help="with --sweep, pause campaigns whose page now fails",
    )
    landing.set_defaults(func=cmd_landing)

    funnel = sub.add_parser(
        "funnel", help="define the steps between the click and the money"
    )
    funnel.add_argument("--offer", type=int, required=True)
    funnel.add_argument(
        "--step", action="append",
        help="repeatable, 'key:kind[:value]' e.g. optin:optin or tripwire:tripwire:17",
    )
    funnel.set_defaults(func=cmd_funnel)

    segments = sub.add_parser(
        "segments", help="find placements or audiences that are wasting budget"
    )
    segments.add_argument("--ad-group", type=int, default=None)
    segments.add_argument(
        "--dimension", default="placement",
        choices=["placement", "device", "age_gender", "region", "hour"],
    )
    segments.add_argument("--days", type=int, default=14)
    segments.set_defaults(func=cmd_segments)

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
