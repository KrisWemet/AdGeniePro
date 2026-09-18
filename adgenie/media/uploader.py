"""Getting generated media into the ad account.

Generation produced a file. An ad needs a reference the platform will still
resolve in six weeks. Those are not the same thing, and the gap between them is
where a campaign quietly starts serving a broken image while continuing to
spend: a provider result URL expires in about a day, and even a URL hosted here
is only as durable as this deployment.

So the file is uploaded into the ad account, which then owns it, and the handle
that comes back is stored against the account it belongs to. Uploading is
idempotent on content: the same bytes going to the same account reuse the
handle they got the first time.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Creative, MediaAsset, MediaKind, MediaStatus, PlatformAsset
from ..platforms.base import AdPlatform, MediaHandle, MediaUpload, PlatformError

logger = logging.getLogger(__name__)

__all__ = ["MediaUploader"]


class MediaUploader:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    def handles_for_creative(
        self, creative: Creative, client: AdPlatform
    ) -> list[MediaHandle]:
        """Upload whatever this creative needs, and return usable handles.

        Anything that cannot be uploaded is logged and skipped rather than
        raised: a missing image is a worse ad, but a failed launch is no ad at
        all, and Meta will serve a link ad without one.
        """
        assets = self._assets_for(creative)
        handles: list[MediaHandle] = []
        for asset in assets:
            try:
                handle = self.ensure_uploaded(asset, client)
            except PlatformError as exc:
                if exc.code == "UNSUPPORTED":
                    # Not a failure. This platform has nowhere to put media,
                    # and said so plainly; say it once and move on.
                    logger.info(
                        "%s takes no media, so asset %s stays local.",
                        client.platform.value,
                        asset.id,
                    )
                    return []
                logger.error("Could not upload asset %s: %s", asset.id, exc)
                continue
            if handle is not None:
                handles.append(handle)
        return handles

    def _assets_for(self, creative: Creative) -> list[MediaAsset]:
        return list(
            self.session.execute(
                select(MediaAsset)
                .where(
                    MediaAsset.creative_id == creative.id,
                    MediaAsset.status == MediaStatus.READY,
                )
                .order_by(MediaAsset.id)
            ).scalars()
        )

    # ------------------------------------------------------------------
    def ensure_uploaded(
        self, asset: MediaAsset, client: AdPlatform
    ) -> MediaHandle | None:
        """Return this asset's handle in this ad account, uploading if needed."""
        if not asset.content_hash:
            logger.warning(
                "Asset %s has no content hash, so it cannot be deduplicated or "
                "looked up. Skipping rather than uploading it repeatedly.",
                asset.id,
            )
            return None

        if self.settings.dry_run or getattr(client, "dry_run", False):
            return None

        account = client.account_key
        record = self.session.execute(
            select(PlatformAsset).where(
                PlatformAsset.platform == client.platform,
                PlatformAsset.account_id == account,
                PlatformAsset.content_hash == asset.content_hash,
            )
        ).scalar_one_or_none()

        if (
            record is not None
            and record.handle
            and not record.handle.startswith("dryrun_")
            and not (record.thumbnail_handle or "").startswith("dryrun_")
        ):
            handle = _to_handle(record)
            if not handle.ready:
                # A video that was still transcoding last time. Ask again
                # rather than leaving it permanently unusable.
                handle = client.refresh_media(handle)
                record.ready = handle.ready
                record.thumbnail_url = handle.thumbnail_url or record.thumbnail_url
                self.session.flush()
            return handle

        path = Path(asset.local_path or "")
        if not path.is_file():
            logger.error(
                "Asset %s has no local file at %r. The provider URL it came "
                "from has almost certainly expired, so it has to be "
                "regenerated rather than re-fetched.",
                asset.id,
                asset.local_path,
            )
            return None

        handle = client.upload_media(
            MediaUpload(
                path=str(path),
                content_type=_content_type(asset, path),
                kind=asset.kind.value,
                name=f"adgenie-{asset.content_hash[:16]}{path.suffix}",
                content_hash=asset.content_hash,
            )
        )
        if (
            handle.handle.startswith("dryrun_")
            or handle.thumbnail_handle.startswith("dryrun_")
        ):
            return None
        if record is None:
            record = PlatformAsset(
                platform=client.platform,
                account_id=account,
                content_hash=asset.content_hash,
            )
            self.session.add(record)
        record.media_asset_id = asset.id
        record.kind = asset.kind
        record.handle = handle.handle
        record.ready = handle.ready
        record.thumbnail_url = handle.thumbnail_url or None
        record.thumbnail_handle = handle.thumbnail_handle or None
        record.width = handle.width or asset.width
        record.height = handle.height or asset.height
        record.error = None
        self.session.flush()
        logger.info(
            "Uploaded %s asset %s to %s as %s",
            asset.kind.value,
            asset.id,
            account,
            handle.handle,
        )
        return handle


def _to_handle(record: PlatformAsset) -> MediaHandle:
    return MediaHandle(
        kind=record.kind.value,
        handle=record.handle,
        ready=record.ready,
        thumbnail_url=record.thumbnail_url or "",
        thumbnail_handle=record.thumbnail_handle or "",
        width=record.width,
        height=record.height,
    )


def _content_type(asset: MediaAsset, path: Path) -> str:
    """Prefer the file's own extension over the asset's declared kind.

    The kind says image or video; the platform needs the exact type, and the
    stored file was named from the content type the provider actually served.
    """
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    return "video/mp4" if asset.kind is MediaKind.VIDEO else "image/jpeg"
