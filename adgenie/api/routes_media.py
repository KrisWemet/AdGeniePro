"""Operator-controlled media-provider verification.

Submitting a generation may consume provider credit, so submission and status
polling are separate endpoints. Refreshing or polling can never create a
second task accidentally.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field

from ..config import get_settings
from ..media.base import MediaRequest
from ..media.kie import KieClient
from ..media.prompts import review_media_prompt

router = APIRouter(tags=["media"])


class KieImageTestIn(BaseModel):
    prompt: str = Field(
        default=(
            "Candid iPhone photo in a real home kitchen. A hand holds a cold "
            "metal cup covered in large water drops while one finger points at "
            "the drops. The odd close-up must make a person pause and wonder "
            "where the water came from. Slight tilt, imperfect framing, natural "
            "window light, real counter clutter, sharp cup and drops, no staged "
            "studio look. No product, text, logo, packaging or fear scene."
        ),
        min_length=20,
        max_length=1500,
    )
    aspect_ratio: Literal["1:1", "4:5", "9:16", "16:9"] = "1:1"


@router.post(
    "/media/kie/image-test",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit One Kie Image Test",
    description=(
        "Creates exactly one 1K image task and may consume Kie credit. "
        "Use the returned task_id with the status endpoint; do not resubmit "
        "while the task is running."
    ),
)
def submit_kie_image_test(request: KieImageTestIn) -> dict:
    settings = get_settings()
    if not settings.kie_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="KIE_API_KEY is not configured",
        )

    findings = review_media_prompt(request.prompt)
    if findings:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "The image prompt failed the advertising policy review",
                "findings": findings,
            },
        )

    client = KieClient(settings)
    try:
        task_id = client.submit(
            MediaRequest(
                prompt=request.prompt,
                kind="image",
                aspect_ratio=request.aspect_ratio,
                count=1,
                extra={"resolution": "1K", "output_format": "png"},
            )
        )
    finally:
        client.close()
    return {
        "task_id": task_id,
        "state": "submitted",
        "provider": "kie",
        "model": settings.kie_image_model,
        "aspect_ratio": request.aspect_ratio,
        "next": f"GET /api/media/kie/tasks/{task_id}",
        "warning": "Poll this task id; submitting again may consume another credit.",
    }


@router.get(
    "/media/kie/tasks/{task_id}",
    summary="Check Kie Media Task",
    description="Checks an existing task once. This never starts or charges for a new task.",
)
def get_kie_task(task_id: str = Path(min_length=1, max_length=200)) -> dict:
    settings = get_settings()
    if not settings.kie_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="KIE_API_KEY is not configured",
        )

    client = KieClient(settings)
    try:
        result = client.poll(task_id)
    finally:
        client.close()
    return {
        "task_id": result.task_id,
        "state": result.state,
        "provider": result.provider,
        "model": result.model or settings.kie_image_model,
        "urls": result.urls,
        "error": result.error,
    }
