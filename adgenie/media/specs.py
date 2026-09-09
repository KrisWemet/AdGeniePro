"""Placement dimensions for generated creative.

Wrong aspect ratio is the image equivalent of an over-length headline: the ad
is accepted and then rendered badly, cropping the subject or letterboxing it.
Each placement here is the size the platform actually serves.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Platform


@dataclass(frozen=True)
class MediaSpec:
    placement: str
    aspect_ratio: str
    width: int
    height: int
    kind: str = "image"
    max_seconds: float = 0.0
    notes: str = ""


MEDIA_SPECS: dict[str, MediaSpec] = {
    # Meta. 4:5 wins the most feed real estate on mobile without being cropped.
    "meta_feed": MediaSpec("meta_feed", "4:5", 1080, 1350, notes="Default feed image."),
    "meta_square": MediaSpec("meta_square", "1:1", 1080, 1080, notes="Safe everywhere."),
    "meta_story": MediaSpec(
        "meta_story", "9:16", 1080, 1920,
        notes="Stories and Reels. Keep the subject clear of the top and bottom 250px.",
    ),
    "meta_reel_video": MediaSpec(
        "meta_reel_video", "9:16", 1080, 1920, kind="video", max_seconds=15.0,
        notes="Short vertical video for Reels.",
    ),
    "meta_feed_video": MediaSpec(
        "meta_feed_video", "1:1", 1080, 1080, kind="video", max_seconds=15.0,
    ),
    # Google Demand Gen takes all three ratios; supplying all three lifts reach.
    "google_landscape": MediaSpec("google_landscape", "1.91:1", 1200, 628),
    "google_square": MediaSpec("google_square", "1:1", 1200, 1200),
    "google_portrait": MediaSpec("google_portrait", "4:5", 960, 1200),
    "google_video": MediaSpec(
        "google_video", "9:16", 1080, 1920, kind="video", max_seconds=15.0,
        notes="Demand Gen vertical video.",
    ),
}

# Defaults per platform and media kind. Video is listed explicitly: leaving it
# out silently resolved every video request to zero placements.
PLATFORM_DEFAULTS: dict[tuple[Platform, str], tuple[str, ...]] = {
    (Platform.META, "image"): ("meta_feed", "meta_square", "meta_story"),
    (Platform.META, "video"): ("meta_reel_video", "meta_feed_video"),
    # Search ads carry no imagery at all; these are for Demand Gen and Display.
    (Platform.GOOGLE, "image"): (
        "google_landscape", "google_square", "google_portrait",
    ),
    # Demand Gen accepts video; the vertical asset reaches the most placements.
    (Platform.GOOGLE, "video"): ("google_video",),
}


def get_media_spec(placement: str) -> MediaSpec:
    try:
        return MEDIA_SPECS[placement]
    except KeyError as exc:
        raise ValueError(
            f"unknown placement '{placement}'; expected one of "
            + ", ".join(sorted(MEDIA_SPECS))
        ) from exc


# Ad formats that carry no imagery at all. Generating for one of these spends
# money on assets the platform will never render.
TEXT_ONLY_FORMATS = frozenset({"responsive_search_ad", "expanded_text_ad", "call_ad"})


def default_placements(
    platform: Platform, kind: str = "image", ad_format: str | None = None
) -> list[str]:
    """Placements worth generating for. Empty means this ad format has no media."""
    if ad_format and ad_format in TEXT_ONLY_FORMATS:
        return []
    return [
        p
        for p in PLATFORM_DEFAULTS.get((platform, kind), ())
        if MEDIA_SPECS[p].kind == kind
    ]
