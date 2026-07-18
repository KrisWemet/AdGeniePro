import { NextRequest, NextResponse } from "next/server";
import { db, getSettings, logAction, type Campaign, type MetricsDaily } from "@/lib/db";
import { decideForCampaign } from "@/lib/optimizer";
import { canAllocateBudget, type SpendState } from "@/lib/guardrails";
import { setStatus, setAdSetDailyBudget } from "@/lib/meta";
import { requireCronAuth } from "@/lib/cron-auth";

export const maxDuration = 300;

// The optimizer loop: for each active campaign, apply the rules engine and
// execute the decision on Meta — but only within the guardrails, and every
// action (or refusal) is written to the activity log.
export async function GET(req: NextRequest) {
  const denied = requireCronAuth(req);
  if (denied) return denied;

  try {
    const settings = await getSettings();
    if (settings.kill_switch) {
      // Kill switch pauses everything that's running, then stops.
      const { data: running } = await db()
        .from("campaigns")
        .select("*")
        .eq("status", "active");
      for (const c of (running ?? []) as Campaign[]) {
        if (c.meta_campaign_id) await setStatus(c.meta_campaign_id, "PAUSED");
        await db().from("campaigns").update({ status: "killed" }).eq("id", c.id);
        await logAction({
          actor: "optimizer",
          action: "kill_campaign",
          target_type: "campaign",
          target_id: c.id,
          rationale: "kill switch engaged — pausing all delivery",
        });
      }
      return NextResponse.json({ killed: running?.length ?? 0 });
    }

    const { data: campaigns, error } = await db()
      .from("campaigns")
      .select("*")
      .eq("status", "active");
    if (error) throw new Error(error.message);

    const spendState = await currentSpendState();
    const results: Array<{ campaign: string; action: string }> = [];

    for (const c of (campaigns ?? []) as Campaign[]) {
      const { data: metrics } = await db()
        .from("metrics_daily")
        .select("*")
        .eq("campaign_id", c.id)
        .order("date", { ascending: false })
        .limit(7);

      const decision = decideForCampaign(c, (metrics ?? []) as MetricsDaily[], settings);

      if (decision.action === "pause") {
        if (c.meta_campaign_id) await setStatus(c.meta_campaign_id, "PAUSED");
        await db().from("campaigns").update({ status: "paused" }).eq("id", c.id);
      } else if (decision.action === "scale_up" || decision.action === "scale_down") {
        const gate = canAllocateBudget(
          settings,
          spendState,
          decision.newDailyBudgetCents,
          c.daily_budget_cents
        );
        if (!gate.allowed && decision.action === "scale_up") {
          await logAction({
            actor: "optimizer",
            action: "scale_blocked",
            target_type: "campaign",
            target_id: c.id,
            rationale: `wanted to scale but guardrail refused: ${gate.reason}`,
          });
          results.push({ campaign: c.name, action: "scale_blocked" });
          continue;
        }
        if (c.meta_adset_id) {
          await setAdSetDailyBudget(c.meta_adset_id, decision.newDailyBudgetCents);
        }
        await db()
          .from("campaigns")
          .update({ daily_budget_cents: decision.newDailyBudgetCents })
          .eq("id", c.id);
        spendState.activeDailyBudgetCents +=
          decision.newDailyBudgetCents - c.daily_budget_cents;
      }

      await logAction({
        actor: "optimizer",
        action: decision.action,
        target_type: "campaign",
        target_id: c.id,
        detail:
          "newDailyBudgetCents" in decision
            ? { new_daily_budget_cents: decision.newDailyBudgetCents }
            : undefined,
        rationale: decision.rationale,
      });
      results.push({ campaign: c.name, action: decision.action });
    }

    return NextResponse.json({ results });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await logAction({ actor: "optimizer", action: "optimize_failed", rationale: message });
    return NextResponse.json({ error: message }, { status: 500 });
  }
}

async function currentSpendState(): Promise<SpendState> {
  const { data: active } = await db()
    .from("campaigns")
    .select("daily_budget_cents")
    .eq("status", "active");
  const { data: totals } = await db().from("metrics_daily").select("spend_cents");
  return {
    activeDailyBudgetCents: (active ?? []).reduce(
      (a, c) => a + (c.daily_budget_cents ?? 0),
      0
    ),
    totalSpendCents: (totals ?? []).reduce((a, m) => a + (m.spend_cents ?? 0), 0),
  };
}
