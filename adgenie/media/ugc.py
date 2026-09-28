"""Presenter videos: a person on camera speaking a reviewed script.

This is the format UGC-ad tools sell — a vertical, phone-shot clip of someone
talking to camera — and it is the format in which an affiliate account is
easiest to lose, for two reasons this module is built around.

**What is said is an ad claim.** A spoken line gets an ad rejected as readily
as a written one. A video model given dialogue in its prompt will usually say
it, and sometimes paraphrase it, add a line, or ad-lib a claim nobody reviewed.
So the voice is synthesised from the approved script and the face is animated
to that audio. The words the policy engine read and the words the ad speaks are
then the same words, rather than probably the same words.

**The presenter does not exist.** The genre's default script is a testimonial:
"I was sceptical, then I tried it, and now I sleep through the night." Spoken
by a synthetic person, that is a review by someone who does not exist about an
experience nobody had. The FTC's rule on consumer reviews and testimonials
(16 CFR Part 465, in force since October 2024) names that case explicitly and
carries civil penalties per violation. So scripts are written in a presenter's
voice — what the product is, how it works, what the brief can prove — and
first-person experience, borrowed credentials and "this is not an ad" framing
are blocked, not warned about.

Everything here is planning: it costs nothing and generates nothing. The paid
steps — presenter image, voice, lip-sync — run in `MediaStudio`, and only for a
plan whose script and presenter have both passed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..core.angles import angles_for
from ..core.compliance import (
    RULES,
    ComplianceEngine,
    ComplianceReport,
    Finding,
    Rule,
    Severity,
)
from ..core.copywriter import CopyBrief, build_brief
from ..models import ComplianceVerdict, Platform
from .prompts import PromptPlan, review_media_prompt
from .specs import MediaSpec, get_media_spec

logger = logging.getLogger(__name__)

__all__ = [
    "SpokenScript",
    "PresenterPlan",
    "PresenterVideoPlan",
    "TemplateScriptWriter",
    "LLMScriptWriter",
    "ScriptStudio",
    "review_script",
    "build_presenter_plan",
    "plan_presenter_video",
    "WORDS_PER_SECOND",
]

# Conversational delivery runs at about 150 words a minute. Scripts are
# budgeted from this rather than from a writer's sense of length, because a
# script that overruns the placement is a video the placement cuts off.
WORDS_PER_SECOND = 2.5

# Vertical first: presenter videos are made for Reels and Stories, and the same
# 9:16 asset is what Google's Demand Gen serves most widely.
DEFAULT_PLACEMENT = {
    Platform.META: "meta_reel_video",
    Platform.GOOGLE: "google_video",
}

DEFAULT_PERSONA = "an approachable adult presenter in everyday clothes"

# Handed to the lip-sync model with the still and the voice track. It asks for
# delivery only; what is said is fixed by the audio.
DELIVERY_PROMPT = (
    "Natural, conversational delivery straight to camera. Subtle head movement, "
    "natural blinking, relaxed expression. Steady framing; no gestures toward "
    "the screen."
)


# --------------------------------------------------------------------------
# the script
# --------------------------------------------------------------------------


@dataclass
class SpokenScript:
    """What the presenter says, line by line, in the order it is said."""

    hook: str
    body: list[str] = field(default_factory=list)
    call_to_action: str = ""
    angle: str = ""
    rationale: str = ""
    generator: str = "template"
    generator_meta: dict = field(default_factory=dict)
    report: ComplianceReport | None = None

    @property
    def lines(self) -> list[str]:
        spoken = [self.hook, *self.body, self.call_to_action]
        return [line.strip() for line in spoken if line and line.strip()]

    @property
    def text(self) -> str:
        return " ".join(self.lines)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def estimated_seconds(self) -> float:
        return round(self.word_count / WORDS_PER_SECOND, 1)

    @property
    def is_approved(self) -> bool:
        return (
            self.report is not None
            and self.report.verdict is not ComplianceVerdict.BLOCK
        )

    def as_dict(self) -> dict:
        return {
            "hook": self.hook,
            "body": list(self.body),
            "call_to_action": self.call_to_action,
            "text": self.text,
            "words": self.word_count,
            "estimated_seconds": self.estimated_seconds,
            "angle": self.angle,
            "rationale": self.rationale,
            "generator": self.generator,
            "review": self.report.as_dict() if self.report else None,
        }


# --------------------------------------------------------------------------
# reviewing what is said
# --------------------------------------------------------------------------

_TESTIMONY_POLICY = (
    "FTC 16 CFR Part 465 (fake testimonials) / FTC Endorsement Guides, 16 CFR Part 255"
)

# Rules a synthetic presenter adds on top of the ad-text rules. The patterns
# are deliberately broad: a false positive costs a rewrite, while a fake
# testimonial that ships costs the account and carries a per-violation
# penalty.
SCRIPT_RULES: list[Rule] = [
    Rule(
        code="FABRICATED_TESTIMONY",
        severity=Severity.BLOCK,
        pattern=(
            # "I tried it", "I've been taking", "I started using"
            r"\bI(?:'ve|’ve| have| had)?\s+(?:been\s+|just\s+|finally\s+|actually\s+|"
            r"personally\s+|recently\s+)*(?:tried|using|used|taking|taken|took|started|"
            r"switched|bought|ordered|lost|dropped|gained|noticed|felt|slept|seen)\b"
            # "I love it", "I swear by it", "I recommend"
            r"|\bI\s+(?:really\s+|honestly\s+|absolutely\s+)?(?:love|swear by|"
            r"recommend|can(?:'|’)?t live without)\b"
            r"|\bI(?:'m|’m| am)\s+(?:obsessed|hooked|using|taking|loving)\b"
            # the backstory beat: "I was sceptical", "I used to struggle"
            r"|\bI\s+was\s+(?:so\s+|really\s+)?(?:sceptical|skeptical|doubtful|"
            r"struggling|exhausted|desperate)\b|\bI\s+used\s+to\b"
            # results and a life the presenter does not have
            r"|\b(?:changed|saved)\s+my\s+life\b|\bworked\s+(?:for|on)\s+me\b"
            r"|\bmy\s+results?\b"
            r"|\bmy\s+(?:doctor|dermatologist|trainer|husband|wife|partner|mom|mum|"
            r"kids?|family)\s+(?:said|noticed|told|recommended|loves?)\b"
            r"|\bas\s+an?\s+(?:busy\s+|working\s+|single\s+|new\s+)?(?:mom|mum|dad|"
            r"mother|father|parent)\b"
        ),
        message=(
            "The presenter claims personal experience, results or a personal life. "
            "A synthetic presenter has none, so this is a testimonial from someone "
            "who does not exist."
        ),
        policy_ref=_TESTIMONY_POLICY,
        suggestion=(
            "Speak as a presenter, not a customer: say what the product is, how it "
            "works and what the brief can prove, in the second person or about the "
            "product."
        ),
    ),
    Rule(
        code="BORROWED_CREDENTIAL",
        severity=Severity.BLOCK,
        pattern=(
            r"\b(?:as\s+an?|I(?:'m|’m| am)\s+an?)\s+(?:[\w-]+\s+){0,2}?(?:doctor|"
            r"physician|nurse|pharmacist|dermatologist|dietitian|dietician|"
            r"nutritionist|therapist|psychologist|dentist|surgeon|chiropractor|"
            r"scientist|chemist|paediatrician|pediatrician|trainer|financial "
            r"(?:advisor|adviser|planner)|accountant|lawyer|attorney)\b"
            r"|\b(?:doctor|nurse|pharmacist|dermatologist|dietitian|nutritionist|"
            r"dentist|trainer)\s+here\b"
        ),
        message="The presenter claims a professional credential it does not hold.",
        policy_ref=f"{_TESTIMONY_POLICY} (expert endorsements, 255.3)",
        suggestion=(
            "Remove the credential. If a qualified person reviewed the product, that "
            "belongs in the brief's proof points, attributed to them."
        ),
    ),
    Rule(
        code="FALSE_INDEPENDENCE",
        severity=Severity.BLOCK,
        pattern=(
            r"\bnot\s+(?:sponsored|an?\s+ad|a\s+paid\s+\w+|paid\s+to\s+say)\b"
            r"|\bunsponsored\b|\b(?:nobody|no\s+one)\s+(?:paid|is\s+paying)\s+me\b"
            r"|\b(?:honest|unbiased|genuine|independent|real)\s+review\b"
            r"|\ba\s+real\s+(?:customer|person|user|mom|mum)\b"
        ),
        message="The script presents a paid ad as independent opinion.",
        policy_ref="FTC Act Section 5 / FTC 16 CFR Part 465",
        suggestion="Drop the independence claim; the ad is paid and says so.",
    ),
]


class ScriptComplianceEngine(ComplianceEngine):
    """The ad-text engine, without the checks that describe ad fields.

    A spoken script has no headline limits or asset counts. Its limit is time,
    which `review_script` checks against the placement instead.
    """

    def _length_findings(self, texts, platform, ad_format):
        return []


def _timing_check(max_seconds: float | None):
    def check(texts: dict[str, list[str]], platform: Platform) -> list[Finding]:
        words = sum(len(v.split()) for values in texts.values() for v in values)
        if not words:
            return [
                Finding(
                    code="EMPTY_SCRIPT",
                    severity=Severity.BLOCK,
                    message="The script has nothing to say.",
                    policy_ref="Presenter video format",
                    field_name="spoken_script",
                    suggestion="Write a hook, at least one line of argument and a close.",
                )
            ]
        seconds = words / WORDS_PER_SECOND
        if max_seconds and seconds > max_seconds:
            return [
                Finding(
                    code="SCRIPT_TOO_LONG",
                    severity=Severity.BLOCK,
                    message=(
                        f"About {seconds:.0f}s spoken ({words} words) against a "
                        f"{max_seconds:.0f}s placement; the end would be cut off."
                    ),
                    policy_ref="Placement duration",
                    field_name="spoken_script",
                    suggestion=(
                        f"Cut to {int(max_seconds * WORDS_PER_SECOND)} words or fewer."
                    ),
                )
            ]
        return []

    return check


def _spoken_terms(offer) -> SimpleNamespace:
    """The offer's terms that apply to spoken words.

    Banned phrases are banned however they are delivered. Required disclosures
    are not demanded of the script: they belong to the ad text the video runs
    under, which is reviewed and enforced on its own, and requiring the script
    to speak them would block every script for an offer that has one.
    """
    return SimpleNamespace(
        banned_claims=list(getattr(offer, "banned_claims", None) or []),
        required_disclosures=[],
        is_regulated=bool(getattr(offer, "is_regulated", False)),
        vertical=getattr(offer, "vertical", "general"),
    )


def review_script(
    script: SpokenScript,
    platform: Platform = Platform.META,
    offer=None,
    max_seconds: float | None = None,
) -> ComplianceReport:
    """Review what the presenter will say, the way ad text is reviewed.

    Every ad-text rule applies — a guaranteed outcome is no safer spoken — plus
    the rules a synthetic presenter adds. The affiliate disclosure is not
    required of the script: it belongs to the ad text the video runs under.
    """
    engine = ScriptComplianceEngine(
        rules=[*RULES, *SCRIPT_RULES],
        extra_checks=[_timing_check(max_seconds)],
    )
    return engine.review(
        {"spoken_script": script.lines},
        platform=platform,
        offer=_spoken_terms(offer) if offer is not None else None,
        requires_disclosure=False,
    )


# --------------------------------------------------------------------------
# writing the script
# --------------------------------------------------------------------------

_HOOKS: dict[str, tuple[str, ...]] = {
    "problem_solution": (
        "Want to {benefit}? Here's what {product_name} does.",
        "If you want to {benefit}, this is worth a look.",
    ),
    "mechanism": (
        "Here's what {product_name} actually does differently.",
        "Most options skip the part that matters. Here's the part that matters.",
    ),
    "social_proof": (
        "{proof}",
        "Here's why people pick {product_name}.",
    ),
    "comparison": (
        "Here's how {product_name} compares with the usual way.",
        "Before you settle for the usual option, see this.",
    ),
    "objection": (
        "Fair question: does {product_name} actually do anything? Here's the answer.",
        "Here's what {product_name} does, and what it doesn't.",
    ),
    "cost_of_inaction": (
        "Putting this off costs more than it seems.",
        "The longer this waits, the harder it gets.",
    ),
    "identity": (
        "This is for people who like to {benefit}.",
        "If your routine matters to you, this fits it.",
    ),
    "how_to": (
        "Here's one step that makes it easier to {benefit}.",
        "Try this one step first.",
    ),
    "offer_led": (
        "{offer_terms}.",
        "Here's the current offer on {product_name}.",
    ),
    "search_intent": (
        "Looking for {keyword}? Here's the short version.",
        "Here's the short version on {keyword}.",
    ),
}

_CLOSES = (
    "Tap below to see the details.",
    "Tap the link to see how it works.",
    "The full details are one tap away.",
)


def _mid_sentence(text: str) -> str:
    """Lower-case a phrase's first letter so it can sit mid-sentence, unless it
    opens with an acronym or a name ("FDA-registered", "L-theanine", "CalmLeaf")."""
    text = (text or "").strip()
    if not text:
        return text
    first = text.split()[0]
    ordinary = first == "A" or (
        len(first) > 1 and first[1].isalpha() and first[1:].islower()
    )
    return text[0].lower() + text[1:] if ordinary else text


class TemplateScriptWriter:
    """Deterministic scripts from the angle library, used without an API key.

    Never writes in the first person, so nothing it produces can read as a
    testimonial. Lines are added in priority order until the word budget is
    spent, so the script fits the placement by construction.
    """

    name = "template"

    def write(self, brief: CopyBrief, seconds: float) -> SpokenScript:
        angle = brief.angle or angles_for(brief.platform.value, 1)[0]
        subs = self._substitutions(brief)
        nth = brief.variant_index

        hooks = [h for h in _HOOKS.get(angle.key, _HOOKS["mechanism"]) if self._fillable(h, subs)]
        hook = self._fill(hooks[nth % len(hooks)] if hooks else _HOOKS["mechanism"][0], subs)
        close = _CLOSES[nth % len(_CLOSES)]

        candidates = self._body_lines(angle.key, subs, hook)
        budget = int(seconds * WORDS_PER_SECOND)
        used = len(hook.split()) + len(close.split())
        body: list[str] = []
        for line in candidates:
            if line in body or line == hook:
                continue
            words = len(line.split())
            if used + words > budget:
                continue
            body.append(line)
            used += words

        return SpokenScript(
            hook=hook,
            body=body,
            call_to_action=close,
            angle=angle.key,
            rationale=f"{angle.name}: {angle.thesis}",
            generator=self.name,
            generator_meta={"angle": angle.key, "word_budget": budget},
        )

    @staticmethod
    def _substitutions(brief: CopyBrief) -> dict[str, str]:
        benefits = [b.strip().rstrip(".") for b in brief.key_benefits if b and b.strip()]
        description = (brief.product_description or "").split(".")[0].strip()
        return {
            "product_name": brief.product_name,
            "benefit": _mid_sentence(benefits[0]) if benefits else "",
            "second_benefit": _mid_sentence(benefits[1]) if len(benefits) > 1 else "",
            "description": _mid_sentence(description),
            "proof": brief.proof().strip().rstrip(".") + "." if brief.proof() else "",
            "offer_terms": (brief.offer_terms or "").strip().rstrip("."),
            "keyword": brief.keyword or brief.product_name,
        }

    @staticmethod
    def _fillable(pattern: str, subs: dict[str, str]) -> bool:
        """A pattern whose slots the brief cannot fill is skipped, not spoken
        with a hole in it."""
        return all(subs.get(slot) for slot in re.findall(r"{(\w+)}", pattern))

    def _body_lines(self, angle_key: str, subs: dict[str, str], hook: str) -> list[str]:
        # "A magnesium blend..." is said as "It's a magnesium blend"; anything
        # that is not a noun phrase needs the longer frame to stay grammatical.
        what_it_is = (
            "It's {description}."
            if re.match(r"(a|an|the)\s", subs["description"])
            else "Here's what it is: {description}."
        )
        designed = "It's designed to help you {benefit}."
        also = "It also helps you {second_benefit}."
        proof = "{proof}"
        # Which supporting line comes first is the angle's argument; the rest
        # fill whatever time is left.
        order = {
            "mechanism": (what_it_is, designed, proof, also),
            "social_proof": (proof, designed, what_it_is, also),
            "objection": (what_it_is, proof, designed, also),
            "comparison": (what_it_is, designed, proof, also),
            "offer_led": (designed, what_it_is, proof, also),
        }.get(angle_key, (what_it_is, designed, proof, also))
        if subs["benefit"] and subs["benefit"] in hook:
            # The hook already made the promise. Saying it again spends a
            # sixth of a fifteen-second video on a repeat.
            order = tuple(p for p in order if p is not designed)
        return [self._fill(p, subs) for p in order if self._fillable(p, subs)]

    @staticmethod
    def _fill(pattern: str, subs: dict[str, str]) -> str:
        out = pattern
        for key, value in subs.items():
            out = out.replace("{" + key + "}", value)
        out = re.sub(r"\s+", " ", out).strip()
        return out[:1].upper() + out[1:]


class GeneratedScript(BaseModel):
    """Schema the model must fill. Enforced by structured outputs."""

    hook: str = Field(
        description="The opening line, spoken in the first two or three seconds."
    )
    body: list[str] = Field(
        description="One to three short spoken lines that make the angle's argument."
    )
    call_to_action: str = Field(
        description="One closing line saying what to do next, such as tapping the link."
    )
    rationale: str = Field(
        default="", description="One sentence on why this opening suits this audience."
    )


_SCRIPT_SYSTEM_PROMPT = """You write short scripts for vertical video ads in \
which a presenter speaks straight to camera. The presenter is synthetic: an \
AI-generated person, not a customer and not a professional.

That fact sets the rules. A synthetic presenter has never used the product. It \
has no results, no family, no job and no credentials, so the script never \
gives it any. It explains what the product is, how it works and what the brief \
can prove, speaking to the viewer or about the product, the way a \
knowledgeable presenter would. It never says anything like "I tried", "I've \
been using", "it worked for me", "my results", "as a nurse", "not sponsored" \
or "honest review". An experience claim from someone who does not exist is a \
fake testimonial, which is unlawful as well as against platform policy.

Every claim traces back to the brief. You never invent a statistic, a rating, \
a review count, a timeframe, a discount or an endorsement. If the brief gives \
you no proof, write a script that needs none.

Write the way people talk: short sentences, plain words, one idea per line, \
nothing that has to be read to be understood. No emoji, hashtags, stage \
directions or symbols; every character is spoken aloud."""


class LLMScriptWriter:
    """Writes scripts with Claude using structured outputs."""

    name = "llm"

    def __init__(self, settings: Settings | None = None, client=None) -> None:
        self.settings = settings or get_settings()
        self._client = client

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "The 'anthropic' package is required for LLM script writing. "
                "Install it with: pip install anthropic"
            ) from exc
        self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def write(self, brief: CopyBrief, seconds: float) -> SpokenScript:
        client = self._get_client()
        response = client.messages.parse(
            model=self.settings.copywriter_model,
            max_tokens=self.settings.copywriter_max_tokens,
            system=_SCRIPT_SYSTEM_PROMPT,
            output_config={"effort": self.settings.copywriter_effort},
            messages=[{"role": "user", "content": self._render_brief(brief, seconds)}],
            output_format=GeneratedScript,
        )

        if getattr(response, "stop_reason", None) == "refusal":
            detail = getattr(response, "stop_details", None)
            raise RuntimeError(
                "Script generation was declined by the model"
                + (f" ({getattr(detail, 'category', 'unspecified')})" if detail else "")
                + ". This usually means the offer itself is not advertisable."
            )

        parsed: GeneratedScript = response.parsed_output
        return SpokenScript(
            hook=parsed.hook,
            body=list(parsed.body),
            call_to_action=parsed.call_to_action,
            angle=brief.angle.key if brief.angle else "",
            rationale=parsed.rationale,
            generator=self.name,
            generator_meta={
                "model": getattr(response, "model", self.settings.copywriter_model),
                "input_tokens": getattr(getattr(response, "usage", None), "input_tokens", None),
                "output_tokens": getattr(getattr(response, "usage", None), "output_tokens", None),
            },
        )

    @staticmethod
    def _render_brief(brief: CopyBrief, seconds: float) -> str:
        budget = int(seconds * WORDS_PER_SECOND)
        parts = [
            f"Write a presenter script for a {seconds:.0f}-second vertical video ad "
            f"on {brief.platform.value}.",
            "",
            "## Offer brief",
            f"Product: {brief.product_name}",
            f"Vertical: {brief.vertical}",
            f"What it is: {brief.product_description or 'not supplied'}",
            f"Audience: {brief.target_audience or 'general consumers'}",
        ]
        if brief.key_benefits:
            parts.append(
                "Benefits (use only these):\n"
                + "\n".join(f"  - {b}" for b in brief.key_benefits)
            )
        if brief.proof_points:
            parts.append(
                "Proof points (the ONLY proof you may cite):\n"
                + "\n".join(f"  - {p}" for p in brief.proof_points)
            )
        else:
            parts.append(
                "Proof points: none supplied. Do not cite any statistic, rating, "
                "review count or testimonial."
            )
        if brief.offer_terms:
            parts.append(f"Commercial terms: {brief.offer_terms}")

        if brief.angle:
            parts += [
                "",
                "## Angle to argue",
                f"{brief.angle.name} - {brief.angle.thesis}",
                brief.angle.guidance,
            ]

        rules = [
            f"The whole script, hook and close included, is at most {budget} words; "
            f"at speaking pace that is about {seconds:.0f} seconds.",
            "The hook has to earn the next line within about three seconds.",
            "Never speak as a customer or give the presenter an experience, a result, "
            "a personal life or a credential.",
            "Never assert or imply a sensitive personal attribute of the viewer.",
            "Never promise a guaranteed outcome or a specific timeframe.",
            "Never state a weight-loss amount or an income figure.",
            "Never use before-and-after framing.",
        ]
        if brief.banned_claims:
            rules.append(
                "The advertiser forbids these phrases: "
                + ", ".join(f"'{c}'" for c in brief.banned_claims)
            )
        if brief.is_regulated:
            rules.append(
                f"'{brief.vertical}' is a regulated category. Use structure-function "
                "wording ('supports', 'designed to') rather than disease claims."
            )
        parts += ["", "## Rules", *[f"- {r}" for r in rules]]

        if brief.variant_index:
            parts += [
                "",
                "## This is another execution of the same angle",
                f"You have already written {brief.variant_index} script(s) for this "
                "argument. Open differently and use a different concrete detail; "
                "rewording the same hook produces videos that fatigue together.",
            ]

        if brief.repair_notes:
            parts += [
                "",
                "## A previous draft was rejected by the policy checker",
                "Fix every point below. Do not reintroduce the flagged wording.",
                *[f"- {n}" for n in brief.repair_notes],
            ]
        return "\n".join(parts)


class ScriptStudio:
    """Write -> review -> repair: the loop ad copy goes through, for speech."""

    def __init__(self, writer=None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.writer = writer or self._default_writer()

    def _default_writer(self):
        if self.settings.has_copywriter_llm:
            return LLMScriptWriter(self.settings)
        return TemplateScriptWriter()

    def write(
        self,
        brief: CopyBrief,
        offer=None,
        seconds: float = 15.0,
    ) -> SpokenScript:
        """One reviewed script, repaired up to the configured limit.

        A blocked final attempt is returned rather than raised, so a preview
        can show exactly what failed and why. Nothing downstream generates
        from a script that is not approved.
        """
        attempts = max(1, self.settings.copywriter_max_repair_attempts + 1)
        notes: list[str] = list(brief.repair_notes)
        script: SpokenScript | None = None

        for attempt in range(attempts):
            brief.repair_notes = notes
            try:
                script = self.writer.write(brief, seconds)
            except Exception as exc:
                if isinstance(self.writer, TemplateScriptWriter):
                    raise
                logger.warning("Script writer failed (%s); falling back to templates.", exc)
                self.writer = TemplateScriptWriter()
                script = self.writer.write(brief, seconds)

            script.report = review_script(
                script, platform=brief.platform, offer=offer, max_seconds=seconds
            )
            script.generator_meta = {
                **script.generator_meta,
                "attempt": attempt + 1,
                "compliance_score": script.report.score,
            }
            if script.is_approved:
                return script
            notes = script.report.rewrite_instructions()
            logger.info(
                "Script blocked on attempt %s: %s", attempt + 1, "; ".join(notes[:3])
            )

        assert script is not None
        return script


# --------------------------------------------------------------------------
# the presenter
# --------------------------------------------------------------------------

# Checked against the operator's persona text only. The fixed half of the
# prompt is written here, and would otherwise trip the likeness rule by
# saying the presenter resembles nobody.
_PERSONA_RULES: tuple[tuple[str, str, str], ...] = (
    (
        r"\b(doctor|physician|nurse|surgeon|dentist|pharmacist|dermatologist|"
        r"scientist|chemist|paramedic|lab coat|white coat|scrubs|stethoscope|"
        r"hospital|clinic|laboratory)\b",
        "IMPLIED_AUTHORITY",
        "A presenter styled as a clinician or scientist implies a professional "
        "endorsement nobody gave. Describe an ordinary person instead.",
    ),
    (
        r"\b(child|children|kid|kids|teen|teens|teenager|minor|toddler|baby|"
        r"schoolgirl|schoolboy)\b",
        "MINOR_PRESENTER",
        "Do not use a minor as a presenter; several restricted categories forbid "
        "it outright. Describe an adult.",
    ),
    (
        r"\b(looks?\s+like|resembl\w*|look-?alike|double\s+of|impersonat\w*)\b",
        "LIKENESS",
        "Do not model the presenter on a real person. Describe a type, not an "
        "individual.",
    ),
)

_COMPILED_PERSONA_RULES = tuple(
    (re.compile(p, re.IGNORECASE), code, fix) for p, code, fix in _PERSONA_RULES
)

# Who each stock voice in the speech library sounds like. The first live run
# drew a man for a gender-neutral persona and gave him a woman's voice: the
# image model and the voice are chosen separately, and nothing tied them
# together. A presenter who sounds like someone else is the first thing a
# viewer notices, and the lip-sync is charged either way.
#
# Only voices whose sound is certain are listed: an unlisted voice skips the
# check, which is safer than a wrong entry drawing the wrong presenter.
VOICE_GENDERS: dict[str, str] = {
    # ElevenLabs
    **dict.fromkeys(
        ("rachel", "aria", "sarah", "laura", "charlotte", "alice", "matilda",
         "jessica", "lily"),
        "woman",
    ),
    **dict.fromkeys(
        ("roger", "charlie", "george", "callum", "liam", "will", "eric", "chris",
         "brian", "daniel", "bill"),
        "man",
    ),
    # Gemini
    **dict.fromkeys(("zephyr", "kore", "leda", "aoede"), "woman"),
    **dict.fromkeys(("puck", "charon", "fenrir", "orus"), "man"),
}

_PERSONA_GENDER = {
    "woman": re.compile(r"\b(woman|women|female|lady|girl|she|her)\b", re.IGNORECASE),
    "man": re.compile(r"\b(man|men|male|guy|gentleman|boy|he|his|him)\b", re.IGNORECASE),
}


def _persona_gender(persona: str) -> str | None:
    """The gender a persona names, if it names exactly one."""
    named = [g for g, pattern in _PERSONA_GENDER.items() if pattern.search(persona)]
    return named[0] if len(named) == 1 else None


def _persona_findings(persona: str) -> list[dict]:
    findings: list[dict] = []
    for pattern, code, fix in _COMPILED_PERSONA_RULES:
        match = pattern.search(persona or "")
        if match:
            findings.append(
                {
                    "code": code,
                    "matched_text": match.group(0)[:80],
                    "suggestion": fix,
                    "policy_ref": "Meta Advertising Standards / FTC Endorsement Guides",
                }
            )
    return findings


@dataclass
class PresenterPlan:
    """Who appears on camera: a generated still, or an image the operator owns."""

    persona: str
    prompt: str = ""
    image_url: str | None = None
    findings: list[dict] = field(default_factory=list)

    @property
    def is_supplied(self) -> bool:
        return bool(self.image_url)

    @property
    def is_safe(self) -> bool:
        return not self.findings

    def prompt_plan(self, spec: MediaSpec, placement: str) -> PromptPlan:
        """The still as an ordinary image generation, at the video's shape."""
        return PromptPlan(
            prompt=self.prompt,
            negative_prompt="",
            placement=placement,
            aspect_ratio=spec.aspect_ratio,
            width=spec.width,
            height=spec.height,
            kind="image",
            findings=list(self.findings),
        )

    def as_dict(self) -> dict:
        return {
            "persona": self.persona,
            "source": "supplied" if self.is_supplied else "generated",
            "image_url": self.image_url,
            "prompt": self.prompt,
            "findings": self.findings,
        }


def build_presenter_plan(
    spec: MediaSpec,
    persona: str = "",
    image_url: str | None = None,
    voice: str | None = None,
) -> PresenterPlan:
    """Plan the still the lip-sync model animates, screened before it costs money.

    A supplied image skips generation and prompt screening, since there is no
    prompt; the operator is vouching for their rights to it, its content, and
    that it suits the voice. It must be a URL, because the provider fetches it.

    A generated presenter is matched to the voice when the voice is a known
    one: a persona that names no gender gets the voice's, and one that names
    the other gender is refused rather than drawn.
    """
    persona = (persona or DEFAULT_PERSONA).strip()
    if image_url:
        findings = []
        if not re.match(r"^https?://", image_url.strip(), re.IGNORECASE):
            findings.append(
                {
                    "code": "PRESENTER_IMAGE_NOT_A_URL",
                    "matched_text": image_url[:80],
                    "suggestion": (
                        "The provider fetches the image, so it has to be a public "
                        "http(s) URL rather than a local path."
                    ),
                    "policy_ref": "kie.ai input contract",
                }
            )
        return PresenterPlan(persona=persona, image_url=image_url.strip(), findings=findings)

    voice_gender = VOICE_GENDERS.get((voice or "").strip().lower())
    persona_gender = _persona_gender(persona)
    matching: list[dict] = []
    if voice_gender and persona_gender and voice_gender != persona_gender:
        example = next(name for name, g in VOICE_GENDERS.items() if g == persona_gender)
        matching.append(
            {
                "code": "VOICE_MISMATCH",
                "matched_text": voice or "",
                "suggestion": (
                    f"The voice '{voice}' is a {voice_gender}'s and the presenter "
                    f"is a {persona_gender}. Choose a {persona_gender}'s voice, such "
                    f"as '{example.title()}', or describe a {voice_gender}."
                ),
                "policy_ref": "Presenter video format",
            }
        )

    parts = [
        f"Vertical phone-camera frame of {persona}, looking straight into the lens "
        "as if mid-sentence.",
        *(
            [f"The presenter is a {voice_gender}."]
            if voice_gender and not persona_gender
            else []
        ),
        "Head and shoulders, face fully visible and evenly lit, mouth relaxed, so "
        "the face can be animated to speech.",
        "Setting: a real, lived-in home with natural window light and ordinary "
        "background detail.",
        "Candid and unposed, like a front-camera selfie video rather than a studio "
        "portrait.",
        "A synthetic person who resembles no real individual.",
        f"Framing: {spec.aspect_ratio}, face in the middle of the frame, clear of "
        "the top and bottom where the interface sits.",
        "Contain no text, captions, logos or watermarks of any kind.",
    ]
    prompt = " ".join(parts)
    findings = _persona_findings(persona) + review_media_prompt(persona) + matching
    return PresenterPlan(persona=persona, prompt=prompt, findings=findings)


# --------------------------------------------------------------------------
# the whole plan
# --------------------------------------------------------------------------


@dataclass
class PresenterVideoPlan:
    """Everything a presenter video needs, reviewed before anything is paid for."""

    script: SpokenScript
    presenter: PresenterPlan
    placement: str
    aspect_ratio: str
    width: int
    height: int
    voice: str
    platform: Platform
    offer_id: int | None = None

    @property
    def spec(self) -> MediaSpec:
        return get_media_spec(self.placement)

    @property
    def findings(self) -> list[dict]:
        """What stops generation, from the script and the presenter alike."""
        blocking = []
        if self.script.report is not None:
            blocking = [
                {
                    "code": f.code,
                    "matched_text": f.matched_text,
                    "suggestion": f.suggestion or f.message,
                    "policy_ref": f.policy_ref,
                }
                for f in self.script.report.blocking
            ]
        elif not self.script.lines:
            blocking = [
                {
                    "code": "EMPTY_SCRIPT",
                    "matched_text": "",
                    "suggestion": "Write a script.",
                    "policy_ref": "Presenter video format",
                }
            ]
        return blocking + list(self.presenter.findings)

    @property
    def is_safe(self) -> bool:
        return self.script.is_approved and self.presenter.is_safe

    def as_dict(self) -> dict:
        return {
            "offer_id": self.offer_id,
            "platform": self.platform.value,
            "placement": self.placement,
            "aspect_ratio": self.aspect_ratio,
            "width": self.width,
            "height": self.height,
            "voice": self.voice,
            "script": self.script.as_dict(),
            "presenter": self.presenter.as_dict(),
            "findings": self.findings,
            "would_generate": self.is_safe,
        }


def plan_presenter_video(
    offer,
    angle_key: str = "",
    platform: Platform = Platform.META,
    placement: str | None = None,
    seconds: float | None = None,
    persona: str = "",
    presenter_image_url: str | None = None,
    voice: str | None = None,
    variant_index: int = 0,
    script_studio: ScriptStudio | None = None,
    settings: Settings | None = None,
) -> PresenterVideoPlan:
    """Write and review the script, and plan the presenter. Generates nothing."""
    settings = settings or get_settings()
    placement = placement or DEFAULT_PLACEMENT[platform]
    spec = get_media_spec(placement)
    if spec.kind != "video":
        raise ValueError(f"placement '{placement}' is an image placement")
    limit = spec.max_seconds or seconds or 15.0
    seconds = min(seconds or limit, limit)

    brief = build_brief(offer, platform=platform, angle_key=angle_key or None)
    brief.variant_index = variant_index
    studio = script_studio or ScriptStudio(settings=settings)
    script = studio.write(brief, offer=offer, seconds=seconds)
    voice = (voice or settings.kie_tts_voice).strip()

    return PresenterVideoPlan(
        script=script,
        presenter=build_presenter_plan(spec, persona, presenter_image_url, voice=voice),
        placement=placement,
        aspect_ratio=spec.aspect_ratio,
        width=spec.width,
        height=spec.height,
        voice=voice,
        platform=platform,
        offer_id=getattr(offer, "id", None),
    )
