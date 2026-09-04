"""The angle library.

An "angle" is the argument an ad makes, independent of its wording. Rotating
wording while keeping the angle produces ads that all fail together; rotating
the angle is what actually finds a winner. Testing angles first and wording
second is the difference between a testing budget that learns and one that
just spends.

Each angle carries guidance for the LLM copywriter and a structural template
for the deterministic fallback, so the platform produces sensible copy with or
without an API key.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Angle:
    key: str
    name: str
    thesis: str
    guidance: str
    # Verticals where this angle tends to run into policy trouble.
    risky_for: tuple[str, ...] = ()
    headline_patterns: tuple[str, ...] = ()
    body_pattern: str = ""


ANGLES: tuple[Angle, ...] = (
    Angle(
        key="problem_solution",
        name="Problem / Solution",
        thesis="Name the friction the reader already feels, then show the fix.",
        guidance=(
            "Open on a concrete, specific frustration the audience recognises. "
            "Do not diagnose the reader or reference a sensitive trait. Move to "
            "the product within one sentence and end on what changes for them."
        ),
        headline_patterns=(
            "A Simpler Way To {benefit}",
            "{benefit}, Made Simple",
            "Skip The {friction_title}",
        ),
        body_pattern=(
            "The {friction} is the part nobody warns you about. {product_name} handles "
            "it differently: {mechanism}. {proof} {cta}"
        ),
    ),
    Angle(
        key="mechanism",
        name="Unique Mechanism",
        thesis="Explain *why* it works. Specificity is what makes a claim believable.",
        guidance=(
            "Lead with the mechanism, not the outcome. Name the ingredient, the "
            "process or the technique. Specificity beats intensity: 'a 12-minute "
            "guided routine' outperforms 'amazing results'."
        ),
        headline_patterns=(
            "How {product_name} Works",
            "What Makes {product_name} Different",
            "The Part That Matters",
        ),
        body_pattern=(
            "Most options in this category skip the hard part. {product_name} "
            "{mechanism}. {benefit_line} {proof} {cta}"
        ),
    ),
    Angle(
        key="social_proof",
        name="Social Proof",
        thesis="Other people already made this decision and it went fine.",
        guidance=(
            "Use only proof points supplied in the brief. Never invent a number, "
            "a review count, a rating or a testimonial. If the brief has no proof "
            "points, use category-level language instead of fabricated figures."
        ),
        headline_patterns=(
            "Why People Switch To {product_name}",
            "{proof_short}",
            "Join Them",
        ),
        body_pattern=(
            "{proof} That is usually the tell. {product_name} {mechanism}. "
            "{benefit_line} {cta}"
        ),
    ),
    Angle(
        key="comparison",
        name="Comparison",
        thesis="Position against the alternative the reader is currently using.",
        guidance=(
            "Compare against a generic category, never a named competitor brand. "
            "Be concrete about the tradeoff you are claiming to win on."
        ),
        headline_patterns=(
            "{product_name} vs. The Usual Way",
            "A Better Trade-Off",
            "Why Not The Cheaper Option?",
        ),
        body_pattern=(
            "The usual approach means more {friction}. {product_name} {mechanism} "
            "instead. {benefit_line} {cta}"
        ),
    ),
    Angle(
        key="objection",
        name="Objection Handling",
        thesis="Say the doubt out loud before the reader does.",
        guidance=(
            "Name the single most likely objection (price, effort, scepticism, "
            "time) and answer it in the same breath. Honest framing outperforms "
            "hype and survives review."
        ),
        headline_patterns=(
            "Sceptical? Fair.",
            "Read This First",
            "What It Does Not Do",
        ),
        body_pattern=(
            "Fair question: {objection} Here is the honest answer: {product_name} "
            "{mechanism}. {proof} {cta}"
        ),
    ),
    Angle(
        key="cost_of_inaction",
        name="Cost Of Inaction",
        thesis="Staying put is also a choice, and it has a price.",
        guidance=(
            "Quantify the ongoing cost of the status quo in time, money or effort. "
            "Keep it factual. Do not use fear, shame or health scares."
        ),
        headline_patterns=(
            "The Slow Cost Of Waiting",
            "What Waiting Really Costs",
            "Every Week Adds Up",
        ),
        body_pattern=(
            "The {friction} rarely feels urgent, which is exactly why it persists. "
            "{product_name} {mechanism}. {benefit_line} {cta}"
        ),
    ),
    Angle(
        key="identity",
        name="Identity / Aspiration",
        thesis="Speak to who the reader is becoming, not what they lack.",
        guidance=(
            "Affirming, never shaming. Describe the person who already solved this "
            "and what their routine looks like. Do not assert traits about the "
            "reader."
        ),
        headline_patterns=(
            "For People Who {aspiration}",
            "Built For The Long Game",
            "Made For Real Routines",
        ),
        body_pattern=(
            "The people who {aspiration} tend to share one habit: they make it "
            "easy on themselves. {product_name} {mechanism}. {benefit_line} {cta}"
        ),
    ),
    Angle(
        key="how_to",
        name="Educational / How-To",
        thesis="Teach something useful; the product is the shortcut.",
        guidance=(
            "Give away one genuinely useful step in the ad itself. Earn the click "
            "with usefulness rather than withholding. Strong on cold traffic."
        ),
        headline_patterns=(
            "Start With This One Step",
            "{benefit} In Three Steps",
            "The Part Everyone Skips",
        ),
        body_pattern=(
            "Step one is always the same: make it easy to repeat. That alone gets most "
            "people moving. {product_name} {mechanism}. {benefit_line} {cta}"
        ),
    ),
    Angle(
        key="offer_led",
        name="Offer-Led",
        thesis="The deal itself is the reason to click.",
        guidance=(
            "Lead with the concrete commercial terms: price, guarantee, shipping, "
            "trial. Only state terms supplied in the brief. Never invent a discount."
        ),
        headline_patterns=(
            "See The Current Offer",
            "{offer_terms}",
            "Check Today's Pricing",
        ),
        body_pattern=(
            "{offer_terms}. {product_name} {mechanism}. {benefit_line} {cta}"
        ),
    ),
    Angle(
        key="search_intent",
        name="Search Intent Match",
        thesis="Mirror the exact phrase the searcher typed.",
        guidance=(
            "For Google search only. Echo the keyword in at least three headlines "
            "so the ad reads as the obvious answer to the query. Keep every "
            "headline inside 30 characters."
        ),
        headline_patterns=(
            "{keyword}",
            "{keyword} - Compare",
            "Official {keyword} Info",
        ),
        body_pattern=(
            "Looking for {keyword}? {product_name} {mechanism}. {benefit_line} {cta}"
        ),
    ),
)

ANGLES_BY_KEY: dict[str, Angle] = {a.key: a for a in ANGLES}

# Angles that suit each platform's user intent. Search traffic is
# solution-aware and already looking; feed traffic is not.
PLATFORM_ANGLE_PRIORITY: dict[str, tuple[str, ...]] = {
    "google": (
        "search_intent",
        "comparison",
        "offer_led",
        "mechanism",
        "problem_solution",
        "social_proof",
    ),
    "meta": (
        "problem_solution",
        "mechanism",
        "social_proof",
        "how_to",
        "objection",
        "identity",
        "cost_of_inaction",
        "comparison",
    ),
}


def angles_for(platform: str, limit: int | None = None) -> list[Angle]:
    keys = PLATFORM_ANGLE_PRIORITY.get(platform, tuple(ANGLES_BY_KEY))
    picked = [ANGLES_BY_KEY[k] for k in keys if k in ANGLES_BY_KEY]
    return picked[:limit] if limit else picked


def get_angle(key: str) -> Angle:
    return ANGLES_BY_KEY.get(key, ANGLES[0])
