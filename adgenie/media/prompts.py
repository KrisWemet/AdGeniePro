"""Building image and video prompts, and screening them before they cost money.

An image gets an ad rejected as readily as its text does, and Meta's imagery
rules are stricter than its copy rules: no before-and-after, no idealised or
negative body framing, nothing that mimics a user-interface element, no implied
knowledge of the viewer's health or finances.

Screening the prompt is cheaper than screening the output. A generation costs
money and a minute; a prompt check costs nothing, and a prompt that asks for a
banned image reliably produces one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Platform
from .specs import MediaSpec, get_media_spec

__all__ = [
    "PromptPlan",
    "build_image_prompt",
    "build_video_prompt",
    "review_media_prompt",
    "NEGATIVE_PROMPT",
]

# Asked for on every generation. Cheaper than regenerating, and it removes the
# failure modes that get an ad rejected rather than merely making it ugly.
NEGATIVE_PROMPT = (
    "before and after comparison, split-screen body transformation, weight loss "
    "comparison, distorted or idealised bodies, exposed skin, medical procedure "
    "imagery, syringes, pills spilling, fake play button, fake close button, "
    "fake user interface, fake notification, imitation of a social media post, "
    "watermark, brand logos, celebrity likeness, misspelled text, garbled text, "
    "dense paragraphs of text, stock-photo thumbs up, shocking or gory imagery"
)

# Prompt phrasings that reliably produce a policy-violating image.
_PROMPT_RULES: tuple[tuple[str, str, str], ...] = (
    (
        r"\bbefore\s*(and|&|/|\s*\|\s*)\s*after\b|\bsplit[- ]screen\b.{0,30}\bbody\b|"
        r"\btransformation\s+(photo|picture|comparison)\b",
        "BEFORE_AFTER_IMAGERY",
        "Meta prohibits before-and-after imagery. Show the product in use instead.",
    ),
    (
        r"\b(slim(mer)?|thin(ner)?|flat(ter)? (stomach|belly)|six[- ]pack|"
        r"overweight|obese|fat)\b.{0,40}\b(body|figure|waist|stomach|belly)\b|"
        r"\b(body|figure|waist|stomach|belly)\b.{0,40}\b(slim(mer)?|thinner|"
        r"overweight|obese|fat)\b",
        "BODY_IMAGE",
        "Meta prohibits idealised or negative body imagery. Show the routine or "
        "the product, not a body being judged.",
    ),
    (
        r"\b(play button|close button|\bx\b button|fake (ui|interface|notification|"
        r"comment|like|message)|imitat\w+ (a )?(facebook|instagram|whatsapp|ios|"
        r"android) (post|ui|interface|notification))\b",
        "FAKE_INTERFACE",
        "Images that mimic a working interface element are prohibited.",
    ),
    (
        r"\b(logo of|branded as|in the style of (nike|apple|amazon|coca[- ]cola)|"
        r"celebrity|famous (actor|athlete|singer)|likeness of)\b",
        "THIRD_PARTY_IP",
        "Do not generate third-party trademarks or a recognisable person's likeness.",
    ),
    (
        r"\b(doctor|nurse|physician|surgeon)\b.{0,40}\b(recommend|endorse|approv)",
        "IMPLIED_MEDICAL_ENDORSEMENT",
        "An implied medical endorsement needs substantiation and is usually rejected.",
    ),
    (
        r"\b(x-?ray|mri|scan of|diseased|infected|rash|wound|fungus|parasite|"
        r"mole|blood)\b",
        "SHOCKING_MEDICAL",
        "Graphic medical imagery is prohibited and depresses delivery.",
    ),
    (
        r"\b(cash|money|banknotes|dollar bills)\b.{0,30}\b(pile|stack|spread|raining)\b|"
        r"\b(luxury|sports) car\b.{0,30}\b(mansion|yacht)\b",
        "WEALTH_BAIT",
        "Wealth imagery reads as a get-rich-quick claim and invites review.",
    ),
)

_COMPILED_RULES = tuple(
    (re.compile(p, re.IGNORECASE), code, fix) for p, code, fix in _PROMPT_RULES
)

# Visual direction per angle. The image should carry the same argument as the
# copy; a mismatch between them is one of the most common reasons a
# well-written ad still fails.
_ANGLE_DIRECTION: dict[str, str] = {
    "problem_solution": (
        "a calm, ordinary moment showing the friction resolved, shot in a real "
        "home or workplace"
    ),
    "mechanism": (
        "a clean, close product or component shot that makes the mechanism "
        "legible, on a plain surface with soft directional light"
    ),
    "social_proof": (
        "a candid group or lifestyle scene of ordinary people using the product, "
        "documentary rather than posed"
    ),
    "comparison": (
        "the product placed beside the generic alternative it replaces, equal "
        "lighting, no disparaging framing"
    ),
    "objection": (
        "an honest, unglamorous product shot in real use, deliberately plain"
    ),
    "cost_of_inaction": (
        "a quiet everyday scene suggesting time passing, warm and unthreatening"
    ),
    "identity": (
        "a person who plainly belongs to the audience going about their routine, "
        "natural light, unposed"
    ),
    "how_to": (
        "a simple step laid out clearly, overhead flat lay with generous space"
    ),
    "offer_led": (
        "a crisp packshot with room for a price or offer overlay, plain background"
    ),
    "search_intent": ("a straightforward product shot on a plain background"),
}

_DEFAULT_DIRECTION = "the product in natural, everyday use"


@dataclass
class PromptPlan:
    prompt: str
    negative_prompt: str
    placement: str
    aspect_ratio: str
    width: int
    height: int
    kind: str = "image"
    duration_seconds: float = 0.0
    findings: list[dict] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "placement": self.placement,
            "aspect_ratio": self.aspect_ratio,
            "width": self.width,
            "height": self.height,
            "kind": self.kind,
            "duration_seconds": self.duration_seconds,
            "findings": self.findings,
        }


def review_media_prompt(prompt: str) -> list[dict]:
    """Findings for a prompt that would produce a policy-violating image."""
    findings: list[dict] = []
    for pattern, code, fix in _COMPILED_RULES:
        match = pattern.search(prompt or "")
        if match:
            findings.append(
                {
                    "code": code,
                    "matched_text": match.group(0)[:80],
                    "suggestion": fix,
                    "policy_ref": "Meta Advertising Standards: images",
                }
            )
    return findings


def _compose(
    subject: str,
    angle: str,
    spec: MediaSpec,
    extra_direction: str = "",
    text_free: bool = True,
) -> str:
    direction = _ANGLE_DIRECTION.get(angle, _DEFAULT_DIRECTION)
    parts = [
        f"Advertising photograph of {subject}.",
        f"Composition: {direction}.",
        "Style: honest editorial product photography, natural light, shallow "
        "depth of field, muted realistic colour, no heavy retouching.",
        f"Framing: {spec.aspect_ratio} aspect ratio, subject centred with "
        "generous margins so no placement crop loses it.",
    ]
    if extra_direction:
        parts.append(extra_direction.rstrip(".") + ".")
    if text_free:
        # Generated lettering is nearly always malformed, and platform text is
        # added as a real overlay anyway.
        parts.append(
            "Contain no text, lettering, numbers, logos or watermarks of any kind."
        )
    if spec.placement == "meta_story":
        parts.append(
            "Keep the subject inside the middle 60% vertically; the top and "
            "bottom of a story are covered by interface elements."
        )
    return " ".join(parts)


def _subject_from(offer, creative_angle: str = "") -> str:
    name = getattr(offer, "name", "the product")
    description = (getattr(offer, "product_description", "") or "").strip()
    if description:
        first = description.split(".")[0].strip()
        return f"{name}, {first[:160].lower()}"
    return str(name)


def build_image_prompt(
    offer,
    angle: str = "",
    placement: str = "meta_feed",
    extra_direction: str = "",
) -> PromptPlan:
    """Plan one image, pre-screened against the imagery rules."""
    spec = get_media_spec(placement)
    if spec.kind != "image":
        raise ValueError(f"placement '{placement}' is a video placement")

    prompt = _compose(_subject_from(offer), angle, spec, extra_direction)
    findings = review_media_prompt(prompt + " " + extra_direction)
    return PromptPlan(
        prompt=prompt,
        negative_prompt=NEGATIVE_PROMPT,
        placement=placement,
        aspect_ratio=spec.aspect_ratio,
        width=spec.width,
        height=spec.height,
        kind="image",
        findings=findings,
    )


def build_video_prompt(
    offer,
    angle: str = "",
    placement: str = "meta_reel_video",
    hook_line: str = "",
    seconds: float = 8.0,
    extra_direction: str = "",
) -> PromptPlan:
    """Plan a short video.

    The first second decides whether it is watched at all, so the opening beat
    is stated explicitly rather than left to the model.
    """
    spec = get_media_spec(placement)
    if spec.kind != "video":
        raise ValueError(f"placement '{placement}' is an image placement")
    seconds = min(seconds, spec.max_seconds or seconds)

    beats = [
        f"Advertising video of {_subject_from(offer)}.",
        "Open on the product already in use; the first second must show the "
        "subject, not a logo or a title card.",
        f"Then: {_ANGLE_DIRECTION.get(angle, _DEFAULT_DIRECTION)}.",
        "Close on a clean, steady shot of the product.",
        f"Style: handheld documentary realism, natural light, {seconds:.0f} "
        f"seconds, {spec.aspect_ratio} vertical framing.",
        "No on-screen text, captions, logos or watermarks.",
    ]
    if hook_line:
        beats.insert(
            1, f"The visual should support the idea '{hook_line.strip()}' without "
            "displaying it as text."
        )
    if extra_direction:
        beats.append(extra_direction.rstrip(".") + ".")

    prompt = " ".join(beats)
    return PromptPlan(
        prompt=prompt,
        negative_prompt=NEGATIVE_PROMPT,
        placement=placement,
        aspect_ratio=spec.aspect_ratio,
        width=spec.width,
        height=spec.height,
        kind="video",
        duration_seconds=seconds,
        findings=review_media_prompt(prompt + " " + extra_direction),
    )
