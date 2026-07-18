import { NextRequest, NextResponse } from "next/server";
import { sql, getSettings, logAction } from "@/lib/db";
import { fetchMarketplaceCandidates, hoplink } from "@/lib/clickbank";
import { scoreProducts } from "@/lib/anthropic";
import { requireCronAuth } from "@/lib/cron-auth";

export const maxDuration = 300;

// Daily product discovery: pull the ClickBank marketplace, pre-filter, have
// Claude score sellability, and upsert into the products table.
export async function GET(req: NextRequest) {
  const denied = requireCronAuth(req);
  if (denied) return denied;

  try {
    const settings = await getSettings();
    if (settings.kill_switch) {
      return NextResponse.json({ skipped: "kill switch engaged" });
    }

    const candidates = await fetchMarketplaceCandidates(40);
    if (candidates.length === 0) {
      await logAction({
        actor: "discovery",
        action: "discover_products",
        rationale: "ClickBank feed returned no candidates after filtering",
      });
      return NextResponse.json({ discovered: 0 });
    }

    const scores = await scoreProducts(candidates);
    const byId = new Map(scores.map((s) => [s.vendor_id, s]));

    const rows = candidates.map((c) => {
      const s = byId.get(c.vendor_id);
      return {
        source: "clickbank",
        vendor_id: c.vendor_id,
        title: c.title,
        description: c.description,
        category: c.category,
        gravity: c.gravity,
        commission_pct: c.commission_pct,
        avg_dollars_per_sale: c.avg_dollars_per_sale,
        affiliate_link: hoplink(c.vendor_id),
        ai_score: s?.score ?? null,
        ai_rationale: s?.rationale ?? null,
        status: (s?.score ?? 0) >= 70 ? "shortlisted" : "discovered",
      };
    });

    await sql()`
      insert into products ${sql()(rows)}
      on conflict (source, vendor_id) do update set
        title = excluded.title,
        description = excluded.description,
        category = excluded.category,
        gravity = excluded.gravity,
        commission_pct = excluded.commission_pct,
        avg_dollars_per_sale = excluded.avg_dollars_per_sale,
        affiliate_link = excluded.affiliate_link,
        ai_score = excluded.ai_score,
        ai_rationale = excluded.ai_rationale,
        status = excluded.status,
        updated_at = now()`;

    const shortlisted = rows.filter((r) => r.status === "shortlisted").length;
    await logAction({
      actor: "discovery",
      action: "discover_products",
      detail: { candidates: candidates.length, shortlisted },
      rationale: `Scored ${candidates.length} ClickBank products; ${shortlisted} shortlisted (score ≥ 70)`,
    });

    return NextResponse.json({ discovered: candidates.length, shortlisted });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await logAction({
      actor: "discovery",
      action: "discover_products_failed",
      rationale: message,
    });
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
