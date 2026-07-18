import { NextRequest, NextResponse } from "next/server";
import {
  sql,
  isUuid,
  getSettings,
  logAction,
  currentSpendState,
  type Campaign,
} from "@/lib/db";
import { setStatus } from "@/lib/meta";
import { canAllocateBudget } from "@/lib/guardrails";

// Human controls: approve a pending campaign, or pause an active one.
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  if (!isUuid(id)) {
    return NextResponse.json({ error: "invalid campaign id" }, { status: 400 });
  }
  const form = await req.formData();
  const op = String(form.get("op") ?? "");

  try {
    const [c] = await sql()<Campaign[]>`select * from campaigns where id = ${id}`;
    if (!c) {
      return NextResponse.json({ error: "campaign not found" }, { status: 404 });
    }

    if (op === "approve") {
      const settings = await getSettings();
      const spendState = await currentSpendState();
      const gate = canAllocateBudget(settings, spendState, c.daily_budget_cents);
      if (!gate.allowed) {
        return NextResponse.json({ error: gate.reason }, { status: 409 });
      }
      if (c.meta_campaign_id) await setStatus(c.meta_campaign_id, "ACTIVE");
      if (c.meta_adset_id) await setStatus(c.meta_adset_id, "ACTIVE");
      await sql()`update campaigns set status = 'active', updated_at = now() where id = ${id}`;
      await logAction({
        actor: "human",
        action: "approve_campaign",
        target_type: "campaign",
        target_id: id,
        rationale: `approved at $${(c.daily_budget_cents / 100).toFixed(2)}/day`,
      });
    } else if (op === "pause") {
      if (c.meta_campaign_id) await setStatus(c.meta_campaign_id, "PAUSED");
      await sql()`update campaigns set status = 'paused', updated_at = now() where id = ${id}`;
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
