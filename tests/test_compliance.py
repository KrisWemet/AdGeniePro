"""Policy enforcement. These rules are what stand between the account and a ban."""

from __future__ import annotations

import pytest

from adgenie.core.compliance import ComplianceEngine, Severity, review_texts
from adgenie.models import ComplianceVerdict, Platform


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


# --- personal attributes ---------------------------------------------------


def test_direct_personal_attribute_is_blocked_on_meta():
    report = review_texts(
        {"primary_texts": ["Are you diabetic and tired of finger pricks?"]},
        Platform.META,
    )
    assert report.verdict is ComplianceVerdict.BLOCK
    assert "PERSONAL_ATTRIBUTE_DIRECT" in codes(report)


def test_possessive_personal_attribute_is_blocked():
    report = review_texts({"headlines": ["Fix your bad credit today"]}, Platform.META)
    assert "PERSONAL_ATTRIBUTE_DIRECT" in codes(report)


def test_third_person_health_topic_is_allowed():
    """The topic is fine; asserting it about the reader is not."""
    report = review_texts(
        {
            "headlines": ["Blood Sugar Support"],
            "primary_texts": [
                "A simpler daily routine for blood sugar support. Paid link."
            ],
        },
        Platform.META,
    )
    assert "PERSONAL_ATTRIBUTE_DIRECT" not in codes(report)
    assert report.verdict is not ComplianceVerdict.BLOCK


# --- claims ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,code",
    [
        ("Guaranteed results in 30 days", "GUARANTEED_OUTCOME"),
        ("This miracle blend changes everything", "MIRACLE_CURE"),
        ("Doctors hate this simple trick", "MIRACLE_CURE"),
        ("Lose 30 pounds this month", "SPECIFIC_WEIGHT_LOSS"),
        ("See the before and after photos", "BEFORE_AFTER"),
        ("Make $5,000 a day from home", "INCOME_CLAIM"),
        ("Guaranteed returns, no risk investment", "INVESTMENT_RETURN"),
        ("Our supplement cures arthritis", "SUPPLEMENT_DISEASE_CLAIM"),
    ],
)
def test_prohibited_claims_are_blocked(text, code):
    report = review_texts({"primary_texts": [text]}, Platform.META)
    assert code in codes(report)
    assert report.verdict is ComplianceVerdict.BLOCK


def test_clean_copy_passes():
    report = review_texts(
        {
            "headlines": ["A Simpler Evening Routine"],
            "primary_texts": [
                "Third-party tested in a US facility. A 12-minute wind-down you "
                "can actually keep. #ad"
            ],
        },
        Platform.META,
    )
    assert report.verdict is ComplianceVerdict.PASS
    assert report.score == 100.0


# --- platform-specific editorial rules -------------------------------------


def test_google_rejects_emoji_but_meta_tolerates_a_few():
    texts = {"headlines": ["Sleep Better 😴"], "descriptions": ["A calm routine", "Ships fast"]}
    assert "EMOJI_ON_SEARCH" in codes(review_texts(texts, Platform.GOOGLE))
    assert "EMOJI_ON_SEARCH" not in codes(review_texts(texts, Platform.META))


def test_google_rejects_shouting_and_repeated_punctuation():
    report = review_texts(
        {"headlines": ["AMAZING OFFER!!"], "descriptions": ["Buy now", "Ships fast"]},
        Platform.GOOGLE,
    )
    assert {"EXCESSIVE_CAPS", "REPEATED_PUNCTUATION"} <= codes(report)


def test_short_acronyms_are_not_treated_as_shouting():
    report = review_texts(
        {
            "headlines": ["Made In The USA", "Third Party Tested", "Ships In Two Days"],
            "descriptions": ["A calm evening routine.", "Free returns for 60 days."],
        },
        Platform.GOOGLE,
    )
    assert "EXCESSIVE_CAPS" not in codes(report)


def test_meta_flags_emoji_spam():
    report = review_texts(
        {"primary_texts": ["Sleep 😴 better 🔥 tonight 💤 now ⭐ really ✨"]},
        Platform.META,
    )
    assert "EMOJI_DENSITY" in codes(report)


# --- structural limits -----------------------------------------------------


def test_over_length_headline_is_blocked():
    report = review_texts(
        {
            "headlines": ["This headline is far too long for a Google search ad"],
            "descriptions": ["One", "Two"],
        },
        Platform.GOOGLE,
    )
    assert "OVER_CHAR_LIMIT" in codes(report)
    assert report.verdict is ComplianceVerdict.BLOCK


def test_too_few_google_assets_is_blocked():
    report = review_texts(
        {"headlines": ["Only One"], "descriptions": ["Only one description here"]},
        Platform.GOOGLE,
    )
    assert "TOO_FEW_ASSETS" in codes(report)


def test_duplicate_assets_are_warned():
    report = review_texts(
        {
            "headlines": ["Same Thing", "Same thing", "Third Option"],
            "descriptions": ["A description here", "Another description"],
        },
        Platform.GOOGLE,
    )
    assert "DUPLICATE_ASSETS" in codes(report)


# --- affiliate and advertiser rules ----------------------------------------


def test_missing_affiliate_disclosure_is_warned():
    report = review_texts(
        {"headlines": ["A Calm Routine"], "primary_texts": ["A 12-minute wind-down."]},
        Platform.META,
    )
    assert "MISSING_AFFILIATE_DISCLOSURE" in codes(report)
    assert report.verdict is ComplianceVerdict.WARN


@pytest.mark.parametrize("marker", ["#ad", "Sponsored", "affiliate link", "Paid link"])
def test_disclosure_markers_satisfy_the_rule(marker):
    report = review_texts(
        {"primary_texts": [f"A calm evening routine. {marker}"]}, Platform.META
    )
    assert "MISSING_AFFILIATE_DISCLOSURE" not in codes(report)


def test_advertiser_banned_claim_is_blocked(offer):
    offer.banned_claims = ["cures insomnia"]
    report = review_texts(
        {"primary_texts": ["This cures insomnia overnight. #ad"]},
        Platform.META,
        offer=offer,
    )
    assert "ADVERTISER_BANNED_CLAIM" in codes(report)
    assert report.verdict is ComplianceVerdict.BLOCK


def test_advertiser_required_disclosure_is_enforced(offer):
    offer.required_disclosures = ["Results vary"]
    report = review_texts(
        {"primary_texts": ["A calm routine. #ad"]}, Platform.META, offer=offer
    )
    assert "MISSING_REQUIRED_DISCLOSURE" in codes(report)


def test_regulated_vertical_is_noted_without_blocking(offer):
    report = review_texts(
        {
            "headlines": ["A Calm Evening Routine"],
            "primary_texts": ["A calm evening routine. #ad"],
        },
        Platform.META,
        offer=offer,
    )
    assert "REGULATED_VERTICAL" in codes(report)
    assert report.verdict is not ComplianceVerdict.BLOCK


# --- report behaviour ------------------------------------------------------


def test_rewrite_instructions_exclude_info_findings_and_name_the_trigger():
    report = review_texts(
        {"primary_texts": ["Guaranteed results, 100% effective"]}, Platform.META
    )
    notes = report.rewrite_instructions()
    assert notes
    assert any("triggered by" in note for note in notes)
    assert all("regulated vertical" not in note.lower() for note in notes)


def test_score_decreases_with_severity():
    clean = review_texts(
        {"primary_texts": ["A calm evening routine. #ad"]}, Platform.META
    )
    dirty = review_texts(
        {"primary_texts": ["Guaranteed miracle cure, lose 40 pounds! #ad"]},
        Platform.META,
    )
    assert clean.score > dirty.score
    assert dirty.score >= 0.0


def test_findings_are_sorted_most_severe_first():
    report = review_texts(
        {"primary_texts": ["Guaranteed! Amazing!! Wow!!!"]}, Platform.META
    )
    ranks = [f.severity.rank for f in report.findings]
    assert ranks == sorted(ranks, reverse=True)


def test_engine_accepts_custom_rules():
    def no_purple(texts, platform):
        from adgenie.core.compliance import Finding

        joined = " ".join(v for values in texts.values() for v in values)
        if "purple" in joined.lower():
            return [
                Finding(
                    code="NO_PURPLE",
                    severity=Severity.BLOCK,
                    message="brand rule",
                    policy_ref="internal",
                )
            ]
        return []

    engine = ComplianceEngine(extra_checks=[no_purple])
    report = engine.review({"headlines": ["Purple pill"]}, Platform.META)
    assert "NO_PURPLE" in {f.code for f in report.findings}
