import { NextRequest, NextResponse } from "next/server";
import {
  sql,
  getSettings,
  logAction,
  currentSpendState,
  type Campaign,
  type MetricsDaily,
} from "@/lib/db";
import { decideForCampaign } from "@/lib/optimizer";
import { canAllocateBudget } from "@/lib/guardrails";
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
      const running = await sql()<Campaign[]>`
        select * from campaigns where status = 'active'`;
      for (const c of running) {
        if (c.meta_campaign_id) await setStatus(c.meta_campaign_id, "PAUSED");
        await sql()`update campaigns set status = 'killed', updated_at = now() where id = ${c.id}`;
        await logAction({
          actor: "optimizer",
          action: "kill_campaign",
          target_type: "campaign",
          target_id: c.id,
          rationale: "kill switch engaged — pausing all delivery",
        });
      }
      return NextResponse.json({ killed: running.length });
    }

    const campaigns = await sql()<Campaign[]>`
      select * from campaigns where status = 'active'`;

    const spendState = await currentSpendState();
    const results: Array<{ campaign: string; action: string }> = [];

    for (const c of campaigns) {
      const metrics = await sql()<MetricsDaily[]>`
        select * from metrics_daily
        where campaign_id = ${c.id} order by date desc limit 7`;

      const decision = decideForCampaign(c, metrics, settings);

      if (decision.action === "pause") {
        if (c.meta_campaign_id) await setStatus(c.meta_campaign_id, "PAUSED");
        await sql()`update campaigns set status = 'paused', updated_at = now() where id = ${c.id}`;
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
        await sql()`
          update campaigns set daily_budget_cents = ${decision.newDailyBudgetCents},
            updated_at = now()
          where id = ${c.id}`;
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
