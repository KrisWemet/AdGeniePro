"""Public, crawler-consistent pre-landing pages for affiliate offers."""

from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..core.tracking import (
    PLATFORM_CLICK_PARAM,
    PLATFORM_MACROS,
    TrackingContext,
    decode_subid,
    encode_subid,
)
from ..db import get_session
from ..models import Offer

router = APIRouter(tags=["landing"])

_PASSTHROUGH = set(PLATFORM_CLICK_PARAM.values()) | {
    key for fields in PLATFORM_MACROS.values() for key in fields
}

_STYLE = """
:root{color-scheme:light;--ink:#172033;--muted:#526079;--blue:#2257d6;--paper:#f5f8ff}
*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;color:var(--ink);line-height:1.58;background:#fff}
main{max-width:800px;margin:auto;padding:32px 22px 64px}.eyebrow{font-weight:700;color:var(--blue);letter-spacing:.05em}
h1{font-size:clamp(2rem,6vw,3.5rem);line-height:1.08;margin:.25em 0}.lead{font-size:1.15rem;color:var(--muted)}
.disclosure{background:var(--paper);border-left:5px solid var(--blue);padding:15px 18px;margin:24px 0}
.card{border:1px solid #d8dfec;border-radius:14px;padding:22px;margin:22px 0}.button{display:inline-block;background:var(--blue);color:#fff;text-decoration:none;font-weight:700;padding:14px 22px;border-radius:9px}
small,footer{color:var(--muted)}footer{border-top:1px solid #d8dfec;margin-top:36px;padding-top:20px}
footer a{color:var(--blue);margin-right:18px}@media(max-width:520px){main{padding-top:22px}.button{width:100%;text-align:center}}
"""

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="An independent overview of a third-party digital guide about household water preparedness.">
<title>Household Water Preparedness Guide | AdGenie Pro</title>
<style>__STYLE__</style>
</head>
<body><main>
<p class="eyebrow">INDEPENDENT PRODUCT OVERVIEW</p>
<h1>Explore a guide to household water preparedness</h1>
<p class="lead">This page introduces <strong>__OFFER_NAME__</strong>, a third-party digital guide. It is intended for adults comparing information about household water systems and practical preparedness topics.</p>
<div class="disclosure"><strong>Affiliate disclosure:</strong> We may earn a commission if you purchase through the link on this page, at no additional cost to you. That relationship does not change the price you pay.</div>
<section class="card">
<h2>What you are reviewing</h2>
<p>This is a digital information product offered by an independent seller through ClickBank. It is not a physical water-treatment device, installation service, emergency service, or professional assessment. The seller controls the product, checkout, pricing, delivery, support, and refund terms.</p>
<p>Before purchasing, read the seller's description carefully. Check what files or materials are included, how access is delivered, which devices can open them, and whether the subject matter fits what you are actually trying to learn. Keep a copy of the checkout receipt and the terms shown at purchase.</p>
</section>
<section>
<h2>A practical evaluation checklist</h2>
<ul>
<li>Confirm that the product is a digital guide and understand what is included.</li>
<li>Review the current price, billing currency, and any optional items at checkout.</li>
<li>Read the seller's refund and customer-support information before paying.</li>
<li>Treat the material as general information, not professional engineering, safety, health, or legal advice.</li>
<li>For household water equipment or safety decisions, consult an appropriately qualified local professional.</li>
</ul>
<p>No outcome is promised by this page. Individual needs, properties, local conditions, equipment, and regulations differ. Make decisions based on your own circumstances and independently verified information.</p>
</section>
<section class="card">
<h2>Continue to the seller</h2>
<p>The button below records an outbound affiliate click so AdGenie Pro can connect any ClickBank notification back to the campaign that produced it. It then sends you through the original ClickBank HopLink supplied for this offer.</p>
<a class="button" rel="sponsored nofollow" href="__CTA_URL__">Review the seller's product details</a>
<p><small>You will leave this site. The destination and checkout are operated by third parties.</small></p>
</section>
<footer>
<a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/contact">Contact</a>
<p>AdGenie Pro provides this independent disclosure page and campaign measurement. It is not the product seller or ClickBank.</p>
</footer>
</main></body></html>"""

def _token_for(offer_id: int, supplied: str | None) -> str:
    context = decode_subid(supplied or "")
    if supplied and context.offer_id == offer_id:
        return supplied
    return encode_subid(TrackingContext(offer_id=offer_id))


@router.get("/offer/{offer_id}", response_class=HTMLResponse, include_in_schema=False)
def offer_landing(
    offer_id: int,
    request: Request,
    s: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Show the same substantive disclosure page to visitors and reviewers."""
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise HTTPException(404, "offer not found")

    params = {"s": _token_for(offer_id, s)}
    params.update(
        (key, value)
        for key, value in request.query_params.items()
        if key in _PASSTHROUGH and value
    )
    cta = "/r?" + urlencode(params)
    html = (
        _PAGE.replace("__STYLE__", _STYLE)
        .replace("__OFFER_NAME__", escape(offer.name))
        .replace("__CTA_URL__", escape(cta, quote=True))
    )
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        },
    )


def _support_page(title: str, body: str) -> HTMLResponse:
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} | AdGenie Pro</title><style>{_STYLE}</style></head>
<body><main><p class="eyebrow">ADGENIE PRO</p><h1>{escape(title)}</h1>
{body}<p><a href="/">Return to AdGenie Pro</a></p></main></body></html>"""
    return HTMLResponse(html, headers={"X-Content-Type-Options": "nosniff"})


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy() -> HTMLResponse:
    return _support_page(
        "Privacy",
        """<p>The offer page does not collect names, email addresses, or payment details. When a visitor chooses to continue, AdGenie Pro records campaign attribution, a random click identifier, request metadata, and a salted hash of the network address. It does not store the original network address.</p>
<p>ClickBank and the product seller operate the destination and checkout under their own privacy terms. Do not submit sensitive information unless you have reviewed those terms.</p>""",
    )


@router.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def terms() -> HTMLResponse:
    return _support_page(
        "Terms",
        """<p>The offer page is an independent informational introduction to a third-party product. Product descriptions, prices, availability, delivery, support, and refund terms are controlled by the seller and may change.</p>
<p>Review the seller's current terms before purchasing. Information on this site is not professional engineering, safety, health, financial, or legal advice.</p>""",
    )


@router.get("/contact", response_class=HTMLResponse, include_in_schema=False)
def contact() -> HTMLResponse:
    return _support_page(
        "Contact",
        """<p>Questions about the advertised product, order, download, or refund should be directed to the seller or ClickBank using the contact details shown on the checkout receipt.</p>
<p>Technical questions about this disclosure and tracking service can be submitted through the <a href="https://github.com/KrisWemet/AdGeniePro/issues">AdGenie Pro project contact page</a>.</p>""",
    )
