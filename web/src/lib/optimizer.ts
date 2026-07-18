import type { AppSettings, Campaign, MetricsDaily } from "./db";
import { clampBudgetChange } from "./guardrails";

export type Decision =
  | { action: "hold"; rationale: string }
  | { action: "pause"; rationale: string }
  | { action: "scale_up" | "scale_down"; newDailyBudgetCents: number; rationale: string };

function sum(rows: MetricsDaily[], f: (r: MetricsDaily) => number): number {
  return rows.reduce((acc, r) => acc + f(r), 0);
}

// Deterministic rules engine. Claude generates the creative; the money
// decisions stay in auditable code so every action has a reproducible reason.
export function decideForCampaign(
  campaign: Campaign,
  recent: MetricsDaily[], // last N days, newest first
  settings: AppSettings
): Decision {
  const spend = sum(recent, (r) => r.spend_cents);
  const revenue = sum(recent, (r) => r.revenue_cents);
  const minSpend = settings.min_spend_before_judgement_cents;

  if (spend < minSpend) {
    return {
      action: "hold",
      rationale: `only $${(spend / 100).toFixed(2)} spent, below the $${(minSpend / 100).toFixed(2)} learning threshold — not enough data to judge`,
    };
  }

  const roas = revenue / spend;

  if (roas < 0.5) {
    return {
      action: "pause",
      rationale: `ROAS ${roas.toFixed(2)} after $${(spend / 100).toFixed(2)} spend — losing more than half of every dollar, pausing`,
    };
  }

  if (roas < 1.0 && spend >= minSpend * 2) {
    const target = clampBudgetChange(
      campaign.daily_budget_cents,
      Math.round(campaign.daily_budget_cents * 0.7),
      settings.max_budget_change_pct
    );
    return {
      action: "scale_down",
      newDailyBudgetCents: target,
      rationale: `ROAS ${roas.toFixed(2)} is below breakeven after extended spend — reducing budget to $${(target / 100).toFixed(2)}/day while creatives are revisited`,
    };
  }

  const profitableDays = recent.filter(
    (r) => r.spend_cents > 0 && r.revenue_cents / r.spend_cents >= settings.target_roas
  ).length;

  if (roas >= settings.target_roas && profitableDays >= 3) {
    const target = clampBudgetChange(
      campaign.daily_budget_cents,
      Math.round(campaign.daily_budget_cents * 1.5),
      settings.max_budget_change_pct
    );
    if (target > campaign.daily_budget_cents) {
      return {
        action: "scale_up",
        newDailyBudgetCents: target,
        rationale: `ROAS ${roas.toFixed(2)} ≥ target ${settings.target_roas} on ${profitableDays} recent days — scaling budget to $${(target / 100).toFixed(2)}/day`,
      };
    }
  }

  return {
    action: "hold",
    rationale: `ROAS ${roas.toFixed(2)} — between breakeven and scale threshold, holding budget`,
  };
}
