"""Getting generated media into the ad account.

Generation produces a file; an ad needs a reference that still resolves in six
weeks. The gap between the two is where a campaign serves a broken image while
continuing to spend, so these tests are mostly about that gap.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from adgenie.config import Settings
from adgenie.media.uploader import MediaUploader
from adgenie.models import (
    AdGroup,
    Campaign,
    Creative,
    EntityStatus,
    MediaAsset,
    MediaKind,
    MediaStatus,
    Platform,
    PlatformAsset,
)
from adgenie.platforms.base import CreativeSpec, MediaHandle, MediaUpload, PlatformError
from adgenie.platforms.google import GoogleAdsClient
from adgenie.platforms.meta import MetaAdsClient
from adgenie.platforms.sandbox import SandboxPlatform

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDAT\x08\x1dc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def meta_settings() -> Settings:
    return Settings(
        meta_access_token="tok",
        meta_ad_account_id="123",
        meta_page_id="page1",
        dry_run=False,
        secret_key="k",
    )


def _mock_meta(handler, settings) -> MetaAdsClient:
    def authenticated(request):
        assert request.headers["authorization"] == f"Bearer {settings.meta_access_token}"
        assert "access_token" not in request.url.params
        return handler(request)

    return MetaAdsClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(authenticated)),
        dry_run=False,
    )


@pytest.fixture
def image_file(tmp_path) -> Path:
    path = tmp_path / "creative.png"
    path.write_bytes(PNG)
    return path


# --- Meta upload -----------------------------------------------------------


def test_an_image_is_uploaded_and_its_hash_comes_back(meta_settings, image_file):
    def handler(request):
        assert request.url.path.endswith("/adimages")
        assert b"multipart/form-data" in request.headers["content-type"].encode()
        return httpx.Response(
            200,
            json={"images": {"whatever_meta_calls_it": {
                "hash": "abc123", "width": 1200, "height": 628,
            }}},
        )

    client = _mock_meta(handler, meta_settings)
    handle = client.upload_media(
        MediaUpload(path=str(image_file), content_type="image/png", kind="image")
    )
    assert handle.kind == "image"
    assert handle.handle == "abc123"
    assert (handle.width, handle.height) == (1200, 628)
    assert handle.ready is True


def test_the_image_hash_is_read_by_shape_not_by_the_name_we_sent(
    meta_settings, image_file
):
    """Meta keys the response by the filename it decided on, not always ours.

    Looking it up by the name we sent returns nothing the moment Meta
    normalises it, and the upload silently produces no hash.
    """
    def handler(request):
        return httpx.Response(
            200, json={"images": {"a_completely_different_key": {"hash": "xyz"}}}
        )

    client = _mock_meta(handler, meta_settings)
    handle = client.upload_media(
        MediaUpload(
            path=str(image_file), content_type="image/png",
            kind="image", name="adgenie-deadbeef.png",
        )
    )
    assert handle.handle == "xyz"


def test_an_upload_that_returns_no_hash_is_an_error(meta_settings, image_file):
    """Better to fail here than to build an ad with no image on it."""
    client = _mock_meta(lambda r: httpx.Response(200, json={"images": {}}), meta_settings)
    with pytest.raises(PlatformError, match="no hash"):
        client.upload_media(
            MediaUpload(path=str(image_file), content_type="image/png")
        )


def test_uploading_a_file_that_is_not_there_fails_before_the_call(meta_settings):
    called = []

    def handler(request):
        called.append(request.url.path)
        return httpx.Response(200, json={})

    client = _mock_meta(handler, meta_settings)
    with pytest.raises(PlatformError, match="no file to upload"):
        client.upload_media(
            MediaUpload(path="/nowhere/missing.png", content_type="image/png")
        )
    assert called == []


def test_a_video_is_waited_for_before_it_is_called_ready(meta_settings, tmp_path):
    """A video is not usable the moment it uploads.

    Meta transcodes it, and an ad built against one still processing is
    rejected — so the upload is not finished until Meta says it is.
    """
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    statuses = ["processing", "processing", "ready"]

    def handler(request):
        if request.url.path.endswith("/advideos"):
            return httpx.Response(200, json={"id": "vid_1"})
        if request.url.path.endswith("/thumbnails"):
            return httpx.Response(200, json={"data": [
                {"uri": "https://scontent.test/a.jpg", "is_preferred": False},
                {"uri": "https://scontent.test/b.jpg", "is_preferred": True},
            ]})
        return httpx.Response(
            200, json={"status": {"video_status": statuses.pop(0)}}
        )

    client = _mock_meta(handler, meta_settings)
    import adgenie.platforms.meta as meta_module

    original = meta_module.VIDEO_POLL_SECONDS
    meta_module.VIDEO_POLL_SECONDS = 0
    try:
        handle = client.upload_media(
            MediaUpload(path=str(video), content_type="video/mp4", kind="video")
        )
    finally:
        meta_module.VIDEO_POLL_SECONDS = original

    assert handle.handle == "vid_1"
    assert handle.ready is True
    assert statuses == []
    # Meta's own preferred frame, not simply the first one it listed.
    assert handle.thumbnail_url == "https://scontent.test/b.jpg"


def test_a_video_meta_cannot_process_raises(meta_settings, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    def handler(request):
        if request.url.path.endswith("/advideos"):
            return httpx.Response(200, json={"id": "vid_1"})
        return httpx.Response(200, json={"status": {"video_status": "error"}})

    client = _mock_meta(handler, meta_settings)
    with pytest.raises(PlatformError, match="could not process video"):
        client.upload_media(
            MediaUpload(path=str(video), content_type="video/mp4", kind="video")
        )


# --- Meta creative building ------------------------------------------------


def _creative_payload(handler_paths, request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def test_an_uploaded_hash_beats_a_url(meta_settings):
    """The ad account owns an uploaded image, so the reference cannot rot."""
    captured = {}

    def handler(request):
        if "adcreatives" in request.url.path:
            captured.update(_creative_payload(None, request))
        return httpx.Response(200, json={"id": "obj"})

    client = _mock_meta(handler, meta_settings)
    client.create_creative(
        CreativeSpec(
            ad_group_external_id="adset_1", name="ad",
            final_url="https://track.test/r", headlines=["H"],
            primary_texts=["B"],
            media_urls=["https://expires-tomorrow.test/a.png"],
            media=[MediaHandle(kind="image", handle="hash_abc")],
        )
    )
    story = json.loads(captured["object_story_spec"])
    assert story["link_data"]["image_hash"] == "hash_abc"
    assert "picture" not in story["link_data"]


def test_a_url_is_still_used_when_there_is_no_upload(meta_settings):
    captured = {}

    def handler(request):
        if "adcreatives" in request.url.path:
            captured.update(_creative_payload(None, request))
        return httpx.Response(200, json={"id": "obj"})

    client = _mock_meta(handler, meta_settings)
    client.create_creative(
        CreativeSpec(
            ad_group_external_id="adset_1", name="ad",
            final_url="https://track.test/r", headlines=["H"],
            primary_texts=["B"],
            media_urls=["https://cdn.test/a.png"],
        )
    )
    story = json.loads(captured["object_story_spec"])
    assert story["link_data"]["picture"] == "https://cdn.test/a.png"


def test_a_video_ad_is_a_different_object_not_a_link_ad(meta_settings):
    """`video_data`, not `link_data` with a video bolted on."""
    captured = {}

    def handler(request):
        if "adcreatives" in request.url.path:
            captured.update(_creative_payload(None, request))
        return httpx.Response(200, json={"id": "obj"})

    client = _mock_meta(handler, meta_settings)
    client.create_creative(
        CreativeSpec(
            ad_group_external_id="adset_1", name="ad",
            final_url="https://track.test/r", headlines=["H"],
            primary_texts=["B"], descriptions=["D"],
            media=[MediaHandle(
                kind="video", handle="vid_1",
                thumbnail_url="https://scontent.test/b.jpg",
            )],
        )
    )
    story = json.loads(captured["object_story_spec"])
    assert "link_data" not in story
    assert story["video_data"]["video_id"] == "vid_1"
    assert story["video_data"]["image_url"] == "https://scontent.test/b.jpg"


def test_a_video_with_no_thumbnail_is_refused(meta_settings):
    client = _mock_meta(lambda r: httpx.Response(200, json={"id": "o"}), meta_settings)
    with pytest.raises(PlatformError, match="needs a thumbnail"):
        client.create_creative(
            CreativeSpec(
                ad_group_external_id="a", name="ad", final_url="u",
                headlines=["H"],
                media=[MediaHandle(kind="video", handle="vid_1")],
            )
        )


def test_an_ad_is_not_built_on_a_video_that_is_still_processing(meta_settings):
    """Meta rejects it, and a rejected ad is a wasted round trip and a
    confusing failure two layers away from the cause."""
    client = _mock_meta(lambda r: httpx.Response(200, json={"id": "o"}), meta_settings)
    with pytest.raises(PlatformError, match="still processing"):
        client.create_creative(
            CreativeSpec(
                ad_group_external_id="a", name="ad", final_url="u",
                headlines=["H"],
                media=[MediaHandle(
                    kind="video", handle="vid_1", ready=False,
                    thumbnail_url="https://x.test/t.jpg",
                )],
            )
        )


def _capture_creative(settings, **spec_kwargs) -> dict:
    captured = {}

    def handler(request):
        if "adcreatives" in request.url.path:
            captured.update(_creative_payload(None, request))
        return httpx.Response(200, json={"id": "obj"})

    fields = dict(
        ad_group_external_id="adset_1", name="ad",
        final_url="https://track.test/r",
        headlines=["One", "Two"], primary_texts=["A", "B"],
    )
    fields.update(spec_kwargs)
    _mock_meta(handler, settings).create_creative(CreativeSpec(**fields))
    return captured


def test_the_asset_feed_carries_hashes_never_urls(meta_settings):
    """Meta's own asset-level test takes hashes and ids only, so imagery that
    was not uploaded cannot take part in it."""
    meta_settings.meta_dynamic_creative = True
    captured = _capture_creative(
        meta_settings,
        media_urls=["https://cdn.test/a.png"],
        media=[
            MediaHandle(kind="image", handle="h1"),
            MediaHandle(kind="image", handle="h2"),
        ],
    )
    feed = json.loads(captured["asset_feed_spec"])
    assert feed["images"] == [{"hash": "h1"}, {"hash": "h2"}]
    assert "cdn.test" not in json.dumps(feed)


def test_a_feed_creative_puts_only_the_page_in_its_story_spec(meta_settings):
    """Meta rejects a creative carrying both an asset feed and a story spec
    with content: each one fully describes the ad, and they can disagree.

    This adapter sent both on every creative with more than one headline, which
    the copywriter produces almost always — so first contact would have failed
    on essentially every ad.
    """
    meta_settings.meta_dynamic_creative = True
    captured = _capture_creative(
        meta_settings, media=[MediaHandle(kind="image", handle="h1")]
    )

    story = json.loads(captured["object_story_spec"])
    assert story == {"page_id": "page1"}
    assert "link_data" not in story and "video_data" not in story
    # A feed with no declared format is rejected.
    assert json.loads(captured["asset_feed_spec"])["ad_formats"] == ["SINGLE_IMAGE"]


def test_the_default_creative_is_a_single_ad_with_no_feed(meta_settings):
    """Off by default, and not only because the feed is unverified against a
    live account. A feed lets Meta choose the headline, body and image and then
    reports delivery for the creative as a whole. This optimizer scales and
    kills per creative, so it would be deciding about content it cannot see.
    """
    captured = _capture_creative(
        meta_settings, media=[MediaHandle(kind="image", handle="h1")]
    )

    assert "asset_feed_spec" not in captured
    story = json.loads(captured["object_story_spec"])
    assert story["page_id"] == "page1"
    assert story["link_data"]["name"] == "One"
    assert story["link_data"]["image_hash"] == "h1"


def test_a_feed_is_not_built_without_uploaded_media(meta_settings):
    """The feed addresses images by hash and videos by id, never by URL. Built
    from copy alone it would describe an ad with no imagery at all."""
    meta_settings.meta_dynamic_creative = True
    captured = _capture_creative(
        meta_settings, media_urls=["https://cdn.test/a.png"], media=[]
    )

    assert "asset_feed_spec" not in captured
    assert "link_data" in json.loads(captured["object_story_spec"])


# --- Google says no --------------------------------------------------------


def test_google_refuses_media_rather_than_uploading_something_unusable():
    """A search ad has no slot for an image. Accepting the file would cost a
    call and produce an asset no ad here references."""
    settings = Settings(
        google_developer_token="t", google_customer_id="123",
        google_client_id="c", google_client_secret="s",
        google_refresh_token="r", secret_key="k",
    )
    client = GoogleAdsClient(settings, client=httpx.Client())
    with pytest.raises(PlatformError, match="carry no imagery") as exc:
        client.upload_media(MediaUpload(path="/x.png", content_type="image/png"))
    assert exc.value.code == "UNSUPPORTED"


# --- the uploader ----------------------------------------------------------


@pytest.fixture
def creative(session, offer) -> Creative:
    campaign = Campaign(
        offer_id=offer.id, platform=Platform.META, name="c",
        external_id="c1", status=EntityStatus.ACTIVE,
    )
    session.add(campaign)
    session.commit()
    group = AdGroup(
        campaign_id=campaign.id, name="g", external_id="g1",
        status=EntityStatus.ACTIVE,
    )
    session.add(group)
    session.commit()
    made = Creative(
        ad_group_id=group.id, name="ad", angle="problem_solution",
        status=EntityStatus.ACTIVE, final_url="https://track.test/r",
        headlines=["H"], primary_texts=["B"],
    )
    session.add(made)
    session.commit()
    return made


def _asset(session, creative, path, content_hash, kind=MediaKind.IMAGE) -> MediaAsset:
    asset = MediaAsset(
        creative_id=creative.id, kind=kind, status=MediaStatus.READY,
        local_path=str(path), content_hash=content_hash,
        remote_url="https://kie.test/expires-tomorrow.png",
    )
    session.add(asset)
    session.commit()
    return asset


@pytest.mark.parametrize("dry_run_source", ["settings", "client"])
@pytest.mark.parametrize("cached", [False, True])
def test_dry_run_upload_never_calls_the_client_or_changes_cached_handles(
    session, creative, image_file, settings, dry_run_source, cached, monkeypatch
):
    from unittest.mock import Mock

    asset = _asset(session, creative, image_file, "a" * 64)
    client = SandboxPlatform(Platform.META)
    client.dry_run = dry_run_source == "client"
    settings.dry_run = dry_run_source == "settings"
    if cached:
        session.add(PlatformAsset(
            media_asset_id=asset.id, platform=Platform.META,
            account_id=client.account_key, content_hash=asset.content_hash,
            kind=MediaKind.VIDEO, handle="existing_video", ready=False,
        ))
        session.commit()
    upload = Mock(wraps=client.upload_media)
    refresh = Mock(wraps=client.refresh_media)
    monkeypatch.setattr(client, "upload_media", upload)
    monkeypatch.setattr(client, "refresh_media", refresh)

    assert MediaUploader(session, settings).handles_for_creative(creative, client) == []
    session.commit()
    session.expire_all()

    upload.assert_not_called()
    refresh.assert_not_called()
    assert session.query(PlatformAsset).count() == int(cached)
    if cached:
        record = session.query(PlatformAsset).one()
        assert record.handle == "existing_video"
        assert record.ready is False


@pytest.mark.parametrize("dry_run_source", ["settings", "client"])
def test_upload_route_previews_then_uploads_without_a_fake_cache_entry(
    api_client, session, creative, image_file, settings, dry_run_source, monkeypatch
):
    from unittest.mock import Mock

    from adgenie.core.orchestrator import Orchestrator

    asset = _asset(session, creative, image_file, "a" * 64)
    client = SandboxPlatform(Platform.META)
    client.dry_run = dry_run_source == "client"
    settings.dry_run = dry_run_source == "settings"
    upload = Mock(wraps=client.upload_media)
    monkeypatch.setattr(client, "upload_media", upload)
    monkeypatch.setattr(Orchestrator, "client", lambda self, platform: client)

    response = api_client.post(f"/api/media/upload/{creative.id}")

    assert response.status_code == 200
    preview = response.json()
    assert preview["dry_run"] is True
    assert preview["applied"] is False
    assert preview["pending_asset_ids"] == [asset.id]
    assert preview["uploaded"] == []
    upload.assert_not_called()
    assert session.query(PlatformAsset).count() == 0

    settings.dry_run = False
    client.dry_run = False
    response = api_client.post(f"/api/media/upload/{creative.id}")

    assert response.status_code == 200
    result = response.json()
    assert result["dry_run"] is False
    assert result["applied"] is True
    assert result["pending_asset_ids"] == []
    assert len(result["uploaded"]) == 1
    upload.assert_called_once()
    session.expire_all()
    record = session.query(PlatformAsset).one()
    assert record.handle == result["uploaded"][0]["handle"]
    assert not record.handle.startswith("dryrun_")
    api_client.post(f"/api/media/upload/{creative.id}")
    upload.assert_called_once()


def test_a_simulated_upload_response_is_not_returned_or_persisted(
    session, creative, image_file, settings, monkeypatch
):
    asset = _asset(session, creative, image_file, "a" * 64)
    client = SandboxPlatform(Platform.META)
    monkeypatch.setattr(
        client, "upload_media", lambda upload: MediaHandle(kind="image", handle="dryrun_x")
    )

    assert MediaUploader(session, settings).ensure_uploaded(asset, client) is None
    session.commit()
    assert session.query(PlatformAsset).count() == 0


def test_an_asset_is_uploaded_once_and_reused(session, creative, image_file, settings):
    """The same bytes going to the same account should cost one call."""
    sandbox = SandboxPlatform(Platform.META)
    _asset(session, creative, image_file, "a" * 64)
    uploader = MediaUploader(session, settings=settings)

    first = uploader.handles_for_creative(creative, sandbox)
    second = uploader.handles_for_creative(creative, sandbox)

    assert [h.handle for h in first] == [h.handle for h in second]
    assert sum(1 for c in sandbox.calls if c[0] == "upload_media") == 1
    assert session.query(PlatformAsset).count() == 1


@pytest.mark.parametrize("stale_handle", ["dryrun_image_old", ""])
def test_a_live_upload_replaces_a_dry_run_cache_row_in_place(
    session, creative, image_file, meta_settings, stale_handle
):
    asset = _asset(session, creative, image_file, "a" * 64)
    requests = []

    def handler(request):
        requests.append(request.url.path)
        assert request.url.path.endswith("/adimages")
        return httpx.Response(200, json={"images": {"image": {"hash": "live_hash"}}})

    client = _mock_meta(handler, meta_settings)
    record = PlatformAsset(
        media_asset_id=asset.id, platform=Platform.META,
        account_id=client.account_key, content_hash=asset.content_hash,
        kind=MediaKind.IMAGE, handle=stale_handle, ready=False,
        thumbnail_handle="dryrun_thumbnail", thumbnail_url="https://stale.test/thumb",
    )
    session.add(record)
    session.commit()
    record_id = record.id

    uploader = MediaUploader(session, settings=meta_settings)
    first = uploader.ensure_uploaded(asset, client)
    session.commit()
    session.expire_all()
    second = uploader.ensure_uploaded(asset, client)

    assert first.handle == second.handle == "live_hash"
    assert len(requests) == 1
    persisted = session.query(PlatformAsset).one()
    assert persisted.id == record_id
    assert persisted.handle == "live_hash"
    assert persisted.ready is True
    assert persisted.thumbnail_handle is None
    assert persisted.thumbnail_url is None


@pytest.mark.parametrize("file_present", [False, True])
def test_an_unrecoverable_dry_run_cache_handle_is_never_reused(
    session, creative, image_file, meta_settings, file_present
):
    path = image_file if file_present else image_file.with_name("missing.png")
    asset = _asset(session, creative, path, "a" * 64)
    requests = []

    def handler(request):
        requests.append(request.url.path)
        assert request.url.path.endswith("/adimages")
        return httpx.Response(400, json={"error": {"message": "upload rejected"}})

    client = _mock_meta(handler, meta_settings)
    session.add(PlatformAsset(
        media_asset_id=asset.id, platform=Platform.META,
        account_id=client.account_key, content_hash=asset.content_hash,
        kind=MediaKind.IMAGE, handle="dryrun_old", ready=False,
    ))
    session.commit()

    assert MediaUploader(session, meta_settings).handles_for_creative(creative, client) == []
    session.commit()
    session.expire_all()
    assert len(requests) == int(file_present)
    assert session.query(PlatformAsset).one().handle == "dryrun_old"


def test_the_same_file_in_a_second_account_is_a_second_upload(
    session, creative, image_file, settings
):
    """An image hash belongs to the ad account it was uploaded into.

    Handing account A's hash to account B gives an ad referencing something
    that does not exist there.
    """
    _asset(session, creative, image_file, "a" * 64)
    uploader = MediaUploader(session, settings=settings)

    first = SandboxPlatform(Platform.META, account="act_111")
    second = SandboxPlatform(Platform.META, account="act_222")

    one = uploader.handles_for_creative(creative, first)
    two = uploader.handles_for_creative(creative, second)

    rows = session.query(PlatformAsset).all()
    assert {row.account_id for row in rows} == {"act_111", "act_222"}
    assert len(rows) == 2
    # Same file, so the same content hash keyed under two different accounts.
    assert {row.content_hash for row in rows} == {"a" * 64}
    assert one and two


def test_an_asset_whose_file_is_gone_is_skipped_not_re_fetched(
    session, creative, tmp_path, settings, caplog
):
    """The provider URL it came from has expired, so there is nothing to
    re-fetch. It has to be regenerated."""
    _asset(session, creative, tmp_path / "vanished.png", "b" * 64)
    sandbox = SandboxPlatform(Platform.META)

    handles = MediaUploader(session, settings=settings).handles_for_creative(
        creative, sandbox
    )
    assert handles == []
    assert not any(c[0] == "upload_media" for c in sandbox.calls)


def test_an_asset_that_is_not_ready_is_not_uploaded(
    session, creative, image_file, settings
):
    asset = _asset(session, creative, image_file, "c" * 64)
    asset.status = MediaStatus.FAILED
    session.commit()

    sandbox = SandboxPlatform(Platform.META)
    assert MediaUploader(session, settings=settings).handles_for_creative(
        creative, sandbox
    ) == []


def test_a_platform_that_takes_no_media_is_not_an_error(
    session, creative, image_file, settings
):
    """Google saying no is an answer, not a failure. The launch continues."""
    _asset(session, creative, image_file, "d" * 64)
    google = SandboxPlatform(Platform.GOOGLE)

    handles = MediaUploader(session, settings=settings).handles_for_creative(
        creative, google
    )
    assert handles == []
    assert session.query(PlatformAsset).count() == 0


def test_a_video_still_transcoding_is_re_checked_rather_than_left_unusable(
    session, creative, tmp_path, settings
):
    """Otherwise the first attempt stores ready=False forever and the video
    can never be used, though it finished a minute later."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    _asset(session, creative, video, "e" * 64, kind=MediaKind.VIDEO)

    sandbox = SandboxPlatform(Platform.META)
    sandbox.video_processing_delay = True
    uploader = MediaUploader(session, settings=settings)

    first = uploader.handles_for_creative(creative, sandbox)
    assert first[0].ready is False
    assert session.query(PlatformAsset).one().ready is False

    sandbox.video_processing_delay = False
    second = uploader.handles_for_creative(creative, sandbox)
    assert second[0].ready is True
    assert second[0].thumbnail_url
    # Re-checked, not re-uploaded.
    assert sum(1 for c in sandbox.calls if c[0] == "upload_media") == 1
    assert session.query(PlatformAsset).one().ready is True


def test_an_asset_with_no_content_hash_is_skipped(
    session, creative, image_file, settings
):
    """Without a hash there is no way to tell it apart from the next one, so
    it would be re-uploaded on every launch."""
    asset = _asset(session, creative, image_file, "f" * 64)
    asset.content_hash = None
    session.commit()

    sandbox = SandboxPlatform(Platform.META)
    assert MediaUploader(session, settings=settings).handles_for_creative(
        creative, sandbox
    ) == []
    assert not any(c[0] == "upload_media" for c in sandbox.calls)


# --- end to end through a launch -------------------------------------------


def test_a_launch_with_media_puts_a_handle_on_the_ad(session, offer, settings, tmp_path):
    """The whole point. Before this, generated files stayed on disk and the ad
    went out with nothing on it, or with a link that expires."""
    from adgenie.core.launcher import CampaignLauncher, LaunchPlan

    sandbox = SandboxPlatform(Platform.META)
    media_settings = settings.model_copy(
        update={"media_storage_dir": str(tmp_path / "media")}
    )
    launched = CampaignLauncher(
        session, settings=media_settings, platform_client=sandbox
    ).launch(
        LaunchPlan(
            offer_id=offer.id, platform=Platform.META, daily_budget_usd=30.0,
            angle_count=1, creatives_per_angle=1, start_paused=True,
            generate_media=True,
        )
    )
    assert launched.creative_ids, launched.errors

    assets = session.query(MediaAsset).filter(
        MediaAsset.status == MediaStatus.READY
    ).all()
    assert assets, "the sandbox media provider produced nothing to upload"

    uploads = session.query(PlatformAsset).all()
    assert uploads, "media was generated but never reached the ad account"
    assert all(row.handle for row in uploads)

    # And the handle actually travelled into the creative spec.
    creative_calls = [c for c in sandbox.calls if c[0] == "create_creative"]
    assert creative_calls
    assert any(c[0] == "upload_media" for c in sandbox.calls)


# --- the paths that only bite in production --------------------------------


def test_a_dry_run_upload_simulates_rather_than_failing(meta_settings, image_file):
    """DRY_RUN fakes POSTs but not the GETs that follow an upload.

    Left alone, a dry-run launch would report media it could not attach — the
    one mode whose whole purpose is to tell you what a real run would do.
    """
    called = []

    def handler(request):
        called.append(request.url.path)
        return httpx.Response(200, json={})

    client = MetaAdsClient(
        meta_settings, client=httpx.Client(transport=httpx.MockTransport(handler)),
        dry_run=True,
    )
    handle = client.upload_media(
        MediaUpload(path=str(image_file), content_type="image/png", kind="image")
    )
    assert handle.handle.startswith("dryrun_")
    assert handle.ready is True
    assert called == [], "a dry run reached the network"


def test_a_dry_run_video_gets_a_thumbnail_too(meta_settings, tmp_path):
    """Otherwise create_creative refuses it and the dry run reports a failure
    that a real run would not have."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    client = MetaAdsClient(
        meta_settings,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={})
        )),
        dry_run=True,
    )
    handle = client.upload_media(
        MediaUpload(path=str(video), content_type="video/mp4", kind="video")
    )
    assert handle.ready is True
    assert handle.thumbnail_url

    # And it is accepted by the creative builder rather than refused.
    client.create_creative(
        CreativeSpec(
            ad_group_external_id="a", name="ad", final_url="u",
            headlines=["H"], primary_texts=["B"], media=[handle],
        )
    )


def test_an_oversized_file_fails_locally_not_after_a_long_upload(
    meta_settings, tmp_path
):
    big = tmp_path / "huge.png"
    big.write_bytes(b"\x00" * 1024)
    called = []

    def handler(request):
        called.append(request.url.path)
        return httpx.Response(200, json={})

    client = _mock_meta(handler, meta_settings)
    import adgenie.platforms.meta as meta_module

    original = meta_module.MAX_IMAGE_BYTES
    meta_module.MAX_IMAGE_BYTES = 512
    try:
        with pytest.raises(PlatformError, match="over Meta's"):
            client.upload_media(
                MediaUpload(path=str(big), content_type="image/png", kind="image")
            )
    finally:
        meta_module.MAX_IMAGE_BYTES = original
    assert called == []


def test_an_ambiguous_upload_response_is_not_guessed_at(meta_settings, image_file):
    """One file went up, so one entry is unambiguous. Several are not, and
    picking arbitrarily would attach the wrong image to the ad."""
    def handler(request):
        return httpx.Response(200, json={"images": {
            "not_ours_a": {"hash": "h_a"},
            "not_ours_b": {"hash": "h_b"},
        }})

    client = _mock_meta(handler, meta_settings)
    with pytest.raises(PlatformError, match="no hash"):
        client.upload_media(
            MediaUpload(
                path=str(image_file), content_type="image/png", name="ours.png"
            )
        )


def test_a_retried_upload_still_carries_the_file(meta_settings, image_file, monkeypatch):
    """`_request` retries transient failures. A streamed file handle would be
    at EOF by the second attempt, uploading nothing and succeeding — so the
    body is held in memory, and this is the test that says why.
    """
    import adgenie.platforms.meta as meta_module

    monkeypatch.setattr(meta_module.time, "sleep", lambda *_: None)
    sizes = []

    def handler(request):
        sizes.append(len(request.content))
        if len(sizes) < 3:
            return httpx.Response(500, json={"error": {"message": "transient"}})
        return httpx.Response(200, json={"images": {"k": {"hash": "ok123"}}})

    client = _mock_meta(handler, meta_settings)
    handle = client.upload_media(
        MediaUpload(path=str(image_file), content_type="image/png")
    )
    assert handle.handle == "ok123"
    assert len(sizes) == 3
    assert len(set(sizes)) == 1, "a retry sent a different amount of data"
    assert sizes[0] > len(PNG)
