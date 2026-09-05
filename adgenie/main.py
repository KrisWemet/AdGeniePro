"""FastAPI application.

Run it with:

    uvicorn adgenie.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import (
    routes_campaigns,
    routes_funnel,
    routes_offers,
    routes_optimizer,
    routes_research,
    routes_tracking,
)
from .api.security import require_api_key
from .config import get_settings
from .db import init_db
from .models import Platform
from .platforms.base import PlatformError
from .platforms.factory import get_platform, is_sandbox

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    logger.info("%s %s starting (env=%s)", settings.app_name, __version__, settings.environment)
    if settings.dry_run:
        logger.info(
            "DRY RUN is on: no mutation will be sent to a live ad account. "
            "Set DRY_RUN=false to let the optimizer spend."
        )
    from .core.tracking import secret_is_placeholder

    if secret_is_placeholder(settings.postback_secret):
        logger.warning(
            "POSTBACK_SECRET is still the example value. Conversion postbacks "
            "will be rejected until it is set, because revenue posted there is "
            "what the optimizer spends against."
        )
    if not settings.requires_api_key:
        logger.warning(
            "API_KEY is not set: the /api routes, which launch campaigns and "
            "move budgets, are unauthenticated. Bind to localhost or set a key."
        )
    for platform, configured in (
        (Platform.META, settings.has_meta),
        (Platform.GOOGLE, settings.has_google),
    ):
        logger.info(
            "%s: %s", platform.value, "connected" if configured else "sandbox (simulated)"
        )
    if not settings.has_copywriter_llm:
        logger.info(
            "No ANTHROPIC_API_KEY set: copy will be generated from templates. "
            "Set it for materially better ad copy."
        )
    if not settings.has_media_generation:
        logger.info(
            "No KIE_API_KEY set: images and video are simulated placeholders at "
            "the correct dimensions."
        )
    if not settings.has_ad_library:
        logger.info(
            "No META_ACCESS_TOKEN set: competitor research is unavailable."
        )
    yield


app = FastAPI(
    title="AdGenie Pro",
    version=__version__,
    description=(
        "Writes, launches and optimizes ads on Meta and Google for affiliate "
        "offers. Copy is policy-checked before launch, revenue is measured "
        "from network postbacks rather than platform pixels, and every "
        "optimizer decision is recorded with the evidence behind it."
    ),
    lifespan=lifespan,
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=_settings.cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(PlatformError)
async def platform_error_handler(request, exc: PlatformError) -> JSONResponse:
    return JSONResponse(
        status_code=502 if exc.retryable else 400,
        content={
            "error": "platform_error",
            "platform": exc.platform.value if exc.platform else None,
            "code": str(exc.code),
            "retryable": exc.retryable,
            "detail": str(exc),
        },
    )


# Everything under /api is guarded. The tracking router is not: `/r` takes
# anonymous ad clicks and `/postback` carries its own shared secret.
_guard = [Depends(require_api_key)]
app.include_router(routes_offers.router, prefix="/api", dependencies=_guard)
app.include_router(routes_campaigns.router, prefix="/api", dependencies=_guard)
app.include_router(routes_optimizer.router, prefix="/api", dependencies=_guard)
app.include_router(routes_research.router, prefix="/api", dependencies=_guard)
app.include_router(routes_funnel.router, prefix="/api", dependencies=_guard)
app.include_router(routes_tracking.router)


@app.get("/api/health", tags=["system"], dependencies=[Depends(require_api_key)])
def health() -> dict:
    settings = get_settings()
    platforms = {}
    for platform in Platform:
        try:
            client = get_platform(platform, settings)
            status = client.health_check()
            status["simulated"] = is_sandbox(client)
        except Exception as exc:  # a misconfigured platform must not 500 health
            status = {"ok": False, "error": str(exc)}
        platforms[platform.value] = status

    return {
        "status": "ok",
        "version": __version__,
        "environment": settings.environment,
        "dry_run": settings.dry_run,
        "copywriter": "claude" if settings.has_copywriter_llm else "template",
        "media": "kie" if settings.has_media_generation else "sandbox",
        "ad_library": "connected" if settings.has_ad_library else "unavailable",
        "authenticated": settings.requires_api_key,
        "global_daily_budget_cap_usd": settings.global_daily_budget_cap_usd,
        "platforms": platforms,
    }


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
