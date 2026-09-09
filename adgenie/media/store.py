"""Persisting generated assets.

Provider result URLs expire within about a day. An ad still pointing at one
after that is an ad with a broken image, so every asset is downloaded as soon
as it is produced and the local copy becomes the source of truth.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

__all__ = ["StoredAsset", "MediaStore"]

# Refuse anything larger than this. A runaway download should not fill the disk.
MAX_BYTES = 200 * 1024 * 1024

_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


@dataclass
class StoredAsset:
    path: Path
    content_hash: str
    bytes: int
    content_type: str
    public_url: str | None = None


class MediaStore:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        root: Path | str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.root = Path(root or self.settings.media_storage_dir)
        self._client = client or httpx.Client(timeout=180.0, follow_redirects=True)

    def fetch(self, url: str, subdir: str = "") -> StoredAsset:
        """Download one asset and store it under a content-addressed name.

        Naming by content hash means regenerating the same prompt does not
        accumulate duplicate files, and an interrupted download can never leave
        a half-written file in place of a good one.
        """
        with self._client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise RuntimeError(
                    f"could not download the generated asset ({response.status_code}). "
                    "Provider URLs expire in about a day, so a stale task result "
                    "cannot be recovered and has to be regenerated."
                )
            declared = int(response.headers.get("content-length") or 0)
            if declared > MAX_BYTES:
                raise RuntimeError(f"asset is {declared} bytes, over the limit")

            content_type = (
                response.headers.get("content-type", "").split(";")[0].strip()
                or "application/octet-stream"
            )
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise RuntimeError("asset exceeded the size limit mid-download")
                digest.update(chunk)
                chunks.append(chunk)

        content_hash = digest.hexdigest()
        extension = _EXTENSIONS.get(content_type) or mimetypes.guess_extension(
            content_type
        ) or Path(url.split("?")[0]).suffix or ".bin"

        directory = self.root / subdir if subdir else self.root
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{content_hash[:24]}{extension}"

        if not path.exists():
            # Write beside the target and move into place, so a failure part way
            # through never leaves a truncated file that looks complete.
            temporary = path.with_suffix(path.suffix + ".part")
            temporary.write_bytes(b"".join(chunks))
            temporary.replace(path)

        logger.info("Stored %s (%.1f KB) at %s", content_type, total / 1024, path)
        return StoredAsset(
            path=path,
            content_hash=content_hash,
            bytes=total,
            content_type=content_type,
            public_url=self.public_url_for(path, subdir),
        )

    def public_url_for(self, path: Path, subdir: str = "") -> str | None:
        """Where a platform can fetch this asset, if a public base is set."""
        base = self.settings.media_public_base_url
        if not base:
            return None
        relative = f"{subdir}/{path.name}" if subdir else path.name
        return f"{base.rstrip('/')}/{relative}"
