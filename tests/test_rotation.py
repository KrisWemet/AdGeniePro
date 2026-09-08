"""Angle rotation.

Two distinctions do the work: an angle is not an execution, and fatigue is not
failure. Collapsing either one costs money — the first by throwing away a good
argument over one bad ad, the second by deleting a proven asset that only
needed a rest.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from adgenie.core.rotation import RotationPolicy, analyse_rotation
from adgenie.models import (
    AdGroup,
    Campaign,
    Conversion,
    ConversionStatus,
    Creative,
    EntityLevel,
    EntityStatus,
    MetricSnapshot,
    Platform,
)
from adgenie.money import usd_to_micros

START = date(2026, 1, 1)
NOW = datetime(2026, 2, 5, tzinfo=timezone.utc)


@pytest.fixture
def ad_group(session, offer) -> AdGroup:
    campaign = Campaign(
        offer_id=offer.id,
        platform=Platform.META,
        name="c",
        external_id="c1",
        status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.commit()
    group = AdGroup(
        campaign_id=campaign.id, name="g", external_id="g1",
        status=EntityStatus.ACTIVE,
    )
    session.add(group)
    session.commit()
    return group


def run_angle(
    session,
    offer,
    group,
    angle: str,
    executions: int,
    opening_ctr: float,
    closing_ctr: float,
    cvr: float,
    days: int = 30,
    status: EntityStatus = EntityStatus.ACTIVE,
) -> list[int]:
    """Deliver an angle over `days`, with click-through drifting as told."""
    campaign_id = session.get(AdGroup, group.id).campaign_id
    ids = []
    for i in range(executions):
        creative = Creative(
            ad_group_id=group.id, name=f"{angle}-{i}", angle=angle, status=status
        )
        session.add(creative)
        session.commit()
        ids.append(creative.id)

    txn = 0
    # Conversions are carried over between days rather than truncated each
    # day. Truncating would bias an angle with a falling click-through
    # downward and make a fatigue test look like a conversion failure.
    pending = 0.0
    for offset in range(days):
        day = START + timedelta(days=offset)
        share = offset / max(1, days - 1)
        ctr = opening_ctr + (closing_ctr - opening_ctr) * share
        impressions = 2000
        clicks = int(impressions * ctr)
        for creative_id in ids:
            session.add(
                MetricSnapshot(
                    level=EntityLevel.CREATIVE,
                    entity_id=creative_id,
                    day=day,
                    impressions=impressions,
                    clicks=clicks,
                    spend_micros=usd_to_micros(clicks * 0.50),
                )
            )
        pending += clicks * len(ids) * cvr
        today_conversions, pending = int(pending), pending - int(pending)
        for _ in range(today_conversions):
            txn += 1
            session.add(
                Conversion(
                    offer_id=offer.id,
                    campaign_id=campaign_id,
                    creative_id=ids[0],
                    status=ConversionStatus.APPROVED,
                    revenue_micros=usd_to_micros(40),
                    network_txn_id=f"{angle}-{txn}",
                    occurred_at=day,
                )
            )
    session.commit()
    return ids


def verdicts(plan) -> dict:
    return {stat.key: stat.verdict for stat in plan.angles}


# --- an angle is not an execution ------------------------------------------


def test_one_bad_ad_does_not_retire_the_argument():
    """The most common outcome in advertising is a bad ad for a good idea."""
    policy = RotationPolicy(min_executions_to_judge=3)
    assert policy.min_executions_to_judge > 1


def test_an_angle_with_a_single_execution_is_unproven_however_bad(
    session, offer, ad_group
):
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.02, 0.019, 0.04)
    run_angle(session, offer, ad_group, "identity", 1, 0.02, 0.02, 0.0)

    plan = analyse_rotation(session, offer, now=NOW)
    stats = {s.key: s for s in plan.angles}
    assert stats["identity"].verdict == "unproven"
    assert "execution" in stats["identity"].reason
    # And the evidence against it is real — it is being spared on principle,
    # not because the numbers look fine.
    assert stats["identity"].prob_worse > 0.5


def test_an_angle_that_failed_across_several_ads_is_retired(session, offer, ad_group):
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.02, 0.019, 0.04)
    run_angle(session, offer, ad_group, "social_proof", 4, 0.018, 0.017, 0.0)

    plan = analyse_rotation(session, offer, now=NOW)
    stats = {s.key: s for s in plan.angles}
    assert stats["social_proof"].verdict == "retire"
    assert stats["social_proof"].executions == 4
    assert "not one ad" in stats["social_proof"].reason


def test_the_bar_for_cutting_rises_with_the_number_of_angles_tested(
    session, offer, ad_group
):
    """Seven angles at 90% finds a loser by chance more often than not.

    The guard is checked by holding the evidence fixed and varying only how
    many angles were compared: the same numbers that justify a cut among two
    should not justify one among seven.
    """
    from adgenie.core.rotation import _judge
    from adgenie.core.rotation import AngleStat

    def stat(key: str, conversions: int, clicks: int) -> AngleStat:
        made = AngleStat(key=key, name=key, creative_ids=[1, 2, 3])
        made.clicks = clicks
        made.conversions = conversions
        made.impressions = clicks * 50
        return made

    def verdict_among(peer_split: list[tuple[int, int]]) -> AngleStat:
        stats = [stat("social_proof", 20, 1000)]
        stats += [
            stat(f"peer{i}", conversions, clicks)
            for i, (conversions, clicks) in enumerate(peer_split)
        ]
        _judge(stats, RotationPolicy(), date(2026, 2, 5))
        return stats[0]

    # The peer evidence is held identical — 62 conversions on ~2000 clicks
    # either way — so the only thing that changes is how many angles were
    # compared, and therefore how many chances there were to find a loser.
    one_peer = verdict_among([(62, 2000)])
    six_peers = verdict_among([(10, 333), (10, 333), (10, 333),
                               (11, 333), (11, 333), (10, 333)])

    assert one_peer.prob_worse == pytest.approx(six_peers.prob_worse, abs=0.01)
    assert one_peer.verdict == "retire"
    assert six_peers.verdict != "retire"


# --- fatigue is not failure ------------------------------------------------


def test_a_fatigued_angle_is_rested_not_retired(session, offer, ad_group):
    """Audiences forget. Retiring a worn-out winner deletes a proven asset."""
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.02, 0.019, 0.03)
    run_angle(session, offer, ad_group, "mechanism", 3, 0.020, 0.007, 0.03)

    plan = analyse_rotation(session, offer, now=NOW)
    stats = {s.key: s for s in plan.angles}
    assert stats["mechanism"].verdict == "rest"
    assert stats["mechanism"].rest_until is not None
    assert stats["mechanism"].decay > 0.35
    assert "conversion rate held" in stats["mechanism"].reason


def test_decay_is_measured_against_the_angles_own_opening(session, offer, ad_group):
    """Not against its siblings, which ran to different audiences."""
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.050, 0.048, 0.03)
    # Lower click-through throughout, but stable. Compared to its sibling it
    # looks terrible; compared to its own opening it has not decayed at all.
    run_angle(session, offer, ad_group, "mechanism", 3, 0.010, 0.0098, 0.03)

    plan = analyse_rotation(session, offer, now=NOW)
    stats = {s.key: s for s in plan.angles}
    assert stats["mechanism"].decay < 0.10
    assert stats["mechanism"].verdict != "rest"


def test_decay_uses_the_recent_window_not_the_lifetime_rate(session, offer, ad_group):
    """Lifetime click-through averages in the good opening days.

    An angle halfway through wearing out would read as healthy right up until
    it was worthless.
    """
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.02, 0.019, 0.03)
    run_angle(session, offer, ad_group, "mechanism", 3, 0.030, 0.008, 0.03)

    stats = {s.key: s for s in analyse_rotation(session, offer, now=NOW).angles}
    worn = stats["mechanism"]
    assert worn.recent_ctr < worn.ctr < worn.opening_ctr
    lifetime_decay = (worn.opening_ctr - worn.ctr) / worn.opening_ctr
    assert worn.decay > lifetime_decay


def test_a_rested_angle_returns_ahead_of_an_untried_one(session, offer, ad_group):
    """You already know the argument lands. An untried one is a coin flip."""
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.02, 0.019, 0.03)
    run_angle(session, offer, ad_group, "mechanism", 3, 0.020, 0.006, 0.035)

    # Far enough past the last delivery that the rest is over.
    later = datetime(2026, 4, 1, tzinfo=timezone.utc)
    plan = analyse_rotation(session, offer, now=later)
    assert plan.introduce[0] == "mechanism"
    assert "already know" in plan.recommendation


def test_a_rested_angle_that_also_converted_badly_does_not_jump_the_queue(
    session, offer, ad_group
):
    """It is not a proven asset coming back, it is a worse coin flip."""
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.02, 0.019, 0.05)
    run_angle(session, offer, ad_group, "mechanism", 3, 0.020, 0.006, 0.004)

    later = datetime(2026, 4, 1, tzinfo=timezone.utc)
    plan = analyse_rotation(session, offer, now=later)
    assert plan.angles
    assert "mechanism" not in plan.introduce[:1]


# --- not spreading too thin ------------------------------------------------


def test_no_new_angle_while_the_running_ones_are_unproven(session, offer, ad_group):
    """Adding another test makes every test in flight slower to conclude."""
    policy = RotationPolicy(max_angles_in_flight=4)
    for key in ("problem_solution", "mechanism", "comparison", "objection"):
        run_angle(session, offer, ad_group, key, 1, 0.02, 0.019, 0.03, days=3)

    plan = analyse_rotation(session, offer, policy=policy, now=NOW)
    assert all(v == "unproven" for v in verdicts(plan).values())
    assert plan.introduce == []
    assert "finish" in plan.recommendation.lower()


def test_retiring_an_angle_frees_a_slot(session, offer, ad_group):
    policy = RotationPolicy(max_angles_in_flight=3)
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.02, 0.019, 0.04)
    run_angle(session, offer, ad_group, "mechanism", 3, 0.02, 0.019, 0.035)
    run_angle(session, offer, ad_group, "social_proof", 4, 0.02, 0.019, 0.0)

    plan = analyse_rotation(session, offer, policy=policy, now=NOW)
    assert verdicts(plan)["social_proof"] == "retire"
    assert len(plan.introduce) == 1


# --- never rotate down to nothing ------------------------------------------


def test_the_last_live_angle_is_kept_running(session, offer, ad_group):
    """Retiring and resting are each right and can still switch the offer off.

    The replacements are drafts waiting on policy review; an offer with no ads
    earns nothing in the meantime.
    """
    run_angle(session, offer, ad_group, "mechanism", 3, 0.020, 0.005, 0.03)

    plan = analyse_rotation(session, offer, now=NOW)
    stat = plan.angles[0]
    assert stat.verdict == "last_resort"
    assert "last angle" in stat.reason
    assert stat.rest_until is None


def test_a_paused_angle_does_not_count_as_the_last_one_standing(
    session, offer, ad_group
):
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.02, 0.019, 0.03)
    run_angle(
        session, offer, ad_group, "mechanism", 3, 0.020, 0.005, 0.03,
        status=EntityStatus.PAUSED,
    )

    plan = analyse_rotation(session, offer, now=NOW)
    # The healthy angle is live, so the worn-out one is free to rest.
    assert verdicts(plan)["mechanism"] == "rest"


# --- exhaustion ------------------------------------------------------------


def test_an_offer_with_nothing_left_says_so(session, offer, ad_group):
    """More creative is not the answer when the argument space is spent."""
    policy = RotationPolicy(max_angles_in_flight=1)
    from adgenie.core.angles import ANGLES

    # Every argument in the library tried, and every one worn out on this
    # audience. There is nothing left to say to these people.
    for angle in ANGLES:
        run_angle(session, offer, ad_group, angle.key, 3, 0.020, 0.005, 0.03)

    plan = analyse_rotation(session, offer, policy=policy, now=NOW)
    assert plan.untested == []
    # One is kept alive as a stopgap, and that must not read as health.
    assert [s.verdict for s in plan.angles].count("last_resort") == 1
    assert plan.exhausted is True
    assert "Change the audience" in plan.recommendation


def test_an_offer_that_has_run_nothing_is_not_exhausted(session, offer, ad_group):
    plan = analyse_rotation(session, offer, now=NOW)
    assert plan.angles == []
    assert plan.exhausted is False
    assert "Launch a structured test" in plan.recommendation


# --- acting on a plan ------------------------------------------------------


def _orchestrator(session, settings, client=None):
    from adgenie.core.orchestrator import Orchestrator

    return Orchestrator(
        session,
        settings=settings,
        platform_clients={Platform.META: client} if client else None,
    )


def test_a_new_angle_is_tested_in_the_proven_ad_group(
    session, offer, settings, sandbox_meta
):
    """Testing an argument in a weak ad set confounds the two.

    The angle fails and there is no way to tell whether the argument was wrong
    or the audience was, so a fresh angle goes to the audience that has
    already shown it converts.
    """
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    launched = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META,
            daily_budget_usd=30.0, angle_count=2, start_paused=False,
        )
    )
    weak_id, strong_id = launched.ad_group_ids[0], launched.ad_group_ids[1]
    campaign_id = launched.campaign_id

    until = date.today() - timedelta(days=2)
    for group_id, conversions in ((weak_id, 2), (strong_id, 60)):
        session.add(
            MetricSnapshot(
                level=EntityLevel.AD_GROUP, entity_id=group_id, day=until,
                impressions=40_000, clicks=1000,
                spend_micros=usd_to_micros(500),
            )
        )
        for i in range(conversions):
            session.add(
                Conversion(
                    offer_id=offer.id, campaign_id=campaign_id,
                    ad_group_id=group_id, status=ConversionStatus.APPROVED,
                    revenue_micros=usd_to_micros(40),
                    network_txn_id=f"{group_id}-{i}", occurred_at=until,
                )
            )
    session.commit()

    result = _orchestrator(session, settings, sandbox_meta).introduce_angle(
        offer.id, "objection", count=2
    )
    assert result["ad_group_id"] == strong_id
    assert result["created_creative_ids"]

    made = session.get(Creative, result["created_creative_ids"][0])
    assert made.angle == "objection"
    assert made.external_id in sandbox_meta.entities
    # Staged paused: an ad that only exists in the database replaces nothing.
    assert made.status is EntityStatus.PAUSED
    # A fresh angle has no parent — it is not bred from a tired ad.
    assert made.parent_id is None
    assert made.generation == 0


def test_applying_a_rotation_pauses_spent_angles(session, offer, ad_group, settings):
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.02, 0.019, 0.04)
    retired = run_angle(session, offer, ad_group, "social_proof", 4, 0.02, 0.019, 0.0)

    result = _orchestrator(session, settings).apply_rotation(offer.id, now=NOW)
    assert result["apply"]["applied"] is True
    assert set(retired) <= set(result["apply"]["paused_creative_ids"])
    for creative_id in retired:
        assert session.get(Creative, creative_id).status is EntityStatus.PAUSED


def test_a_dry_run_rotation_changes_nothing(session, offer, ad_group, settings):
    run_angle(session, offer, ad_group, "problem_solution", 3, 0.02, 0.019, 0.04)
    retired = run_angle(session, offer, ad_group, "social_proof", 4, 0.02, 0.019, 0.0)
    dry = settings.model_copy(update={"dry_run": True})

    result = _orchestrator(session, dry).apply_rotation(offer.id, now=NOW)
    assert result["apply"]["applied"] is False
    # It still reports what it would stop.
    assert set(retired) <= set(result["apply"]["paused_creative_ids"])
    assert result["apply"]["introduced"] == []
    for creative_id in retired:
        assert session.get(Creative, creative_id).status is EntityStatus.ACTIVE


def test_an_offer_with_no_live_ad_group_cannot_be_given_an_angle(
    session, offer, settings
):
    from adgenie.platforms.base import PlatformError

    with pytest.raises(PlatformError, match="no live ad group"):
        _orchestrator(session, settings).introduce_angle(offer.id, "objection")


# --- several executions of one argument ------------------------------------


def test_asking_for_one_angle_gives_different_ads_not_the_same_one(settings):
    """Three copies of one ad is one ad running three times.

    They compete in the same auction, split the delivery, and the split tells
    you nothing either of them would not have told you alone.
    """
    from adgenie.core.angles import get_angle
    from adgenie.core.copywriter import CopyBrief, CopyStudio

    brief = CopyBrief(
        product_name="CalmLeaf",
        platform=Platform.META,
        ad_format="feed",
        destination_url="https://offer.test/x",
        product_description="A magnesium and L-theanine blend.",
        key_benefits=["wind down without grogginess"],
        proof_points=["Third-party tested"],
        angle=get_angle("objection"),
    )
    drafts = CopyStudio(settings=settings).write_variants(
        brief, count=3, same_angle=True
    )

    assert {d.angle for d in drafts} == {"objection"}
    assert len({tuple(d.headlines) for d in drafts}) == 3
    assert len({d.primary_texts[0] for d in drafts}) == 3


def test_variants_still_rotate_angles_by_default(settings):
    """Refreshing a worn-out ad wants a spread of arguments, not more of one.

    Which of the two a caller wants is not inferable from the brief, so an
    angle on the brief must not silently change what write_variants does.
    """
    from adgenie.core.angles import get_angle
    from adgenie.core.copywriter import CopyBrief, CopyStudio

    brief = CopyBrief(
        product_name="CalmLeaf",
        platform=Platform.META,
        ad_format="feed",
        destination_url="https://offer.test/x",
        angle=get_angle("objection"),
    )
    drafts = CopyStudio(settings=settings).write_variants(brief, count=3)
    assert len({d.angle for d in drafts}) == 3


def test_identical_drafts_are_not_staged_twice(session, offer, settings, sandbox_meta):
    """The guard for when a generator does not vary despite being asked to."""
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan
    from adgenie.core.orchestrator import Orchestrator

    launched = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META,
            daily_budget_usd=30.0, angle_count=1, start_paused=False,
        )
    )
    group = session.get(AdGroup, launched.ad_group_ids[0])
    campaign = session.get(Campaign, group.campaign_id)

    orchestrator = Orchestrator(
        session, settings=settings, platform_clients={Platform.META: sandbox_meta}
    )
    one = orchestrator.studio.write_variants(
        __import__("adgenie.core.copywriter", fromlist=["build_brief"]).build_brief(
            offer, platform=Platform.META, angle_key="objection"
        ),
        count=1,
        same_angle=True,
    )[0]

    created = orchestrator._materialise_drafts(
        [one, one, one],
        group=group,
        campaign=campaign,
        offer=offer,
        name_for=lambda i: f"dup {i}",
    )
    assert len(created) == 1
