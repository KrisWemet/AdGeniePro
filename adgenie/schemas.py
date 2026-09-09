"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import PayoutType, Platform


class OfferIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    destination_url: str
    network: str = "manual"
    network_offer_id: str | None = None
    vertical: str = "general"
    payout_type: PayoutType = PayoutType.CPA
    payout_usd: float = Field(default=0.0, ge=0)
    payout_percent: float = Field(default=0.0, ge=0, le=1)
    average_order_value_usd: float = Field(default=0.0, ge=0)
    expected_reversal_rate: float = Field(default=0.10, ge=0, le=1)
    product_description: str = ""
    target_audience: str = ""
    key_benefits: list[str] = Field(default_factory=list)
    proof_points: list[str] = Field(default_factory=list)
    landing_page_copy: str = ""
    geo_targets: list[str] = Field(default_factory=lambda: ["US"])
    banned_claims: list[str] = Field(default_factory=list)
    required_disclosures: list[str] = Field(default_factory=list)
    is_regulated: bool = False

    @field_validator("destination_url")
    @classmethod
    def _must_be_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("destination_url must start with http:// or https://")
        return value


class OfferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    network: str
    vertical: str
    destination_url: str
    payout_type: PayoutType
    payout_usd: float
    expected_value_usd: float
    is_regulated: bool
    created_at: datetime


class GenerateCopyIn(BaseModel):
    offer_id: int
    platform: Platform
    ad_format: str | None = None
    angle: str | None = None
    keyword: str = ""
    variants: int = Field(default=3, ge=1, le=10)


class ComplianceFindingOut(BaseModel):
    code: str
    severity: str
    message: str
    policy_ref: str
    field_name: str = ""
    matched_text: str = ""
    suggestion: str = ""


class ComplianceOut(BaseModel):
    verdict: str
    score: float
    platform: str
    findings: list[ComplianceFindingOut] = Field(default_factory=list)


class CopyOut(BaseModel):
    angle: str
    headlines: list[str]
    descriptions: list[str]
    primary_texts: list[str]
    call_to_action: str
    image_prompt: str = ""
    rationale: str = ""
    generator: str
    compliance: ComplianceOut | None = None


class ReviewCopyIn(BaseModel):
    platform: Platform
    ad_format: str | None = None
    offer_id: int | None = None
    headlines: list[str] = Field(default_factory=list)
    descriptions: list[str] = Field(default_factory=list)
    primary_texts: list[str] = Field(default_factory=list)


class LaunchIn(BaseModel):
    offer_id: int
    platform: Platform
    daily_budget_usd: float = Field(gt=0, le=100_000)
    name: str | None = None
    objective: str | None = None
    angles: list[str] = Field(default_factory=list)
    angle_count: int = Field(default=3, ge=1, le=10)
    creatives_per_angle: int = Field(default=1, ge=1, le=5)
    keywords: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    geo_targets: list[str] = Field(default_factory=list)
    targeting: dict[str, Any] = Field(default_factory=dict)
    start_paused: bool = True
    max_daily_budget_usd: float | None = None


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    offer_id: int
    platform: Platform
    name: str
    external_id: str | None
    objective: str
    daily_budget_usd: float
    status: str
    created_at: datetime


class CreativeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ad_group_id: int
    name: str
    angle: str
    external_id: str | None
    headlines: list[str]
    descriptions: list[str]
    primary_texts: list[str]
    call_to_action: str
    final_url: str
    status: str
    compliance_verdict: str
    generation: int
    generator: str


class SyncIn(BaseModel):
    since: date | None = None
    until: date | None = None


class OptimizeIn(BaseModel):
    lookback_days: int | None = Field(default=None, ge=1, le=90)
    # Defaults to the configured dry-run setting, so a POST cannot accidentally
    # start spending money that the deployment was configured not to spend.
    apply: bool | None = None


class ActionOut(BaseModel):
    id: int
    level: str
    entity_id: int
    action: str
    rule: str
    reason: str
    confidence: float
    status: str
    requires_approval: bool
    payload: dict[str, Any] = Field(default_factory=dict)


class PostbackIn(BaseModel):
    """Server-to-server conversion notice from an affiliate network."""

    click_id: str | None = None
    subid: str | None = None
    transaction_id: str
    network: str = "manual"
    revenue: float = 0.0
    sale_amount: float = 0.0
    status: Literal["pending", "approved", "reversed"] = "approved"
    event: str = "sale"
    timestamp: int | None = None


class PerformanceOut(BaseModel):
    level: str
    entity_id: int
    name: str = ""
    since: str
    until: str
    impressions: int
    clicks: int
    spend_usd: float
    conversions: int
    revenue_usd: float
    profit_usd: float
    ctr: float
    cpc_usd: float
    cvr: float
    cpa_usd: float
    epc_usd: float
    roas: float
    roas_lower: float
    roas_upper: float
    prob_profitable: float
    attribution_gap: float = 0.0
