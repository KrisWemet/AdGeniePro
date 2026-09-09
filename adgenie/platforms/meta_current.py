"""Current Meta Marketing API creation rules.

The original adapter was written against an older Marketing API version. Keep
this compatibility layer deliberately small while first-contact testing shakes
out version-specific request differences; the rest of Meta's transport,
creative, media and measurement behavior remains in :mod:`adgenie.platforms.meta`.
"""

from __future__ import annotations

import json

from ..money import micros_to_cents
from .base import CampaignSpec
from .meta import MetaAdsClient


class CurrentMetaAdsClient(MetaAdsClient):
    """Meta client with the campaign fields required by current MAPI."""

    def create_campaign(self, spec: CampaignSpec) -> str:
        campaign_budget = bool(spec.extra.get("campaign_budget_optimization"))
        data = {
            "name": spec.name,
            "objective": spec.objective,
            "status": spec.status.upper(),
            "special_ad_categories": json.dumps(
                spec.extra.get("special_ad_categories", [])
            ),
        }

        if campaign_budget and spec.daily_budget_micros:
            data["daily_budget"] = micros_to_cents(spec.daily_budget_micros)
            data["bid_strategy"] = spec.bid_strategy
        else:
            # Marketing API v24+ requires campaigns that intend to budget at
            # the ad-set level to state whether Meta may share part of that
            # budget between sibling ad sets. AdGenie owns that allocation, so
            # keep sharing off rather than letting the platform move up to 20%
            # behind the optimizer's back.
            data["is_adset_budget_sharing_enabled"] = "false"

        if spec.target_roas and spec.bid_strategy == "LOWEST_COST_WITH_MIN_ROAS":
            data["bid_strategy"] = spec.bid_strategy

        result = self._request("POST", f"act_{self.account_id}/campaigns", data=data)
        return str(result["id"])
