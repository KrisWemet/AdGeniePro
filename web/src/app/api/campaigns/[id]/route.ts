import { NextRequest, NextResponse } from "next/server";
import { db, getSettings, logAction, type Campaign } from "@/lib/db";
import { setStatus } from "@/lib/meta";
import { canAllocateBudget, type SpendState } from "@/lib/guardrails";

// Human controls: approve a pending campaign, or pause an active one.
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const form = await req.formData();
  const op = String(form.get("op") ?? "");

  const { data: campaign, error } = await db()
    .from("campaigns")
    .select("*")
    .eq("id", id)
    .single();
  if (error || !campaign) {
    return NextResponse.json({ error: "campaign not found" }, { status: 404 });
  }
  const c = campaign as Campaign;

  try {
    if (op === "approve") {
      const settings = await getSettings();
      const spendState = await currentSpendState();
      const gate = canAllocateBudget(settings, spendState, c.daily_budget_cents);
      if (!gate.allowed) {
        return NextResponse.json({ error: gate.reason }, { status: 409 });
      }
      if (c.meta_campaign_id) await setStatus(c.meta_campaign_id, "ACTIVE");
      if (c.meta_adset_id) await setStatus(c.meta_adset_id, "ACTIVE");
      await db().from("campaigns").update({ status: "active" }).eq("id", id);
      await logAction({
        actor: "human",
        action: "approve_campaign",
        target_type: "campaign",
        target_id: id,
        rationale: `approved at $${(c.daily_budget_cents / 100).toFixed(2)}/day`,
      });
    } else if (op === "pause") {
      if (c.meta_campaign_id) await setStatus(c.meta_campaign_id, "PAUSED");
      await db().from("campaigns").update({ status: "paused" }).eq("id", id);
      await logAction({
        actor: "human",
        action: "pause_campaign",
        target_type: "campaign",
        target_id: id,
        rationale: "paused from dashboard",
      });
    } else {
      return NextResponse.json({ error: "unknown op" }, { status: 400 });
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: message }, { status: 500 });
  }

  return NextResponse.redirect(new URL("/campaigns", req.url), 303);
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
