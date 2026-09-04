"""Click tracking and conversion attribution.

Affiliate conversions fire on the advertiser's domain, where neither the Meta
pixel nor a Google tag can see them. So the platform's own reported conversion
count is not a trustworthy basis for spending money. This module builds the
independent measurement path:

    ad click -> /r/<token> (recorded here) -> offer page with a sub-id
             -> network postback to /postback (matched back here)

Every click gets an opaque click id that is passed to the network as its sub-id
and returned on the postback, which is what lets revenue be attributed down to
the individual creative rather than just the campaign.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import (
    AdGroup,
    Campaign,
    Click,
    Conversion,
    ConversionStatus,
    Creative,
    Offer,
    Platform,
)

__all__ = [
    "TrackingContext",
    "new_click_id",
    "encode_subid",
    "decode_subid",
    "build_tracking_url",
    "build_final_url",
    "sign_payload",
    "verify_signature",
    "record_click",
    "record_conversion",
    "attribution_window_ok",
]

# Platform macros the ad server expands at click time. These give a second,
# independent path back to the right entity if the sub-id is ever stripped.
PLATFORM_MACROS: dict[Platform, dict[str, str]] = {
    Platform.META: {
        "pc": "{{campaign.id}}",
        "pg": "{{adset.id}}",
        "pa": "{{ad.id}}",
        "ps": "{{placement}}",
        "psite": "{{site_source_name}}",
    },
    Platform.GOOGLE: {
        "pc": "{campaignid}",
        "pg": "{adgroupid}",
        "pa": "{creative}",
        "pk": "{keyword}",
        "pd": "{device}",
        "pm": "{matchtype}",
        "pn": "{network}",
    },
}

# Query parameter each platform appends with its own click identifier.
PLATFORM_CLICK_PARAM: dict[Platform, str] = {
    Platform.META: "fbclid",
    Platform.GOOGLE: "gclid",
}

DEFAULT_ATTRIBUTION_DAYS = 30

_BOT_MARKERS = (
    "bot",
    "crawler",
    "spider",
    "headlesschrome",
    "python-requests",
    "curl/",
    "wget",
    "facebookexternalhit",
    "adsbot",
    "lighthouse",
    "pingdom",
)


@dataclass(frozen=True)
class TrackingContext:
    offer_id: int
    campaign_id: int | None = None
    ad_group_id: int | None = None
    creative_id: int | None = None
    platform: Platform | None = None


def new_click_id() -> str:
    """22 characters of URL-safe randomness. Opaque and non-enumerable."""
    return secrets.token_urlsafe(16)[:22]


# --------------------------------------------------------------------------
# sub-id encoding
# --------------------------------------------------------------------------


_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_PLATFORM_FLAGS = {"m": Platform.META, "g": Platform.GOOGLE}


def _to_b36(value: int) -> str:
    if value <= 0:
        return "0"
    out = ""
    while value:
        value, digit = divmod(value, 36)
        out = _B36[digit] + out
    return out


def _from_b36(text: str) -> int:
    try:
        return int(text, 36)
    except ValueError:
        return 0


def encode_subid(ctx: TrackingContext) -> str:
    """Pack the entity ids into a compact token.

    Networks truncate sub-ids hard: ClickBank's TID field is 24 characters, and
    a token that gets cut in half attributes revenue to nothing. So ids are
    base-36 encoded, and only the offer and the creative are carried. The
    campaign and ad group are derivable from the creative, and are included
    only when there is no creative to derive them from.
    """
    parts = [f"o{_to_b36(ctx.offer_id)}"]
    if ctx.creative_id:
        parts.append(f"a{_to_b36(ctx.creative_id)}")
    else:
        if ctx.campaign_id:
            parts.append(f"c{_to_b36(ctx.campaign_id)}")
        if ctx.ad_group_id:
            parts.append(f"g{_to_b36(ctx.ad_group_id)}")
    if ctx.platform:
        parts.append(f"p{ctx.platform.value[0]}")
    # Base-36 digits include the segment letters, so the segments need an
    # explicit separator to stay unambiguous.
    return "-".join(parts)


def decode_subid(token: str) -> TrackingContext:
    """Inverse of `encode_subid`. Unparseable segments are skipped, not fatal."""
    fields: dict[str, int] = {}
    platform: Platform | None = None
    for segment in (token or "").split("-"):
        if len(segment) < 2:
            continue
        key, value = segment[0], segment[1:]
        if key == "p":
            platform = _PLATFORM_FLAGS.get(value)
        elif key in "ocga" and re.fullmatch(r"[0-9a-z]+", value):
            fields[key] = _from_b36(value)
    return TrackingContext(
        offer_id=fields.get("o", 0),
        campaign_id=fields.get("c"),
        ad_group_id=fields.get("g"),
        creative_id=fields.get("a"),
        platform=platform,
    )


# --------------------------------------------------------------------------
# URL construction
# --------------------------------------------------------------------------


def build_tracking_url(
    ctx: TrackingContext,
    settings: Settings | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    """The URL that goes in the ad's final-URL field.

    It points at this platform, not at the offer, so the click is recorded
    before the redirect. Platform macros are appended unencoded because the ad
    server has to recognise and expand them.
    """
    settings = settings or get_settings()
    base = settings.public_base_url.rstrip("/")
    params: dict[str, str] = {"s": encode_subid(ctx)}
    params.update(extra or {})
    query = urlencode(params)
    if ctx.platform:
        macros = PLATFORM_MACROS.get(ctx.platform, {})
        if macros:
            query += "&" + "&".join(f"{k}={v}" for k, v in macros.items())
    return f"{base}/r?{query}"


def build_final_url(
    destination_url: str,
    click_id: str,
    subid_param: str = "subid",
    extra: dict[str, str] | None = None,
) -> str:
    """Append the click id to the advertiser's URL, preserving existing query."""
    parsed = urlparse(destination_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[subid_param] = click_id
    if extra:
        query.update({k: v for k, v in extra.items() if v})
    return urlunparse(parsed._replace(query=urlencode(query)))


# --------------------------------------------------------------------------
# postback security
# --------------------------------------------------------------------------


def sign_payload(payload: dict, secret: str | None = None, ttl: int = 300) -> str:
    """Create a short-lived signed token. Used for outbound callbacks."""
    secret = secret or get_settings().postback_secret
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{encoded}.{sig}"


def verify_signature(
    token: str, secret: str | None = None
) -> dict | None:
    """Constant-time verification. Returns the payload, or None if invalid."""
    secret = secret or get_settings().postback_secret
    if not token or "." not in token:
        return None
    encoded, _, sig = token.rpartition(".")
    expected = hmac.new(
        secret.encode(), encoded.encode("utf-8", "surrogateescape"), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(
        sig.encode("utf-8", "surrogateescape"), expected.encode()
    ):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    if payload.get("exp", 0) < int(time.time()):
        return None
    return payload


# Values shipped in the example configuration. A deployment still using one is
# not configured, and treating it as valid would let anyone post conversions.
PLACEHOLDER_SECRETS = frozenset(
    {
        "change-me-postback",
        "change-me-to-something-random",
        "change-me",
        "changeme",
        "secret",
        "",
    }
)


def secret_is_placeholder(secret: str | None) -> bool:
    return (secret or "").strip().lower() in PLACEHOLDER_SECRETS


def verify_postback_secret(provided: str | None, settings: Settings | None = None) -> bool:
    """Compare a shared postback secret without leaking timing information.

    Compared as bytes: `compare_digest` raises TypeError on a non-ASCII str,
    which would turn a malformed query parameter into a 500 rather than a 401.
    """
    settings = settings or get_settings()
    if not provided:
        return False
    if secret_is_placeholder(settings.postback_secret):
        # Revenue posted here is what the optimizer spends against, so an
        # unconfigured secret has to reject everything rather than accept
        # whatever the example file happens to say.
        return False
    return hmac.compare_digest(
        str(provided).encode("utf-8", "surrogateescape"),
        settings.postback_secret.encode("utf-8"),
    )


# --------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------


def looks_like_bot(user_agent: str | None) -> bool:
    ua = (user_agent or "").lower()
    if not ua:
        return True
    return any(marker in ua for marker in _BOT_MARKERS)


def hash_ip(ip: str | None, salt: str | None = None) -> str | None:
    """Store a salted hash rather than the address itself."""
    if not ip:
        return None
    salt = salt or get_settings().secret_key
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:32]


def record_click(
    session: Session,
    subid: str,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
    referrer: str | None = None,
    country: str | None = None,
    query_params: dict[str, str] | None = None,
) -> tuple[Click, Offer | None]:
    """Persist a click and return it with the offer to redirect to."""
    ctx = decode_subid(subid)
    query_params = query_params or {}

    platform = ctx.platform
    platform_click_id = None
    for plat, param in PLATFORM_CLICK_PARAM.items():
        if query_params.get(param):
            platform_click_id = query_params[param]
            platform = platform or plat
            break

    # The macro-expanded ids are a fallback identity if the sub-id was mangled.
    creative_id = ctx.creative_id
    campaign_id = ctx.campaign_id
    ad_group_id = ctx.ad_group_id
    if creative_id is None and query_params.get("pa"):
        # No format check: the value is only ever compared against ids this
        # platform stored itself, so an unexpanded macro or junk simply fails
        # to match. Requiring digits here would reject Google's composite ids.
        platform_ad_id = query_params["pa"]
        # Meta stores the ad id verbatim; Google stores "{adGroupId}~{adId}",
        # so an exact match alone would never resolve a Google click.
        match = session.execute(
            select(Creative).where(
                or_(
                    Creative.external_id == platform_ad_id,
                    Creative.external_id.like(f"%~{platform_ad_id}"),
                )
            )
        ).scalars().first()
        if match:
            creative_id = match.id

    # The sub-id carries only the creative, so walk up to fill in its parents.
    if creative_id and not (campaign_id and ad_group_id):
        creative = session.get(Creative, creative_id)
        if creative is not None:
            ad_group_id = ad_group_id or creative.ad_group_id
            group = session.get(AdGroup, creative.ad_group_id)
            if group is not None:
                campaign_id = campaign_id or group.campaign_id

    click = Click(
        click_id=new_click_id(),
        offer_id=ctx.offer_id or None,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        creative_id=creative_id,
        platform=platform,
        platform_click_id=platform_click_id,
        user_agent=user_agent,
        ip_hash=hash_ip(ip),
        country=country,
        referrer=referrer,
        is_bot=looks_like_bot(user_agent),
    )
    session.add(click)
    session.flush()

    offer = session.get(Offer, ctx.offer_id) if ctx.offer_id else None
    return click, offer


def attribution_window_ok(
    click: Click, occurred_at: datetime, days: int = DEFAULT_ATTRIBUTION_DAYS
) -> bool:
    created = click.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return created <= occurred_at + timedelta(minutes=5) <= created + timedelta(days=days)


def record_conversion(
    session: Session,
    *,
    network: str,
    network_txn_id: str,
    click_id: str | None = None,
    subid: str | None = None,
    revenue_micros: int = 0,
    sale_amount_micros: int = 0,
    status: ConversionStatus = ConversionStatus.PENDING,
    event_name: str = "sale",
    occurred_at: datetime | None = None,
    raw: dict | None = None,
    attribution_days: int = DEFAULT_ATTRIBUTION_DAYS,
) -> tuple[Conversion, str]:
    """Attribute a network postback back to the creative that earned it.

    Returns the conversion and a short string describing how it was matched, so
    the share of unattributed revenue is visible rather than silently absorbed.
    """
    occurred_at = occurred_at or datetime.now(timezone.utc)
    raw = raw or {}

    existing = session.execute(
        select(Conversion).where(
            Conversion.network == network,
            Conversion.network_txn_id == network_txn_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Networks re-send postbacks on refunds and status upgrades. A pending
        # sale is often posted with no commission and only carries its real
        # value once approved, so the amounts have to be updated alongside the
        # status. Leaving revenue at the pending figure would count an approved
        # conversion as zero and get a profitable creative killed.
        changed = existing.status is not status
        if revenue_micros and revenue_micros != existing.revenue_micros:
            existing.revenue_micros = revenue_micros
            changed = True
        if sale_amount_micros and sale_amount_micros != existing.sale_amount_micros:
            existing.sale_amount_micros = sale_amount_micros
            changed = True
        if status is ConversionStatus.REVERSED:
            # A reversal takes the money back rather than restating it.
            existing.revenue_micros = 0
            changed = True
        if changed:
            existing.status = status
            existing.raw = {**(existing.raw or {}), "last_update": raw}
            session.flush()
            return existing, "updated"
        return existing, "duplicate"

    click: Click | None = None
    method = "unmatched"
    expired = False
    if click_id:
        click = session.execute(
            select(Click).where(Click.click_id == click_id)
        ).scalar_one_or_none()
        if click is not None:
            if attribution_window_ok(click, occurred_at, attribution_days):
                method = "click_id"
            else:
                # The click is known and too old to credit. Falling through to
                # the sub-id would credit the same creative anyway and quietly
                # undo the window.
                method = "click_id_expired"
                expired = True
                click = None

    ctx = decode_subid(subid) if subid else None
    if click is None and not expired and ctx is not None and ctx.offer_id:
        method = "subid"

    conversion = Conversion(
        click_id=click.click_id if click else click_id,
        offer_id=(
            click.offer_id if click else (ctx.offer_id if ctx and not expired else None)
        )
        or None,
        campaign_id=(
            click.campaign_id if click else (ctx.campaign_id if ctx and not expired else None)
        ),
        ad_group_id=(
            click.ad_group_id if click else (ctx.ad_group_id if ctx and not expired else None)
        ),
        creative_id=(
            click.creative_id if click else (ctx.creative_id if ctx and not expired else None)
        ),
        network=network,
        network_txn_id=network_txn_id,
        revenue_micros=revenue_micros,
        sale_amount_micros=sale_amount_micros,
        status=status,
        event_name=event_name,
        occurred_at=occurred_at,
        raw={**raw, "attribution_method": method},
    )
    session.add(conversion)
    session.flush()
    return conversion, method


def tracking_url_for_creative(
    session: Session, creative: Creative, settings: Settings | None = None
) -> str:
    """Build the final URL for a stored creative."""
    ad_group = session.get(AdGroup, creative.ad_group_id)
    campaign = session.get(Campaign, ad_group.campaign_id) if ad_group else None
    ctx = TrackingContext(
        offer_id=campaign.offer_id if campaign else 0,
        campaign_id=campaign.id if campaign else None,
        ad_group_id=ad_group.id if ad_group else None,
        creative_id=creative.id,
        platform=campaign.platform if campaign else None,
    )
    return build_tracking_url(ctx, settings=settings)
