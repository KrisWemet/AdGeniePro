"""Copy generation must be compliant and correctly formatted, every time."""

from __future__ import annotations

import pytest

from adgenie.core.angles import ANGLES, angles_for, get_angle
from adgenie.core.copywriter import (
    CopyBrief,
    CopyStudio,
    CreativeDraft,
    GeneratedCopy,
    LLMCopywriter,
    TemplateCopywriter,
    build_brief,
)
from adgenie.models import ComplianceVerdict, Platform
from adgenie.platforms.specs import get_spec, truncate_to_spec


@pytest.fixture
def google_brief(offer) -> CopyBrief:
    return build_brief(
        offer, Platform.GOOGLE, keyword="natural sleep aid", angle_key="search_intent"
    )


@pytest.fixture
def meta_brief(offer) -> CopyBrief:
    return build_brief(offer, Platform.META, angle_key="problem_solution")


# --- format compliance -----------------------------------------------------


def test_google_copy_meets_responsive_search_ad_requirements(google_brief, settings):
    draft = CopyStudio(settings=settings).write(google_brief)
    spec = get_spec(Platform.GOOGLE)

    assert len(draft.headlines) >= spec.fields["headlines"].min_count
    assert len(draft.headlines) <= spec.fields["headlines"].max_count
    assert len(draft.descriptions) >= spec.fields["descriptions"].min_count
    assert all(len(h) <= 30 for h in draft.headlines)
    assert all(len(d) <= 90 for d in draft.descriptions)
    # Search ads have no long-form body copy.
    assert draft.primary_texts == []


def test_meta_copy_meets_feed_requirements(meta_brief, settings):
    draft = CopyStudio(settings=settings).write(meta_brief)
    assert draft.headlines and all(len(h) <= 40 for h in draft.headlines)
    assert draft.primary_texts
    assert all(len(p) <= 500 for p in draft.primary_texts)
    assert draft.call_to_action in get_spec(Platform.META).allowed_ctas


def test_generated_copy_passes_policy_review(google_brief, meta_brief, settings):
    studio = CopyStudio(settings=settings)
    for brief in (google_brief, meta_brief):
        draft = studio.write(brief)
        assert draft.compliance is not None
        assert draft.compliance.verdict is not ComplianceVerdict.BLOCK, (
            f"{brief.platform.value}: "
            f"{[f.code for f in draft.compliance.blocking]}"
        )


def test_assets_are_deduplicated(meta_brief, settings):
    draft = CopyStudio(settings=settings).write(meta_brief)
    lowered = [h.lower() for h in draft.headlines]
    assert len(lowered) == len(set(lowered))


# --- angles ----------------------------------------------------------------


@pytest.mark.parametrize("angle", [a.key for a in ANGLES])
def test_every_angle_produces_usable_meta_copy(angle, offer, settings):
    brief = build_brief(offer, Platform.META, angle_key=angle, keyword="sleep aid")
    draft = CopyStudio(settings=settings).write(brief)
    assert draft.headlines
    assert draft.primary_texts
    assert draft.compliance.verdict is not ComplianceVerdict.BLOCK


def test_variants_cover_distinct_angles(meta_brief, settings):
    drafts = CopyStudio(settings=settings).write_variants(meta_brief, count=4)
    assert len(drafts) == 4
    assert len({d.angle for d in drafts}) == 4, "each variant must test a new argument"


def test_variant_bodies_differ(meta_brief, settings):
    drafts = CopyStudio(settings=settings).write_variants(meta_brief, count=4)
    bodies = {d.primary_texts[0] for d in drafts if d.primary_texts}
    assert len(bodies) == 4


def test_search_angles_are_prioritised_for_google():
    assert angles_for("google")[0].key == "search_intent"
    assert angles_for("meta")[0].key != "search_intent"


def test_template_copy_is_grammatical(offer, settings):
    """Slot substitution must not leave dangling or double articles."""
    studio = CopyStudio(generator=TemplateCopywriter(), settings=settings)
    for angle in ANGLES:
        brief = build_brief(offer, Platform.META, angle_key=angle.key)
        draft = studio.write(brief)
        for text in draft.primary_texts + draft.headlines:
            assert "the the " not in text.lower()
            assert ". ." not in text
            assert "  " not in text
            assert not text.endswith((" to", " the", " a", " of", " without"))


# --- repair loop -----------------------------------------------------------


class _BadThenGoodGenerator:
    """Emits a policy-violating draft first, then a clean one."""

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def generate(self, brief: CopyBrief) -> CreativeDraft:
        self.calls.append(list(brief.repair_notes))
        if len(self.calls) == 1:
            return CreativeDraft(
                angle="mechanism",
                headlines=["Guaranteed Results"],
                descriptions=[],
                primary_texts=["Lose 30 pounds, guaranteed. #ad"],
            )
        return CreativeDraft(
            angle="mechanism",
            headlines=["A Calmer Evening"],
            descriptions=[],
            primary_texts=["A 12-minute wind-down routine you can keep. #ad"],
        )


def test_blocked_copy_is_regenerated_with_the_findings_attached(meta_brief, settings):
    generator = _BadThenGoodGenerator()
    draft = CopyStudio(generator=generator, settings=settings).write(meta_brief)

    assert len(generator.calls) == 2
    assert generator.calls[0] == [], "the first attempt has no repair notes"
    notes = " ".join(generator.calls[1]).lower()
    assert "guarantee" in notes or "weight" in notes
    assert draft.compliance.verdict is not ComplianceVerdict.BLOCK


def test_repair_gives_up_after_the_configured_attempts(meta_brief, settings):
    class AlwaysBad:
        name = "stub"

        def __init__(self):
            self.calls = 0

        def generate(self, brief):
            self.calls += 1
            return CreativeDraft(
                angle="x",
                headlines=["Guaranteed Cure"],
                descriptions=[],
                primary_texts=["Guaranteed miracle cure. #ad"],
            )

    generator = AlwaysBad()
    settings.copywriter_max_repair_attempts = 2
    draft = CopyStudio(generator=generator, settings=settings).write(meta_brief)

    assert generator.calls == 3
    assert draft.compliance.verdict is ComplianceVerdict.BLOCK
    assert not draft.is_launchable()


def test_studio_falls_back_to_templates_when_the_generator_raises(meta_brief, settings):
    class Exploding:
        name = "stub"

        def generate(self, brief):
            raise RuntimeError("api down")

    draft = CopyStudio(generator=Exploding(), settings=settings).write(meta_brief)
    assert draft.generator == "template"
    assert draft.headlines


# --- spec enforcement ------------------------------------------------------


def test_over_long_assets_are_trimmed_not_rejected(meta_brief, settings):
    class TooLong:
        name = "stub"

        def generate(self, brief):
            return CreativeDraft(
                angle="x",
                headlines=["This headline runs well past forty characters for sure"],
                descriptions=[],
                primary_texts=["A calm evening routine. #ad"],
            )

    draft = CopyStudio(generator=TooLong(), settings=settings).write(meta_brief)
    assert all(len(h) <= 40 for h in draft.headlines)


def test_invalid_cta_is_replaced(meta_brief, settings):
    class BadCta:
        name = "stub"

        def generate(self, brief):
            return CreativeDraft(
                angle="x",
                headlines=["A Calm Evening"],
                descriptions=[],
                primary_texts=["A calm evening routine. #ad"],
                call_to_action="NOT_A_REAL_CTA",
            )

    draft = CopyStudio(generator=BadCta(), settings=settings).write(meta_brief)
    assert draft.call_to_action == "LEARN_MORE"


def test_truncation_prefers_a_sentence_boundary():
    text = "First sentence here. Second sentence runs on much longer than allowed."
    assert truncate_to_spec(text, 40) == "First sentence here."


def test_truncation_never_ends_on_a_dangling_word():
    trimmed = truncate_to_spec("Wind down without the grogginess", 22)
    assert trimmed == "Wind down"


def test_truncation_keeps_an_abbreviation_intact():
    """A period inside 'U.S.' is not a sentence boundary."""
    assert truncate_to_spec("Made in the U.S. by a small team of people", 30).startswith(
        "Made in the U.S."
    )


# --- the LLM path ----------------------------------------------------------


class _StubResponse:
    stop_reason = "end_turn"
    stop_details = None
    model = "claude-opus-5"
    usage = None

    def __init__(self, parsed):
        self.parsed_output = parsed


class _StubMessages:
    def __init__(self, parsed):
        self.parsed = parsed
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return _StubResponse(self.parsed)


class _StubClient:
    def __init__(self, parsed):
        self.messages = _StubMessages(parsed)


def test_llm_copywriter_maps_structured_output(meta_brief, settings):
    parsed = GeneratedCopy(
        angle="mechanism",
        headlines=["A Calmer Evening", "Wind Down Simply"],
        descriptions=["A 12-minute routine."],
        primary_texts=["Third-party tested. A routine you can keep. #ad"],
        call_to_action="SHOP_NOW",
        rationale="Mechanism is credible for supplements.",
    )
    client = _StubClient(parsed)
    writer = LLMCopywriter(settings, client=client)
    draft = writer.generate(meta_brief)

    assert draft.generator == "llm"
    assert draft.angle == "mechanism"
    assert draft.headlines == ["A Calmer Evening", "Wind Down Simply"]
    assert client.messages.kwargs["model"] == settings.copywriter_model
    assert client.messages.kwargs["output_format"] is GeneratedCopy


def test_llm_prompt_carries_the_hard_constraints(meta_brief, settings):
    client = _StubClient(GeneratedCopy(angle="x", headlines=["A"]))
    LLMCopywriter(settings, client=client).generate(meta_brief)
    prompt = client.messages.kwargs["messages"][0]["content"]

    assert "sensitive personal attribute" in prompt
    assert "40 characters" in prompt
    assert "Third-party tested in a US facility" in prompt
    assert "regulated category" in prompt


def test_llm_prompt_forbids_inventing_proof_when_none_supplied(offer, settings):
    offer.proof_points = []
    brief = build_brief(offer, Platform.META)
    client = _StubClient(GeneratedCopy(angle="x", headlines=["A"]))
    LLMCopywriter(settings, client=client).generate(brief)
    prompt = client.messages.kwargs["messages"][0]["content"]
    assert "Do not cite any statistic" in prompt


def test_llm_refusal_is_surfaced_not_swallowed(meta_brief, settings):
    class Refusing(_StubResponse):
        stop_reason = "refusal"

    class RefusingMessages:
        def parse(self, **kwargs):
            return Refusing(None)

    client = type("C", (), {"messages": RefusingMessages()})()
    with pytest.raises(RuntimeError, match="declined"):
        LLMCopywriter(settings, client=client).generate(meta_brief)


def test_studio_uses_templates_when_no_api_key(settings):
    settings.anthropic_api_key = None
    assert isinstance(CopyStudio(settings=settings).generator, TemplateCopywriter)
