import { createHmac } from "crypto";
import { db, logAction } from "./db";

// Buyers get the upsell sequence; non-buyers get the stepping-stone
// (tripwire) sequence that warms them back toward the ClickBank offer.
const AUDIENCE_KIND: Record<"buyers" | "non_buyers", "upsell" | "tripwire"> = {
  buyers: "upsell",
  non_buyers: "tripwire",
};

export async function enrollLead(
  leadId: string,
  clickbankProductId: string,
  audience: "buyers" | "non_buyers"
): Promise<number> {
  const { data: product } = await db()
    .from("own_products")
    .select("id")
    .eq("clickbank_product_id", clickbankProductId)
    .eq("kind", AUDIENCE_KIND[audience])
    .eq("status", "live")
    .maybeSingle();
  if (!product) return 0;

  const { data: sequence } = await db()
    .from("email_sequences")
    .select("id")
    .eq("own_product_id", product.id)
    .eq("audience", audience)
    .maybeSingle();
  if (!sequence) return 0;

  const { data: steps } = await db()
    .from("email_steps")
    .select("id, delay_hours")
    .eq("sequence_id", sequence.id)
    .order("step_number");
  if (!steps || steps.length === 0) return 0;

  let cumulativeHours = 0;
  const rows = steps.map((s) => {
    cumulativeHours += s.delay_hours;
    return {
      lead_id: leadId,
      step_id: s.id,
      scheduled_at: new Date(Date.now() + cumulativeHours * 3600_000).toISOString(),
    };
  });
  const { error } = await db()
    .from("email_sends")
    .upsert(rows, { onConflict: "lead_id,step_id", ignoreDuplicates: true });
  if (error) throw new Error(error.message);
  return rows.length;
}

// When someone buys, stop selling them the stepping stone and start the
// buyers (upsell) sequence instead.
export async function promoteLeadToBuyer(
  email: string,
  status: "buyer" | "tripwire_buyer" = "buyer"
): Promise<void> {
  const { data: lead } = await db()
    .from("leads")
    .select("id, clickbank_product_id")
    .eq("email", email.toLowerCase())
    .maybeSingle();
  if (!lead) {
    await logAction({
      actor: "funnel",
      action: "purchase_without_lead",
      rationale: `purchase received for ${email} but no matching lead — buyer likely skipped the opt-in page`,
    });
    return;
  }

  await db()
    .from("leads")
    .update({ status, purchased_at: new Date().toISOString() })
    .eq("id", lead.id);

  // Cancel any not-yet-sent non-buyer emails.
  await db()
    .from("email_sends")
    .update({ status: "skipped", error: "lead purchased" })
    .eq("lead_id", lead.id)
    .eq("status", "scheduled");

  if (status === "buyer" && lead.clickbank_product_id) {
    const enrolled = await enrollLead(lead.id, lead.clickbank_product_id, "buyers");
    await logAction({
      actor: "funnel",
      action: "enroll_buyer_sequence",
      target_type: "lead",
      target_id: lead.id,
      rationale: `purchase detected — ${enrolled} upsell emails scheduled`,
    });
  }
}

export function unsubscribeSignature(email: string): string {
  const secret = process.env.CRON_SECRET ?? "";
  return createHmac("sha256", secret).update(email.toLowerCase()).digest("hex");
}
