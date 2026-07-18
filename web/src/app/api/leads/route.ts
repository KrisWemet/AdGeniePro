import { NextRequest, NextResponse } from "next/server";
import { db, logAction } from "@/lib/db";
import { enrollLead } from "@/lib/funnel";

// Public opt-in endpoint for bridge/landing pages. Accepts JSON or form posts:
// { email, name?, campaign_id? }. New leads are enrolled in the non-buyer
// (stepping stone) sequence for the campaign's product.
export async function POST(req: NextRequest) {
  try {
    let email = "";
    let name: string | null = null;
    let campaignId: string | null = null;

    const contentType = req.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      const body = (await req.json()) as Record<string, string>;
      email = body.email ?? "";
      name = body.name ?? null;
      campaignId = body.campaign_id ?? null;
    } else {
      const form = await req.formData();
      email = String(form.get("email") ?? "");
      name = form.get("name") ? String(form.get("name")) : null;
      campaignId = form.get("campaign_id") ? String(form.get("campaign_id")) : null;
    }

    email = email.trim().toLowerCase();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      return NextResponse.json({ error: "invalid email" }, { status: 400 });
    }

    let clickbankProductId: string | null = null;
    if (campaignId) {
      const { data: campaign } = await db()
        .from("campaigns")
        .select("product_id")
        .eq("id", campaignId)
        .maybeSingle();
      clickbankProductId = campaign?.product_id ?? null;
    }

    const { data: lead, error } = await db()
      .from("leads")
      .upsert(
        {
          email,
          name,
          source_campaign_id: campaignId,
          clickbank_product_id: clickbankProductId,
        },
        { onConflict: "email", ignoreDuplicates: false }
      )
      .select()
      .single();
    if (error) throw new Error(error.message);

    let enrolled = 0;
    if (clickbankProductId && lead.status === "subscriber") {
      enrolled = await enrollLead(lead.id, clickbankProductId, "non_buyers");
    }

    await logAction({
      actor: "funnel",
      action: "lead_captured",
      target_type: "lead",
      target_id: lead.id,
      rationale: `opt-in from campaign ${campaignId ?? "unknown"} — ${enrolled} nurture emails scheduled`,
    });

    // Form posts get a friendly redirect; JSON callers get JSON.
    if (!contentType.includes("application/json")) {
      const thanks = req.nextUrl.searchParams.get("redirect");
      if (thanks) return NextResponse.redirect(thanks, 303);
    }
    return NextResponse.json({ ok: true, enrolled });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
