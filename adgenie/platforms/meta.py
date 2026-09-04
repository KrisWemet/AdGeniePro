"""Meta Marketing API adapter.

Maps the AdGenie model onto Meta's object graph:

    Campaign -> Ad Set -> Ad Creative + Ad

Two details drive most of the design here. Meta reports and accepts money in
the account's minor currency unit (cents for USD) while everything else in this
codebase uses micros, so conversion happens at the boundary and nowhere else.
And affiliate conversions fire off-site, so `upload_conversions` pushes network
postbacks back through the Conversions API. Without that the bidding algorithm
optimizes toward landing-page views it can see rather than sales it cannot.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from urllib.parse import parse_qsl, urlparse

import httpx

from ..config import Settings, get_settings
from ..models import Platform
from ..money import micros_to_cents
from .base import (
    AdGroupSpec,
    AdPlatform,
    BreakdownRow,
    CampaignSpec,
    CreativeSpec,
    InsightRow,
    PlatformError,
)

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"

# Meta error subcodes that mean "try again", as opposed to "you sent something
# invalid". Retrying a rejected creative just burns rate limit.
RETRYABLE_CODES = {1, 2, 4, 17, 32, 341, 368, 613}

LEVEL_TO_META = {"campaign": "campaign", "ad_group": "adset", "creative": "ad"}



class MetaAdsClient(AdPlatform):
    platform = Platform.META

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        dry_run: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.has_meta and client is None:
            raise PlatformError(
                "Meta credentials are not configured "
                "(set META_ACCESS_TOKEN and META_AD_ACCOUNT_ID).",
                platform=self.platform,
                code="NO_CREDENTIALS",
            )
        self.api_version = self.settings.meta_api_version
        self.account_id = (self.settings.meta_ad_account_id or "").removeprefix("act_")
        self.dry_run = self.settings.dry_run if dry_run is None else dry_run
        self._client = client or httpx.Client(timeout=45.0)
        self.calls: list[tuple[str, dict]] = []

    # -- transport -------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{GRAPH_BASE}/{self.api_version}/{path.lstrip('/')}"

    def _request(
        self, method: str, path: str, *, data: dict | None = None, params: dict | None = None
    ) -> dict:
        payload = dict(data or {})
        query = dict(params or {})
        query["access_token"] = self.settings.meta_access_token

        if self.dry_run and method.upper() == "POST":
            self.calls.append((f"DRY {method} {path}", payload))
            logger.info("[dry-run] meta %s %s %s", method, path, _preview(payload))
            return {"id": f"dryrun_{abs(hash(json.dumps(payload, sort_keys=True, default=str))) % 10**12}"}

        last_error: PlatformError | None = None
        for attempt in range(4):
            try:
                response = self._client.request(
                    method, self._url(path), data=payload or None, params=query
                )
            except httpx.HTTPError as exc:
                last_error = PlatformError(
                    f"network error calling Meta: {exc}",
                    platform=self.platform,
                    retryable=True,
                )
            else:
                if response.status_code < 400:
                    self.calls.append((f"{method} {path}", payload))
                    return response.json() if response.content else {}
                last_error = self._to_error(response)

            if not last_error.retryable or attempt == 3:
                raise last_error
            time.sleep(2**attempt)
        raise last_error  # pragma: no cover - loop always raises or returns

    def _to_error(self, response: httpx.Response) -> PlatformError:
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": response.text[:500]}}
        err = body.get("error", {}) if isinstance(body, dict) else {}
        code = err.get("code")
        retryable = (
            response.status_code >= 500
            or response.status_code == 429
            or code in RETRYABLE_CODES
        )
        return PlatformError(
            f"Meta API error {response.status_code}: "
            f"{err.get('message', response.text[:300])}",
            platform=self.platform,
            code=code or response.status_code,
            retryable=retryable,
            payload=body if isinstance(body, dict) else {},
        )

    # -- creation --------------------------------------------------------
    def create_campaign(self, spec: CampaignSpec) -> str:
        data = {
            "name": spec.name,
            "objective": spec.objective,
            "status": spec.status.upper(),
            # Required since 2021; an empty list means "no special category".
            "special_ad_categories": json.dumps(
                spec.extra.get("special_ad_categories", [])
            ),
        }
        # Meta rejects a campaign carrying both campaign-level and ad-set-level
        # budgets, so campaign budget optimisation is explicit.
        if spec.extra.get("campaign_budget_optimization") and spec.daily_budget_micros:
            data["daily_budget"] = micros_to_cents(spec.daily_budget_micros)
            data["bid_strategy"] = spec.bid_strategy
        if spec.target_roas and spec.bid_strategy == "LOWEST_COST_WITH_MIN_ROAS":
            data["bid_strategy"] = spec.bid_strategy
        result = self._request("POST", f"act_{self.account_id}/campaigns", data=data)
        return str(result["id"])

    def create_ad_group(self, spec: AdGroupSpec) -> str:
        targeting = dict(spec.targeting or {})
        targeting.setdefault("geo_locations", {"countries": ["US"]})
        targeting.setdefault(
            "targeting_automation", {"advantage_audience": 1}
        )

        data = {
            "name": spec.name,
            "campaign_id": spec.campaign_external_id,
            "status": spec.status.upper(),
            "billing_event": spec.extra.get("billing_event", "IMPRESSIONS"),
            "optimization_goal": spec.extra.get("optimization_goal", "OFFSITE_CONVERSIONS"),
            "targeting": json.dumps(targeting),
        }
        if spec.daily_budget_micros:
            data["daily_budget"] = micros_to_cents(spec.daily_budget_micros)
        if spec.bid_micros:
            data["bid_amount"] = micros_to_cents(spec.bid_micros)
        if spec.extra.get("promoted_object"):
            data["promoted_object"] = json.dumps(spec.extra["promoted_object"])
        elif self.settings.meta_pixel_id:
            data["promoted_object"] = json.dumps(
                {
                    "pixel_id": self.settings.meta_pixel_id,
                    "custom_event_type": spec.extra.get("custom_event_type", "PURCHASE"),
                }
            )
        result = self._request("POST", f"act_{self.account_id}/adsets", data=data)
        return str(result["id"])

    def create_creative(self, spec: CreativeSpec) -> str:
        page_id = spec.extra.get("page_id") or self.settings.meta_page_id
        if not page_id:
            raise PlatformError(
                "A Facebook Page id is required to create an ad creative "
                "(set META_PAGE_ID).",
                platform=self.platform,
                code="NO_PAGE",
            )

        link_data: dict = {
            "link": spec.final_url,
            "message": spec.primary_texts[0] if spec.primary_texts else "",
            "name": spec.headlines[0] if spec.headlines else "",
            "description": spec.descriptions[0] if spec.descriptions else "",
            "call_to_action": {
                "type": spec.call_to_action,
                "value": {"link": spec.final_url},
            },
        }
        if spec.media_urls:
            link_data["picture"] = spec.media_urls[0]
        if spec.extra.get("image_hash"):
            link_data["image_hash"] = spec.extra["image_hash"]

        creative_data = {
            "name": spec.name,
            "object_story_spec": json.dumps(
                {"page_id": page_id, "link_data": link_data}
            ),
        }
        # Hand Meta the extra variants so it can run its own asset-level test.
        if len(spec.headlines) > 1 or len(spec.primary_texts) > 1:
            creative_data["asset_feed_spec"] = json.dumps(
                {
                    "titles": [{"text": h} for h in spec.headlines[:5]],
                    "bodies": [{"text": p} for p in spec.primary_texts[:5]],
                    "descriptions": [{"text": d} for d in spec.descriptions[:5]],
                    "link_urls": [{"website_url": spec.final_url}],
                    "call_to_action_types": [spec.call_to_action],
                }
            )
        creative = self._request(
            "POST", f"act_{self.account_id}/adcreatives", data=creative_data
        )

        ad = self._request(
            "POST",
            f"act_{self.account_id}/ads",
            data={
                "name": spec.name,
                "adset_id": spec.ad_group_external_id,
                "creative": json.dumps({"creative_id": creative["id"]}),
                "status": spec.status.upper(),
            },
        )
        return str(ad["id"])

    # -- mutation --------------------------------------------------------
    def set_status(self, level: str, external_id: str, active: bool) -> None:
        self._request(
            "POST", external_id, data={"status": "ACTIVE" if active else "PAUSED"}
        )

    def set_budget(self, level: str, external_id: str, daily_budget_micros: int) -> None:
        if daily_budget_micros <= 0:
            raise PlatformError(
                "daily budget must be positive",
                platform=self.platform,
                code="INVALID_BUDGET",
            )
        self._request(
            "POST",
            external_id,
            data={"daily_budget": micros_to_cents(daily_budget_micros)},
        )

    def set_bid(self, level: str, external_id: str, bid_micros: int) -> None:
        self._request(
            "POST", external_id, data={"bid_amount": micros_to_cents(bid_micros)}
        )

    # -- measurement -----------------------------------------------------
    def fetch_insights(
        self, level: str, since: date, until: date, external_ids: list[str] | None = None
    ) -> list[InsightRow]:
        meta_level = LEVEL_TO_META.get(level, "ad")
        id_field = {"campaign": "campaign_id", "adset": "adset_id", "ad": "ad_id"}[
            meta_level
        ]
        params = {
            "level": meta_level,
            "time_increment": 1,
            "limit": 500,
            "time_range": json.dumps(
                {"since": since.isoformat(), "until": until.isoformat()}
            ),
            "fields": ",".join(
                [
                    id_field,
                    "impressions",
                    "clicks",
                    "spend",
                    "reach",
                    "frequency",
                    "actions",
                    "action_values",
                    "video_thruplay_watched_actions",
                    "date_start",
                ]
            ),
        }
        if external_ids:
            params["filtering"] = json.dumps(
                [{"field": f"{meta_level}.id", "operator": "IN", "value": external_ids}]
            )

        rows: list[InsightRow] = []
        path = f"act_{self.account_id}/insights"
        page_params: dict = params
        # Meta caps a report at a few hundred pages; the bound stops a malformed
        # cursor from looping forever.
        for _ in range(500):
            body = self._request("GET", path, params=page_params)
            for item in body.get("data", []):
                rows.append(self._to_insight_row(item, id_field))
            nxt = (body.get("paging") or {}).get("next")
            if not nxt:
                break
            # `next` is an absolute URL whose query carries the cursor *and* the
            # original filters. Its query has to be parsed out and passed on:
            # httpx replaces a URL's query string with `params` rather than
            # merging, so sending the path alone would silently re-request the
            # unfiltered first page forever.
            parsed = urlparse(nxt)
            path = parsed.path.split(f"/{self.api_version}/", 1)[-1].lstrip("/")
            page_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            page_params.pop("access_token", None)
        return rows

    def _to_insight_row(self, item: dict, id_field: str) -> InsightRow:
        conversions = 0.0
        conversion_value_micros = 0
        for action in item.get("actions", []) or []:
            if action.get("action_type") in (
                "purchase",
                "offsite_conversion.fb_pixel_purchase",
                "omni_purchase",
            ):
                conversions += float(action.get("value", 0) or 0)
        for value in item.get("action_values", []) or []:
            if value.get("action_type") in (
                "purchase",
                "offsite_conversion.fb_pixel_purchase",
                "omni_purchase",
            ):
                conversion_value_micros += int(float(value.get("value", 0) or 0) * 1e6)

        video_views = 0
        for entry in item.get("video_thruplay_watched_actions", []) or []:
            video_views += int(float(entry.get("value", 0) or 0))

        return InsightRow(
            external_id=str(item.get(id_field, "")),
            day=date.fromisoformat(item["date_start"]),
            impressions=int(item.get("impressions", 0) or 0),
            clicks=int(item.get("clicks", 0) or 0),
            # `spend` comes back as a decimal string in account currency.
            spend_micros=int(round(float(item.get("spend", 0) or 0) * 1_000_000)),
            conversions=conversions,
            conversion_value_micros=conversion_value_micros,
            frequency=float(item.get("frequency", 0) or 0),
            reach=int(item.get("reach", 0) or 0),
            video_views=video_views,
            raw=item,
        )

    # -- breakdowns ------------------------------------------------------
    # Meta's own names for each slice. `publisher_platform` with
    # `platform_position` is the pair that exposes Audience Network and Reels
    # separately, which is where affiliate campaigns most often bleed.
    BREAKDOWN_FIELDS = {
        "placement": ("publisher_platform", "platform_position"),
        "device": ("impression_device",),
        "age_gender": ("age", "gender"),
        "region": ("region",),
        "hour": ("hourly_stats_aggregated_by_advertiser_time_zone",),
    }

    def fetch_breakdowns(
        self,
        level: str,
        since: date,
        until: date,
        dimension: str,
        external_ids: list[str] | None = None,
    ) -> list[BreakdownRow]:
        fields = self.BREAKDOWN_FIELDS.get(dimension)
        if not fields:
            raise PlatformError(
                f"unsupported breakdown '{dimension}'; Meta offers "
                + ", ".join(sorted(self.BREAKDOWN_FIELDS)),
                platform=self.platform,
                code="UNSUPPORTED_BREAKDOWN",
            )

        meta_level = LEVEL_TO_META.get(level, "ad")
        id_field = {"campaign": "campaign_id", "adset": "adset_id", "ad": "ad_id"}[
            meta_level
        ]
        params = {
            "level": meta_level,
            "time_increment": 1,
            "limit": 500,
            "breakdowns": ",".join(fields),
            "time_range": json.dumps(
                {"since": since.isoformat(), "until": until.isoformat()}
            ),
            "fields": ",".join(
                [id_field, "impressions", "clicks", "spend", "actions", "date_start"]
            ),
        }
        if external_ids:
            params["filtering"] = json.dumps(
                [{"field": f"{meta_level}.id", "operator": "IN", "value": external_ids}]
            )

        rows: list[BreakdownRow] = []
        path = f"act_{self.account_id}/insights"
        page_params: dict = params
        for _ in range(500):
            body = self._request("GET", path, params=page_params)
            for item in body.get("data", []):
                rows.append(self._to_breakdown_row(item, id_field, dimension, fields))
            nxt = (body.get("paging") or {}).get("next")
            if not nxt:
                break
            parsed = urlparse(nxt)
            path = parsed.path.split(f"/{self.api_version}/", 1)[-1].lstrip("/")
            page_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            page_params.pop("access_token", None)
        return rows

    def _to_breakdown_row(
        self, item: dict, id_field: str, dimension: str, fields: tuple[str, ...]
    ) -> BreakdownRow:
        segment = ":".join(str(item.get(f, "")) for f in fields).strip(":") or "unknown"
        conversions = 0.0
        for action in item.get("actions", []) or []:
            if action.get("action_type") in (
                "purchase",
                "offsite_conversion.fb_pixel_purchase",
                "omni_purchase",
            ):
                conversions += float(action.get("value", 0) or 0)
        return BreakdownRow(
            external_id=str(item.get(id_field, "")),
            day=date.fromisoformat(item["date_start"]),
            dimension=dimension,
            segment=segment,
            impressions=int(item.get("impressions", 0) or 0),
            clicks=int(item.get("clicks", 0) or 0),
            spend_micros=int(round(float(item.get("spend", 0) or 0) * 1_000_000)),
            conversions=conversions,
            raw=item,
        )

    def apply_exclusion(
        self, level: str, external_id: str, dimension: str, segment: str
    ) -> None:
        """Drop one placement by pinning the ad set to the ones that remain.

        Meta has no "exclude this placement" call. The only way to stop serving
        somewhere is to list every placement that stays, which means the ad set
        must already have an explicit list. An ad set on automatic placements is
        refused rather than guessed at: Meta's automatic set changes over time,
        so enumerating it from a constant in this file would silently switch off
        placements nobody asked to lose.
        """
        if dimension != "placement":
            raise PlatformError(
                f"Meta exclusions are supported for placements only, not {dimension}. "
                "Age, gender and region are targeting changes that reset ad set "
                "learning and are left to a human.",
                platform=self.platform,
                code="UNSUPPORTED",
            )
        if level != "ad_group":
            raise PlatformError(
                "placement exclusions are set on the ad set",
                platform=self.platform,
                code="INVALID_LEVEL",
            )

        current = self._request(
            "GET", external_id, params={"fields": "targeting"}
        ).get("targeting", {})
        publishers = list(current.get("publisher_platforms") or [])
        if not publishers:
            raise PlatformError(
                "This ad set uses automatic placements. Excluding one means "
                "listing every placement that stays, and guessing that list "
                "would switch off placements you did not ask to lose. Set the "
                "ad set's placements explicitly first, then re-run this.",
                platform=self.platform,
                code="AUTOMATIC_PLACEMENTS",
            )

        publisher, _, position = segment.partition(":")
        targeting = dict(current)

        if not position:
            remaining = [p for p in publishers if p != publisher]
            if not remaining:
                raise PlatformError(
                    "excluding this would leave the ad set with no placements",
                    platform=self.platform,
                    code="EMPTY_TARGETING",
                )
            targeting["publisher_platforms"] = remaining
            # Positions for a platform no longer targeted are invalid.
            targeting.pop(f"{publisher}_positions", None)
        else:
            key = f"{publisher}_positions"
            positions = list(current.get(key) or [])
            if not positions:
                raise PlatformError(
                    f"The ad set does not list {publisher} positions explicitly, "
                    f"so {position} cannot be removed without enumerating them. "
                    "Set them explicitly first, or exclude the whole platform.",
                    platform=self.platform,
                    code="POSITIONS_NOT_ENUMERATED",
                )
            remaining = [p for p in positions if p != position]
            if not remaining:
                raise PlatformError(
                    f"excluding {segment} would leave {publisher} with no "
                    "positions; drop the platform instead",
                    platform=self.platform,
                    code="EMPTY_TARGETING",
                )
            targeting[key] = remaining

        # Automatic placements and an explicit list are mutually exclusive.
        targeting.pop("targeting_automation", None)
        self._request(
            "POST", external_id, data={"targeting": json.dumps(targeting)}
        )

    # -- conversions API -------------------------------------------------
    def upload_conversions(self, conversions: list[dict]) -> int:
        """Send network-confirmed sales back through the Conversions API.

        Each entry needs `event_time` (unix seconds), `value` (float),
        `currency`, and at least one identifier such as `fbclid`.
        """
        if not conversions:
            return 0
        pixel_id = self.settings.meta_pixel_id
        if not pixel_id:
            # Returning 0 here would let the caller mark these sales as sent and
            # filter them out for good, so a missing pixel has to be an error.
            raise PlatformError(
                "META_PIXEL_ID is required to upload conversions through the "
                "Conversions API",
                platform=self.platform,
                code="NO_PIXEL",
            )

        events = []
        for conv in conversions:
            user_data: dict = {}
            if conv.get("fbclid"):
                # Meta's required click-id format: fb.<subdomain>.<ts>.<fbclid>
                user_data["fbc"] = (
                    f"fb.1.{int(conv.get('click_time', conv['event_time']))}000."
                    f"{conv['fbclid']}"
                )
            if conv.get("email_sha256"):
                user_data["em"] = [conv["email_sha256"]]
            # `client_ip_address` must be the raw address; Meta hashes the
            # fields that need hashing itself. This platform deliberately never
            # stores a raw IP, so the field is omitted rather than filled with a
            # salted hash, which Meta would simply fail to match against.
            if not user_data:
                continue
            events.append(
                {
                    "event_name": conv.get("event_name", "Purchase"),
                    "event_time": int(conv["event_time"]),
                    "action_source": "website",
                    "event_id": conv.get("event_id"),
                    "user_data": user_data,
                    "custom_data": {
                        "value": round(float(conv.get("value", 0.0)), 2),
                        "currency": conv.get("currency", "USD"),
                    },
                }
            )
        if not events:
            return 0

        self._request(
            "POST",
            f"{pixel_id}/events",
            data={"data": json.dumps(events)},
        )
        return len(events)

    def health_check(self) -> dict:
        try:
            body = self._request(
                "GET",
                f"act_{self.account_id}",
                params={"fields": "name,account_status,currency,timezone_name"},
            )
        except PlatformError as exc:
            return {"platform": "meta", "ok": False, "error": str(exc)}
        return {
            "platform": "meta",
            "ok": body.get("account_status") == 1,
            "account": body.get("name"),
            "currency": body.get("currency"),
            "timezone": body.get("timezone_name"),
            "dry_run": self.dry_run,
        }


def _preview(payload: dict) -> str:
    return json.dumps(payload, default=str)[:240]
