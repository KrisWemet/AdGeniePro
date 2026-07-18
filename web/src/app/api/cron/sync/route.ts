import { NextRequest, NextResponse } from "next/server";
import { sql, logAction, type Campaign } from "@/lib/db";
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
    const campaigns = await sql()<Campaign[]>`
      select * from campaigns
      where meta_campaign_id is not null and status in ('active', 'paused')`;

    const days = [isoDate(new Date(Date.now() - 86400000)), isoDate(new Date())];
    let updates = 0;

    for (const c of campaigns) {
      for (const date of days) {
        const insights = await getCampaignInsights(c.meta_campaign_id as string, date);
        await sql()`
          insert into metrics_daily
            (campaign_id, date, spend_cents, impressions, clicks, meta_conversions)
          values (${c.id}, ${date}, ${insights.spendCents}, ${insights.impressions},
                  ${insights.clicks}, ${insights.conversions})
          on conflict (campaign_id, date) do update set
            spend_cents = excluded.spend_cents,
            impressions = excluded.impressions,
            clicks = excluded.clicks,
            meta_conversions = excluded.meta_conversions`;
        updates++;
      }
    }

    // Revenue: only auto-attribute when exactly one campaign is running.
    const active = campaigns.filter((c) => c.status === "active");
    for (const date of days) {
      const revenue = await fetchRevenueCentsForDay(date);
      if (revenue === null) continue; // ClickBank API not configured
      if (active.length === 1) {
        await sql()`
          update metrics_daily set revenue_cents = ${revenue}
          where campaign_id = ${active[0].id} and date = ${date}`;
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
