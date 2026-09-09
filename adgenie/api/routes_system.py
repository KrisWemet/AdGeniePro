"""Operator-only system diagnostics.

The preflight endpoint exists so a deployed environment can prove its own
credentials, public tracking origin and live platform access without requiring
an interactive shell on the host. It is mounted under /api and therefore uses
the same X-API-Key guard as campaign and budget controls.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from ..models import Platform
from ..preflight import run_preflight

router = APIRouter(tags=["system"])


@router.get("/preflight")
def production_preflight(
    platform: Literal["meta", "google", "all"] = Query(default="all"),
    live: bool = Query(
        default=False,
        description=(
            "When true, make read-only calls to the selected platform and the "
            "public AdGenie health endpoint. No ad objects are mutated."
        ),
    ),
) -> dict:
    if platform == "meta":
        selected = (Platform.META,)
    elif platform == "google":
        selected = (Platform.GOOGLE,)
    else:
        selected = (Platform.META, Platform.GOOGLE)

    return run_preflight(platforms=selected, live=live).as_dict()
