"""Google Ads API adapter.

Maps the AdGenie model onto Google's object graph:

    Campaign -> Ad Group -> Responsive Search Ad (+ keywords)

Google speaks REST over `googleads.googleapis.com` with an OAuth2 refresh-token
flow and a developer token. Money is already in micros, which is why micros is
the unit used everywhere in this codebase.

Two things make Google materially different from Meta for affiliate traffic.
Keywords are the targeting, so ad groups carry keyword criteria rather than
interest sets. And conversions are uploaded against a click identifier (gclid)
into a named conversion action, which is how an off-site affiliate sale gets
back into Smart Bidding.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from ..config import Settings, get_settings
from ..models import Platform
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

ADS_BASE = "https://googleads.googleapis.com"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Google Ads channel/status enums used below, spelled out so the mapping is
# obvious at the call site.
ADVERTISING_CHANNEL = {
    "search": "SEARCH",
    "display": "DISPLAY",
    "demand_gen": "DEMAND_GEN",
    "performance_max": "PERFORMANCE_MAX",
}

MATCH_TYPES = {"broad": "BROAD", "phrase": "PHRASE", "exact": "EXACT"}

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GoogleAdsClient(AdPlatform):
    platform = Platform.GOOGLE

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        dry_run: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.has_google and client is None:
            raise PlatformError(
                "Google Ads credentials are not configured (set "
                "GOOGLE_DEVELOPER_TOKEN, GOOGLE_REFRESH_TOKEN and GOOGLE_CUSTOMER_ID).",
                platform=self.platform,
                code="NO_CREDENTIALS",
            )
        self.api_version = self.settings.google_api_version
        self.customer_id = (self.settings.google_customer_id or "").replace("-", "")
        self.dry_run = self.settings.dry_run if dry_run is None else dry_run
        self._client = client or httpx.Client(timeout=60.0)
        self._access_token: str | None = None
        self._token_expiry: datetime = datetime.now(timezone.utc)
        self.calls: list[tuple[str, dict]] = []

    # -- auth ------------------------------------------------------------
    def _token(self) -> str:
        if self._access_token and datetime.now(timezone.utc) < self._token_expiry:
            return self._access_token
        response = self._client.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "refresh_token": self.settings.google_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if response.status_code >= 400:
            raise PlatformError(
                f"Google OAuth refresh failed ({response.status_code}): "
                f"{response.text[:300]}",
                platform=self.platform,
                code=response.status_code,
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        body = response.json()
        self._access_token = body["access_token"]
        self._token_expiry = datetime.now(timezone.utc) + timedelta(
            seconds=int(body.get("expires_in", 3600)) - 60
        )
        return self._access_token

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "developer-token": self.settings.google_developer_token or "",
            "Content-Type": "application/json",
        }
        if self.settings.google_login_customer_id:
            headers["login-customer-id"] = self.settings.google_login_customer_id.replace(
                "-", ""
            )
        return headers

    # -- transport -------------------------------------------------------
    def _post(self, path: str, body: dict, *, mutating: bool = True) -> dict:
        if mutating and self.dry_run:
            self.calls.append((f"DRY POST {path}", body))
            logger.info("[dry-run] google POST %s", path)
            return _fake_mutate_response(path, body)

        url = f"{ADS_BASE}/{self.api_version}/{path.lstrip('/')}"
        last: PlatformError | None = None
        for attempt in range(4):
            try:
                response = self._client.post(url, json=body, headers=self._headers())
            except httpx.HTTPError as exc:
                last = PlatformError(
                    f"network error calling Google Ads: {exc}",
                    platform=self.platform,
                    retryable=True,
                )
            else:
                if response.status_code < 400:
                    self.calls.append((f"POST {path}", body))
                    return response.json() if response.content else {}
                last = self._to_error(response)
            if not last.retryable or attempt == 3:
                raise last
            time.sleep(2**attempt)
        raise last  # pragma: no cover

    def _to_error(self, response: httpx.Response) -> PlatformError:
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": response.text[:500]}}
        message = body.get("error", {}).get("message", response.text[:300])
        details = []
        for failure in body.get("error", {}).get("details", []) or []:
            for err in failure.get("errors", []) or []:
                details.append(err.get("message", ""))
        if details:
            message = f"{message} | {' | '.join(details[:3])}"
        return PlatformError(
            f"Google Ads error {response.status_code}: {message}",
            platform=self.platform,
            code=response.status_code,
            retryable=response.status_code in RETRYABLE_STATUS,
            payload=body if isinstance(body, dict) else {},
        )

    def _resource(self, name: str) -> str:
        return f"customers/{self.customer_id}/{name}"

    # -- creation --------------------------------------------------------
    def create_campaign(self, spec: CampaignSpec) -> str:
        # A campaign needs its own budget resource first.
        budget_result = self._post(
            f"customers/{self.customer_id}/campaignBudgets:mutate",
            {
                "operations": [
                    {
                        "create": {
                            "name": f"{spec.name} budget {int(time.time())}",
                            "amountMicros": str(spec.daily_budget_micros),
                            "deliveryMethod": "STANDARD",
                            "explicitlyShared": False,
                        }
                    }
                ]
            },
        )
        budget_resource = budget_result["results"][0]["resourceName"]

        campaign: dict = {
            "name": spec.name,
            "status": "ENABLED" if spec.status.upper() == "ACTIVE" else "PAUSED",
            "advertisingChannelType": ADVERTISING_CHANNEL.get(
                spec.extra.get("channel", "search"), "SEARCH"
            ),
            "campaignBudget": budget_resource,
            "networkSettings": {
                "targetGoogleSearch": True,
                "targetSearchNetwork": spec.extra.get("search_partners", False),
                "targetContentNetwork": False,
                "targetPartnerSearchNetwork": False,
            },
        }
        # Bid strategy. Target ROAS needs conversion history, so a new campaign
        # starts on maximize clicks and is migrated once data exists.
        if spec.bid_strategy == "TARGET_ROAS" and spec.target_roas:
            campaign["maximizeConversionValue"] = {
                "targetRoas": round(spec.target_roas, 2)
            }
        elif spec.bid_strategy == "TARGET_CPA" and spec.extra.get("target_cpa_micros"):
            campaign["maximizeConversions"] = {
                "targetCpaMicros": str(spec.extra["target_cpa_micros"])
            }
        else:
            campaign["manualCpc"] = {"enhancedCpcEnabled": True}

        if spec.extra.get("start_date"):
            campaign["startDate"] = spec.extra["start_date"]
        if spec.extra.get("geo_target_constants"):
            campaign["geoTargetTypeSetting"] = {
                "positiveGeoTargetType": "PRESENCE",
                "negativeGeoTargetType": "PRESENCE",
            }

        result = self._post(
            f"customers/{self.customer_id}/campaigns:mutate",
            {"operations": [{"create": campaign}]},
        )
        return _id_from_resource(result["results"][0]["resourceName"])

    def create_ad_group(self, spec: AdGroupSpec) -> str:
        ad_group: dict = {
            "name": spec.name,
            "campaign": self._resource(f"campaigns/{spec.campaign_external_id}"),
            "status": "ENABLED" if spec.status.upper() == "ACTIVE" else "PAUSED",
            "type": spec.extra.get("ad_group_type", "SEARCH_STANDARD"),
        }
        if spec.bid_micros:
            ad_group["cpcBidMicros"] = str(spec.bid_micros)

        result = self._post(
            f"customers/{self.customer_id}/adGroups:mutate",
            {"operations": [{"create": ad_group}]},
        )
        ad_group_id = _id_from_resource(result["results"][0]["resourceName"])

        if spec.keywords or spec.negative_keywords:
            self._add_keywords(ad_group_id, spec)
        return ad_group_id

    def _add_keywords(self, ad_group_id: str, spec: AdGroupSpec) -> None:
        resource = self._resource(f"adGroups/{ad_group_id}")
        match_type = MATCH_TYPES.get(
            str(spec.extra.get("match_type", "phrase")).lower(), "PHRASE"
        )
        operations = [
            {
                "create": {
                    "adGroup": resource,
                    "status": "ENABLED",
                    "keyword": {"text": keyword, "matchType": match_type},
                }
            }
            for keyword in spec.keywords
        ]
        operations += [
            {
                "create": {
                    "adGroup": resource,
                    "negative": True,
                    "keyword": {"text": keyword, "matchType": "BROAD"},
                }
            }
            for keyword in spec.negative_keywords
        ]
        if operations:
            self._post(
                f"customers/{self.customer_id}/adGroupCriteria:mutate",
                {"operations": operations},
            )

    def create_creative(self, spec: CreativeSpec) -> str:
        if len(spec.headlines) < 3:
            raise PlatformError(
                f"a responsive search ad needs at least 3 headlines, got "
                f"{len(spec.headlines)}",
                platform=self.platform,
                code="TOO_FEW_HEADLINES",
            )
        if len(spec.descriptions) < 2:
            raise PlatformError(
                f"a responsive search ad needs at least 2 descriptions, got "
                f"{len(spec.descriptions)}",
                platform=self.platform,
                code="TOO_FEW_DESCRIPTIONS",
            )

        ad: dict = {
            "finalUrls": [spec.final_url],
            "responsiveSearchAd": {
                "headlines": [{"text": h} for h in spec.headlines[:15]],
                "descriptions": [{"text": d} for d in spec.descriptions[:4]],
            },
        }
        if spec.display_url_path:
            ad["responsiveSearchAd"]["path1"] = spec.display_url_path[0]
            if len(spec.display_url_path) > 1:
                ad["responsiveSearchAd"]["path2"] = spec.display_url_path[1]
        if spec.extra.get("tracking_template"):
            ad["trackingUrlTemplate"] = spec.extra["tracking_template"]

        result = self._post(
            f"customers/{self.customer_id}/adGroupAds:mutate",
            {
                "operations": [
                    {
                        "create": {
                            "adGroup": self._resource(
                                f"adGroups/{spec.ad_group_external_id}"
                            ),
                            "status": "ENABLED"
                            if spec.status.upper() == "ACTIVE"
                            else "PAUSED",
                            "ad": ad,
                        }
                    }
                ]
            },
        )
        # An ad group ad's resource name is `.../adGroupAds/{adGroupId}~{adId}`.
        return _id_from_resource(result["results"][0]["resourceName"])

    # -- mutation --------------------------------------------------------
    def set_status(self, level: str, external_id: str, active: bool) -> None:
        status = "ENABLED" if active else "PAUSED"
        if level == "campaign":
            self._post(
                f"customers/{self.customer_id}/campaigns:mutate",
                {
                    "operations": [
                        {
                            "update": {
                                "resourceName": self._resource(
                                    f"campaigns/{external_id}"
                                ),
                                "status": status,
                            },
                            "updateMask": "status",
                        }
                    ]
                },
            )
        elif level == "ad_group":
            self._post(
                f"customers/{self.customer_id}/adGroups:mutate",
                {
                    "operations": [
                        {
                            "update": {
                                "resourceName": self._resource(
                                    f"adGroups/{external_id}"
                                ),
                                "status": status,
                            },
                            "updateMask": "status",
                        }
                    ]
                },
            )
        else:
            if "~" not in external_id:
                raise PlatformError(
                    "a Google ad id must be '{adGroupId}~{adId}'; "
                    f"got '{external_id}'",
                    platform=self.platform,
                    code="INVALID_AD_ID",
                )
            self._post(
                f"customers/{self.customer_id}/adGroupAds:mutate",
                {
                    "operations": [
                        {
                            "update": {
                                "resourceName": self._resource(
                                    f"adGroupAds/{external_id}"
                                ),
                                "status": status,
                            },
                            "updateMask": "status",
                        }
                    ]
                },
            )

    def set_budget(self, level: str, external_id: str, daily_budget_micros: int) -> None:
        """Budgets live on the campaign in Google, never on the ad group."""
        if level != "campaign":
            raise PlatformError(
                "Google Ads budgets are set at the campaign level, not on ad groups.",
                platform=self.platform,
                code="INVALID_LEVEL",
            )
        if daily_budget_micros <= 0:
            raise PlatformError(
                "daily budget must be positive",
                platform=self.platform,
                code="INVALID_BUDGET",
            )
        budget_resource = self._campaign_budget_resource(external_id)
        self._post(
            f"customers/{self.customer_id}/campaignBudgets:mutate",
            {
                "operations": [
                    {
                        "update": {
                            "resourceName": budget_resource,
                            "amountMicros": str(daily_budget_micros),
                        },
                        "updateMask": "amount_micros",
                    }
                ]
            },
        )

    def _campaign_budget_resource(self, campaign_id: str) -> str:
        if self.dry_run:
            return self._resource(f"campaignBudgets/dryrun_{campaign_id}")
        rows = self.search(
            "SELECT campaign.campaign_budget FROM campaign "
            f"WHERE campaign.id = {int(campaign_id)}"
        )
        if not rows:
            raise PlatformError(
                f"campaign {campaign_id} not found",
                platform=self.platform,
                code="NOT_FOUND",
            )
        return rows[0]["campaign"]["campaignBudget"]

    def set_bid(self, level: str, external_id: str, bid_micros: int) -> None:
        if level != "ad_group":
            raise PlatformError(
                "CPC bids are set on ad groups.",
                platform=self.platform,
                code="INVALID_LEVEL",
            )
        self._post(
            f"customers/{self.customer_id}/adGroups:mutate",
            {
                "operations": [
                    {
                        "update": {
                            "resourceName": self._resource(f"adGroups/{external_id}"),
                            "cpcBidMicros": str(bid_micros),
                        },
                        "updateMask": "cpc_bid_micros",
                    }
                ]
            },
        )

    # -- measurement -----------------------------------------------------
    def search(self, query: str) -> list[dict]:
        """Run a GAQL query through searchStream."""
        body = self._post(
            f"customers/{self.customer_id}/googleAds:searchStream",
            {"query": query},
            mutating=False,
        )
        # searchStream returns an array of chunks, each with `results`.
        if isinstance(body, list):
            return [row for chunk in body for row in chunk.get("results", [])]
        return body.get("results", [])

    def fetch_insights(
        self, level: str, since: date, until: date, external_ids: list[str] | None = None
    ) -> list[InsightRow]:
        resource, id_field = {
            "campaign": ("campaign", "campaign.id"),
            "ad_group": ("ad_group", "ad_group.id"),
            "creative": ("ad_group_ad", "ad_group_ad.ad.id"),
        }.get(level, ("ad_group_ad", "ad_group_ad.ad.id"))

        fields = [
            id_field,
            "segments.date",
            "metrics.impressions",
            "metrics.clicks",
            "metrics.cost_micros",
            "metrics.conversions",
            "metrics.conversions_value",
            "metrics.video_views",
        ]
        # An ad group ad is identified by "{adGroupId}~{adId}", which is what
        # `create_creative` stores. The ad id alone would never match it, so
        # the ad group id has to come back too and be recombined below.
        if level == "creative":
            fields.insert(1, "ad_group.id")

        where = [
            f"segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}'"
        ]
        numeric_ids = [
            part for part in (e.split("~")[-1] for e in external_ids or []) if part.isdigit()
        ]
        if numeric_ids:
            where.append(f"{id_field} IN ({', '.join(numeric_ids)})")

        query = (
            f"SELECT {', '.join(fields)} FROM {resource} "
            f"WHERE {' AND '.join(where)}"
        )
        rows = self.search(query)

        out: list[InsightRow] = []
        for row in rows:
            metrics = row.get("metrics", {})
            external_id = _nested_id(row, id_field)
            if level == "creative":
                ad_group_id = _nested_id(row, "ad_group.id")
                if ad_group_id:
                    external_id = f"{ad_group_id}~{external_id}"
            out.append(
                InsightRow(
                    external_id=external_id,
                    day=date.fromisoformat(row["segments"]["date"]),
                    impressions=int(metrics.get("impressions", 0) or 0),
                    clicks=int(metrics.get("clicks", 0) or 0),
                    spend_micros=int(metrics.get("costMicros", 0) or 0),
                    conversions=float(metrics.get("conversions", 0) or 0),
                    conversion_value_micros=int(
                        float(metrics.get("conversionsValue", 0) or 0) * 1_000_000
                    ),
                    video_views=int(metrics.get("videoViews", 0) or 0),
                    raw=row,
                )
            )
        return out

    # -- breakdowns ------------------------------------------------------
    # GAQL segments. Google has no placement concept on search, so the
    # equivalent lever is network and device.
    BREAKDOWN_SEGMENTS = {
        "placement": ("segments.ad_network_type",),
        "device": ("segments.device",),
        "hour": ("segments.hour",),
        "region": ("segments.geo_target_region",),
    }

    def fetch_breakdowns(
        self,
        level: str,
        since: date,
        until: date,
        dimension: str,
        external_ids: list[str] | None = None,
    ) -> list[BreakdownRow]:
        segments = self.BREAKDOWN_SEGMENTS.get(dimension)
        if not segments:
            raise PlatformError(
                f"unsupported breakdown '{dimension}'; Google offers "
                + ", ".join(sorted(self.BREAKDOWN_SEGMENTS)),
                platform=self.platform,
                code="UNSUPPORTED_BREAKDOWN",
            )

        resource, id_field = {
            "campaign": ("campaign", "campaign.id"),
            "ad_group": ("ad_group", "ad_group.id"),
            "creative": ("ad_group_ad", "ad_group_ad.ad.id"),
        }.get(level, ("ad_group", "ad_group.id"))

        fields = [
            id_field, "segments.date", *segments,
            "metrics.impressions", "metrics.clicks",
            "metrics.cost_micros", "metrics.conversions",
        ]
        if level == "creative":
            fields.insert(1, "ad_group.id")

        where = [f"segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}'"]
        numeric = [
            part for part in (e.split("~")[-1] for e in external_ids or [])
            if part.isdigit()
        ]
        if numeric:
            where.append(f"{id_field} IN ({', '.join(numeric)})")

        rows = self.search(
            f"SELECT {', '.join(fields)} FROM {resource} WHERE {' AND '.join(where)}"
        )

        out: list[BreakdownRow] = []
        for row in rows:
            metrics = row.get("metrics", {})
            external_id = _nested_id(row, id_field)
            if level == "creative":
                group_id = _nested_id(row, "ad_group.id")
                if group_id:
                    external_id = f"{group_id}~{external_id}"
            segment = ":".join(
                str(_nested_id(row, s) or "") for s in segments
            ).strip(":") or "unknown"
            out.append(
                BreakdownRow(
                    external_id=external_id,
                    day=date.fromisoformat(row["segments"]["date"]),
                    dimension=dimension,
                    segment=segment,
                    impressions=int(metrics.get("impressions", 0) or 0),
                    clicks=int(metrics.get("clicks", 0) or 0),
                    spend_micros=int(metrics.get("costMicros", 0) or 0),
                    conversions=float(metrics.get("conversions", 0) or 0),
                    raw=row,
                )
            )
        return out

    def apply_exclusion(
        self, level: str, external_id: str, dimension: str, segment: str
    ) -> None:
        """Apply a negative criterion.

        Google's device lever is a bid modifier rather than an on/off switch, so
        excluding a device means bidding it to zero.
        """
        if dimension != "device" or level != "ad_group":
            raise PlatformError(
                f"Google exclusions here cover devices on ad groups, not "
                f"{dimension} at {level} level.",
                platform=self.platform,
                code="UNSUPPORTED",
            )
        device = segment.upper().split(":")[0]
        self._post(
            f"customers/{self.customer_id}/adGroupCriteria:mutate",
            {
                "operations": [
                    {
                        "create": {
                            "adGroup": self._resource(f"adGroups/{external_id}"),
                            "device": {"type": device},
                            # -100% is Google's way of saying "never serve here".
                            "bidModifier": 0.0,
                        }
                    }
                ]
            },
        )

    # -- offline conversions ---------------------------------------------
    def upload_conversions(self, conversions: list[dict]) -> int:
        """Upload network-confirmed sales against their gclid.

        Each entry needs `gclid`, `event_time` (unix seconds), `value` and the
        `conversion_action` resource name or id.
        """
        if not conversions:
            return 0
        action_id = self.settings.google_conversion_action_id
        if not action_id and not any(c.get("conversion_action") for c in conversions):
            raise PlatformError(
                "GOOGLE_CONVERSION_ACTION_ID is required to upload offline "
                "conversions",
                platform=self.platform,
                code="NO_CONVERSION_ACTION",
            )
        operations = []
        for conv in conversions:
            gclid = conv.get("gclid")
            if not gclid:
                continue
            action = conv.get("conversion_action") or action_id
            if not action:
                continue
            occurred = datetime.fromtimestamp(
                int(conv["event_time"]), tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S+00:00")
            operations.append(
                {
                    "gclid": gclid,
                    "conversionAction": self._resource(f"conversionActions/{action}")
                    if str(action).isdigit()
                    else action,
                    "conversionDateTime": occurred,
                    "conversionValue": round(float(conv.get("value", 0.0)), 4),
                    "currencyCode": conv.get("currency", "USD"),
                    "orderId": conv.get("order_id"),
                }
            )
        if not operations:
            return 0

        self._post(
            f"customers/{self.customer_id}:uploadClickConversions",
            {"conversions": operations, "partialFailure": True},
        )
        return len(operations)

    def health_check(self) -> dict:
        try:
            rows = self.search(
                "SELECT customer.id, customer.descriptive_name, "
                "customer.currency_code, customer.time_zone FROM customer LIMIT 1"
            )
        except PlatformError as exc:
            return {"platform": "google", "ok": False, "error": str(exc)}
        customer = rows[0]["customer"] if rows else {}
        return {
            "platform": "google",
            "ok": bool(rows),
            "account": customer.get("descriptiveName"),
            "currency": customer.get("currencyCode"),
            "timezone": customer.get("timeZone"),
            "dry_run": self.dry_run,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _id_from_resource(resource_name: str) -> str:
    """`customers/123/campaigns/456` -> `456`."""
    return resource_name.rstrip("/").rsplit("/", 1)[-1]


def _nested_id(row: dict, field_path: str) -> str:
    """Resolve `ad_group_ad.ad.id` against the camelCase JSON response."""
    node: object = row
    for part in field_path.split("."):
        camel = part.split("_")[0] + "".join(w.title() for w in part.split("_")[1:])
        if isinstance(node, dict):
            node = node.get(camel, node.get(part, {}))
        else:
            return ""
    return str(node) if node not in ({}, None) else ""


def _fake_mutate_response(path: str, body: dict) -> dict:
    """Shape-compatible response so dry-run exercises the same parsing code.

    Ad group ads keep their "{adGroupId}~{adId}" shape, because code
    downstream depends on it and a dry run that produces a differently-shaped
    id would hide exactly the bugs a dry run exists to surface.
    """
    count = len(body.get("operations", [])) or 1
    collection = path.split("/")[-1].split(":")[0]
    suffix = "0~{i}" if collection == "adGroupAds" else "dryrun{i}"
    return {
        "results": [
            {"resourceName": f"customers/0/{collection}/" + suffix.format(i=i + 1)}
            for i in range(count)
        ]
    }
