"""API authentication.

The `/api` routes create campaigns, move budgets and approve spend, so they are
not safe to expose unauthenticated. Authentication is opt-in by configuration
rather than mandatory, because the common local-development case is a server
bound to localhost with no key set. Setting `API_KEY` turns it on everywhere.

The two public routes are deliberately outside this: `/r` has to accept
anonymous ad clicks, and `/postback` authenticates with its own shared secret
because affiliate networks cannot send custom headers.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from ..config import Settings, get_settings


async def require_api_key(
    x_api_key: str | None = Header(default=None),
) -> None:
    """FastAPI dependency guarding every mutating and reporting route."""
    settings: Settings = get_settings()
    if not settings.requires_api_key:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key or ""):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid X-API-Key header is required",
            headers={"WWW-Authenticate": "ApiKey"},
        )
