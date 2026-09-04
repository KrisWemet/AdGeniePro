"""Generating the visual half of an ad.

The sequence is deliberate: plan the prompt, screen it, generate, download
before the URL expires, then record the row. Screening first is what keeps a
policy-violating image from being paid for, and downloading immediately is what
keeps an ad from pointing at a dead URL a day later.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Creative, MediaAsset, MediaKind, MediaStatus, Offer, Platform
from .base import MediaError, MediaProvider, MediaRequest
from .prompts import PromptPlan, build_image_prompt, build_video_prompt
from .sandbox import SandboxMediaProvider
from .specs import default_placements, get_media_spec
from .store import MediaStore

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
    ) -> list[MediaAsset]:
        """Produce the assets one creative needs, one per placement."""
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
                build_image_prompt(offer, creative.angle, placement)
                if kind == "image"
                else build_video_prompt(offer, creative.angle, placement, hook_line=hook)
            )
            assets.append(
                self._run_plan(plan, creative_id=creative.id, offer_id=offer.id)
            )

        # Only a URL the platform can fetch belongs here. A local path would be
        # handed to Meta as if it were an image source, and rejected.
        fetchable = [
            a.public_url for a in assets
            if a.status is MediaStatus.READY and a.public_url
        ]
        if fetchable:
            creative.media_urls = fetchable
        elif any(a.status is MediaStatus.READY for a in assets):
            logger.warning(
                "Generated %s asset(s) for creative %s but MEDIA_PUBLIC_BASE_URL "
                "is not set, so there is no address a platform can fetch them "
                "from. The files are on disk; set it to attach them to a live ad.",
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
            logger.error("Media generation failed: %s", exc)
            self.session.flush()
            return asset

        asset.task_id = result.task_id
        asset.remote_url = result.urls[0] if result.urls else None
        asset.model = result.model or asset.model

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
