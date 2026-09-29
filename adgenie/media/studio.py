"""Generating the visual half of an ad.

The sequence is deliberate: plan the prompt, screen it, generate, download
before the URL expires, then record the row. Screening first is what keeps a
policy-violating image from being paid for, and downloading immediately is what
keeps an ad from pointing at a dead URL a day later.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Creative, MediaAsset, MediaKind, MediaStatus, Offer, Platform
from .base import MediaError, MediaProvider, MediaRequest
from .prompts import PromptPlan, build_image_prompt, build_video_prompt
from .sandbox import SandboxMediaProvider
from .specs import default_placements, get_media_spec
from .store import MediaStore
from .ugc import DELIVERY_PROMPT, PresenterVideoPlan, plan_presenter_video

logger = logging.getLogger(__name__)

__all__ = ["MediaStudio", "get_media_provider"]


def get_media_provider(settings: Settings | None = None) -> MediaProvider:
    settings = settings or get_settings()
    if settings.has_media_generation:
        if settings.dry_run:
            # Every ad-platform mutation is suppressed in dry run, so spending
            # real money on generation would be the one paid side effect of a
            # mode whose whole purpose is to have none.
            logger.warning(
                "DRY_RUN is on, so media generation is simulated rather than "
                "billed. Set DRY_RUN=false to generate real creative."
            )
            return SandboxMediaProvider()
        from .kie import KieClient

        return KieClient(settings)
    logger.warning(
        "No KIE_API_KEY set; media generation is simulated. Placeholder images "
        "are correctly sized but are not real creative."
    )
    return SandboxMediaProvider()


def _credits(record: dict | None) -> float | None:
    """What kie.ai's task record says the task cost, when it says.

    Recorded per task because the credit balance is shared by everything the
    key generates, so it cannot say what one asset cost. A running task shows
    its charge already; a task that failed on kie.ai's side shows zero, and
    the balance confirms it was not charged.
    """
    value = (record or {}).get("creditsConsumed")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


class MediaStudio:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        provider: MediaProvider | None = None,
        store: MediaStore | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.provider = provider or get_media_provider(self.settings)
        self.store = store or MediaStore(self.settings)

    # ------------------------------------------------------------------
    def generate_for_creative(
        self,
        creative: Creative,
        placements: list[str] | None = None,
        kind: str = "image",
        platform: Platform | None = None,
        ad_format: str | None = None,
        scene: str = "",
    ) -> list[MediaAsset]:
        """Produce the assets one creative needs, one per placement.

        `scene` is what the asset shows, in place of the angle's default shot:
        an ad's own visual direction, screened like the rest of the prompt.
        """
        offer = self._offer_for(creative)
        platform = platform or self._platform_for(creative)
        placements = placements or default_placements(platform, kind, ad_format)
        if not placements:
            logger.info(
                "No %s placements for %s/%s; this ad format carries no imagery.",
                kind,
                platform.value,
                ad_format or "default",
            )
            return []

        hook = creative.headlines[0] if creative.headlines else ""
        assets: list[MediaAsset] = []
        for placement in placements:
            plan = (
                build_image_prompt(offer, creative.angle, placement, scene=scene)
                if kind == "image"
                else build_video_prompt(
                    offer, creative.angle, placement, hook_line=hook, scene=scene
                )
            )
            assets.append(
                self._run_plan(plan, creative_id=creative.id, offer_id=offer.id)
            )

        # Only a URL the platform can fetch belongs here. A local path would be
        # handed to Meta as if it were an image source, and rejected.
        #
        # This is the fallback path, not the main one. Media reaches an ad by
        # being uploaded into the ad account, which needs nothing but the file
        # on disk; a public URL matters only where a platform fetches by URL
        # instead. So its absence is worth noting once, not warning about.
        fetchable = [
            a.public_url for a in assets
            if a.status is MediaStatus.READY and a.public_url
        ]
        if fetchable:
            creative.media_urls = fetchable
        elif any(a.status is MediaStatus.READY for a in assets):
            logger.debug(
                "Generated %s asset(s) for creative %s with no "
                "MEDIA_PUBLIC_BASE_URL set. They will be uploaded to the ad "
                "account from disk, which is the durable route anyway.",
                sum(1 for a in assets if a.status is MediaStatus.READY),
                creative.id,
            )
        self.session.flush()
        return assets

    def generate_from_prompt(
        self,
        plan: PromptPlan,
        creative_id: int | None = None,
        offer_id: int | None = None,
    ) -> MediaAsset:
        return self._run_plan(plan, creative_id=creative_id, offer_id=offer_id)

    # ------------------------------------------------------------------
    def presenter_plan_for(
        self,
        creative: Creative,
        placement: str | None = None,
        seconds: float | None = None,
        persona: str = "",
        presenter_image_url: str | None = None,
        voice: str | None = None,
        variant_index: int = 0,
        script_studio=None,
    ) -> PresenterVideoPlan:
        """Write and review a presenter script for this creative's argument.

        Costs nothing and generates nothing, which also makes it the preview.
        """
        return plan_presenter_video(
            self._offer_for(creative),
            angle_key=creative.angle,
            platform=self._platform_for(creative),
            placement=placement,
            seconds=seconds,
            persona=persona,
            presenter_image_url=presenter_image_url,
            voice=voice,
            variant_index=variant_index,
            script_studio=script_studio,
            settings=self.settings,
        )

    def generate_presenter_video(
        self, plan: PresenterVideoPlan, creative_id: int | None = None
    ) -> MediaAsset:
        """Presenter still, voice and lip-sync, for a plan that passed review.

        Three paid tasks in a chain, each consuming the previous one's output
        while the provider still hosts it. The voice is synthesised from the
        approved script, so the words in the finished video are the words the
        policy engine read. Each task id is recorded as it completes, so a
        failure part way through says what was already paid for rather than
        inviting a blind resubmission.
        """
        asset = MediaAsset(
            creative_id=creative_id,
            offer_id=plan.offer_id,
            kind=MediaKind.VIDEO,
            provider=self.provider.name,
            model=self.settings.kie_avatar_model,
            prompt=plan.script.text,
            aspect_ratio=plan.aspect_ratio,
            width=plan.width,
            height=plan.height,
            duration_seconds=plan.script.estimated_seconds,
            compliance_report={
                "findings": plan.findings,
                "script": plan.script.report.as_dict() if plan.script.report else {},
            },
            extra={
                "placement": plan.placement,
                "format": "presenter",
                # The person on camera and the voice are both generated. Kept
                # on the asset because declaring AI-generated people and
                # voices is increasingly a platform requirement, and the
                # declaration is made per ad.
                "synthetic_presenter": True,
                "script": plan.script.as_dict(),
                "voice": plan.voice,
                "presenter": plan.presenter.as_dict(),
                "steps": {},
            },
        )
        self.session.add(asset)
        self.session.flush()

        # Nothing is paid for until the script and the presenter have both
        # passed. A rejected plan costs nothing; a generated one cannot be
        # un-generated.
        if not plan.is_safe:
            asset.status = MediaStatus.REJECTED
            asset.error = "; ".join(
                f["code"] + ": " + f["suggestion"] for f in plan.findings
            )
            logger.warning(
                "Presenter video rejected before generation: %s",
                ", ".join(f["code"] for f in plan.findings),
            )
            self.session.flush()
            return asset

        asset.status = MediaStatus.GENERATING
        self.session.flush()

        image_url = plan.presenter.image_url
        if not plan.presenter.is_supplied:
            still = self._run_plan(
                plan.presenter.prompt_plan(plan.spec, plan.placement),
                creative_id=None,
                offer_id=plan.offer_id,
            )
            # Kept, but not on the creative. It is reusable for the next
            # script, and attached to the creative it would be uploaded as the
            # ad's image even when the video failed.
            still.extra = {
                **(still.extra or {}), "role": "presenter", "presenter_for": asset.id,
            }
            self._record_step(
                asset, "presenter",
                task_id=still.task_id, url=still.remote_url,
                asset_id=still.id, model=still.model,
                credits=(still.extra or {}).get("credits"),
            )
            if still.status is not MediaStatus.READY or not still.remote_url:
                return self._fail(
                    asset, f"presenter image: {still.error or still.status.value}"
                )
            image_url = still.remote_url

        try:
            voice = self.provider.generate(
                MediaRequest(
                    prompt=plan.script.text,
                    kind="audio",
                    model=self.settings.kie_tts_model,
                    extra={"voice": plan.voice},
                )
            )
        except MediaError as exc:
            return self._fail(asset, f"voice: {exc}", step="voice", exc=exc)
        self._record_step(
            asset, "voice",
            task_id=voice.task_id, url=voice.urls[0] if voice.urls else None,
            model=voice.model or self.settings.kie_tts_model,
            credits=_credits(voice.raw),
        )
        if not voice.ok:
            return self._fail(asset, f"voice: {voice.error or voice.state}")

        request = MediaRequest(
            prompt=DELIVERY_PROMPT,
            kind="video",
            model=self.settings.kie_avatar_model,
            aspect_ratio=plan.aspect_ratio,
            width=plan.width,
            height=plan.height,
            duration_seconds=plan.script.estimated_seconds,
            reference_image_url=image_url,
            extra={"audio_url": voice.urls[0]},
        )
        try:
            video = self.provider.generate(request)
        except MediaError as exc:
            return self._fail(asset, f"lip-sync: {exc}", step="lip_sync", exc=exc)
        asset.task_id = video.task_id
        asset.remote_url = video.urls[0] if video.urls else None
        asset.model = video.model or asset.model
        self._record_step(
            asset, "lip_sync",
            task_id=video.task_id, url=asset.remote_url, model=asset.model,
            credits=_credits(video.raw),
        )
        if not video.ok:
            return self._fail(asset, f"lip-sync: {video.error or video.state}")

        try:
            self._persist(asset, request, video.urls[0])
        except Exception as exc:
            # Finished and paid for. The task id stays on the asset so the
            # result can be fetched again while the provider still hosts it.
            return self._fail(asset, f"generated but could not be stored: {exc}")

        asset.status = MediaStatus.READY
        asset.completed_at = datetime.now(timezone.utc)
        self.session.flush()
        return asset

    def _fail(
        self,
        asset: MediaAsset,
        error: str,
        step: str | None = None,
        exc: MediaError | None = None,
    ) -> MediaAsset:
        task_id = exc.payload.get("task_id") if exc is not None else None
        if step and task_id and exc.code == "TIMEOUT":
            # A task that timed out may still finish, and is charged if it
            # does. Its id is what lets it be collected rather than resubmitted
            # and paid for twice.
            self._record_step(asset, step, task_id=task_id, state="unfinished")
        elif step and task_id:
            # Ran and failed on the provider's side. The id is what kie.ai's
            # support asks for; the charge it reports has been zero for every
            # failure seen so far.
            self._record_step(
                asset, step,
                task_id=task_id, state="failed", credits=_credits(exc.payload),
            )
        asset.status = MediaStatus.FAILED
        asset.error = error
        logger.error("Presenter video %s failed: %s", asset.id, error)
        self.session.flush()
        return asset

    @staticmethod
    def _record_step(asset: MediaAsset, name: str, **details) -> None:
        # Reassigned rather than mutated in place: a change inside a JSON
        # column is invisible to the session and would never be written.
        extra = dict(asset.extra or {})
        steps = {
            **(extra.get("steps") or {}),
            name: {k: v for k, v in details.items() if v is not None},
        }
        extra["steps"] = steps
        # The video's cost is known only while every step reported its own. A
        # task that timed out is charged but not yet reported, and a partial
        # sum would understate what was spent.
        costs = [step.get("credits") for step in steps.values()]
        if all(cost is not None for cost in costs):
            extra["credits"] = round(sum(costs), 2)
        else:
            extra.pop("credits", None)
        asset.extra = extra

    # ------------------------------------------------------------------
    def _run_plan(
        self, plan: PromptPlan, creative_id: int | None, offer_id: int | None
    ) -> MediaAsset:
        asset = MediaAsset(
            creative_id=creative_id,
            offer_id=offer_id,
            kind=MediaKind.VIDEO if plan.kind == "video" else MediaKind.IMAGE,
            provider=self.provider.name,
            model=self._model_name(plan),
            prompt=plan.prompt,
            negative_prompt=plan.negative_prompt,
            aspect_ratio=plan.aspect_ratio,
            width=plan.width,
            height=plan.height,
            duration_seconds=plan.duration_seconds,
            compliance_report={"findings": plan.findings},
            extra={"placement": plan.placement},
        )
        self.session.add(asset)
        self.session.flush()

        # Screening before generating: a rejected prompt costs nothing, and a
        # prompt that asks for a banned image reliably produces one.
        if not plan.is_safe:
            asset.status = MediaStatus.REJECTED
            asset.error = "; ".join(
                f["code"] + ": " + f["suggestion"] for f in plan.findings
            )
            logger.warning(
                "Prompt rejected before generation: %s",
                ", ".join(f["code"] for f in plan.findings),
            )
            self.session.flush()
            return asset

        request = MediaRequest(
            prompt=plan.prompt,
            negative_prompt=plan.negative_prompt,
            kind=plan.kind,
            aspect_ratio=plan.aspect_ratio,
            width=plan.width,
            height=plan.height,
            duration_seconds=plan.duration_seconds,
        )

        asset.status = MediaStatus.GENERATING
        try:
            result = self.provider.generate(request)
        except MediaError as exc:
            asset.status = MediaStatus.FAILED
            asset.error = str(exc)
            # A task that ran has an id worth keeping: a timed-out one may
            # still be collected, and a failed one is what support asks about.
            asset.task_id = exc.payload.get("task_id") or asset.task_id
            credits = _credits(exc.payload)
            if credits is not None:
                asset.extra = {**(asset.extra or {}), "credits": credits}
            logger.error("Media generation failed: %s", exc)
            self.session.flush()
            return asset

        asset.task_id = result.task_id
        asset.remote_url = result.urls[0] if result.urls else None
        asset.model = result.model or asset.model
        credits = _credits(result.raw)
        if credits is not None:
            asset.extra = {**(asset.extra or {}), "credits": credits}

        if not result.ok:
            asset.status = MediaStatus.FAILED
            asset.error = result.error or f"provider returned state '{result.state}'"
            logger.error("Media generation did not succeed: %s", asset.error)
            self.session.flush()
            return asset

        try:
            self._persist(asset, request, result.urls[0])
        except Exception as exc:
            # The generation succeeded and was paid for, so the task id is kept
            # even though the download failed; it can be retried while the URL
            # is still alive.
            asset.status = MediaStatus.FAILED
            asset.error = f"generated but could not be stored: {exc}"
            logger.error("Could not store asset for task %s: %s", result.task_id, exc)
            self.session.flush()
            return asset

        asset.status = MediaStatus.READY
        from datetime import datetime, timezone

        asset.completed_at = datetime.now(timezone.utc)
        self.session.flush()
        return asset

    def _persist(self, asset: MediaAsset, request: MediaRequest, url: str) -> None:
        subdir = f"offer-{asset.offer_id or 'none'}"
        if url.startswith("sandbox://") and isinstance(self.provider, SandboxMediaProvider):
            # Nothing to download: write the placeholder the sandbox stands for.
            directory = self.store.root / subdir
            directory.mkdir(parents=True, exist_ok=True)
            payload = self.provider.render(request)
            import hashlib

            content_hash = hashlib.sha256(payload).hexdigest()
            path = directory / f"{content_hash[:24]}.png"
            path.write_bytes(payload)
            asset.local_path = str(path)
            asset.public_url = self.store.public_url_for(path, subdir)
            asset.content_hash = content_hash
            asset.bytes = len(payload)
            return

        stored = self.store.fetch(url, subdir=subdir)
        asset.local_path = str(stored.path)
        asset.public_url = stored.public_url
        asset.content_hash = stored.content_hash
        asset.bytes = stored.bytes

    def _model_name(self, plan: PromptPlan) -> str:
        if plan.kind == "video":
            return self.settings.kie_video_model
        return self.settings.kie_image_model

    def _offer_for(self, creative: Creative) -> Offer:
        from ..models import AdGroup, Campaign

        group = self.session.get(AdGroup, creative.ad_group_id)
        campaign = self.session.get(Campaign, group.campaign_id) if group else None
        offer = self.session.get(Offer, campaign.offer_id) if campaign else None
        if offer is None:
            raise MediaError(
                f"creative {creative.id} has no reachable offer", code="ORPHANED"
            )
        return offer

    def _platform_for(self, creative: Creative) -> Platform:
        from ..models import AdGroup, Campaign

        group = self.session.get(AdGroup, creative.ad_group_id)
        campaign = self.session.get(Campaign, group.campaign_id) if group else None
        return campaign.platform if campaign else Platform.META
