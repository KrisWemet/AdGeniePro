import { NextRequest, NextResponse } from "next/server";
import { db, logAction, type Campaign } from "@/lib/db";
import { getCampaignInsights } from "@/lib/meta";
import { fetchRevenueCentsForDay } from "@/lib/clickbank";
import { requireCronAuth } from "@/lib/cron-auth";

export const maxDuration = 300;

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

// Pulls yesterday's + today's spend from Meta and revenue from ClickBank into
// metrics_daily. Revenue is account-level from ClickBank, so it is attributed
// to the single active campaign when there is exactly one; with multiple
// active campaigns it is left for manual attribution (logged).
export async function GET(req: NextRequest) {
  const denied = requireCronAuth(req);
  if (denied) return denied;

  try {
    const { data: campaigns, error } = await db()
      .from("campaigns")
      .select("*")
      .not("meta_campaign_id", "is", null)
      .in("status", ["active", "paused"]);
    if (error) throw new Error(error.message);

    const days = [isoDate(new Date(Date.now() - 86400000)), isoDate(new Date())];
    let updates = 0;

    for (const c of (campaigns ?? []) as Campaign[]) {
      for (const date of days) {
        const insights = await getCampaignInsights(c.meta_campaign_id as string, date);
        const { error: upErr } = await db().from("metrics_daily").upsert(
          {
            campaign_id: c.id,
            date,
            spend_cents: insights.spendCents,
            impressions: insights.impressions,
            clicks: insights.clicks,
            meta_conversions: insights.conversions,
          },
          { onConflict: "campaign_id,date" }
        );
        if (upErr) throw new Error(upErr.message);
        updates++;
      }
    }

    // Revenue: only auto-attribute when exactly one campaign is running.
    const active = ((campaigns ?? []) as Campaign[]).filter((c) => c.status === "active");
    for (const date of days) {
      const revenue = await fetchRevenueCentsForDay(date);
      if (revenue === null) continue; // ClickBank API not configured
      if (active.length === 1) {
        const { error: revErr } = await db()
          .from("metrics_daily")
          .update({ revenue_cents: revenue })
          .eq("campaign_id", active[0].id)
          .eq("date", date);
        if (revErr) throw new Error(revErr.message);
      } else if (active.length > 1 && revenue > 0) {
        await logAction({
          actor: "sync",
          action: "revenue_unattributed",
          detail: { date, revenue_cents: revenue, active_campaigns: active.length },
          rationale:
            "Multiple active campaigns — ClickBank account revenue needs per-campaign tracking IDs before auto-attribution",
        });
      }
    }

    return NextResponse.json({ metricRowsUpdated: updates });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await logAction({ actor: "sync", action: "sync_failed", rationale: message });
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
