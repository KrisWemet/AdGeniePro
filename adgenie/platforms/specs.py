"""Hard format constraints for each ad platform.

Getting these wrong is the single most common reason a programmatically built
ad is rejected, so they are declared once here and enforced in three places:
when the copywriter generates text, when compliance reviews it, and again in
the platform adapter right before the API call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Platform


@dataclass(frozen=True)
class FieldSpec:
    name: str
    max_chars: int
    min_count: int
    max_count: int
    recommended_count: int
    # Google truncates on display width for some surfaces; Meta truncates the
    # primary text in-feed at roughly this point with a "See more" link.
    soft_truncate_at: int | None = None


@dataclass(frozen=True)
class AdSpec:
    platform: Platform
    ad_format: str
    fields: dict[str, FieldSpec]
    allowed_ctas: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def spec_for(self, field_name: str) -> FieldSpec | None:
        return self.fields.get(field_name)


META_FEED = AdSpec(
    platform=Platform.META,
    ad_format="feed",
    fields={
        # Meta itself only hard-limits primary text loosely, but text past the
        # fold is unread, so we treat 125 characters as the working budget.
        "primary_texts": FieldSpec("primary_texts", 500, 1, 5, 3, soft_truncate_at=125),
        "headlines": FieldSpec("headlines", 40, 1, 5, 3, soft_truncate_at=27),
        "descriptions": FieldSpec("descriptions", 60, 0, 5, 2, soft_truncate_at=27),
    },
    allowed_ctas=(
        "LEARN_MORE",
        "SHOP_NOW",
        "SIGN_UP",
        "GET_OFFER",
        "SUBSCRIBE",
        "DOWNLOAD",
        "BOOK_TRAVEL",
        "APPLY_NOW",
        "GET_QUOTE",
        "SEE_MENU",
        "CONTACT_US",
        "ORDER_NOW",
    ),
    notes=(
        "Headlines are truncated near 27 characters on mobile feed placements.",
        "Primary text collapses behind 'See more' after roughly 125 characters.",
    ),
)

GOOGLE_RSA = AdSpec(
    platform=Platform.GOOGLE,
    ad_format="responsive_search_ad",
    fields={
        "headlines": FieldSpec("headlines", 30, 3, 15, 12),
        "descriptions": FieldSpec("descriptions", 90, 2, 4, 4),
        "display_url_path": FieldSpec("display_url_path", 15, 0, 2, 2),
    },
    notes=(
        "Ad Strength reaches 'Excellent' near 12 distinct headlines and 4 descriptions.",
        "At least 3 headlines and 2 descriptions are required by the API.",
        "Duplicate or near-duplicate assets are counted once for Ad Strength.",
    ),
)

GOOGLE_DEMAND_GEN = AdSpec(
    platform=Platform.GOOGLE,
    ad_format="demand_gen",
    fields={
        "headlines": FieldSpec("headlines", 40, 1, 5, 5),
        "descriptions": FieldSpec("descriptions", 90, 1, 5, 5),
    },
)

SPECS: dict[tuple[Platform, str], AdSpec] = {
    (Platform.META, "feed"): META_FEED,
    (Platform.GOOGLE, "responsive_search_ad"): GOOGLE_RSA,
    (Platform.GOOGLE, "demand_gen"): GOOGLE_DEMAND_GEN,
}

DEFAULT_FORMAT = {
    Platform.META: "feed",
    Platform.GOOGLE: "responsive_search_ad",
}


def get_spec(platform: Platform, ad_format: str | None = None) -> AdSpec:
    fmt = ad_format or DEFAULT_FORMAT[platform]
    try:
        return SPECS[(platform, fmt)]
    except KeyError as exc:  # pragma: no cover - guarded by callers
        raise ValueError(f"no spec for {platform.value}/{fmt}") from exc


# A truncated result shorter than this is not worth the sentence boundary.
_MIN_SENTENCE_CHARS = 20


def _is_abbreviation(text: str, period_index: int) -> bool:
    """Tell "Dr." and "U.S." apart from the end of a sentence."""
    token = text[:period_index].rsplit(" ", 1)[-1]
    return len(token) <= 2 or "." in token


def truncate_to_spec(text: str, max_chars: int) -> str:
    """Trim to the limit, preferring a sentence boundary, then a word boundary.

    A headline cut mid-phrase reads as broken and depresses click-through, so
    prefer to lose a whole sentence over ending on a dangling preposition.
    """
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text

    window = text[:max_chars]
    # A complete short sentence reads better than a longer fragment, so prefer
    # the last sentence boundary in the window. The 20-character floor keeps an
    # abbreviation ("Dr.", "U.S.") from truncating the text to nothing.
    boundary = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if boundary + 1 >= _MIN_SENTENCE_CHARS and not _is_abbreviation(window, boundary):
        return window[: boundary + 1].strip()
    if window.endswith((".", "!", "?")):
        return window.strip()

    cut = window.rsplit(" ", 1)[0] if " " in window else window
    cut = cut.rstrip(" ,;:-")
    # Never end a headline on a word that leaves the reader hanging.
    dangling = {
        "a", "an", "the", "and", "or", "but", "to", "of", "for", "in", "on",
        "with", "without", "your", "you", "that", "this", "is", "are", "at",
        "by", "from", "into", "than", "then", "it", "its",
    }
    words = cut.split()
    while words and words[-1].lower().strip(",.;:") in dangling:
        words.pop()
    return " ".join(words).rstrip(" ,;:-")
