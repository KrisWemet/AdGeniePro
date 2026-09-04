"""FastAPI application.

Run it with:

    uvicorn adgenie.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import routes_campaigns, routes_offers, routes_optimizer, routes_tracking
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


app.include_router(routes_offers.router, prefix="/api")
app.include_router(routes_campaigns.router, prefix="/api")
app.include_router(routes_optimizer.router, prefix="/api")
app.include_router(routes_tracking.router)


@app.get("/api/health", tags=["system"])
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
        "global_daily_budget_cap_usd": settings.global_daily_budget_cap_usd,
        "platforms": platforms,
    }


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
