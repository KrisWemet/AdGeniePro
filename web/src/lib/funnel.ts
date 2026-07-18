import { createHmac } from "crypto";
import { sql, logAction } from "./db";

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
  const [product] = await sql()<Array<{ id: string }>>`
    select id from own_products
    where clickbank_product_id = ${clickbankProductId}
      and kind = ${AUDIENCE_KIND[audience]} and status = 'live'
    limit 1`;
  if (!product) return 0;

  const [sequence] = await sql()<Array<{ id: string }>>`
    select id from email_sequences
    where own_product_id = ${product.id} and audience = ${audience}
    limit 1`;
  if (!sequence) return 0;

  const steps = await sql()<Array<{ id: string; delay_hours: number }>>`
    select id, delay_hours from email_steps
    where sequence_id = ${sequence.id} order by step_number`;
  if (steps.length === 0) return 0;

  let cumulativeHours = 0;
  const rows = steps.map((s) => {
    cumulativeHours += s.delay_hours;
    return {
      lead_id: leadId,
      step_id: s.id,
      scheduled_at: new Date(Date.now() + cumulativeHours * 3600_000).toISOString(),
    };
  });
  await sql()`
    insert into email_sends ${sql()(rows)}
    on conflict (lead_id, step_id) do nothing`;
  return rows.length;
}

// When someone buys, stop selling them the stepping stone and start the
// buyers (upsell) sequence instead.
export async function promoteLeadToBuyer(
  email: string,
  status: "buyer" | "tripwire_buyer" = "buyer"
): Promise<void> {
  const [lead] = await sql()<
    Array<{ id: string; clickbank_product_id: string | null }>
  >`select id, clickbank_product_id from leads where email = ${email.toLowerCase()}`;
  if (!lead) {
    await logAction({
      actor: "funnel",
      action: "purchase_without_lead",
      rationale: `purchase received for ${email} but no matching lead — buyer likely skipped the opt-in page`,
    });
    return;
  }

  await sql()`
    update leads set status = ${status}, purchased_at = now() where id = ${lead.id}`;

  // Cancel any not-yet-sent non-buyer emails.
  await sql()`
    update email_sends set status = 'skipped', error = 'lead purchased'
    where lead_id = ${lead.id} and status = 'scheduled'`;

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
