import type { AppSettings } from "./db";

export interface SpendState {
  // Sum of daily budgets across all currently-active campaigns, in cents.
  activeDailyBudgetCents: number;
  // Lifetime spend across all campaigns, in cents.
  totalSpendCents: number;
}

export interface GuardrailResult {
  allowed: boolean;
  reason: string;
}

// Central gate every spend-affecting action must pass. The optimizer and the
// campaign launcher both call this before touching the Meta API.
export function canAllocateBudget(
  settings: AppSettings,
  spend: SpendState,
  proposedDailyBudgetCents: number,
  currentDailyBudgetCents = 0
): GuardrailResult {
  if (settings.kill_switch) {
    return { allowed: false, reason: "kill switch is engaged" };
  }
  if (proposedDailyBudgetCents < 100) {
    return { allowed: false, reason: "budget below $1/day minimum" };
  }
  const projected =
    spend.activeDailyBudgetCents - currentDailyBudgetCents + proposedDailyBudgetCents;
  if (projected > settings.daily_budget_cap_cents) {
    return {
      allowed: false,
      reason: `would put total daily budget at $${(projected / 100).toFixed(2)}, above the $${(settings.daily_budget_cap_cents / 100).toFixed(2)} cap`,
    };
  }
  if (spend.totalSpendCents >= settings.total_budget_cap_cents) {
    return {
      allowed: false,
      reason: `lifetime spend $${(spend.totalSpendCents / 100).toFixed(2)} has reached the $${(settings.total_budget_cap_cents / 100).toFixed(2)} cap`,
    };
  }
  return { allowed: true, reason: "within caps" };
}

// Limits how far a single optimizer pass can move a budget, so a bad signal
// can't triple spend in one shot.
export function clampBudgetChange(
  currentCents: number,
  proposedCents: number,
  maxChangePct: number
): number {
  const maxUp = Math.round(currentCents * (1 + maxChangePct / 100));
  const maxDown = Math.round(currentCents * (1 - maxChangePct / 100));
  return Math.min(maxUp, Math.max(maxDown, proposedCents));
}
