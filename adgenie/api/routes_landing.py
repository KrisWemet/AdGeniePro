"""Public, crawler-consistent pre-landing pages for affiliate offers."""

from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ..config import get_settings
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
:root{color-scheme:light;--navy:#071c2c;--ink:#102838;--muted:#567080;--aqua:#4fd1c5;--blue:#0786b5;--foam:#eefbfc;--orange:#f4a340}
*{box-sizing:border-box}body{margin:0;font-family:Inter,Arial,sans-serif;color:var(--ink);line-height:1.5;background:#fff}
main{max-width:1080px;margin:auto;padding:22px 24px 44px}.top{display:flex;justify-content:space-between;align-items:center;font-weight:800;color:var(--navy)}
.badge{font-size:.77rem;letter-spacing:.12em;color:var(--blue)}.hero{display:grid;grid-template-columns:1.25fr .75fr;gap:48px;align-items:center;padding:68px 0 48px}
h1{font-size:clamp(2.65rem,7vw,5.25rem);line-height:.98;letter-spacing:-.045em;margin:.18em 0;color:var(--navy)}h2{font-size:1.55rem}
.lead{font-size:1.25rem;max-width:620px;color:var(--muted);margin:20px 0 26px}.button{display:inline-block;background:var(--orange);color:#16222a;text-decoration:none;font-weight:900;padding:16px 24px;border-radius:10px;box-shadow:0 9px 24px #d98a283d}
.micro{display:block;color:var(--muted);font-size:.82rem;margin-top:10px}.visual{height:360px;border-radius:42% 58% 60% 40%/45% 38% 62% 55%;background:radial-gradient(circle at 38% 30%,#dfffff 0 8%,#62d9d1 9% 26%,#0786b5 58%,#063454 100%);box-shadow:0 28px 70px #0786b540;position:relative;overflow:hidden}
.visual:after{content:"";position:absolute;left:-10%;right:-10%;bottom:12%;height:28%;background:#fff5;border-radius:50% 50% 0 0;transform:rotate(-8deg)}
.disclosure{font-size:.78rem;color:var(--muted);border-top:1px solid #d7e8eb;border-bottom:1px solid #d7e8eb;padding:12px 0}
.why{text-align:center;max-width:710px;margin:54px auto 30px}.why p{font-size:1.08rem;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.card{background:var(--foam);border-radius:16px;padding:24px}.num{font-size:.78rem;font-weight:900;color:var(--blue);letter-spacing:.1em}
.final{text-align:center;background:var(--navy);color:white;border-radius:22px;padding:38px 24px;margin-top:38px}.final h2{margin-top:0}.final p{color:#c5d7df}
footer{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-top:28px;color:var(--muted);font-size:.8rem}footer a{color:var(--blue);margin-right:16px}
@media(max-width:760px){.hero{grid-template-columns:1fr;padding:44px 0 34px}.visual{height:230px;order:-1}.grid{grid-template-columns:1fr}.button{width:100%;text-align:center}footer{display:block}.top span:last-child{display:none}}
"""

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Explore a practical digital guide focused on household water preparedness.">
<title>Build a More Water-Ready Home</title>
<style>__STYLE__</style>
</head>
<body><main>
<header class="top"><span>WATER READY</span><span class="badge">HOUSEHOLD PREPAREDNESS</span></header>
<section class="hero">
<div>
<p class="badge">A PRACTICAL DIGITAL GUIDE</p>
<h1>Build a more water-ready home.</h1>
<p class="lead">Water is easy to take for granted. Explore a practical approach to household water preparedness and see whether <strong>__OFFER_NAME__</strong> fits your plans.</p>
<a class="button" rel="sponsored nofollow" href="__CTA_URL__">Explore Water Freedom System</a>
<span class="micro">See the full presentation, product details and current checkout information.</span>
</div>
<div class="visual" role="img" aria-label="Abstract water illustration"></div>
</section>
<div class="disclosure"><strong>Affiliate disclosure:</strong> We may earn a commission if you purchase through this link, at no additional cost to you.</div>
<section class="why">
<p class="badge">WHY EXPLORE IT?</p>
<h2>A simple starting point for thinking ahead</h2>
<p>Household water planning can feel complicated. This guide offers a focused place to explore the subject, understand the approach being presented, and decide what deserves a closer look.</p>
</section>
<section class="grid">
<div class="card"><span class="num">01</span><h3>Explore the idea</h3><p>Learn how the Water Freedom approach is presented and what the digital guide includes.</p></div>
<div class="card"><span class="num">02</span><h3>Compare it to your needs</h3><p>Review the information against your home, priorities, local conditions and existing plans.</p></div>
<div class="card"><span class="num">03</span><h3>Make your own decision</h3><p>Check the current offer, support information and purchase terms before choosing.</p></div>
</section>
<section class="final">
<h2>Ready to take a closer look?</h2>
<p>Continue to the complete Water Freedom System presentation.</p>
<a class="button" rel="sponsored nofollow" href="__CTA_URL__">See the Water Freedom System</a>
</section>
<footer>
<div><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/contact">Contact</a></div>
<span>Information only. Product details and checkout are provided on the next page.</span>
</footer>
</main></body></html>"""

def _token_for(offer_id: int, supplied: str | None) -> str:
    context = decode_subid(supplied or "")
    if supplied and context.offer_id == offer_id:
        return supplied
    return encode_subid(TrackingContext(offer_id=offer_id))


def _merge_query(url: str, params: dict[str, str]) -> str:
    """Add attribution without dropping the provider page query."""
    from urllib.parse import parse_qsl, urlsplit, urlunsplit

    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode(query), parsed.fragment))


@router.get("/offer/{offer_id}", response_class=HTMLResponse, include_in_schema=False)
def offer_landing(
    offer_id: int,
    request: Request,
    s: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> Response:
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
    settings = get_settings()
    if settings.systeme_capture_url:
        return RedirectResponse(
            _merge_query(settings.systeme_capture_url, params),
            status_code=302,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
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
        """<p>The offer page provides general information about a digital product. Product descriptions, prices, availability, delivery, support, and refund terms may change.</p>
<p>Review the seller's current terms before purchasing. Information on this site is not professional engineering, safety, health, financial, or legal advice.</p>""",
    )


@router.get("/contact", response_class=HTMLResponse, include_in_schema=False)
def contact() -> HTMLResponse:
    return _support_page(
        "Contact",
        """<p>Questions about the advertised product, order, download, or refund should be directed to the seller or ClickBank using the contact details shown on the checkout receipt.</p>
<p>Technical questions about this disclosure and tracking service can be submitted through the <a href="https://github.com/KrisWemet/AdGeniePro/issues">AdGenie Pro project contact page</a>.</p>""",
    )
