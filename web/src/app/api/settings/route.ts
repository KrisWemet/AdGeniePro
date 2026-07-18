import { NextRequest, NextResponse } from "next/server";
import { sql, logAction } from "@/lib/db";

// Updates budget caps, autonomy mode, and the kill switch from the settings
// page form.
export async function POST(req: NextRequest) {
  const form = await req.formData();

  const update: Record<string, unknown> = {};

  const dollars = (name: string) => {
    const v = form.get(name);
    if (v === null || v === "") return undefined;
    const n = Number(v);
    return Number.isFinite(n) && n >= 0 ? Math.round(n * 100) : undefined;
  };

  const daily = dollars("daily_cap_dollars");
  if (daily !== undefined) update.daily_budget_cap_cents = daily;
  const total = dollars("total_cap_dollars");
  if (total !== undefined) update.total_budget_cap_cents = total;

  const roas = Number(form.get("target_roas"));
  if (Number.isFinite(roas) && roas > 0) update.target_roas = roas;

  const autonomy = String(form.get("autonomy_mode") ?? "");
  if (autonomy === "approve" || autonomy === "auto") update.autonomy_mode = autonomy;

  update.kill_switch = form.get("kill_switch") === "on";
  update.updated_at = new Date().toISOString();

  try {
    await sql()`update app_settings set ${sql()(update)} where id = 1`;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: message }, { status: 500 });
  }

  await logAction({
    actor: "human",
    action: "update_settings",
    detail: update,
    rationale: "settings changed from dashboard",
  });

  return NextResponse.redirect(new URL("/settings", req.url), 303);
}
