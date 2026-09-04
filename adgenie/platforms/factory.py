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
# Live clients are cached too: each one owns an httpx connection pool and, for
# Google, a cached OAuth token. Rebuilding one per request would open a fresh
# pool that is never closed and re-authenticate on every call.
_LIVE_CACHE: dict[tuple[Platform, int], AdPlatform] = {}


def get_platform(
    platform: Platform,
    settings: Settings | None = None,
    force_sandbox: bool = False,
) -> AdPlatform:
    settings = settings or get_settings()

    if not force_sandbox:
        configured = (
            settings.has_meta if platform is Platform.META else settings.has_google
        )
        if configured:
            key = (platform, id(settings))
            if key not in _LIVE_CACHE:
                if platform is Platform.META:
                    from .meta import MetaAdsClient

                    _LIVE_CACHE[key] = MetaAdsClient(settings)
                else:
                    from .google import GoogleAdsClient

                    _LIVE_CACHE[key] = GoogleAdsClient(settings)
            return _LIVE_CACHE[key]
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
    _LIVE_CACHE.clear()
