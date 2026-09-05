"""Auditing the page the ad sends people to.

Both platforms review the destination, not just the ad. An ad can be written
perfectly and still take the account down because the page behind it collects
card details over plain HTTP, has no privacy policy, or quietly serves
different content to Meta's crawler than to a human.

That last one is the reason this module fetches twice. Cloaking is an instant
and usually permanent ban, and an affiliate can be cloaked without knowing it:
the network controls the page and may be doing it on their own initiative. So
the page is requested once as an ordinary browser and once as each platform's
crawler, and the two are compared. Finding out from this tool is survivable;
finding out from an enforcement email is not.

Nothing here is a substitute for reading your own landing pages. It is a
pre-flight check that catches the failures a human skims past, and a monitor
for the ones that appear later when the network swaps the page under you.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from ..models import ComplianceVerdict, Platform
from .compliance import ComplianceEngine, Finding, Severity

logger = logging.getLogger(__name__)

__all__ = [
    "PageSnapshot",
    "LandingPageAudit",
    "LandingPageFetcher",
    "audit_landing_page",
    "CRAWLER_AGENTS",
    "BROWSER_AGENT",
]

BROWSER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# The user agents each platform reviews with. A page that behaves differently
# for these than for a human is cloaking.
CRAWLER_AGENTS: dict[str, str] = {
    "meta": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "google": "AdsBot-Google (+http://www.google.com/adsbot.html)",
}

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 10

# Pages differing by more than this share of their visible text are serving
# materially different content. Some variation is normal: timestamps, rotating
# testimonials, a session id in the markup.
CLOAKING_DIVERGENCE = 0.30

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class PageSnapshot:
    """One fetch of a landing page."""

    url: str
    final_url: str = ""
    status_code: int = 0
    fetched_as: str = "browser"
    html: str = ""
    text: str = ""
    title: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    link_texts: list[str] = field(default_factory=list)
    forms: list[dict] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    has_viewport: bool = False
    has_meta_refresh: bool = False
    bytes: int = 0
    error: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 300

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", "ignore")).hexdigest()

    @property
    def is_https(self) -> bool:
        return (self.final_url or self.url).lower().startswith("https://")

    def word_set(self) -> set[str]:
        return set(re.findall(r"[a-z0-9']{3,}", self.text.lower()))


@dataclass
class LandingPageAudit:
    url: str
    findings: list[Finding] = field(default_factory=list)
    verdict: ComplianceVerdict = ComplianceVerdict.UNREVIEWED
    score: float = 100.0
    snapshot: PageSnapshot | None = None
    crawler_snapshots: dict[str, PageSnapshot] = field(default_factory=dict)
    content_hash: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCK]

    @property
    def passed(self) -> bool:
        return self.verdict is not ComplianceVerdict.BLOCK

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.snapshot.final_url if self.snapshot else None,
            "status_code": self.snapshot.status_code if self.snapshot else None,
            "verdict": self.verdict.value,
            "score": self.score,
            "content_hash": self.content_hash,
            "redirect_hops": (
                len(self.snapshot.redirect_chain) if self.snapshot else 0
            ),
            "checked_at": self.checked_at.isoformat(),
            "findings": [f.as_dict() for f in self.findings],
        }


class LandingPageFetcher:
    """Fetches a page and records how it got there."""

    def __init__(self, client: httpx.Client | None = None, timeout: float = 20.0) -> None:
        # Redirects are followed by hand so the chain can be recorded: the
        # number of hops and whether any of them leave HTTPS both matter.
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=False
        )

    def fetch(self, url: str, user_agent: str = BROWSER_AGENT, label: str = "browser") -> PageSnapshot:
        snapshot = PageSnapshot(url=url, fetched_as=label)
        current = url
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }

        for _ in range(MAX_REDIRECTS):
            try:
                response = self._client.get(current, headers=headers)
            except httpx.HTTPError as exc:
                snapshot.error = f"could not reach the page: {exc}"
                snapshot.final_url = current
                return snapshot

            snapshot.status_code = response.status_code
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    break
                current = urljoin(current, location)
                snapshot.redirect_chain.append(current)
                continue
            break
        else:
            snapshot.error = f"more than {MAX_REDIRECTS} redirects"
            snapshot.final_url = current
            return snapshot

        snapshot.final_url = current
        body = response.content[:MAX_BYTES]
        snapshot.bytes = len(response.content)
        html = body.decode(response.encoding or "utf-8", "ignore")
        _populate(snapshot, html)
        return snapshot


def _populate(snapshot: PageSnapshot, html: str) -> None:
    """Pull the structure out of the markup.

    A real parser would be better, but a landing page audit should not force a
    parsing dependency on a project that otherwise needs none, and the checks
    below only need coarse structure.
    """
    snapshot.html = html
    stripped = _SCRIPT_STYLE_RE.sub(" ", html)
    snapshot.text = _WHITESPACE_RE.sub(" ", _TAG_RE.sub(" ", stripped)).strip()

    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    snapshot.title = _WHITESPACE_RE.sub(" ", title.group(1)).strip() if title else ""

    for anchor in re.finditer(
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL
    ):
        snapshot.links.append(anchor.group(1))
        snapshot.link_texts.append(
            _WHITESPACE_RE.sub(" ", _TAG_RE.sub(" ", anchor.group(2))).strip()
        )
    # Anchors without closing tags still count as links.
    for anchor in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        if anchor.group(1) not in snapshot.links:
            snapshot.links.append(anchor.group(1))
    snapshot.scripts = [
        m.group(1)
        for m in re.finditer(r'<script\b[^>]*src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    ]
    snapshot.has_viewport = bool(
        re.search(r'<meta\b[^>]*name=["\']viewport["\']', html, re.IGNORECASE)
    )
    snapshot.has_meta_refresh = bool(
        re.search(
            r'<meta\b[^>]*http-equiv=["\']refresh["\']', html, re.IGNORECASE
        )
    )

    for form in re.finditer(r"<form\b[^>]*>(.*?)</form>", html, re.IGNORECASE | re.DOTALL):
        attrs = form.group(0)[: form.group(0).find(">") + 1]
        action = re.search(r'action=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
        inputs = [
            (m.group(1) or "").lower()
            for m in re.finditer(
                r'<input\b[^>]*(?:name|type)=["\']([^"\']*)["\']', form.group(1), re.IGNORECASE
            )
        ]
        snapshot.forms.append(
            {"action": action.group(1) if action else "", "inputs": inputs}
        )


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

# Links a compliant landing page is expected to carry. Meta requires a privacy
# policy for any page collecting data; Google requires it far more broadly.
_POLICY_LINK_PATTERNS = {
    "privacy": r"privacy",
    "terms": r"terms|conditions|tos\b",
    "contact": r"contact|support|about",
}

_DISCLOSURE_RE = re.compile(
    r"\b(affiliate|commission|paid link|sponsored|advertorial|#ad|"
    r"we may (earn|receive)|material connection)\b",
    re.IGNORECASE,
)

_SENSITIVE_INPUTS = ("card", "cardnumber", "cc-number", "cvv", "cvc", "ssn", "password")

# Markers of a page trying to force the visitor's hand rather than persuade.
_DARK_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        r"<audio\b[^>]*autoplay|<video\b[^>]*autoplay(?![^>]*muted)",
        "AUTOPLAY_MEDIA",
        "Audio that plays on load is a rejection reason and drives people off.",
    ),
    (
        r"onbeforeunload\s*=|history\.pushState\([^)]*\);\s*window\.onpopstate",
        "BACK_BUTTON_TRAP",
        "Interfering with the back button is prohibited on both platforms.",
    ),
    (
        r"\b(setInterval|setTimeout)\b[^;]{0,80}\bcountdown\b|"
        r"\bcountdown\b[^;]{0,60}\blocalStorage\b",
        "RESETTING_COUNTDOWN",
        "A countdown that restarts for each visitor is a false scarcity claim.",
    ),
    (
        r"\b(as seen on|featured in)\b[^<]{0,60}\b(cnn|fox|nbc|abc|forbes|shark tank)\b",
        "UNSUBSTANTIATED_ENDORSEMENT",
        "Press logos need a real citation; both platforms treat this as "
        "misrepresentation.",
    ),
)


def _link_findings(snapshot: PageSnapshot) -> list[Finding]:
    out: list[Finding] = []
    # Only the links themselves, never the body text. Searching the whole page
    # means one mention of "our support team" silently satisfies the check for
    # a contact link that is not there.
    haystack = (
        " ".join(snapshot.links).lower() + " " + " ".join(snapshot.link_texts).lower()
    )
    for name, pattern in _POLICY_LINK_PATTERNS.items():
        if re.search(pattern, haystack):
            continue
        severity = Severity.BLOCK if name == "privacy" else Severity.WARN
        out.append(
            Finding(
                code=f"MISSING_{name.upper()}_LINK",
                severity=severity,
                message=f"No {name} link found on the landing page.",
                policy_ref=(
                    "Meta: Landing Page Requirements / Google: Editorial and "
                    "Destination Requirements"
                ),
                field_name="landing_page",
                suggestion=(
                    f"Add a visible {name} link. Both platforms check for one, and "
                    "a missing privacy policy is a common rejection."
                    if name == "privacy"
                    else f"Add a {name} link; its absence reads as a throwaway page."
                ),
            )
        )
    return out


def _security_findings(snapshot: PageSnapshot) -> list[Finding]:
    out: list[Finding] = []
    collects_sensitive = any(
        any(marker in value for marker in _SENSITIVE_INPUTS)
        for form in snapshot.forms
        for value in form["inputs"]
    )
    if not snapshot.is_https:
        out.append(
            Finding(
                code="NOT_HTTPS",
                severity=Severity.BLOCK if collects_sensitive else Severity.WARN,
                message="The landing page is served over plain HTTP.",
                policy_ref="Meta & Google: Destination Requirements",
                field_name="landing_page",
                matched_text=snapshot.final_url[:120],
                suggestion=(
                    "Serve the page over HTTPS. Collecting personal or payment "
                    "details without it is a certain rejection."
                ),
            )
        )
    for form in snapshot.forms:
        action = form["action"]
        if action.lower().startswith("http://"):
            out.append(
                Finding(
                    code="INSECURE_FORM_ACTION",
                    severity=Severity.BLOCK,
                    message="A form on the page submits over plain HTTP.",
                    policy_ref="Meta & Google: Destination Requirements",
                    field_name="landing_page",
                    matched_text=action[:120],
                    suggestion="Point the form at an HTTPS endpoint.",
                )
            )
    return out


def _redirect_findings(snapshot: PageSnapshot) -> list[Finding]:
    out: list[Finding] = []
    hops = len(snapshot.redirect_chain)
    if hops >= 3:
        out.append(
            Finding(
                code="LONG_REDIRECT_CHAIN",
                severity=Severity.WARN,
                message=f"{hops} redirects between the ad and the page.",
                policy_ref="Google: Destination Requirements",
                field_name="landing_page",
                matched_text=" -> ".join(snapshot.redirect_chain[:4])[:200],
                suggestion=(
                    "Each hop loses visitors and slows the page. Long chains also "
                    "read as cloaking infrastructure to a reviewer."
                ),
            )
        )
    if any(u.lower().startswith("http://") for u in snapshot.redirect_chain):
        out.append(
            Finding(
                code="INSECURE_REDIRECT_HOP",
                severity=Severity.WARN,
                message="A redirect passes through plain HTTP before arriving.",
                policy_ref="Meta & Google: Destination Requirements",
                field_name="landing_page",
                suggestion="Keep every hop on HTTPS.",
            )
        )
    if snapshot.has_meta_refresh:
        out.append(
            Finding(
                code="META_REFRESH_REDIRECT",
                severity=Severity.WARN,
                message="The page redirects with a meta refresh tag.",
                policy_ref="Google: Misleading Ad Design",
                field_name="landing_page",
                suggestion=(
                    "Redirect server-side. A client-side hop is a common cloaking "
                    "signature and reviewers treat it as one."
                ),
            )
        )
    return out


def _quality_findings(snapshot: PageSnapshot) -> list[Finding]:
    out: list[Finding] = []
    if not snapshot.has_viewport:
        out.append(
            Finding(
                code="NOT_MOBILE_READY",
                severity=Severity.WARN,
                message="No viewport meta tag; the page is unlikely to be mobile-ready.",
                policy_ref="Meta & Google: Landing Page Experience",
                field_name="landing_page",
                suggestion=(
                    "Most of this traffic is on a phone. A desktop-only page wastes "
                    "the clicks you paid for."
                ),
            )
        )
    words = len(snapshot.text.split())
    if words < 120:
        out.append(
            Finding(
                code="THIN_CONTENT",
                severity=Severity.WARN,
                message=f"Only about {words} words of visible text.",
                policy_ref="Google: Insufficient Original Content",
                field_name="landing_page",
                suggestion=(
                    "A page that is mostly a button reads as a bridge page, which "
                    "Google rejects outright."
                ),
            )
        )
    if not _DISCLOSURE_RE.search(snapshot.text):
        out.append(
            Finding(
                code="NO_AFFILIATE_DISCLOSURE_ON_PAGE",
                severity=Severity.WARN,
                message="No affiliate or paid-promotion disclosure on the page.",
                policy_ref="FTC Endorsement Guides, 16 CFR Part 255",
                field_name="landing_page",
                suggestion=(
                    "Disclose the material connection above the fold. The ad "
                    "disclosure alone does not cover the destination."
                ),
            )
        )
    for pattern, code, fix in _DARK_PATTERNS:
        match = re.search(pattern, snapshot.html, re.IGNORECASE)
        if match:
            out.append(
                Finding(
                    code=code,
                    severity=Severity.BLOCK if code != "AUTOPLAY_MEDIA" else Severity.WARN,
                    message=fix.split(".")[0] + ".",
                    policy_ref="Meta: Deceptive Content / Google: Misleading Ad Design",
                    field_name="landing_page",
                    matched_text=match.group(0)[:100],
                    suggestion=fix,
                )
            )
    return out


def _availability_findings(snapshot: PageSnapshot) -> list[Finding]:
    if snapshot.error:
        return [
            Finding(
                code="PAGE_UNREACHABLE",
                severity=Severity.BLOCK,
                message=f"The landing page could not be fetched: {snapshot.error}",
                policy_ref="Meta & Google: Destination Requirements",
                field_name="landing_page",
                suggestion=(
                    "Traffic is being paid for and sent nowhere. Fix the page or "
                    "pause the campaign."
                ),
            )
        ]
    if snapshot.status_code >= 400:
        return [
            Finding(
                code="PAGE_ERROR_STATUS",
                severity=Severity.BLOCK,
                message=f"The landing page returned HTTP {snapshot.status_code}.",
                policy_ref="Meta & Google: Destination Requirements",
                field_name="landing_page",
                matched_text=snapshot.final_url[:120],
                suggestion="Every click is being paid for and wasted until this is fixed.",
            )
        ]
    return []


def _cloaking_findings(
    browser: PageSnapshot, crawlers: dict[str, PageSnapshot]
) -> list[Finding]:
    """Compare what a human sees with what each platform's reviewer sees."""
    out: list[Finding] = []
    baseline = browser.word_set()
    if not baseline:
        return out

    for platform, crawler in crawlers.items():
        if not crawler.ok:
            if browser.ok:
                out.append(
                    Finding(
                        code="CRAWLER_BLOCKED",
                        severity=Severity.BLOCK,
                        message=(
                            f"The page serves an error to {platform}'s crawler while "
                            "loading normally for a browser."
                        ),
                        policy_ref="Meta & Google: Cloaking",
                        field_name="landing_page",
                        matched_text=str(crawler.status_code or crawler.error)[:120],
                        suggestion=(
                            "A reviewer cannot see the page, which is treated as "
                            "cloaking. Allow the platform crawlers through."
                        ),
                    )
                )
            continue

        other = crawler.word_set()
        overlap = len(baseline & other) / max(1, len(baseline | other))
        divergence = 1.0 - overlap
        if divergence > CLOAKING_DIVERGENCE:
            out.append(
                Finding(
                    code="POSSIBLE_CLOAKING",
                    severity=Severity.BLOCK,
                    message=(
                        f"The page shows {divergence:.0%} different content to "
                        f"{platform}'s crawler than to a browser."
                    ),
                    policy_ref="Meta & Google: Cloaking",
                    field_name="landing_page",
                    suggestion=(
                        "Serving reviewers different content is grounds for a "
                        "permanent ban. If the network controls this page, they may "
                        "be doing it without telling you; confirm before spending."
                    ),
                )
            )
        if crawler.final_url != browser.final_url:
            out.append(
                Finding(
                    code="CRAWLER_REDIRECTED_ELSEWHERE",
                    severity=Severity.BLOCK,
                    message=(
                        f"{platform}'s crawler is redirected to a different final "
                        "URL than a browser."
                    ),
                    policy_ref="Meta & Google: Cloaking",
                    field_name="landing_page",
                    matched_text=f"{browser.final_url} vs {crawler.final_url}"[:200],
                    suggestion="This is the clearest possible cloaking signal.",
                )
            )
    return out


def _consistency_findings(snapshot: PageSnapshot, ad_texts: list[str]) -> list[Finding]:
    """Does the page deliver what the ad promised?

    Google rejects destination mismatch outright, and it is also the most
    common reason a technically compliant ad converts badly.
    """
    joined = " ".join(t for t in ad_texts if t)
    if not joined or not snapshot.text:
        return []

    stop = {
        "the", "and", "for", "you", "your", "with", "that", "this", "from",
        "have", "our", "get", "now", "are", "was", "can", "all", "out", "new",
        "more", "here", "how", "why", "what", "when", "who", "will", "just",
    }
    ad_words = {w for w in re.findall(r"[a-z']{4,}", joined.lower()) if w not in stop}
    if len(ad_words) < 5:
        return []

    page_words = snapshot.word_set()
    overlap = len(ad_words & page_words) / len(ad_words)
    if overlap >= 0.25:
        return []

    return [
        Finding(
            code="AD_PAGE_MISMATCH",
            severity=Severity.WARN,
            message=(
                f"Only {overlap:.0%} of the ad's distinctive wording appears on the "
                "page."
            ),
            policy_ref="Google: Destination Requirements / Meta: Deceptive Content",
            field_name="landing_page",
            suggestion=(
                "The page should visibly deliver what the ad promised. A mismatch "
                "is a rejection reason and, short of that, the most common cause of "
                "clicks that never convert."
            ),
        )
    ]


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def audit_landing_page(
    url: str,
    *,
    fetcher: LandingPageFetcher | None = None,
    ad_texts: list[str] | None = None,
    platforms: tuple[Platform, ...] = (Platform.META, Platform.GOOGLE),
    check_cloaking: bool = True,
    offer=None,
    text_engine: ComplianceEngine | None = None,
) -> LandingPageAudit:
    """Fetch the destination and check what the platforms will check."""
    fetcher = fetcher or LandingPageFetcher()
    audit = LandingPageAudit(url=url)

    browser = fetcher.fetch(url, BROWSER_AGENT, "browser")
    audit.snapshot = browser
    audit.content_hash = browser.content_hash

    findings = _availability_findings(browser)
    if findings:
        # Nothing else can be assessed on a page that did not load.
        audit.findings = findings
        audit.verdict = ComplianceVerdict.BLOCK
        audit.score = 0.0
        return audit

    findings += _link_findings(browser)
    findings += _security_findings(browser)
    findings += _redirect_findings(browser)
    findings += _quality_findings(browser)
    if ad_texts:
        findings += _consistency_findings(browser, ad_texts)

    # The page's own text is held to the same claim rules as the ad. A "lose 30
    # pounds guaranteed" promise is no safer for being one click further away.
    engine = text_engine or ComplianceEngine()
    for platform in platforms:
        report = engine.review(
            {"primary_texts": [browser.text[:8000]]},
            platform=platform,
            offer=offer,
            requires_disclosure=False,
        )
        for finding in report.findings:
            if finding.code in {"TOO_FEW_ASSETS", "OVER_CHAR_LIMIT", "SOFT_TRUNCATION"}:
                continue  # ad-format rules, meaningless for a web page
            findings.append(
                Finding(
                    code=f"PAGE_{finding.code}",
                    severity=finding.severity,
                    message=f"On the landing page: {finding.message}",
                    policy_ref=finding.policy_ref,
                    field_name="landing_page",
                    matched_text=finding.matched_text,
                    suggestion=finding.suggestion,
                )
            )

    if check_cloaking:
        crawlers: dict[str, PageSnapshot] = {}
        for platform in platforms:
            agent = CRAWLER_AGENTS.get(platform.value)
            if agent:
                crawlers[platform.value] = fetcher.fetch(url, agent, platform.value)
        audit.crawler_snapshots = crawlers
        findings += _cloaking_findings(browser, crawlers)

    # The message is part of the key: the same code can describe genuinely
    # different problems, such as cloaking detected against two platforms, and
    # keying on the code alone would silently report only the first.
    seen: set[tuple[str, str, str]] = set()
    deduped: list[Finding] = []
    for finding in findings:
        key = (finding.code, finding.matched_text.lower(), finding.message.lower())
        if key not in seen:
            seen.add(key)
            deduped.append(finding)

    penalty = {Severity.BLOCK: 25.0, Severity.WARN: 6.0, Severity.INFO: 1.0}
    audit.score = max(0.0, round(100.0 - sum(penalty[f.severity] for f in deduped), 1))
    deduped.sort(key=lambda f: (-f.severity.rank, f.code))
    audit.findings = deduped

    if any(f.severity is Severity.BLOCK for f in deduped):
        audit.verdict = ComplianceVerdict.BLOCK
    elif any(f.severity is Severity.WARN for f in deduped):
        audit.verdict = ComplianceVerdict.WARN
    else:
        audit.verdict = ComplianceVerdict.PASS

    logger.info(
        "Landing page audit %s: %s (score %s, %s findings)",
        url,
        audit.verdict.value,
        audit.score,
        len(deduped),
    )
    return audit
