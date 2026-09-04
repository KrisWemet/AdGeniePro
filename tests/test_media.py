"""Media generation: prompt screening, provider handling, storage, wiring."""

from __future__ import annotations

import json
import struct

import httpx
import pytest

from adgenie.config import Settings
from adgenie.media.base import MediaError, MediaRequest
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
    assert seen["input"]["prompt"] == "a photo"
    assert seen["input"]["aspect_ratio"] == "4:5"


def test_video_requests_use_the_video_model_and_duration(kie_settings):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": {"taskId": "t"}})

    _kie(handler, kie_settings).submit(
        MediaRequest(prompt="x", kind="video", duration_seconds=8)
    )
    assert seen["model"] == kie_settings.kie_video_model
    assert seen["input"]["duration"] == 8


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


def test_a_timeout_says_not_to_resubmit(kie_settings):
    """Resubmitting a running task is charged twice."""
    kie_settings.kie_poll_timeout_seconds = 0.2

    def handler(request):
        if request.url.path.endswith("createTask"):
            return httpx.Response(200, json={"data": {"taskId": "t-1"}})
        return httpx.Response(200, json={"data": {"state": "generating"}})

    with pytest.raises(MediaError, match="charged again"):
        _kie(handler, kie_settings).generate(MediaRequest(prompt="x"))


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


def test_ready_assets_are_attached_to_the_creative(
    session, settings, launched_creative, tmp_path
):
    settings.media_storage_dir = str(tmp_path / "media")
    MediaStudio(session, settings, provider=SandboxMediaProvider()).generate_for_creative(
        launched_creative, platform=Platform.META
    )
    assert len(launched_creative.media_urls) == 3


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
