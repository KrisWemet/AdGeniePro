"""Presenter videos: what is said, who says it, and what gets paid for.

The failures these tests guard against are expensive in a particular order: a
fake testimonial that ships (a per-violation penalty and a lost account), a
spoken claim nobody reviewed, and a paid generation for a plan that was never
going to be usable.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from sqlalchemy import select

from adgenie.cli import build_parser
from adgenie.config import Settings
from adgenie.core.angles import ANGLES
from adgenie.core.copywriter import build_brief
from adgenie.media.base import MediaError, MediaRequest, MediaResult
from adgenie.media.kie import KieClient
from adgenie.media.sandbox import SandboxMediaProvider
from adgenie.media.specs import get_media_spec
from adgenie.media.studio import MediaStudio
from adgenie.media.ugc import (
    WORDS_PER_SECOND,
    GeneratedScript,
    LLMScriptWriter,
    ScriptStudio,
    SpokenScript,
    TemplateScriptWriter,
    build_presenter_plan,
    plan_presenter_video,
    review_script,
)
from adgenie.models import (
    ComplianceVerdict,
    MediaAsset,
    MediaKind,
    MediaStatus,
    Platform,
)

REEL = get_media_spec("meta_reel_video")


def _script(*lines: str) -> SpokenScript:
    return SpokenScript(hook=lines[0], body=list(lines[1:-1]), call_to_action=lines[-1])


def _codes(report) -> set[str]:
    return {f.code for f in report.findings}


# --- what the presenter may say ---------------------------------------------


@pytest.mark.parametrize(
    "line,code",
    [
        ("I tried everything before this one.", "FABRICATED_TESTIMONY"),
        ("I've been taking it for a month now.", "FABRICATED_TESTIMONY"),
        ("I was so sceptical at first.", "FABRICATED_TESTIMONY"),
        ("Honestly, it changed my life.", "FABRICATED_TESTIMONY"),
        ("It worked for me in the first week.", "FABRICATED_TESTIMONY"),
        ("My doctor recommended it.", "FABRICATED_TESTIMONY"),
        ("As a busy mom, evenings are chaos.", "FABRICATED_TESTIMONY"),
        ("I swear by this stuff.", "FABRICATED_TESTIMONY"),
        ("As a registered nurse, here is what I look for.", "BORROWED_CREDENTIAL"),
        ("Pharmacist here, and this is the one.", "BORROWED_CREDENTIAL"),
        ("This is not sponsored, just sharing.", "FALSE_INDEPENDENCE"),
        ("Here's my honest review.", "FALSE_INDEPENDENCE"),
    ],
)
def test_a_synthetic_presenter_cannot_claim_a_life_it_does_not_have(line, code):
    """An AI presenter reporting experience is a testimonial from someone who
    does not exist. The FTC names that case; it is blocked, not warned."""
    report = review_script(
        _script(line, "Tap below to see the details."), max_seconds=15
    )
    assert report.verdict is ComplianceVerdict.BLOCK
    assert code in {f.code for f in report.blocking}
    assert all(f.suggestion for f in report.blocking)


def test_a_presenter_speaking_about_the_product_passes():
    script = _script(
        "Want to wind down without grogginess?",
        "Here's what it is: a magnesium and L-theanine blend taken before bed.",
        "It's designed to help you keep a consistent routine.",
        "Tap below to see the details.",
    )
    report = review_script(script, max_seconds=15)
    assert report.verdict is not ComplianceVerdict.BLOCK, report.as_dict()


@pytest.mark.parametrize(
    "line,code",
    [
        ("Guaranteed results or your money back.", "GUARANTEED_OUTCOME"),
        ("Are you diabetic? Watch this.", "PERSONAL_ATTRIBUTE_DIRECT"),
        ("Lose 10 pounds with one capsule.", "SPECIFIC_WEIGHT_LOSS"),
        ("Doctors hate this one trick.", "MIRACLE_CURE"),
    ],
)
def test_the_ad_text_rules_apply_to_spoken_words(line, code):
    """A guaranteed outcome is no safer said out loud than written down."""
    report = review_script(_script(line, "Tap below."), max_seconds=15)
    assert code in _codes(report)


def test_a_script_longer_than_the_placement_is_blocked():
    """An overrun is a video the placement cuts off mid-sentence."""
    long_line = " ".join(["word"] * 60)
    report = review_script(_script("Here it is.", long_line, "Tap below."), max_seconds=15)
    assert "SCRIPT_TOO_LONG" in {f.code for f in report.blocking}
    budget = int(15 * WORDS_PER_SECOND)
    assert str(budget) in next(f.suggestion for f in report.blocking if f.code == "SCRIPT_TOO_LONG")


def test_an_empty_script_is_blocked():
    report = review_script(SpokenScript(hook=""), max_seconds=15)
    assert "EMPTY_SCRIPT" in {f.code for f in report.blocking}


def test_a_script_is_not_judged_as_ad_fields():
    """A script has no headline counts and carries no disclosure of its own.

    Reviewed as ad text it would be blocked for missing headlines on every
    run, which would make the review mean nothing.
    """
    report = review_script(_script("Here's what it does.", "Tap below."), max_seconds=15)
    assert not {"TOO_FEW_ASSETS", "MISSING_AFFILIATE_DISCLOSURE"} & _codes(report)


def test_advertiser_banned_phrases_are_banned_when_spoken(offer):
    offer.banned_claims = ["clinically proven"]
    offer.required_disclosures = ["Results vary"]
    report = review_script(
        _script("It's clinically proven.", "Tap below."), offer=offer, max_seconds=15
    )
    assert "ADVERTISER_BANNED_CLAIM" in _codes(report)
    # The required disclosure belongs to the ad text, which enforces it. The
    # script is not asked to speak it.
    assert "MISSING_REQUIRED_DISCLOSURE" not in _codes(report)


# --- writing the script -----------------------------------------------------


@pytest.mark.parametrize("angle", [a.key for a in ANGLES])
def test_every_template_script_fits_the_placement_and_passes(offer, settings, angle):
    """Without an API key the templates are the whole product, so every angle
    has to produce something shippable, not just most of them."""
    brief = build_brief(offer, Platform.META, angle_key=angle)
    script = ScriptStudio(writer=TemplateScriptWriter(), settings=settings).write(
        brief, offer=offer, seconds=REEL.max_seconds
    )
    assert script.is_approved, script.report.as_dict()
    assert script.word_count <= int(REEL.max_seconds * WORDS_PER_SECOND)
    assert script.hook and script.call_to_action


@pytest.mark.parametrize("angle", [a.key for a in ANGLES])
def test_template_scripts_never_speak_in_the_first_person(offer, angle):
    brief = build_brief(offer, Platform.META, angle_key=angle)
    text = TemplateScriptWriter().write(brief, seconds=15).text
    assert not re.search(r"\b(I|I'm|I've|me|my)\b", text), text


def test_the_hook_is_not_repeated_by_the_next_line(offer):
    """Fifteen seconds is about 37 words; restating the promise spends six."""
    brief = build_brief(offer, Platform.META, angle_key="problem_solution")
    script = TemplateScriptWriter().write(brief, seconds=15)
    assert "wind down without grogginess" in script.hook
    assert not any("wind down without grogginess" in line for line in script.body)


def test_a_description_keeps_its_acronyms_mid_sentence(offer):
    offer.product_description = "FDA-registered facility blend. Taken nightly."
    brief = build_brief(offer, Platform.META, angle_key="mechanism")
    assert "FDA-registered" in TemplateScriptWriter().write(brief, seconds=15).text


def test_a_description_is_spoken_the_way_a_person_would_say_it(offer):
    brief = build_brief(offer, Platform.META, angle_key="mechanism")
    script = TemplateScriptWriter().write(brief, seconds=15)
    assert "It's a magnesium and L-theanine blend taken before bed." in script.body


def test_a_second_execution_opens_differently(offer):
    first = build_brief(offer, Platform.META, angle_key="mechanism")
    second = build_brief(offer, Platform.META, angle_key="mechanism")
    second.variant_index = 1
    writer = TemplateScriptWriter()
    assert writer.write(first, 15).hook != writer.write(second, 15).hook


class _StubResponse:
    stop_reason = "end_turn"
    stop_details = None
    model = "stub-model"
    usage = None

    def __init__(self, parsed):
        self.parsed_output = parsed


class _ScriptedMessages:
    """Returns each queued script in turn and remembers what it was asked."""

    def __init__(self, *scripts: GeneratedScript):
        self.scripts = list(scripts)
        self.prompts: list[str] = []
        self.kwargs: dict = {}

    def parse(self, **kwargs):
        self.kwargs = kwargs
        self.prompts.append(kwargs["messages"][0]["content"])
        return _StubResponse(self.scripts.pop(0))


class _Client:
    def __init__(self, messages):
        self.messages = messages


def test_a_testimonial_from_the_model_is_sent_back_with_the_finding(offer, settings):
    """The model will reach for the genre's default. The repair loop is what
    turns that into a script that can ship."""
    messages = _ScriptedMessages(
        GeneratedScript(
            hook="I was sceptical until I tried it.",
            body=["Now I sleep through the night."],
            call_to_action="Tap below.",
        ),
        GeneratedScript(
            hook="Here's what CalmLeaf actually does.",
            body=["It's a magnesium blend designed to help you wind down."],
            call_to_action="Tap below to see the details.",
        ),
    )
    writer = LLMScriptWriter(settings, client=_Client(messages))
    brief = build_brief(offer, Platform.META, angle_key="mechanism")
    script = ScriptStudio(writer=writer, settings=settings).write(brief, offer=offer, seconds=15)

    assert script.is_approved
    assert script.generator == "llm"
    assert len(messages.prompts) == 2
    assert "rejected by the policy checker" in messages.prompts[1]
    assert "presenter, not a customer" in messages.prompts[1]
    assert messages.kwargs["output_format"] is GeneratedScript


def test_the_model_is_told_the_presenter_is_synthetic_and_the_budget(offer, settings):
    messages = _ScriptedMessages(
        GeneratedScript(hook="Here's what it does.", body=[], call_to_action="Tap below.")
    )
    brief = build_brief(offer, Platform.META, angle_key="mechanism")
    LLMScriptWriter(settings, client=_Client(messages)).write(brief, seconds=15)

    assert "synthetic" in messages.kwargs["system"]
    assert f"at most {int(15 * WORDS_PER_SECOND)} words" in messages.prompts[0]
    assert "Third-party tested in a US facility" in messages.prompts[0]


def test_a_refusing_model_falls_back_to_templates(offer, settings):
    class Refusing(_StubResponse):
        stop_reason = "refusal"

    class RefusingMessages:
        def parse(self, **kwargs):
            return Refusing(None)

    writer = LLMScriptWriter(settings, client=_Client(RefusingMessages()))
    brief = build_brief(offer, Platform.META, angle_key="mechanism")
    script = ScriptStudio(writer=writer, settings=settings).write(brief, offer=offer, seconds=15)
    assert script.generator == "template"
    assert script.is_approved


def test_scripts_use_templates_without_an_api_key(settings):
    settings.anthropic_api_key = None
    assert isinstance(ScriptStudio(settings=settings).writer, TemplateScriptWriter)


# --- who is on camera --------------------------------------------------------


@pytest.mark.parametrize(
    "persona,code",
    [
        ("a doctor in a white coat", "IMPLIED_AUTHORITY"),
        ("a friendly pharmacist", "IMPLIED_AUTHORITY"),
        ("a teenager on a sofa", "MINOR_PRESENTER"),
        ("someone who looks like a famous actor", "LIKENESS"),
        ("a woman with a slim body", "BODY_IMAGE"),
    ],
)
def test_presenters_that_imply_an_endorsement_are_refused(persona, code):
    plan = build_presenter_plan(REEL, persona)
    assert not plan.is_safe
    assert code in {f["code"] for f in plan.findings}


@pytest.mark.parametrize("voice,gender", [("Rachel", "woman"), ("Chris", "man")])
def test_a_presenter_is_drawn_to_match_the_voice(voice, gender):
    """Found on the first live run: a gender-neutral persona was drawn as a
    man and given a woman's voice, because nothing tied the two together."""
    plan = build_presenter_plan(REEL, voice=voice)
    assert plan.is_safe
    assert f"The presenter is a {gender}." in plan.prompt


def test_a_persona_that_contradicts_the_voice_is_refused_before_it_is_drawn():
    plan = build_presenter_plan(REEL, "a man in his forties in a kitchen", voice="Rachel")
    assert not plan.is_safe
    finding = next(f for f in plan.findings if f["code"] == "VOICE_MISMATCH")
    assert "man's voice" in finding["suggestion"]


def test_a_persona_that_agrees_with_the_voice_is_not_second_guessed():
    plan = build_presenter_plan(REEL, "a woman in her forties in a kitchen", voice="Rachel")
    assert plan.is_safe
    assert "The presenter is a" not in plan.prompt


def test_an_unknown_voice_leaves_the_presenter_to_the_persona():
    plan = build_presenter_plan(REEL, "a man in his forties", voice="MyClonedVoice")
    assert plan.is_safe
    assert "The presenter is a" not in plan.prompt


def test_the_voice_chosen_for_a_plan_reaches_the_presenter(offer, settings):
    plan = plan_presenter_video(offer, persona="a woman at her desk", voice="Chris", settings=settings)
    assert "VOICE_MISMATCH" in {f["code"] for f in plan.findings}
    assert not plan.is_safe


def test_the_default_presenter_is_safe_text_free_and_vertical():
    plan = build_presenter_plan(REEL)
    assert plan.is_safe
    assert "no text" in plan.prompt.lower()
    assert "resembles no real individual" in plan.prompt
    assert plan.prompt_plan(REEL, "meta_reel_video").aspect_ratio == "9:16"


def test_a_supplied_presenter_must_be_something_the_provider_can_fetch():
    assert not build_presenter_plan(REEL, image_url="/home/me/actor.png").is_safe
    supplied = build_presenter_plan(REEL, image_url="https://cdn.example.com/actor.png")
    assert supplied.is_safe and supplied.is_supplied and supplied.prompt == ""


def test_an_image_placement_cannot_carry_a_presenter_video(offer, settings):
    with pytest.raises(ValueError, match="image placement"):
        plan_presenter_video(offer, placement="meta_feed", settings=settings)


def test_a_requested_length_is_capped_by_the_placement(offer, settings):
    plan = plan_presenter_video(offer, seconds=90, settings=settings)
    assert plan.script.word_count <= int(REEL.max_seconds * WORDS_PER_SECOND)


# --- the kie.ai contracts ----------------------------------------------------


@pytest.fixture
def kie_settings(tmp_path) -> Settings:
    return Settings(
        kie_api_key="test-key",
        kie_poll_interval_seconds=0.0,
        kie_poll_timeout_seconds=5.0,
        media_storage_dir=str(tmp_path / "media"),
    )


def _kie(handler, settings):
    return KieClient(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))


def _capture(seen: dict):
    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"code": 200, "data": {"taskId": "t-1"}})

    return handler


def test_elevenlabs_speech_is_requested_as_text_and_a_voice_not_a_prompt(kie_settings):
    seen: dict = {}
    _kie(_capture(seen), kie_settings).submit(
        MediaRequest(
            prompt="Here's what it does.", kind="audio",
            model="elevenlabs/text-to-speech-multilingual-v2", extra={"voice": "Aria"},
        )
    )
    assert seen["input"] == {"text": "Here's what it does.", "voice": "Aria"}


def test_gemini_speech_is_one_speaker_with_one_turn(kie_settings):
    """The shape the live API accepted, after rejecting plain text, a missing
    speaker list, and speaker ids not of the form "Speaker N"."""
    seen: dict = {}
    _kie(_capture(seen), kie_settings).submit(
        MediaRequest(prompt="Here's what it does.", kind="audio", extra={"voice": "Puck"})
    )
    assert seen["model"] == kie_settings.kie_tts_model == "google/gemini-3-1-flash-tts"
    assert seen["input"] == {
        "speakers": [{"speaker_id": "Speaker 1", "voice_name": "Puck"}],
        "dialogue_turns": [{"speaker_id": "Speaker 1", "text": "Here's what it does."}],
    }


def test_speech_falls_back_to_the_configured_voice(kie_settings):
    seen: dict = {}
    _kie(_capture(seen), kie_settings).submit(MediaRequest(prompt="Hi.", kind="audio"))
    assert seen["input"]["speakers"][0]["voice_name"] == kie_settings.kie_tts_voice


def test_the_default_voice_draws_a_matching_default_presenter(settings):
    """The defaults have to agree with each other, or every default run pays
    for a presenter who does not match the voice."""
    plan = build_presenter_plan(REEL, voice=settings.kie_tts_voice)
    assert plan.is_safe
    assert "The presenter is a" in plan.prompt


def test_lip_sync_sends_the_still_and_the_voice_and_nothing_generic(kie_settings):
    seen: dict = {}
    _kie(_capture(seen), kie_settings).submit(
        MediaRequest(
            prompt="Natural delivery.",
            kind="video",
            model=kie_settings.kie_avatar_model,
            aspect_ratio="9:16",
            duration_seconds=12,
            negative_prompt="text",
            reference_image_url="https://cdn.test/face.png",
            extra={"audio_url": "https://cdn.test/voice.mp3"},
        )
    )
    assert seen["model"] == kie_settings.kie_avatar_model
    assert seen["input"] == {
        "image_url": "https://cdn.test/face.png",
        "audio_url": "https://cdn.test/voice.mp3",
        "prompt": "Natural delivery.",
    }


def test_the_short_form_lip_sync_model_also_gets_a_resolution(kie_settings):
    seen: dict = {}
    _kie(_capture(seen), kie_settings).submit(
        MediaRequest(
            prompt="",
            kind="video",
            model="infinitalk/from-audio",
            reference_image_url="https://cdn.test/face.png",
            extra={"audio_url": "https://cdn.test/voice.mp3"},
        )
    )
    assert seen["input"]["resolution"] == "720p"


def test_a_lip_sync_task_missing_an_input_is_refused_before_it_is_paid_for(kie_settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {"taskId": "t-1"}})

    with pytest.raises(MediaError, match="voice track"):
        _kie(handler, kie_settings).submit(
            MediaRequest(
                prompt="",
                kind="video",
                model=kie_settings.kie_avatar_model,
                reference_image_url="https://cdn.test/face.png",
            )
        )
    assert calls == []


# --- generation --------------------------------------------------------------


@pytest.fixture
def launched_creative(session, offer, settings, sandbox_meta):
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan
    from adgenie.models import Creative

    result = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META,
            daily_budget_usd=30.0, angle_count=1, start_paused=False,
        )
    )
    return session.get(Creative, result.creative_ids[0])


@pytest.fixture
def studio(session, settings, tmp_path):
    settings.media_storage_dir = str(tmp_path / "media")
    return MediaStudio(session, settings, provider=SandboxMediaProvider())


def test_the_voice_says_exactly_the_reviewed_words(studio, launched_creative):
    """The point of synthesising speech rather than prompting a video model
    with dialogue: the audio is generated from the approved text itself."""
    plan = studio.presenter_plan_for(launched_creative)
    asset = studio.generate_presenter_video(plan, creative_id=launched_creative.id)

    assert asset.status is MediaStatus.READY, asset.error
    requests = studio.provider.generated
    assert [r.kind for r in requests] == ["image", "audio", "video"]
    assert requests[1].prompt == plan.script.text
    assert asset.prompt == plan.script.text


def test_the_lip_sync_is_driven_by_the_voice_and_the_still(studio, launched_creative):
    plan = studio.presenter_plan_for(launched_creative)
    studio.generate_presenter_video(plan, creative_id=launched_creative.id)
    image, voice, video = studio.provider.generated

    assert video.model == studio.settings.kie_avatar_model
    assert video.extra["audio_url"].endswith(".mp3")
    assert video.reference_image_url.endswith(".png")
    assert image.aspect_ratio == video.aspect_ratio == "9:16"


def test_the_finished_video_is_a_stored_video_asset_on_the_creative(
    studio, session, launched_creative
):
    plan = studio.presenter_plan_for(launched_creative)
    asset = studio.generate_presenter_video(plan, creative_id=launched_creative.id)

    assert asset.kind is MediaKind.VIDEO
    assert asset.creative_id == launched_creative.id
    assert asset.local_path and asset.content_hash and asset.bytes > 0
    assert (asset.width, asset.height) == (1080, 1920)
    assert asset.extra["format"] == "presenter"
    assert asset.extra["synthetic_presenter"] is True
    assert set(asset.extra["steps"]) == {"presenter", "voice", "lip_sync"}


def test_the_presenter_still_is_kept_but_never_becomes_the_ads_image(
    studio, session, launched_creative
):
    """Attached to the creative, the still would be uploaded as the ad's
    image, including when the video failed."""
    plan = studio.presenter_plan_for(launched_creative)
    video = studio.generate_presenter_video(plan, creative_id=launched_creative.id)
    session.flush()

    on_creative = session.execute(
        select(MediaAsset).where(MediaAsset.creative_id == launched_creative.id)
    ).scalars().all()
    assert [a.id for a in on_creative] == [video.id]

    still = session.get(MediaAsset, video.extra["steps"]["presenter"]["asset_id"])
    assert still.kind is MediaKind.IMAGE and still.status is MediaStatus.READY
    assert still.creative_id is None and still.offer_id == plan.offer_id
    assert still.extra["role"] == "presenter"


def test_a_plan_that_fails_review_pays_for_nothing(studio, launched_creative, offer):
    class Testimonial:
        name = "stub"

        def write(self, brief, seconds):
            return _script("I tried it and it changed my life.", "Tap below.")

    plan = plan_presenter_video(
        offer,
        script_studio=ScriptStudio(writer=Testimonial(), settings=studio.settings),
        settings=studio.settings,
    )
    asset = studio.generate_presenter_video(plan, creative_id=launched_creative.id)

    assert asset.status is MediaStatus.REJECTED
    assert studio.provider.generated == [], "nothing may be submitted for a rejected plan"
    assert "FABRICATED_TESTIMONY" in asset.error


def test_an_unsafe_presenter_pays_for_nothing(studio, launched_creative):
    plan = studio.presenter_plan_for(launched_creative, persona="a nurse in scrubs")
    asset = studio.generate_presenter_video(plan, creative_id=launched_creative.id)
    assert asset.status is MediaStatus.REJECTED
    assert studio.provider.generated == []


def test_a_supplied_presenter_skips_image_generation(studio, launched_creative):
    url = "https://cdn.example.com/our-presenter.png"
    plan = studio.presenter_plan_for(launched_creative, presenter_image_url=url)
    asset = studio.generate_presenter_video(plan, creative_id=launched_creative.id)

    assert asset.status is MediaStatus.READY
    assert [r.kind for r in studio.provider.generated] == ["audio", "video"]
    assert studio.provider.generated[1].reference_image_url == url


class _FailsAt(SandboxMediaProvider):
    """Succeeds until the named kind of task, which the provider rejects."""

    def __init__(self, kind: str, payload: dict | None = None, code: str = "GENERATION_FAILED"):
        super().__init__()
        self.kind = kind
        self.payload = payload
        self.code = code

    def generate(self, request: MediaRequest) -> MediaResult:
        if request.kind == self.kind:
            self.generated.append(request)
            raise MediaError(
                f"{self.kind} task failed upstream", code=self.code, payload=self.payload
            )
        return super().generate(request)


def test_a_failure_part_way_records_what_was_already_paid_for(
    session, settings, tmp_path, launched_creative
):
    """A resubmission is charged again. Recording the finished tasks is what
    lets an operator see the still and the voice exist before retrying."""
    settings.media_storage_dir = str(tmp_path / "media")
    studio = MediaStudio(session, settings, provider=_FailsAt("video"))
    plan = studio.presenter_plan_for(launched_creative)
    asset = studio.generate_presenter_video(plan, creative_id=launched_creative.id)

    assert asset.status is MediaStatus.FAILED
    assert asset.error.startswith("lip-sync:")
    assert asset.extra["steps"]["presenter"]["task_id"]
    assert asset.extra["steps"]["voice"]["task_id"]
    assert "lip_sync" not in asset.extra["steps"]


def test_a_timed_out_task_is_recorded_so_it_is_collected_not_resubmitted(
    session, settings, tmp_path, launched_creative
):
    """A timed-out task may still finish and is charged either way."""
    settings.media_storage_dir = str(tmp_path / "media")
    provider = _FailsAt("video", payload={"task_id": "t-slow"}, code="TIMEOUT")
    studio = MediaStudio(session, settings, provider=provider)
    asset = studio.generate_presenter_video(
        studio.presenter_plan_for(launched_creative), creative_id=launched_creative.id
    )

    assert asset.status is MediaStatus.FAILED
    assert asset.extra["steps"]["lip_sync"] == {"task_id": "t-slow", "state": "unfinished"}


def test_a_failed_voice_never_reaches_the_lip_sync(session, settings, tmp_path, launched_creative):
    settings.media_storage_dir = str(tmp_path / "media")
    studio = MediaStudio(session, settings, provider=_FailsAt("audio"))
    plan = studio.presenter_plan_for(launched_creative)
    asset = studio.generate_presenter_video(plan, creative_id=launched_creative.id)

    assert asset.status is MediaStatus.FAILED
    assert asset.error.startswith("voice:")
    assert [r.kind for r in studio.provider.generated] == ["image", "audio"]


def test_dry_run_simulates_presenter_generation_instead_of_billing(
    session, launched_creative, tmp_path
):
    """Generation is the one paid side effect a dry run could otherwise have."""
    dry = Settings(
        kie_api_key="real-looking-key",
        dry_run=True,
        anthropic_api_key=None,
        media_storage_dir=str(tmp_path / "media"),
    )
    studio = MediaStudio(session, dry)
    assert isinstance(studio.provider, SandboxMediaProvider)

    asset = studio.generate_presenter_video(
        studio.presenter_plan_for(launched_creative), creative_id=launched_creative.id
    )
    assert asset.status is MediaStatus.READY
    assert asset.provider == "sandbox"


# --- launch, CLI and API -----------------------------------------------------


def test_a_presenter_launch_builds_the_ad_on_the_video(
    session, offer, settings, sandbox_meta, tmp_path
):
    """Media generated after launch reaches the ad account but not the ad, so
    a presenter launch has to make the video before the ad is created."""
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    settings.media_storage_dir = str(tmp_path / "media")
    media = MediaStudio(session, settings, provider=SandboxMediaProvider())
    result = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta, media_studio=media
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META, daily_budget_usd=30.0,
            angle_count=1, generate_media=True, media_kind="presenter",
        )
    )

    assert result.ok and result.creative_ids
    asset = session.get(MediaAsset, result.media_asset_ids[0])
    assert asset.kind is MediaKind.VIDEO and asset.status is MediaStatus.READY
    operations = [name for name, _ in sandbox_meta.calls]
    uploads = [detail for name, detail in sandbox_meta.calls if name == "upload_media"]
    assert {"kind": "video"}.items() <= uploads[0].items()
    assert operations.index("upload_media") < operations.index("create_creative")


def test_a_presenter_launch_on_search_generates_nothing(
    session, offer, settings, sandbox_google, tmp_path
):
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    settings.media_storage_dir = str(tmp_path / "media")
    provider = SandboxMediaProvider()
    media = MediaStudio(session, settings, provider=provider)
    CampaignLauncher(
        session, settings=settings, platform_client=sandbox_google, media_studio=media
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.GOOGLE, daily_budget_usd=30.0,
            angle_count=1, keywords=["sleep aid"], generate_media=True,
            media_kind="presenter",
        )
    )
    assert provider.generated == []


def test_the_cli_exposes_presenter_videos():
    parser = build_parser()
    args = parser.parse_args(["ugc", "--creative", "3", "--preview", "--persona", "a chef"])
    assert (args.creative, args.preview, args.persona) == (3, True, "a chef")
    launch = parser.parse_args(
        ["launch", "--offer", "1", "--platform", "meta", "--budget", "40",
         "--media-kind", "presenter"]
    )
    assert launch.media_kind == "presenter"


def test_the_preview_endpoint_shows_the_script_and_generates_nothing(
    api_client, session, settings, launched_creative, tmp_path
):
    settings.media_storage_dir = str(tmp_path / "media")
    before = session.execute(select(MediaAsset)).scalars().all()

    response = api_client.post(f"/api/media/presenter/{launched_creative.id}/preview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["would_generate"] is True
    assert body["script"]["text"]
    assert body["presenter"]["source"] == "generated"
    assert session.execute(select(MediaAsset)).scalars().all() == before


def test_the_preview_endpoint_reports_a_blocked_presenter(
    api_client, launched_creative
):
    response = api_client.post(
        f"/api/media/presenter/{launched_creative.id}/preview",
        params={"persona": "a surgeon in scrubs"},
    )
    body = response.json()
    assert body["would_generate"] is False
    assert "IMPLIED_AUTHORITY" in {f["code"] for f in body["findings"]}


def test_the_generate_endpoint_records_the_video(
    api_client, settings, launched_creative, tmp_path
):
    settings.media_storage_dir = str(tmp_path / "media")
    response = api_client.post(f"/api/media/presenter/{launched_creative.id}")
    assert response.status_code == 200, response.text
    asset = response.json()["asset"]
    assert asset["status"] == "ready"
    assert asset["kind"] == "video"
    assert asset["format"] == "presenter"


def test_the_endpoints_reject_an_image_placement(api_client, launched_creative):
    response = api_client.post(
        f"/api/media/presenter/{launched_creative.id}/preview",
        params={"placement": "meta_feed"},
    )
    assert response.status_code == 422
