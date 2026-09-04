"""Ad copy generation.

Two implementations behind one interface:

* `LLMCopywriter` calls Claude with a structured-output schema, so the model
  returns validated assets rather than prose that has to be parsed.
* `TemplateCopywriter` composes copy from the angle library. It needs no API
  key, is deterministic under a fixed seed, and exists so the pipeline and its
  tests run end to end anywhere.

`CopyStudio` wraps whichever generator is available in a
generate -> review -> repair loop: copy that fails compliance is sent back to
the generator with the specific findings attached, up to a bounded number of
attempts, and is hard-trimmed to the platform's character limits before it can
reach an ad account.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..models import ComplianceVerdict, Platform
from ..platforms.specs import get_spec, truncate_to_spec
from .angles import ANGLES, Angle, angles_for, get_angle
from .compliance import ComplianceEngine, ComplianceReport

logger = logging.getLogger(__name__)

__all__ = [
    "CopyBrief",
    "CreativeDraft",
    "CopyStudio",
    "LLMCopywriter",
    "TemplateCopywriter",
    "build_brief",
]


# --------------------------------------------------------------------------
# brief & draft
# --------------------------------------------------------------------------


@dataclass
class CopyBrief:
    """Everything the copywriter is allowed to know about the offer."""

    product_name: str
    platform: Platform
    ad_format: str
    destination_url: str
    product_description: str = ""
    target_audience: str = ""
    key_benefits: list[str] = field(default_factory=list)
    proof_points: list[str] = field(default_factory=list)
    offer_terms: str = ""
    keyword: str = ""
    vertical: str = "general"
    banned_claims: list[str] = field(default_factory=list)
    required_disclosures: list[str] = field(default_factory=list)
    is_regulated: bool = False
    angle: Angle | None = None
    # Pattern guidance from the competitor scan: which arguments are surviving
    # in this market and how long-running copy is shaped. Never competitor
    # wording, which would be both a trademark risk and a policy finding.
    market_notes: list[str] = field(default_factory=list)
    # Findings from a previous failed attempt, fed back on regeneration.
    repair_notes: list[str] = field(default_factory=list)

    def mechanism(self) -> str:
        if self.key_benefits:
            return self.key_benefits[0].rstrip(".").lower()
        if self.product_description:
            return self.product_description.split(".")[0].strip().lower()
        return "it does the work for you"

    def friction(self) -> str:
        return "back-and-forth"

    def proof(self) -> str:
        return self.proof_points[0] if self.proof_points else ""


class GeneratedCopy(BaseModel):
    """Schema the model must fill. Enforced by structured outputs."""

    angle: str = Field(description="The angle key this copy expresses.")
    headlines: list[str] = Field(
        description="Short headlines, each within the platform character limit."
    )
    descriptions: list[str] = Field(
        default_factory=list, description="Supporting description lines."
    )
    primary_texts: list[str] = Field(
        default_factory=list,
        description="Longer body copy. Meta only; leave empty for Google search.",
    )
    call_to_action: str = Field(
        default="LEARN_MORE", description="Platform call-to-action enum value."
    )
    image_prompt: str = Field(
        default="",
        description="A prompt for generating the accompanying image or video thumbnail.",
    )
    rationale: str = Field(
        default="", description="One sentence on why this angle suits this audience."
    )


@dataclass
class CreativeDraft:
    angle: str
    headlines: list[str]
    descriptions: list[str]
    primary_texts: list[str]
    call_to_action: str = "LEARN_MORE"
    image_prompt: str = ""
    rationale: str = ""
    generator: str = "template"
    generator_meta: dict = field(default_factory=dict)
    compliance: ComplianceReport | None = None

    def texts(self) -> dict[str, list[str]]:
        return {
            "headlines": self.headlines,
            "descriptions": self.descriptions,
            "primary_texts": self.primary_texts,
        }

    def is_launchable(self) -> bool:
        return (
            self.compliance is not None
            and self.compliance.verdict is not ComplianceVerdict.BLOCK
        )


# --------------------------------------------------------------------------
# generators
# --------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a senior direct-response copywriter who writes \
compliant performance ads for affiliate offers on Meta and Google.

You are good at this because you obey three constraints that most ad copy \
ignores:

1. Every claim traces back to something in the brief. You never invent a \
statistic, a testimonial, a rating, a discount, a timeframe or an endorsement. \
If the brief gives you no proof, you write copy that needs none.
2. You write to the platform's policies as a first-class constraint, not an \
afterthought. In particular you never assert or imply a sensitive attribute of \
the reader (health condition, financial situation, religion, sexual \
orientation, race), never promise a guaranteed outcome, never quantify weight \
loss or income, and never use before-and-after framing.
3. You respect character limits exactly. An asset one character over the limit \
is a rejected ad.

Write like a person talking to a person. Concrete beats clever. Specific beats \
intense. No exclamation stacking, no all-caps, no emoji on Google search ads."""


class TemplateCopywriter:
    """Deterministic generator used when no LLM credentials are configured."""

    name = "template"

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()

    def generate(self, brief: CopyBrief) -> CreativeDraft:
        angle = brief.angle or angles_for(brief.platform.value, 1)[0]
        spec = get_spec(brief.platform, brief.ad_format)
        subs = self._substitutions(brief, angle)

        headline_spec = spec.fields["headlines"]
        headlines: list[str] = []
        pool = list(angle.headline_patterns) + [
            "{product_name} Official Site",
            "See How {product_name} Works",
            "{benefit}",
            "Compare Your Options",
            "Start In Under 5 Minutes",
            "Real Answers, No Fluff",
            "What To Know First",
            "Straightforward Pricing",
            "Free Shipping Available",
            "Read The Details",
            "Made For Everyday Use",
            "Try It This Week",
            "Backed By A Guarantee",
            "Ships In Two Days",
            "Questions Answered Here",
        ]
        for pattern in pool:
            text = truncate_to_spec(self._fill(pattern, subs), headline_spec.max_chars)
            if text and text.lower() not in {h.lower() for h in headlines}:
                headlines.append(text)
            if len(headlines) >= headline_spec.recommended_count:
                break

        desc_spec = spec.fields.get("descriptions")
        descriptions: list[str] = []
        if desc_spec and desc_spec.max_count:
            candidates = [
                self._fill(angle.body_pattern, subs),
                self._fill(
                    "{product_name} {mechanism}. {benefit_line} See the details.", subs
                ),
                self._fill(
                    "Straightforward information about {product_name}. {benefit_line}",
                    subs,
                ),
                self._fill("{proof} Learn what is included. #ad", subs),
            ]
            for cand in candidates:
                text = truncate_to_spec(cand, desc_spec.max_chars)
                if text and text.lower() not in {d.lower() for d in descriptions}:
                    descriptions.append(text)
                if len(descriptions) >= desc_spec.recommended_count:
                    break

        primary_texts: list[str] = []
        primary_spec = spec.fields.get("primary_texts")
        if primary_spec:
            body = self._fill(angle.body_pattern, subs)
            disclosure = " #ad"
            for variant in (
                body,
                self._fill(
                    "{product_name} {mechanism}. {benefit_line} {proof} "
                    "Tap through for the full details.",
                    subs,
                ),
                self._fill(
                    "Here is the short version. {benefit_line} {mechanism}. "
                    "Everything else is on the page.",
                    subs,
                ),
            ):
                text = truncate_to_spec(
                    variant + disclosure, primary_spec.max_chars
                )
                if text and text.lower() not in {p.lower() for p in primary_texts}:
                    primary_texts.append(text)
                if len(primary_texts) >= primary_spec.recommended_count:
                    break

        return CreativeDraft(
            angle=angle.key,
            headlines=headlines,
            descriptions=descriptions,
            primary_texts=primary_texts,
            call_to_action="SHOP_NOW" if brief.platform is Platform.META else "LEARN_MORE",
            image_prompt=(
                f"Clean product photograph of {brief.product_name} in a real home "
                "setting, natural light, no text overlay, no before-and-after."
            ),
            rationale=f"{angle.name}: {angle.thesis}",
            generator=self.name,
            generator_meta={"angle": angle.key},
        )

    # -- helpers --
    @staticmethod
    def _substitutions(brief: CopyBrief, angle: Angle) -> dict[str, str]:
        benefits = [b.rstrip(".") for b in (brief.key_benefits or []) if b.strip()]
        primary = benefits[0] if benefits else "get more done"
        # The second benefit carries the supporting line so the ad does not say
        # the same thing twice in consecutive sentences.
        secondary = benefits[1] if len(benefits) > 1 else ""
        mechanism = f"helps you {primary.lower()}"
        return {
            "product_name": brief.product_name,
            "benefit": primary.title()[:38],
            "benefit_line": (secondary[0].upper() + secondary[1:] + ".")
            if secondary
            else "",
            "mechanism": mechanism,
            "mechanism_short": " ".join(primary.split()[:3]).title(),
            "friction": brief.friction(),
            "friction_title": brief.friction().title(),
            "proof": (brief.proof().rstrip(".") + ".") if brief.proof() else "",
            "proof_short": (brief.proof() or "Trusted By Many")[:38],
            "objection": "does this actually work for people like me?",
            "aspiration": "stay consistent",
            "keyword": brief.keyword or brief.product_name,
            "offer_terms": brief.offer_terms or "See current pricing",
            "cta": "See the details.",
        }

    @classmethod
    def _fill(cls, pattern: str, subs: dict[str, str]) -> str:
        out = pattern
        for key, value in subs.items():
            out = out.replace("{" + key + "}", value)
        return cls._polish(out)

    @staticmethod
    def _polish(text: str) -> str:
        """Clean up the seams left by slot substitution.

        Templates splice phrases into varying sentence positions, so casing and
        spacing have to be normalised after the fact rather than baked into
        each slot.
        """
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"\s+([,.!?;:])", r"\1", text)
        text = re.sub(r"([,.!?;:])(?=[^\s\d])", r"\1 ", text)
        text = re.sub(r"\.{2,}", ".", text)
        text = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
        # A slot that landed mid-sentence should not start with a capital.
        text = re.sub(
            r"(,\s+(?:and|but|or|so)\s+)([A-Z])(?=[a-z])",
            lambda m: m.group(1) + m.group(2).lower(),
            text,
        )
        return text.strip()


class LLMCopywriter:
    """Generates copy with Claude using structured outputs."""

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
                "The 'anthropic' package is required for LLM copywriting. "
                "Install it with: pip install anthropic"
            ) from exc
        self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def generate(self, brief: CopyBrief) -> CreativeDraft:
        client = self._get_client()
        response = client.messages.parse(
            model=self.settings.copywriter_model,
            max_tokens=self.settings.copywriter_max_tokens,
            system=_SYSTEM_PROMPT,
            output_config={"effort": self.settings.copywriter_effort},
            messages=[{"role": "user", "content": self._render_brief(brief)}],
            output_format=GeneratedCopy,
        )

        if getattr(response, "stop_reason", None) == "refusal":
            detail = getattr(response, "stop_details", None)
            raise RuntimeError(
                "Copy generation was declined by the model"
                + (f" ({getattr(detail, 'category', 'unspecified')})" if detail else "")
                + ". This usually means the offer itself is not advertisable."
            )

        parsed: GeneratedCopy = response.parsed_output
        return CreativeDraft(
            angle=parsed.angle or (brief.angle.key if brief.angle else ""),
            headlines=list(parsed.headlines),
            descriptions=list(parsed.descriptions),
            primary_texts=list(parsed.primary_texts),
            call_to_action=parsed.call_to_action,
            image_prompt=parsed.image_prompt,
            rationale=parsed.rationale,
            generator=self.name,
            generator_meta={
                "model": getattr(response, "model", self.settings.copywriter_model),
                "angle": parsed.angle,
                "input_tokens": getattr(getattr(response, "usage", None), "input_tokens", None),
                "output_tokens": getattr(getattr(response, "usage", None), "output_tokens", None),
            },
        )

    # -- prompt construction --
    def _render_brief(self, brief: CopyBrief) -> str:
        spec = get_spec(brief.platform, brief.ad_format)
        limits = []
        for field_name, fspec in spec.fields.items():
            if field_name == "display_url_path":
                continue
            limits.append(
                f"- {field_name}: exactly {fspec.recommended_count} items, "
                f"each at most {fspec.max_chars} characters"
                + (
                    f" (front-load the first {fspec.soft_truncate_at} characters; "
                    "the rest may be visually truncated)"
                    if fspec.soft_truncate_at
                    else ""
                )
            )
        if not spec.fields.get("primary_texts"):
            limits.append("- primary_texts: leave this empty for this ad format")

        parts = [
            f"Write a {spec.platform.value} {spec.ad_format.replace('_', ' ')} for an "
            "affiliate offer.",
            "",
            "## Offer brief",
            f"Product: {brief.product_name}",
            f"Vertical: {brief.vertical}",
            f"What it is: {brief.product_description or 'not supplied'}",
            f"Audience: {brief.target_audience or 'general consumers'}",
            f"Landing page: {brief.destination_url}",
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
        if brief.keyword:
            parts.append(f"Target search keyword: {brief.keyword}")

        if brief.angle:
            parts += [
                "",
                "## Angle to write",
                f"{brief.angle.name} - {brief.angle.thesis}",
                brief.angle.guidance,
            ]

        if brief.market_notes:
            parts += [
                "",
                "## What is running in this market",
                "Observed from ads still live in the Meta Ad Library. These are "
                "patterns to learn from, not copy to reuse: never reproduce a "
                "competitor's wording, claims or brand.",
                *[f"- {note}" for note in brief.market_notes],
            ]

        parts += ["", "## Hard format limits", *limits]

        rules = [
            "Never assert or imply a sensitive personal attribute of the reader.",
            "Never promise a guaranteed outcome or a specific timeframe.",
            "Never state a weight-loss amount or an income figure.",
            "Never use before-and-after framing.",
            "Never invent proof, endorsements or discounts.",
        ]
        if brief.platform is Platform.GOOGLE:
            rules += [
                "No emoji. No ALL-CAPS words. No repeated punctuation (!! or ...).",
                "Echo the target keyword in several headlines so the ad matches intent.",
            ]
        else:
            rules += [
                "At most two emoji across the whole ad.",
                "Include an affiliate disclosure such as '#ad' in the primary text.",
            ]
        if brief.banned_claims:
            rules.append(
                "The advertiser forbids these phrases: "
                + ", ".join(f"'{c}'" for c in brief.banned_claims)
            )
        if brief.required_disclosures:
            rules.append(
                "The advertiser requires this exact wording somewhere in the ad: "
                + ", ".join(f"'{d}'" for d in brief.required_disclosures)
            )
        if brief.is_regulated:
            rules.append(
                f"'{brief.vertical}' is a regulated category. Use structure-function "
                "wording ('supports', 'designed to') rather than disease claims."
            )
        parts += ["", "## Rules", *[f"- {r}" for r in rules]]

        if brief.repair_notes:
            parts += [
                "",
                "## A previous draft was rejected by the policy checker",
                "Fix every point below. Do not reintroduce the flagged wording.",
                *[f"- {n}" for n in brief.repair_notes],
            ]

        return "\n".join(parts)


# --------------------------------------------------------------------------
# studio
# --------------------------------------------------------------------------


class CopyStudio:
    """Generate -> review -> repair -> trim. The only supported way to make copy."""

    def __init__(
        self,
        generator=None,
        compliance: ComplianceEngine | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.compliance = compliance or ComplianceEngine()
        self.generator = generator or self._default_generator()

    def _default_generator(self):
        if self.settings.has_copywriter_llm:
            try:
                return LLMCopywriter(self.settings)
            except Exception:  # pragma: no cover - import-time failure
                logger.warning("LLM copywriter unavailable; using templates.")
        return TemplateCopywriter()

    def write(self, brief: CopyBrief, offer=None) -> CreativeDraft:
        """Produce one compliant draft, repairing up to the configured limit."""
        attempts = max(1, self.settings.copywriter_max_repair_attempts + 1)
        notes: list[str] = list(brief.repair_notes)
        draft: CreativeDraft | None = None

        for attempt in range(attempts):
            brief.repair_notes = notes
            try:
                draft = self.generator.generate(brief)
            except Exception as exc:
                if isinstance(self.generator, TemplateCopywriter):
                    raise
                logger.warning("Generator failed (%s); falling back to templates.", exc)
                self.generator = TemplateCopywriter()
                draft = self.generator.generate(brief)

            draft = self._enforce_spec(draft, brief)
            report = self.compliance.review(
                draft.texts(),
                platform=brief.platform,
                ad_format=brief.ad_format,
                offer=offer,
            )
            draft.compliance = report
            draft.generator_meta = {
                **draft.generator_meta,
                "attempt": attempt + 1,
                "compliance_score": report.score,
            }
            if report.verdict is not ComplianceVerdict.BLOCK:
                return draft
            notes = report.rewrite_instructions()
            logger.info(
                "Draft blocked on attempt %s: %s", attempt + 1, "; ".join(notes[:3])
            )

        assert draft is not None
        return draft

    def write_variants(
        self, brief: CopyBrief, count: int = 3, offer=None
    ) -> list[CreativeDraft]:
        """One draft per angle, so a test explores arguments rather than synonyms."""
        # Start with the angles that suit this platform's intent, then fall back
        # to the rest of the library, then repeat from the top. A caller asking
        # for ten variants gets ten, rather than silently fewer.
        preferred = angles_for(brief.platform.value)
        remaining = [a for a in ANGLES if a not in preferred]
        ordered = preferred + remaining
        pool = [ordered[i % len(ordered)] for i in range(count)]

        drafts: list[CreativeDraft] = []
        for angle in pool:
            variant = CopyBrief(
                **{**brief.__dict__, "angle": angle, "repair_notes": []}
            )
            drafts.append(self.write(variant, offer=offer))
        return drafts

    # -- spec enforcement (belt and braces before the API call) --
    @staticmethod
    def _enforce_spec(draft: CreativeDraft, brief: CopyBrief) -> CreativeDraft:
        spec = get_spec(brief.platform, brief.ad_format)

        def fix(values: list[str], field_name: str) -> list[str]:
            fspec = spec.fields.get(field_name)
            if fspec is None:
                return []
            cleaned: list[str] = []
            seen: set[str] = set()
            for value in values:
                text = truncate_to_spec(str(value), fspec.max_chars)
                key = text.lower()
                if text and key not in seen:
                    seen.add(key)
                    cleaned.append(text)
            return cleaned[: fspec.max_count]

        draft.headlines = fix(draft.headlines, "headlines")
        draft.descriptions = fix(draft.descriptions, "descriptions")
        draft.primary_texts = fix(draft.primary_texts, "primary_texts")

        if brief.platform is Platform.META and draft.call_to_action not in spec.allowed_ctas:
            draft.call_to_action = "LEARN_MORE"
        return draft


def build_brief(
    offer,
    platform: Platform,
    ad_format: str | None = None,
    angle_key: str | None = None,
    keyword: str = "",
    destination_url: str | None = None,
    market_notes: list[str] | None = None,
) -> CopyBrief:
    """Build a brief from an `Offer` row."""
    from ..platforms.specs import DEFAULT_FORMAT

    return CopyBrief(
        product_name=offer.name,
        platform=platform,
        ad_format=ad_format or DEFAULT_FORMAT[platform],
        destination_url=destination_url or offer.destination_url,
        product_description=offer.product_description or "",
        target_audience=offer.target_audience or "",
        key_benefits=list(offer.key_benefits or []),
        proof_points=list(offer.proof_points or []),
        vertical=offer.vertical or "general",
        banned_claims=list(offer.banned_claims or []),
        required_disclosures=list(offer.required_disclosures or []),
        is_regulated=bool(offer.is_regulated),
        angle=get_angle(angle_key) if angle_key else None,
        keyword=keyword,
        market_notes=list(market_notes or []),
    )
