import { NextRequest, NextResponse } from "next/server";
import { db, getSettings, logAction, type Product } from "@/lib/db";
import { generateAdCopy } from "@/lib/anthropic";
import { createCampaign, createAdSet, createAdWithCreative, setStatus } from "@/lib/meta";
import { canAllocateBudget, type SpendState } from "@/lib/guardrails";

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

    const settings = await getSettings();
    if (settings.kill_switch) {
      return NextResponse.json({ error: "kill switch engaged" }, { status: 409 });
    }

    const { data: product, error: pErr } = await db()
      .from("products")
      .select("*")
      .eq("id", product_id)
      .single();
    if (pErr || !product) {
      return NextResponse.json({ error: "product not found" }, { status: 404 });
    }
    const p = product as Product;

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

    const { data: campaign, error: cErr } = await db()
      .from("campaigns")
      .insert({
        product_id: p.id,
        meta_campaign_id: metaCampaignId,
        meta_adset_id: metaAdsetId,
        name,
        status: "pending_approval",
        daily_budget_cents: budget,
        landing_url: url,
      })
      .select()
      .single();
    if (cErr) throw new Error(cErr.message);

    for (const v of variants) {
      const { adId, creativeId } = await createAdWithCreative({
        name: `${name} – ${v.angle}`,
        adsetId: metaAdsetId,
        landingUrl: url,
        headline: v.headline,
        primaryText: v.primary_text,
        description: v.description,
      });
      await db().from("ads").insert({
        campaign_id: campaign.id,
        meta_ad_id: adId,
        meta_creative_id: creativeId,
        headline: v.headline,
        primary_text: v.primary_text,
        description: v.description,
        angle: v.angle,
        status: "paused",
      });
    }

    let activated = false;
    if (settings.autonomy_mode === "auto") {
      await setStatus(metaCampaignId, "ACTIVE");
      await setStatus(metaAdsetId, "ACTIVE");
      await db().from("campaigns").update({ status: "active" }).eq("id", campaign.id);
      activated = true;
    }

    await logAction({
      actor: "builder",
      action: activated ? "launch_campaign" : "build_campaign",
      target_type: "campaign",
      target_id: campaign.id,
      detail: { meta_campaign_id: metaCampaignId, daily_budget_cents: budget, variants: variants.length },
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
