"""Ad policy compliance engine for Meta and Google.

Affiliate marketing dies of account bans, not of bad copy. A rejected ad costs
a few hours; a disabled ad account costs the business. So every creative passes
through this engine before it is ever sent to a platform, and a BLOCK verdict
stops the launch rather than producing a warning nobody reads.

The rules encode the published policies of both platforms plus the FTC's
endorsement guidance for affiliate links. They are deliberately conservative:
a false positive costs one regeneration, a false negative costs the account.

This is an automated pre-screen and a forcing function for better copy. It is
not legal advice and does not replace each platform's own review.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Callable, Iterable, Sequence

from ..models import ComplianceVerdict, Platform
from ..platforms.specs import get_spec

__all__ = [
    "Severity",
    "Finding",
    "ComplianceReport",
    "ComplianceEngine",
    "review_texts",
]


class Severity(str, Enum):
    BLOCK = "block"  # will very likely be rejected or risk the account
    WARN = "warn"  # allowed, but weakens the ad or invites review
    INFO = "info"  # style guidance

    @property
    def rank(self) -> int:
        return {"info": 0, "warn": 1, "block": 2}[self.value]


@dataclass
class Finding:
    code: str
    severity: Severity
    message: str
    policy_ref: str
    field_name: str = ""
    matched_text: str = ""
    suggestion: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class ComplianceReport:
    verdict: ComplianceVerdict
    findings: list[Finding] = field(default_factory=list)
    score: float = 100.0
    platform: str = ""

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCK]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]

    @property
    def passed(self) -> bool:
        return self.verdict in (ComplianceVerdict.PASS, ComplianceVerdict.WARN)

    def rewrite_instructions(self) -> list[str]:
        """Actionable notes to feed back into the copywriter on regeneration."""
        out: list[str] = []
        for f in self.findings:
            if f.severity is Severity.INFO:
                continue
            note = f.suggestion or f.message
            if f.matched_text:
                note = f'{note} (triggered by: "{f.matched_text}")'
            if note not in out:
                out.append(note)
        return out

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "score": self.score,
            "platform": self.platform,
            "findings": [f.as_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------
# rule definitions
# --------------------------------------------------------------------------


@dataclass
class Rule:
    code: str
    severity: Severity
    pattern: str
    message: str
    policy_ref: str
    suggestion: str = ""
    platforms: tuple[Platform, ...] = (Platform.META, Platform.GOOGLE)
    flags: int = re.IGNORECASE
    # Optional second pattern that, when it also matches, cancels the finding.
    exempt_pattern: str | None = None

    def compiled(self) -> re.Pattern:
        return re.compile(self.pattern, self.flags)


# Personal attributes. Meta's most-enforced and least-understood rule: copy may
# not assert or imply knowledge of a sensitive trait of the person reading it.
# "Are you diabetic?" is a violation. "Diabetes management made simpler" is not.
_SENSITIVE_TRAITS = (
    r"diabetic|diabetes|obese|overweight|depressed|depression|anxiety|anxious|"
    r"bipolar|addicted|addiction|alcoholic|cancer|hiv|herpes|std|stds|"
    r"erectile|impotent|infertile|menopausal|arthritic|arthritis|"
    r"bankrupt|in debt|bad credit|unemployed|jobless|divorced|single|widowed|"
    r"gay|lesbian|transgender|christian|muslim|jewish|catholic|"
    r"felon|convicted|disabled|veteran|pregnant|balding|bald"
)

RULES: list[Rule] = [
    # ---------------- personal attributes ----------------
    Rule(
        code="PERSONAL_ATTRIBUTE_DIRECT",
        severity=Severity.BLOCK,
        pattern=rf"\b(are|is)\s+you\b.{{0,25}}\b({_SENSITIVE_TRAITS})\b"
        rf"|\byou(?:'|’)?re\s+(?:\w+\s+){{0,2}}({_SENSITIVE_TRAITS})\b"
        rf"|\byour\s+({_SENSITIVE_TRAITS})\b",
        message="Copy asserts or implies a sensitive personal attribute of the viewer.",
        policy_ref="Meta Advertising Standards: Personal Attributes",
        suggestion=(
            "Rewrite in the third person about the product or the outcome. "
            "Say 'A simpler routine for blood sugar support', not 'Are you diabetic?'."
        ),
        platforms=(Platform.META,),
    ),
    Rule(
        code="PERSONAL_ATTRIBUTE_OTHER_PEOPLE",
        severity=Severity.WARN,
        pattern=rf"\b(people|folks|men|women|guys|moms|dads)\s+(who|with|over)\b.{{0,30}}\b({_SENSITIVE_TRAITS})\b",
        message="Copy targets viewers by a sensitive attribute.",
        policy_ref="Meta Advertising Standards: Personal Attributes",
        suggestion="Describe the product's benefit instead of describing the audience.",
        platforms=(Platform.META,),
    ),
    # ---------------- unrealistic claims ----------------
    Rule(
        code="GUARANTEED_OUTCOME",
        severity=Severity.BLOCK,
        pattern=r"\b(guaranteed|guarantee[sd]?\s+(results|income|profit|weight loss|approval)|"
        r"100%\s*(guaranteed|effective|proven|success)|risk[- ]free\s+(profit|income))\b",
        message="Guaranteed-outcome claim.",
        policy_ref="Meta: Unrealistic Outcomes / Google: Misrepresentation",
        suggestion="Replace with a qualified claim such as 'designed to' or 'typical users report'.",
    ),
    Rule(
        code="MIRACLE_CURE",
        severity=Severity.BLOCK,
        pattern=r"\b(miracle|cure[sd]?\s+(cancer|diabetes|disease)|"
        r"(cures?|heals?|treats?|prevents?|reverses?)\s+(cancer|diabetes|alzheimer|arthritis|covid)|"
        r"doctors hate|big pharma doesn'?t want|banned by|secret the .{0,20} don'?t want)\b",
        message="Miracle-cure or suppressed-secret framing.",
        policy_ref="Meta: Deceptive Claims / Google: Unapproved Substances & Healthcare",
        suggestion="State what the product actually does, with a source for any claim.",
    ),
    Rule(
        code="OVERNIGHT_RESULTS",
        severity=Severity.WARN,
        pattern=r"\b(overnight|in (just )?(24|48|72) hours|in \d+ days? flat|instantly|"
        r"instant results|immediate results|while you sleep)\b",
        message="Implausible speed-of-results claim.",
        policy_ref="Meta: Unrealistic Outcomes",
        suggestion="Give a realistic timeframe or drop the timing claim entirely.",
    ),
    Rule(
        code="SPECIFIC_WEIGHT_LOSS",
        severity=Severity.BLOCK,
        pattern=r"\b(lose|drop|shed|burn)\s+(\d+|\w+teen|twenty|thirty|forty|fifty)\s*"
        r"(lbs?|pounds?|kgs?|kilos?|inches|dress sizes?)\b",
        message="Specific weight-loss amount.",
        policy_ref="Meta: Health & Weight Loss / Google: Healthcare and Medicines",
        suggestion="Remove the number. Focus on the habit or the routine, not a promised result.",
    ),
    Rule(
        code="BEFORE_AFTER",
        severity=Severity.BLOCK,
        pattern=r"\b(before\s*(and|&|/|\s*\|\s*)\s*after|before/after|transformation photos?|"
        r"see (my|her|his|the) results below)\b",
        message="Before-and-after framing.",
        policy_ref="Meta: Adult Content & Body Image / Unexpected Results",
        suggestion="Show the product in use rather than contrasting body states.",
    ),
    Rule(
        code="BODY_SHAMING",
        severity=Severity.BLOCK,
        pattern=r"\b(flabby|fat and ugly|disgusting body|hate your body|embarrassing belly|"
        r"muffin top|ugly|too fat|too skinny)\b",
        message="Negative body-image language.",
        policy_ref="Meta: Idealized Body Image or Health",
        suggestion="Use neutral, affirming language about the goal.",
    ),
    # ---------------- financial ----------------
    Rule(
        code="INCOME_CLAIM",
        severity=Severity.BLOCK,
        pattern=r"(\$\s?[\d,]+(\.\d+)?\s?(k|m)?\s*(a|per|\/)\s*(day|week|month|hour))"
        r"|\b(make money fast|get rich|passive income guaranteed|quit your job|"
        r"financial freedom in \d+|double your (money|investment))\b",
        message="Specific or implied earnings claim.",
        policy_ref="Google: Get-Rich-Quick / Meta: Deceptive Business Models / FTC 16 CFR 255",
        suggestion=(
            "Remove the earnings figure. If income claims are essential, they require "
            "a substantiated earnings disclosure on the landing page."
        ),
    ),
    Rule(
        code="INVESTMENT_RETURN",
        severity=Severity.BLOCK,
        pattern=r"\b(guaranteed returns?|no risk investment|risk[- ]free returns?|"
        r"\d+x your (money|portfolio|investment)|can'?t lose)\b",
        message="Guaranteed investment return.",
        policy_ref="Google: Financial Products and Services / Meta: Financial Products",
        suggestion="Financial offers require risk disclosure and often certification.",
    ),
    # ---------------- deceptive UX ----------------
    Rule(
        code="FAKE_UI",
        severity=Severity.BLOCK,
        pattern=r"(\[?\s*(x|✕|✖)\s*\]?\s*close)|\bclick the (x|play button|button below to close)\b"
        r"|\b(fake|simulated) (button|player)\b|▶️?\s*play\s*(now)?\s*$",
        message="Imitates a user-interface element (close button, video player).",
        policy_ref="Meta: Nonfunctional Features / Google: Misleading Ad Design",
        suggestion="Remove UI-mimicking elements from the creative.",
    ),
    Rule(
        code="FALSE_URGENCY",
        severity=Severity.WARN,
        pattern=r"\b(only \d+ (left|spots?|seats?) (today|right now)|expires in \d+ (minutes?|seconds?)|"
        r"last chance ever|deleted in \d+ hours?)\b",
        message="Countdown or scarcity claim that must be literally true.",
        policy_ref="FTC Act Section 5 / Google: Misrepresentation",
        suggestion="Only use scarcity language backed by real inventory or a real deadline.",
    ),
    Rule(
        code="CLICKBAIT",
        severity=Severity.WARN,
        pattern=r"\b(you won'?t believe|shocking|this one weird trick|"
        r"number \d+ will (shock|surprise) you|what happened next|jaw[- ]dropping)\b",
        message="Clickbait phrasing suppresses reach and invites review.",
        policy_ref="Meta: Engagement Bait / Low-Quality Content",
        suggestion="Lead with the concrete benefit instead of withholding it.",
    ),
    Rule(
        code="ENGAGEMENT_BAIT",
        severity=Severity.WARN,
        pattern=r"\b(like and share|tag a friend|comment below to|share to win|"
        r"type ['\"]?yes['\"]? (below|in the comments))\b",
        message="Engagement bait.",
        policy_ref="Meta: Engagement Bait",
        suggestion="Ask for the click that matters instead of asking for engagement.",
    ),
    # ---------------- prohibited & restricted ----------------
    Rule(
        code="PROHIBITED_CONTENT",
        severity=Severity.BLOCK,
        pattern=r"\b(cbd|thc|kratom|nicotine|vape|e[- ]cig|firearm|ammo|ammunition|"
        r"silencer|escort|adult dating|hookup tonight|payday loan|bail bond|"
        r"essay writing service|fake (id|diploma|reviews?))\b",
        message="Category is prohibited or heavily restricted on both platforms.",
        policy_ref="Meta: Prohibited Content / Google: Restricted & Prohibited Content",
        suggestion="This offer likely cannot be run on this platform without certification.",
    ),
    Rule(
        code="CRYPTO_RESTRICTED",
        severity=Severity.WARN,
        pattern=r"\b(crypto|bitcoin|ethereum|nft|token presale|airdrop|defi|"
        r"trading signals?|forex signals?)\b",
        message="Crypto and trading offers require prior platform certification.",
        policy_ref="Meta: Cryptocurrency Products / Google: Financial Services certification",
        suggestion="Confirm the ad account is certified for this category before launching.",
    ),
    Rule(
        code="SUPPLEMENT_DISEASE_CLAIM",
        severity=Severity.BLOCK,
        pattern=r"\b(supplement|capsule|gummies?|formula|blend)\b.{0,60}"
        r"\b(cures?|treats?|prevents?|diagnoses?|reverses?)\b",
        message="Supplement described as treating or preventing disease.",
        policy_ref="FDA 21 U.S.C. 343(r) / Google: Healthcare and Medicines",
        suggestion="Use structure-function wording such as 'supports' and add the FDA disclaimer.",
    ),
    # ---------------- trademarks & impersonation ----------------
    Rule(
        code="THIRD_PARTY_BRAND",
        severity=Severity.WARN,
        pattern=r"\b(as seen on (cnn|fox|abc|nbc|cbs|forbes|shark tank)|"
        r"(endorsed|approved|recommended) by (fda|nasa|harvard|mayo clinic)|"
        r"official (amazon|google|facebook|meta) partner)\b",
        message="Third-party endorsement or trademark use.",
        policy_ref="Meta: Brand Usage in Ads / Google: Trademarks / FTC Endorsement Guides",
        suggestion="Remove unless you hold written permission and can substantiate it.",
    ),
    Rule(
        code="PLATFORM_IMPERSONATION",
        severity=Severity.BLOCK,
        pattern=r"\b(facebook|meta|instagram|google|youtube)\s+(official|verified|"
        r"approved this|recommends)\b",
        message="Implies platform endorsement.",
        policy_ref="Meta: Brand Usage / Google: Misrepresentation",
        suggestion="Remove references implying the platform endorses the offer.",
    ),
    # ---------------- formatting (Google is strict here) ----------------
    Rule(
        code="EXCESSIVE_CAPS",
        severity=Severity.BLOCK,
        pattern=r"\b[A-Z]{5,}\b",
        message="All-caps word. Google rejects gimmicky capitalization.",
        policy_ref="Google: Editorial Standards",
        suggestion="Use sentence case or title case. Acronyms of four letters or fewer are fine.",
        platforms=(Platform.GOOGLE,),
        flags=0,
        exempt_pattern=r"\b(FREE|USA|FDA|NASA|SALE|GMO|CBD|HIIT|NEW)\b",
    ),
    Rule(
        code="REPEATED_PUNCTUATION",
        severity=Severity.BLOCK,
        pattern=r"([!?])\1{1,}|\.{4,}",
        message="Repeated punctuation.",
        policy_ref="Google: Editorial Standards",
        suggestion="Use a single punctuation mark.",
        platforms=(Platform.GOOGLE,),
    ),
    Rule(
        code="GIMMICKY_SYMBOLS",
        severity=Severity.WARN,
        pattern=r"[★☆●○»«→]{2,}|\$\$+|#{2,}",
        message="Gimmicky symbol repetition.",
        policy_ref="Google: Editorial Standards",
        suggestion="Remove decorative symbol runs.",
        platforms=(Platform.GOOGLE,),
    ),
    Rule(
        code="PHONE_IN_TEXT",
        severity=Severity.WARN,
        pattern=r"\b(\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b",
        message="Phone number in ad text.",
        policy_ref="Google: Editorial Standards (use a call extension instead)",
        suggestion="Move the number into a call asset.",
        platforms=(Platform.GOOGLE,),
    ),
]


# Emoji ranges, used for a density check rather than a hard ban.
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f9ff"
    "\U0001fa70-\U0001faff"
    "☀-➿"
    "⬀-⯿"
    "️"
    "]",
)

_DISCLOSURE_RE = re.compile(
    r"\b(ad|advertisement|sponsored|affiliate|paid (link|partnership)|"
    r"commission|#ad|#sponsored)\b",
    re.IGNORECASE,
)


class ComplianceEngine:
    """Runs the rule set plus structural checks over a creative's text."""

    def __init__(
        self,
        rules: Sequence[Rule] | None = None,
        extra_checks: Iterable[Callable] | None = None,
    ) -> None:
        self.rules = list(rules if rules is not None else RULES)
        self.extra_checks = list(extra_checks or [])

    # -- public API ------------------------------------------------------
    def review(
        self,
        texts: dict[str, list[str]],
        platform: Platform,
        ad_format: str | None = None,
        offer=None,
        requires_disclosure: bool = True,
    ) -> ComplianceReport:
        findings: list[Finding] = []
        findings += self._pattern_findings(texts, platform)
        findings += self._length_findings(texts, platform, ad_format)
        findings += self._emoji_findings(texts, platform)
        findings += self._quality_findings(texts)
        if requires_disclosure:
            findings += self._disclosure_findings(texts)
        if offer is not None:
            findings += self._offer_findings(texts, offer)
        for check in self.extra_checks:
            findings += list(check(texts, platform) or [])

        return self._finalize(findings, platform)

    # -- checks ----------------------------------------------------------
    def _pattern_findings(
        self, texts: dict[str, list[str]], platform: Platform
    ) -> list[Finding]:
        out: list[Finding] = []
        seen: set[tuple[str, str]] = set()
        for rule in self.rules:
            if platform not in rule.platforms:
                continue
            regex = rule.compiled()
            exempt = re.compile(rule.exempt_pattern, rule.flags) if rule.exempt_pattern else None
            for field_name, values in texts.items():
                for value in values:
                    if not value:
                        continue
                    for match in regex.finditer(value):
                        matched = match.group(0).strip()
                        if exempt and exempt.fullmatch(matched):
                            continue
                        key = (rule.code, matched.lower())
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(
                            Finding(
                                code=rule.code,
                                severity=rule.severity,
                                message=rule.message,
                                policy_ref=rule.policy_ref,
                                field_name=field_name,
                                matched_text=matched[:120],
                                suggestion=rule.suggestion,
                            )
                        )
        return out

    def _length_findings(
        self, texts: dict[str, list[str]], platform: Platform, ad_format: str | None
    ) -> list[Finding]:
        out: list[Finding] = []
        try:
            spec = get_spec(platform, ad_format)
        except ValueError:
            return out

        for field_name, fspec in spec.fields.items():
            values = [v for v in texts.get(field_name, []) if v]
            if len(values) < fspec.min_count:
                out.append(
                    Finding(
                        code="TOO_FEW_ASSETS",
                        severity=Severity.BLOCK,
                        message=(
                            f"{field_name} has {len(values)} asset(s); "
                            f"{platform.value} requires at least {fspec.min_count}."
                        ),
                        policy_ref=f"{platform.value} ad format requirements",
                        field_name=field_name,
                        suggestion=f"Generate at least {fspec.recommended_count} {field_name}.",
                    )
                )
            if len(values) > fspec.max_count:
                out.append(
                    Finding(
                        code="TOO_MANY_ASSETS",
                        severity=Severity.BLOCK,
                        message=(
                            f"{field_name} has {len(values)} assets; the maximum is "
                            f"{fspec.max_count}."
                        ),
                        policy_ref=f"{platform.value} ad format requirements",
                        field_name=field_name,
                        suggestion=f"Keep the {fspec.max_count} strongest.",
                    )
                )
            for value in values:
                if len(value) > fspec.max_chars:
                    out.append(
                        Finding(
                            code="OVER_CHAR_LIMIT",
                            severity=Severity.BLOCK,
                            message=(
                                f"{field_name} exceeds {fspec.max_chars} characters "
                                f"({len(value)})."
                            ),
                            policy_ref=f"{platform.value} ad format requirements",
                            field_name=field_name,
                            matched_text=value[:120],
                            suggestion=f"Tighten to {fspec.max_chars} characters or fewer.",
                        )
                    )
                elif (
                    fspec.soft_truncate_at
                    and len(value) > fspec.soft_truncate_at
                    and field_name == "headlines"
                ):
                    out.append(
                        Finding(
                            code="SOFT_TRUNCATION",
                            severity=Severity.INFO,
                            message=(
                                f"{field_name} may be visually truncated after "
                                f"{fspec.soft_truncate_at} characters."
                            ),
                            policy_ref=f"{platform.value} rendering behaviour",
                            field_name=field_name,
                            matched_text=value[:120],
                            suggestion="Front-load the benefit in the first few words.",
                        )
                    )
            lowered = [v.strip().lower() for v in values]
            if len(lowered) != len(set(lowered)):
                out.append(
                    Finding(
                        code="DUPLICATE_ASSETS",
                        severity=Severity.WARN,
                        message=f"Duplicate {field_name} reduce Ad Strength.",
                        policy_ref="Google: Ad Strength / Meta: creative diversity",
                        field_name=field_name,
                        suggestion="Make each asset a distinct angle.",
                    )
                )
        return out

    def _emoji_findings(
        self, texts: dict[str, list[str]], platform: Platform
    ) -> list[Finding]:
        out: list[Finding] = []
        for field_name, values in texts.items():
            for value in values:
                count = len(_EMOJI_RE.findall(value))
                if platform is Platform.GOOGLE and count:
                    out.append(
                        Finding(
                            code="EMOJI_ON_SEARCH",
                            severity=Severity.BLOCK,
                            message="Google search ads do not render emoji; they are stripped or rejected.",
                            policy_ref="Google: Editorial Standards",
                            field_name=field_name,
                            matched_text=value[:120],
                            suggestion="Remove all emoji from search ad text.",
                        )
                    )
                elif platform is Platform.META and count > 3:
                    out.append(
                        Finding(
                            code="EMOJI_DENSITY",
                            severity=Severity.WARN,
                            message=f"{count} emoji in one asset reads as spam and depresses reach.",
                            policy_ref="Meta: Low-Quality Content",
                            field_name=field_name,
                            matched_text=value[:120],
                            suggestion="Keep to one or two emoji per asset.",
                        )
                    )
        return out

    def _quality_findings(self, texts: dict[str, list[str]]) -> list[Finding]:
        out: list[Finding] = []
        for field_name, values in texts.items():
            for value in values:
                letters = [c for c in value if c.isalpha()]
                if len(letters) >= 12:
                    caps_ratio = sum(c.isupper() for c in letters) / len(letters)
                    if caps_ratio > 0.5:
                        out.append(
                            Finding(
                                code="SHOUTING",
                                severity=Severity.WARN,
                                message=f"{int(caps_ratio * 100)}% of letters are capitals.",
                                policy_ref="Meta & Google: Editorial Standards",
                                field_name=field_name,
                                matched_text=value[:120],
                                suggestion="Use sentence case.",
                            )
                        )
                if value.count("!") > 2:
                    out.append(
                        Finding(
                            code="EXCLAMATION_OVERUSE",
                            severity=Severity.WARN,
                            message="More than two exclamation marks in one asset.",
                            policy_ref="Meta & Google: Editorial Standards",
                            field_name=field_name,
                            matched_text=value[:120],
                            suggestion="Keep at most one exclamation mark.",
                        )
                    )
        return out

    def _disclosure_findings(self, texts: dict[str, list[str]]) -> list[Finding]:
        joined = " ".join(v for values in texts.values() for v in values)
        if _DISCLOSURE_RE.search(joined):
            return []
        return [
            Finding(
                code="MISSING_AFFILIATE_DISCLOSURE",
                severity=Severity.WARN,
                message="No affiliate or paid-promotion disclosure found in the ad text.",
                policy_ref="FTC Endorsement Guides, 16 CFR Part 255",
                suggestion=(
                    "Material connections must be disclosed clearly and conspicuously. "
                    "Add '#ad' or 'Paid link' to the ad text, or place the disclosure "
                    "above the fold on the landing page."
                ),
            )
        ]

    def _offer_findings(self, texts: dict[str, list[str]], offer) -> list[Finding]:
        out: list[Finding] = []
        joined = " ".join(v for values in texts.values() for v in values).lower()
        for banned in getattr(offer, "banned_claims", []) or []:
            if banned and banned.lower() in joined:
                out.append(
                    Finding(
                        code="ADVERTISER_BANNED_CLAIM",
                        severity=Severity.BLOCK,
                        message=f"The advertiser prohibits the phrase '{banned}'.",
                        policy_ref="Affiliate program terms",
                        matched_text=banned,
                        suggestion=f"Remove '{banned}' from the copy.",
                    )
                )
        for required in getattr(offer, "required_disclosures", []) or []:
            if required and required.lower() not in joined:
                out.append(
                    Finding(
                        code="MISSING_REQUIRED_DISCLOSURE",
                        severity=Severity.BLOCK,
                        message=f"The advertiser requires the disclosure '{required}'.",
                        policy_ref="Affiliate program terms",
                        suggestion=f"Include '{required}' in the ad or on the landing page.",
                    )
                )
        if getattr(offer, "is_regulated", False):
            out.append(
                Finding(
                    code="REGULATED_VERTICAL",
                    severity=Severity.INFO,
                    message=(
                        f"'{getattr(offer, 'vertical', 'this')}' is a regulated vertical; "
                        "platform certification may be required before delivery."
                    ),
                    policy_ref="Meta & Google: Restricted Categories",
                    suggestion="Confirm the ad account holds the relevant certification.",
                )
            )
        return out

    # -- scoring ---------------------------------------------------------
    @staticmethod
    def _finalize(findings: list[Finding], platform: Platform) -> ComplianceReport:
        penalty = {Severity.BLOCK: 25.0, Severity.WARN: 6.0, Severity.INFO: 1.0}
        score = 100.0
        for f in findings:
            score -= penalty[f.severity]
        score = max(0.0, round(score, 1))

        if any(f.severity is Severity.BLOCK for f in findings):
            verdict = ComplianceVerdict.BLOCK
        elif any(f.severity is Severity.WARN for f in findings):
            verdict = ComplianceVerdict.WARN
        else:
            verdict = ComplianceVerdict.PASS

        findings.sort(key=lambda f: (-f.severity.rank, f.code))
        return ComplianceReport(
            verdict=verdict, findings=findings, score=score, platform=platform.value
        )


_DEFAULT_ENGINE = ComplianceEngine()


def review_texts(
    texts: dict[str, list[str]],
    platform: Platform,
    ad_format: str | None = None,
    offer=None,
    requires_disclosure: bool = True,
) -> ComplianceReport:
    """Convenience wrapper around the default engine."""
    return _DEFAULT_ENGINE.review(
        texts,
        platform=platform,
        ad_format=ad_format,
        offer=offer,
        requires_disclosure=requires_disclosure,
    )
