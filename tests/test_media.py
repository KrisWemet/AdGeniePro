"""Media generation: prompt screening, provider handling, storage, wiring."""

from __future__ import annotations

import json
import struct

import httpx
import pytest

from adgenie.config import Settings
from adgenie.media.base import MediaError, MediaRequest, MediaResult
from adgenie.media.kie import KieClient, _extract_urls
from adgenie.media.prompts import (
    NEGATIVE_PROMPT,
    build_image_prompt,
    build_video_prompt,
    review_media_prompt,
)
from adgenie.media.sandbox import SandboxMediaProvider, render_placeholder_png
from adgenie.media.specs import default_placements, get_media_spec
from adgenie.media.store import MediaStore
from adgenie.media.studio import MediaStudio
from adgenie.models import MediaKind, MediaStatus, Platform


@pytest.fixture
def kie_settings(tmp_path) -> Settings:
    return Settings(
        kie_api_key="test-key",
        kie_poll_interval_seconds=0.0,
        kie_poll_timeout_seconds=5.0,
        media_storage_dir=str(tmp_path / "media"),
    )


# --- placement specs -------------------------------------------------------


def test_specs_match_the_sizes_the_platforms_serve():
    assert get_media_spec("meta_feed").aspect_ratio == "4:5"
    assert (get_media_spec("meta_story").width, get_media_spec("meta_story").height) == (1080, 1920)
    assert get_media_spec("google_landscape").aspect_ratio == "1.91:1"
    assert get_media_spec("meta_reel_video").kind == "video"


def test_unknown_placement_is_rejected():
    with pytest.raises(ValueError, match="unknown placement"):
        get_media_spec("tiktok_vertical")


def test_search_ads_have_no_placements():
    """Generating for a text-only format spends money on unrenderable assets."""
    assert default_placements(Platform.GOOGLE, "image", "responsive_search_ad") == []
    assert default_placements(Platform.GOOGLE, "image") != []
    assert default_placements(Platform.META, "image", "feed") != []


# --- prompt screening ------------------------------------------------------


class _Offer:
    name = "CalmLeaf Sleep Blend"
    product_description = "A magnesium and L-theanine blend taken before bed."


def test_a_clean_prompt_passes_and_carries_the_negative_prompt():
    plan = build_image_prompt(_Offer(), angle="mechanism", placement="meta_feed")
    assert plan.is_safe
    assert plan.negative_prompt == NEGATIVE_PROMPT
    assert plan.aspect_ratio == "4:5"
    assert "no text" in plan.prompt.lower() or "contain no text" in plan.prompt.lower()


@pytest.mark.parametrize(
    "direction,code",
    [
        ("a before and after body transformation", "BEFORE_AFTER_IMAGERY"),
        ("show a slimmer waist and flatter stomach", "BODY_IMAGE"),
        ("add a fake play button overlay", "FAKE_INTERFACE"),
        ("in the style of Nike with a celebrity", "THIRD_PARTY_IP"),
        ("a doctor recommending the product", "IMPLIED_MEDICAL_ENDORSEMENT"),
        ("a close up of an infected rash", "SHOCKING_MEDICAL"),
        ("a pile of cash spread on a table", "WEALTH_BAIT"),
    ],
)
def test_policy_violating_directions_are_caught_before_generating(direction, code):
    plan = build_image_prompt(_Offer(), angle="mechanism", extra_direction=direction)
    assert not plan.is_safe
    assert code in {f["code"] for f in plan.findings}
    assert all(f["suggestion"] for f in plan.findings)


def test_review_is_callable_on_raw_text():
    assert review_media_prompt("a clean product photo") == []
    assert review_media_prompt("before and after photos") != []


def test_angle_changes_the_visual_direction():
    mechanism = build_image_prompt(_Offer(), angle="mechanism").prompt
    social = build_image_prompt(_Offer(), angle="social_proof").prompt
    assert mechanism != social
    assert "mechanism legible" in mechanism
    assert "candid" in social


def test_story_placement_warns_about_the_safe_area():
    assert "middle 60%" in build_image_prompt(_Offer(), placement="meta_story").prompt


def test_video_prompt_states_the_opening_beat():
    plan = build_video_prompt(
        _Offer(), angle="problem_solution", hook_line="Wind down without grogginess"
    )
    assert plan.kind == "video"
    assert plan.duration_seconds > 0
    assert "first second" in plan.prompt
    assert "without displaying it as text" in plan.prompt


def test_generated_people_are_adults_and_video_people_say_nothing():
    """The first live Veo clip, for a sleep supplement, opened on a sleeping
    baby. Speech is only ever synthesised from a reviewed script; a line the
    model makes up would be an unreviewed claim."""
    video = build_video_prompt(_Offer(), angle="problem_solution").prompt
    image = build_image_prompt(_Offer(), angle="problem_solution").prompt
    assert "clearly an adult" in video
    assert "clearly an adult" in image
    assert "no speech, narration or singing" in video


def test_a_scene_replaces_the_default_shot_and_the_product_close():
    """With no real product image, a model asked for "the product" invents its
    packaging; the first live clip closed on a made-up bottle."""
    scene = "Water dripping from an air conditioner line into a glass jar."
    video = build_video_prompt(_Offer(), angle="mechanism", scene=scene)
    assert "Scene: Water dripping from an air conditioner line into a glass jar." in video.prompt
    assert "shot of the product" not in video.prompt
    assert "component shot" not in video.prompt
    assert video.is_safe

    image = build_image_prompt(_Offer(), angle="mechanism", scene=scene)
    assert "Composition: Water dripping from an air conditioner line" in image.prompt
    assert "component shot" not in image.prompt


def test_a_scene_is_screened_like_any_other_prompt():
    plan = build_video_prompt(
        _Offer(), angle="mechanism", scene="a before and after comparison of two bodies"
    )
    assert not plan.is_safe


def test_the_media_command_takes_a_scene(session, settings, launched_creative, tmp_path):
    from adgenie.cli import build_parser

    args = build_parser().parse_args(
        ["media", "--creative", "1", "--kind", "video", "--scene", "A glass jar."]
    )
    assert args.scene == "A glass jar."

    settings.media_storage_dir = str(tmp_path / "media")
    [asset] = MediaStudio(
        session, settings, provider=SandboxMediaProvider()
    ).generate_for_creative(
        launched_creative, kind="video", placements=["meta_reel_video"], scene=args.scene
    )
    assert "Scene: A glass jar." in asset.prompt


def test_video_duration_is_capped_by_the_placement():
    plan = build_video_prompt(_Offer(), placement="meta_reel_video", seconds=600)
    assert plan.duration_seconds <= get_media_spec("meta_reel_video").max_seconds


def test_placement_kind_mismatch_is_rejected():
    with pytest.raises(ValueError, match="video placement"):
        build_image_prompt(_Offer(), placement="meta_reel_video")
    with pytest.raises(ValueError, match="image placement"):
        build_video_prompt(_Offer(), placement="meta_feed")


# --- the kie.ai client -----------------------------------------------------


def _kie(handler, settings):
    return KieClient(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_submit_sends_model_and_input(kie_settings):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"code": 200, "data": {"taskId": "t-1"}})

    task_id = _kie(handler, kie_settings).submit(
        MediaRequest(prompt="a photo", aspect_ratio="4:5", negative_prompt="text")
    )
    assert task_id == "t-1"
    assert seen["model"] == kie_settings.kie_image_model
    assert seen["model"] == "nano-banana-pro"
    assert seen["input"]["prompt"] == "a photo Avoid: text."
    assert seen["input"]["aspect_ratio"] == "4:5"
    assert seen["input"]["image_input"] == []
    assert seen["input"]["resolution"] == "1K"
    assert "negative_prompt" not in seen["input"]


def test_a_model_with_no_negative_prompt_field_gets_the_list_in_the_prompt(kie_settings):
    """Sent as a field, it is rejected; left out, it never reaches the model,
    which is how the first live Veo clip came back with garbled label text."""
    for kind, model in (("image", "nano-banana-pro"), ("video", "veo-3-1")):
        seen: dict = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"data": {"taskId": "t"}})

        _kie(handler, kie_settings).submit(
            MediaRequest(prompt="A kitchen.", kind=kind, negative_prompt="garbled text, logos")
        )
        assert seen["model"] == model
        assert seen["input"]["prompt"] == "A kitchen. Avoid: garbled text, logos."
        assert "negative_prompt" not in seen["input"]


def test_a_model_with_the_field_keeps_the_negative_prompt_separate(kie_settings):
    seen: dict = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"taskId": "t"}})

    _kie(handler, kie_settings).submit(
        MediaRequest(prompt="A kitchen.", model="some/other-model", negative_prompt="logos")
    )
    assert seen["input"]["prompt"] == "A kitchen."
    assert seen["input"]["negative_prompt"] == "logos"


def test_video_requests_use_the_current_veo_contract(kie_settings):
    """kie.ai documents enable_fallback as deprecated and asks for it to be
    removed from requests; translation is off unless asked for."""
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"taskId": "t"}})

    _kie(handler, kie_settings).submit(
        MediaRequest(prompt="x", kind="video", duration_seconds=8)
    )
    assert seen["model"] == "veo-3-1"
    assert seen["input"]["generation_type"] == "TEXT_2_VIDEO"
    assert seen["input"]["aspect_ratio"] == "16:9"
    assert "enable_fallback" not in seen["input"]
    assert "enable_translation" not in seen["input"]
    assert "duration" not in seen["input"]


def test_veo_is_asked_for_the_resolution_the_placement_needs(kie_settings):
    """kie.ai's default is 720p. Left to it, a 1080x1920 Reel placement came
    back 720x1280 while the asset recorded 1080x1920."""
    def resolution(**fields) -> str:
        seen: dict = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"data": {"taskId": "t"}})

        _kie(handler, kie_settings).submit(MediaRequest(prompt="x", kind="video", **fields))
        return seen["input"]["resolution"]

    assert resolution(aspect_ratio="9:16", width=1080, height=1920) == "1080p"
    assert resolution(aspect_ratio="9:16", width=720, height=1280) == "720p"
    assert resolution(width=1080, height=1920, extra={"resolution": "4k"}) == "4k"


def test_a_deprecated_veo_flag_is_not_sent_even_when_asked_for(kie_settings):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"taskId": "t"}})

    _kie(handler, kie_settings).submit(
        MediaRequest(
            prompt="x", kind="video",
            extra={"enable_fallback": True, "resolution": "1080p"},
        )
    )
    assert "enable_fallback" not in seen["input"]
    assert seen["input"]["resolution"] == "1080p"


def test_an_error_code_inside_a_200_response_is_still_an_error(kie_settings):
    """kie.ai reports failures in the envelope, not the status line."""
    handler = lambda r: httpx.Response(200, json={"code": 402, "msg": "insufficient credits"})
    with pytest.raises(MediaError, match="insufficient credits"):
        _kie(handler, kie_settings).submit(MediaRequest(prompt="x"))


def test_a_missing_task_id_is_reported(kie_settings):
    handler = lambda r: httpx.Response(200, json={"code": 200, "data": {}})
    with pytest.raises(MediaError, match="no task id"):
        _kie(handler, kie_settings).submit(MediaRequest(prompt="x"))


def test_out_of_credit_is_explained(kie_settings):
    handler = lambda r: httpx.Response(402, json={"msg": "no credit"})
    with pytest.raises(MediaError, match="out of credit"):
        _kie(handler, kie_settings).submit(MediaRequest(prompt="x"))


def test_polling_reads_the_unified_jobs_envelope(kie_settings):
    payload = {
        "code": 200,
        "data": {
            "taskId": "t-1",
            "state": "success",
            "resultJson": json.dumps({"resultUrls": ["https://cdn.test/a.png"]}),
        },
    }
    result = _kie(lambda r: httpx.Response(200, json=payload), kie_settings).poll("t-1")
    assert result.ok
    assert result.urls == ["https://cdn.test/a.png"]


def test_polling_reads_the_legacy_per_model_envelope(kie_settings):
    payload = {
        "code": 200,
        "data": {"successFlag": 1, "response": {"resultUrls": ["https://cdn.test/v.mp4"]}},
    }
    result = _kie(lambda r: httpx.Response(200, json=payload), kie_settings).poll("t-1")
    assert result.ok
    assert result.urls == ["https://cdn.test/v.mp4"]


def test_generate_polls_until_the_task_finishes(kie_settings):
    calls = {"n": 0}

    def handler(request):
        if request.url.path.endswith("createTask"):
            return httpx.Response(200, json={"data": {"taskId": "t-1"}})
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, json={"data": {"state": "generating"}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "state": "success",
                    "resultJson": json.dumps({"resultUrls": ["https://cdn.test/a.png"]}),
                }
            },
        )

    result = _kie(handler, kie_settings).generate(MediaRequest(prompt="x"))
    assert result.ok
    assert calls["n"] == 3


def test_a_failed_task_raises_with_its_reason(kie_settings):
    def handler(request):
        if request.url.path.endswith("createTask"):
            return httpx.Response(200, json={"data": {"taskId": "t-1"}})
        return httpx.Response(
            200, json={"data": {"state": "fail", "failMsg": "prompt rejected"}}
        )

    with pytest.raises(MediaError, match="prompt rejected"):
        _kie(handler, kie_settings).generate(MediaRequest(prompt="x"))


def test_a_failed_task_carries_its_id_and_what_it_was_charged(kie_settings):
    """The shape a live Veo failure took. kie.ai's support asks for the task
    id, and the charge shows the failure cost nothing."""
    def handler(request):
        if request.url.path.endswith("createTask"):
            return httpx.Response(200, json={"data": {"taskId": "t-1"}})
        return httpx.Response(200, json={"data": {
            "taskId": "t-1", "state": "fail", "failCode": "500",
            "failMsg": "Internal Error, Please try again later.",
            "creditsConsumed": 0.0,
        }})

    with pytest.raises(MediaError, match="Internal Error") as failure:
        _kie(handler, kie_settings).generate(MediaRequest(prompt="x", kind="video"))
    assert failure.value.code == "GENERATION_FAILED"
    assert failure.value.payload["task_id"] == "t-1"
    assert failure.value.payload["creditsConsumed"] == 0.0


def test_a_timeout_says_not_to_resubmit(kie_settings):
    """Resubmitting a running task is charged twice."""
    kie_settings.kie_poll_timeout_seconds = 0.2

    def handler(request):
        if request.url.path.endswith("createTask"):
            return httpx.Response(200, json={"data": {"taskId": "t-1"}})
        return httpx.Response(200, json={"data": {"state": "generating"}})

    with pytest.raises(MediaError, match="charged again"):
        _kie(handler, kie_settings).generate(MediaRequest(prompt="x"))


def test_a_1080p_veo_result_is_read_from_its_nested_envelope(kie_settings):
    """The shape a live 1080p Veo task returned, trimmed. Read as success with
    no output, it cost 65 credits and produced nothing on disk."""
    payload = {"code": 200, "data": {
        "taskId": "t-hd", "state": "success", "creditsConsumed": 65.0,
        "resultJson": json.dumps({"code": 200, "data": {
            "origin_urls": ["https://tempfile.test/v/original.mp4"],
            "result_urls": ["https://tempfile.test/v/upscaled.mp4"],
            "image_urls": ["https://tempfile.test/v/thumbnail.jpg"],
            "high_resolution_pending": False,
        }}),
    }}
    result = _kie(lambda r: httpx.Response(200, json=payload), kie_settings).poll("t-hd")
    assert result.ok
    assert result.urls[0] == "https://tempfile.test/v/upscaled.mp4"
    assert "https://tempfile.test/v/thumbnail.jpg" not in result.urls


def test_url_extraction_handles_every_shape():
    assert _extract_urls({"resultJson": json.dumps({"resultUrls": ["https://a/1.png"]})})
    assert _extract_urls({"response": {"resultUrls": ["https://b/1.mp4"]}})
    assert _extract_urls({"imageUrl": "https://c/x.png"}) == ["https://c/x.png"]
    assert _extract_urls({"state": "generating"}) == []
    assert _extract_urls({"resultJson": "not json"}) == []


def test_missing_api_key_is_a_clear_error():
    with pytest.raises(MediaError, match="KIE_API_KEY"):
        KieClient(Settings(kie_api_key=None))


# --- the sandbox provider --------------------------------------------------


def test_placeholder_is_a_real_png_at_the_requested_size():
    png = render_placeholder_png(1080, 1350, "seed")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (1080, 1350)


def test_placeholder_is_deterministic_but_varies_by_prompt():
    assert render_placeholder_png(64, 64, "a") == render_placeholder_png(64, 64, "a")
    assert render_placeholder_png(64, 64, "a") != render_placeholder_png(64, 64, "b")


def test_sandbox_exercises_the_polling_loop():
    provider = SandboxMediaProvider(polls_before_ready=3)
    result = provider.generate(MediaRequest(prompt="x"))
    assert result.ok
    assert provider.tasks[result.task_id].polls == 4


def test_sandbox_can_simulate_failure():
    result = SandboxMediaProvider(fail=True).generate(MediaRequest(prompt="x"))
    assert not result.ok
    assert result.state == "fail"


# --- the store -------------------------------------------------------------


def test_store_downloads_and_content_addresses(kie_settings, tmp_path):
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 100

    def handler(request):
        return httpx.Response(200, content=payload, headers={"content-type": "image/png"})

    store = MediaStore(
        kie_settings, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    stored = store.fetch("https://cdn.test/a.png", subdir="offer-1")

    assert stored.path.exists()
    assert stored.path.suffix == ".png"
    assert stored.bytes == len(payload)
    assert stored.path.read_bytes() == payload
    # Content addressing means a repeat download does not duplicate the file.
    again = store.fetch("https://cdn.test/a.png", subdir="offer-1")
    assert again.path == stored.path


def test_store_reports_an_expired_url_clearly(kie_settings):
    handler = lambda r: httpx.Response(404)
    store = MediaStore(
        kie_settings, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(RuntimeError, match="expire"):
        store.fetch("https://cdn.test/gone.png")


def test_store_leaves_no_partial_file_on_failure(kie_settings, tmp_path):
    def handler(request):
        return httpx.Response(500)

    store = MediaStore(
        kie_settings, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(RuntimeError):
        store.fetch("https://cdn.test/x.png", subdir="offer-1")
    assert not list((store.root / "offer-1").glob("*.part")) if store.root.exists() else True


def test_public_url_is_none_without_a_configured_base(kie_settings, tmp_path):
    store = MediaStore(kie_settings)
    assert store.public_url_for(tmp_path / "a.png") is None
    kie_settings.media_public_base_url = "https://cdn.example.com/media"
    assert store.public_url_for(tmp_path / "a.png", "offer-1").endswith(
        "/media/offer-1/a.png"
    )


# --- the studio ------------------------------------------------------------


@pytest.fixture
def launched_creative(session, offer, settings, sandbox_meta):
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    result = CampaignLauncher(
        session, settings=settings, platform_client=sandbox_meta
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META,
            daily_budget_usd=30.0, angle_count=1, start_paused=False,
        )
    )
    from adgenie.models import Creative

    return session.get(Creative, result.creative_ids[0])


def test_studio_generates_one_asset_per_placement(
    session, settings, launched_creative, tmp_path
):
    settings.media_storage_dir = str(tmp_path / "media")
    studio = MediaStudio(session, settings, provider=SandboxMediaProvider())
    assets = studio.generate_for_creative(launched_creative, platform=Platform.META)

    assert len(assets) == 3
    assert all(a.status is MediaStatus.READY for a in assets)
    assert {a.aspect_ratio for a in assets} == {"4:5", "1:1", "9:16"}
    for asset in assets:
        assert asset.local_path and asset.bytes > 0
        assert asset.content_hash


def test_ready_assets_are_attached_as_fetchable_urls(
    session, settings, launched_creative, tmp_path
):
    settings.media_storage_dir = str(tmp_path / "media")
    settings.media_public_base_url = "https://cdn.example.com/media"
    MediaStudio(session, settings, provider=SandboxMediaProvider()).generate_for_creative(
        launched_creative, platform=Platform.META
    )
    assert len(launched_creative.media_urls) == 3
    assert all(u.startswith("https://") for u in launched_creative.media_urls)


def test_a_local_path_is_never_passed_off_as_an_image_url(
    session, settings, launched_creative, tmp_path
):
    """Meta fetches the image over HTTP; a filesystem path would be rejected."""
    settings.media_storage_dir = str(tmp_path / "media")
    settings.media_public_base_url = None
    assets = MediaStudio(
        session, settings, provider=SandboxMediaProvider()
    ).generate_for_creative(launched_creative, platform=Platform.META)

    assert all(a.status is MediaStatus.READY for a in assets)
    assert all(a.local_path for a in assets), "the files are still on disk"
    assert launched_creative.media_urls == []


def test_a_rejected_prompt_is_never_generated(session, settings, launched_creative, tmp_path):
    """Screening first is what stops a banned image being paid for."""
    settings.media_storage_dir = str(tmp_path / "media")
    provider = SandboxMediaProvider()
    studio = MediaStudio(session, settings, provider=provider)

    plan = build_image_prompt(
        _Offer(), angle="mechanism", extra_direction="a before and after transformation"
    )
    asset = studio.generate_from_prompt(plan, creative_id=launched_creative.id)

    assert asset.status is MediaStatus.REJECTED
    assert provider.generated == [], "nothing may be submitted for a rejected prompt"
    assert "BEFORE_AFTER_IMAGERY" in asset.error


def test_a_provider_failure_is_recorded_not_raised(
    session, settings, launched_creative, tmp_path
):
    settings.media_storage_dir = str(tmp_path / "media")
    studio = MediaStudio(
        session, settings, provider=SandboxMediaProvider(fail=True)
    )
    assets = studio.generate_for_creative(launched_creative, platform=Platform.META)
    assert all(a.status is MediaStatus.FAILED for a in assets)
    assert launched_creative.media_urls == []


class _FailsUpstream(SandboxMediaProvider):
    def __init__(self, code: str, payload: dict):
        super().__init__()
        self.code, self.payload = code, payload

    def generate(self, request: MediaRequest) -> MediaResult:
        raise MediaError("kie.ai task failed", code=self.code, payload=self.payload)


def test_a_task_that_failed_upstream_keeps_its_id_and_its_charge(
    session, settings, launched_creative, tmp_path
):
    """Before this, a failed asset kept its task id only inside the error text
    and recorded no charge, though kie.ai reported both."""
    settings.media_storage_dir = str(tmp_path / "media")
    provider = _FailsUpstream(
        "GENERATION_FAILED", {"task_id": "t-f", "creditsConsumed": 0.0}
    )
    [asset] = MediaStudio(session, settings, provider=provider).generate_for_creative(
        launched_creative, kind="video", placements=["meta_reel_video"]
    )
    assert asset.status is MediaStatus.FAILED
    assert asset.task_id == "t-f"
    assert asset.extra["credits"] == 0.0


def test_a_timed_out_task_keeps_its_id_but_claims_no_charge(
    session, settings, launched_creative, tmp_path
):
    """It may still finish, so its id is what collects it. What it costs is
    not known until it does."""
    settings.media_storage_dir = str(tmp_path / "media")
    provider = _FailsUpstream("TIMEOUT", {"task_id": "t-slow"})
    [asset] = MediaStudio(session, settings, provider=provider).generate_for_creative(
        launched_creative, kind="video", placements=["meta_reel_video"]
    )
    assert asset.task_id == "t-slow"
    assert "credits" not in asset.extra


def test_video_generation_records_duration(session, settings, launched_creative, tmp_path):
    settings.media_storage_dir = str(tmp_path / "media")
    studio = MediaStudio(session, settings, provider=SandboxMediaProvider())
    assets = studio.generate_for_creative(
        launched_creative, kind="video", platform=Platform.META,
        placements=["meta_reel_video"],
    )
    assert len(assets) == 1
    assert assets[0].kind is MediaKind.VIDEO
    assert assets[0].duration_seconds > 0


def test_search_ads_generate_nothing(session, settings, launched_creative, tmp_path):
    settings.media_storage_dir = str(tmp_path / "media")
    provider = SandboxMediaProvider()
    assets = MediaStudio(session, settings, provider=provider).generate_for_creative(
        launched_creative, platform=Platform.GOOGLE, ad_format="responsive_search_ad"
    )
    assert assets == []
    assert provider.generated == []
