"""Choose the right adapter for a platform.

Selection is deliberate and loud: if credentials for a platform are missing,
you get the sandbox and a log line saying so, never a silent no-op. That way a
misconfigured deployment produces obviously simulated numbers rather than an
empty dashboard that looks like poor performance.
"""

from __future__ import annotations

import logging

from ..config import Settings, get_settings
from ..models import Platform
from .base import AdPlatform
from .sandbox import SandboxPlatform

logger = logging.getLogger(__name__)

_SANDBOX_CACHE: dict[tuple[Platform, int], SandboxPlatform] = {}


def get_platform(
    platform: Platform,
    settings: Settings | None = None,
    force_sandbox: bool = False,
) -> AdPlatform:
    settings = settings or get_settings()

    if not force_sandbox:
        if platform is Platform.META and settings.has_meta:
            from .meta import MetaAdsClient

            return MetaAdsClient(settings)
        if platform is Platform.GOOGLE and settings.has_google:
            from .google import GoogleAdsClient

            return GoogleAdsClient(settings)
        logger.warning(
            "No %s credentials configured; using the sandbox simulator. "
            "Numbers are simulated, not real.",
            platform.value,
        )

    key = (platform, 1337)
    if key not in _SANDBOX_CACHE:
        _SANDBOX_CACHE[key] = SandboxPlatform(platform)
    return _SANDBOX_CACHE[key]


def is_sandbox(client: AdPlatform) -> bool:
    return isinstance(client, SandboxPlatform)


def reset_sandboxes() -> None:
    """Used by tests to get a clean simulated account."""
    _SANDBOX_CACHE.clear()
