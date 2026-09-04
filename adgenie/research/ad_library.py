"""Meta Ad Library client.

What this can and cannot tell you, stated plainly because the difference
decides how the data may be used:

* It carries **no** click-through rate, conversion count, ROAS or spend for
  commercial ads. Political and issue ads report spend and impressions as wide
  ranges; EU commercial ads report a single reach figure; everything else
  reports nothing.
* `ad_type=ALL` returns ordinary product ads **only for EU and UK delivery**,
  where the Digital Services Act requires every ad to be archived. Ask for a US
  or Canadian country code and you get political and issue ads only, plus the
  US special categories (housing, employment, financial products).
* Creative images and videos sit behind `ad_snapshot_url`, a rendered page, not
  a media endpoint. This client records the URL and does not fetch it.

So the library cannot say what is *working*. What it can say is what is still
*running*, and for how long, which is the inference an experienced buyer makes
from it: nobody funds a losing ad for three months. `signals.py` turns that
into something usable.

Uses the official Graph API. There is no scraping here: the Ad Library web
interface forbids it, and a scraper is an account-ban risk on the same account
that runs the ads.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx

from ..config import Settings, get_settings
from ..models import Platform
from ..platforms.base import PlatformError

logger = logging.getLogger(__name__)

__all__ = [
    "AdLibraryAd",
    "AdLibraryClient",
    "CoverageWarning",
    "EU_UK_COUNTRIES",
    "commercial_ads_available",
]

GRAPH_BASE = "https://graph.facebook.com"

# Countries where the DSA ad repository obligation means every ad, including
# ordinary commercial ads, appears in the archive.
EU_UK_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
        "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
        "SI", "ES", "SE", "GB",
    }
)

# The fields worth asking for. Requesting everything slows the call down and
# most of it is null for commercial ads.
DEFAULT_FIELDS = (
    "id",
    "page_id",
    "page_name",
    "ad_creative_bodies",
    "ad_creative_link_titles",
    "ad_creative_link_descriptions",
    "ad_creative_link_captions",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "publisher_platforms",
    "languages",
    "target_ages",
    "target_gender",
    "eu_total_reach",
)

# Only meaningful for political and issue ads; requesting them for commercial
# ads returns nulls, and on some tokens an error.
POLITICAL_FIELDS = ("impressions", "spend", "demographic_distribution", "bylines")


@dataclass
class CoverageWarning:
    """Why a search may return less than the caller expects."""

    code: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class AdLibraryAd:
    """One ad as the library reports it, normalised."""

    ad_archive_id: str
    page_id: str | None = None
    page_name: str = ""
    bodies: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    captions: list[str] = field(default_factory=list)
    snapshot_url: str | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    publisher_platforms: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    eu_total_reach: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.stopped_at is None

    def days_running(self, as_of: datetime | None = None) -> int:
        if self.started_at is None:
            return 0
        as_of = as_of or datetime.now(timezone.utc)
        end = self.stopped_at or as_of
        start, end = _aware(self.started_at), _aware(end)
        return max(0, (end - start).days)

    def all_text(self) -> list[str]:
        return [t for t in (self.bodies + self.titles + self.descriptions) if t]


def commercial_ads_available(countries: list[str]) -> bool:
    """Whether ad_type=ALL will return ordinary product ads for these countries."""
    return any(c.upper() in EU_UK_COUNTRIES for c in countries)


class AdLibraryClient:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.meta_access_token and client is None:
            raise PlatformError(
                "The Ad Library needs a Meta access token with ads_read "
                "(set META_ACCESS_TOKEN).",
                platform=Platform.META,
                code="NO_CREDENTIALS",
            )
        self.api_version = self.settings.meta_api_version
        self._client = client or httpx.Client(timeout=60.0)

    # ------------------------------------------------------------------
    def search(
        self,
        *,
        search_terms: str | None = None,
        page_ids: list[str] | None = None,
        countries: list[str] | None = None,
        ad_type: str = "ALL",
        active_only: bool = True,
        started_after: date | None = None,
        limit: int | None = None,
        max_pages: int = 5,
        include_political_fields: bool = False,
    ) -> tuple[list[AdLibraryAd], list[CoverageWarning]]:
        """Search the archive.

        Returns the ads plus any coverage warnings. The warnings matter: an
        empty result for a US search means "the API does not carry these ads",
        not "this offer has no competition", and acting on the second reading
        would be badly wrong.
        """
        if not (search_terms or page_ids):
            raise ValueError("provide search_terms or page_ids")

        countries = [c.upper() for c in (countries or self.settings.ad_library_country_codes)]
        warnings = self._coverage_warnings(countries, ad_type)

        fields = list(DEFAULT_FIELDS)
        if include_political_fields:
            fields += list(POLITICAL_FIELDS)

        params: dict[str, str | int] = {
            "access_token": self.settings.meta_access_token or "",
            "ad_reached_countries": ",".join(countries),
            "ad_type": ad_type,
            "ad_active_status": "ACTIVE" if active_only else "ALL",
            "fields": ",".join(fields),
            "limit": limit or self.settings.ad_library_page_size,
        }
        if search_terms:
            params["search_terms"] = search_terms
        if page_ids:
            # The API accepts a bounded batch of page ids per call.
            params["search_page_ids"] = ",".join(str(p) for p in page_ids[:10])
        if started_after:
            params["ad_delivery_date_min"] = started_after.isoformat()

        ads: list[AdLibraryAd] = []
        url = f"{GRAPH_BASE}/{self.api_version}/ads_archive"
        request_params: dict | None = params

        for _ in range(max_pages):
            body = self._request(url, request_params)
            for item in body.get("data", []):
                ads.append(self._parse(item, countries))
            nxt = (body.get("paging") or {}).get("next")
            if not nxt:
                break
            url, request_params = nxt, None

        logger.info(
            "Ad Library: %s ads for %r in %s",
            len(ads),
            search_terms or page_ids,
            ",".join(countries),
        )
        if not ads and not warnings:
            warnings.append(
                CoverageWarning(
                    "NO_RESULTS",
                    "The archive returned nothing for this search. Try a broader "
                    "term, more countries, or ad_active_status=ALL.",
                )
            )
        return ads, warnings

    # ------------------------------------------------------------------
    def _coverage_warnings(
        self, countries: list[str], ad_type: str
    ) -> list[CoverageWarning]:
        warnings: list[CoverageWarning] = []
        if ad_type == "ALL" and not commercial_ads_available(countries):
            warnings.append(
                CoverageWarning(
                    "NO_COMMERCIAL_COVERAGE",
                    "None of "
                    + ", ".join(countries)
                    + " is an EU or UK market, so the archive carries only "
                    "political and issue ads there. Ordinary product ads are "
                    "archived under the Digital Services Act, which covers the "
                    "EU and UK only. Add an EU or UK country code to see "
                    "commercial competitors.",
                )
            )
        warnings.append(
            CoverageWarning(
                "NO_PERFORMANCE_DATA",
                "The archive reports no click-through rate, conversion or spend "
                "data for commercial ads. How long an ad has run and whether it "
                "is still live are the only performance signals available.",
            )
        )
        return warnings

    def _request(self, url: str, params: dict | None) -> dict:
        last: PlatformError | None = None
        for attempt in range(4):
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                last = PlatformError(
                    f"network error calling the Ad Library: {exc}",
                    platform=Platform.META,
                    retryable=True,
                )
            else:
                if response.status_code < 400:
                    return response.json() if response.content else {}
                last = self._to_error(response)
            if not last.retryable or attempt == 3:
                raise last
            time.sleep(2**attempt)
        raise last  # pragma: no cover

    @staticmethod
    def _to_error(response: httpx.Response) -> PlatformError:
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": response.text[:400]}}
        err = body.get("error", {}) if isinstance(body, dict) else {}
        message = err.get("message", response.text[:300])
        code = err.get("code")
        hint = ""
        if code == 190:
            hint = " The token is invalid or expired."
        elif "ads_read" in str(message) or code == 200:
            hint = (
                " The token needs the ads_read permission, and political ad "
                "access additionally requires identity confirmation."
            )
        return PlatformError(
            f"Ad Library error {response.status_code}: {message}{hint}",
            platform=Platform.META,
            code=code or response.status_code,
            retryable=response.status_code in (429, 500, 502, 503, 504) or code == 4,
            payload=body if isinstance(body, dict) else {},
        )

    @staticmethod
    def _parse(item: dict, countries: list[str]) -> AdLibraryAd:
        return AdLibraryAd(
            ad_archive_id=str(item.get("id", "")),
            page_id=str(item["page_id"]) if item.get("page_id") else None,
            page_name=item.get("page_name", "") or "",
            bodies=_texts(item.get("ad_creative_bodies")),
            titles=_texts(item.get("ad_creative_link_titles")),
            descriptions=_texts(item.get("ad_creative_link_descriptions")),
            captions=_texts(item.get("ad_creative_link_captions")),
            snapshot_url=item.get("ad_snapshot_url"),
            started_at=_parse_time(item.get("ad_delivery_start_time")),
            stopped_at=_parse_time(item.get("ad_delivery_stop_time")),
            publisher_platforms=list(item.get("publisher_platforms") or []),
            languages=list(item.get("languages") or []),
            countries=countries,
            eu_total_reach=int(item.get("eu_total_reach") or 0),
            raw=item,
        )


def _texts(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
