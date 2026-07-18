import { NextRequest, NextResponse } from "next/server";
import {
  sql,
  isUuid,
  getSettings,
  logAction,
  currentSpendState,
  type Product,
  type Campaign,
} from "@/lib/db";
import { generateAdCopy } from "@/lib/anthropic";
import { createCampaign, createAdSet, createAdWithCreative, setStatus } from "@/lib/meta";
import { canAllocateBudget } from "@/lib/guardrails";

export const maxDuration = 300;

// Builds a full campaign for a shortlisted product: Claude writes 3 ad
// variants, everything is created PAUSED on Meta, then activated only if the
// guardrails allow and autonomy mode is "auto" (otherwise left for approval).
export async function POST(req: NextRequest) {
  try {
    const { product_id, daily_budget_cents, landing_url } = (await req.json()) as {
      product_id: string;
      daily_budget_cents?: number;
      landing_url?: string;
    };
    if (!isUuid(product_id)) {
      return NextResponse.json({ error: "invalid product_id" }, { status: 400 });
    }

    const settings = await getSettings();
    if (settings.kill_switch) {
      return NextResponse.json({ error: "kill switch engaged" }, { status: 409 });
    }

    const [p] = await sql()<Product[]>`select * from products where id = ${product_id}`;
    if (!p) {
      return NextResponse.json({ error: "product not found" }, { status: 404 });
    }

    const url = landing_url || p.affiliate_link;
    if (!url) {
      return NextResponse.json(
        { error: "no landing URL — set CLICKBANK_NICKNAME or pass landing_url" },
        { status: 400 }
      );
    }

    const budget = daily_budget_cents ?? 1000; // default $10/day
    const spendState = await currentSpendState();
    const gate = canAllocateBudget(settings, spendState, budget);
    if (!gate.allowed) {
      await logAction({
        actor: "builder",
        action: "launch_blocked",
        target_type: "product",
        target_id: p.id,
        rationale: gate.reason,
      });
      return NextResponse.json({ error: gate.reason }, { status: 409 });
    }

    const variants = await generateAdCopy(p);

    const name = `AdGenie – ${p.title.slice(0, 60)}`;
    const metaCampaignId = await createCampaign(name);
    const metaAdsetId = await createAdSet({
      name: `${name} adset`,
      campaignId: metaCampaignId,
      dailyBudgetCents: budget,
    });

    const [campaign] = await sql()<Campaign[]>`
      insert into campaigns
        (product_id, meta_campaign_id, meta_adset_id, name, status,
         daily_budget_cents, landing_url)
      values (${p.id}, ${metaCampaignId}, ${metaAdsetId}, ${name},
              'pending_approval', ${budget}, ${url})
      returning *`;

    for (const v of variants) {
      const { adId, creativeId } = await createAdWithCreative({
        name: `${name} – ${v.angle}`,
        adsetId: metaAdsetId,
        landingUrl: url,
        headline: v.headline,
        primaryText: v.primary_text,
        description: v.description,
      });
      await sql()`
        insert into ads
          (campaign_id, meta_ad_id, meta_creative_id, headline, primary_text,
           description, angle, status)
        values (${campaign.id}, ${adId}, ${creativeId}, ${v.headline},
                ${v.primary_text}, ${v.description}, ${v.angle}, 'paused')`;
    }

    let activated = false;
    if (settings.autonomy_mode === "auto") {
      await setStatus(metaCampaignId, "ACTIVE");
      await setStatus(metaAdsetId, "ACTIVE");
      await sql()`update campaigns set status = 'active', updated_at = now() where id = ${campaign.id}`;
      activated = true;
    }

    await logAction({
      actor: "builder",
      action: activated ? "launch_campaign" : "build_campaign",
      target_type: "campaign",
      target_id: campaign.id,
      detail: {
        meta_campaign_id: metaCampaignId,
        daily_budget_cents: budget,
        variants: variants.length,
      },
      rationale: activated
        ? `Built and activated (autonomy=auto) at $${(budget / 100).toFixed(2)}/day — ${gate.reason}`
        : `Built PAUSED with ${variants.length} ad variants; awaiting approval (autonomy=approve)`,
    });

    return NextResponse.json({ campaign_id: campaign.id, activated, variants });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await logAction({ actor: "builder", action: "launch_failed", rationale: message });
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
