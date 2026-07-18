import { NextRequest, NextResponse } from "next/server";
import { db, getSettings, logAction } from "@/lib/db";
import { sendEmail } from "@/lib/email";
import { requireCronAuth } from "@/lib/cron-auth";

export const maxDuration = 300;

interface DueSend {
  id: string;
  lead: { id: string; email: string; name: string | null; status: string };
  step: {
    subject: string;
    body_md: string;
    sequence: {
      own_product: {
        slug: string;
        checkout_url: string | null;
        clickbank_product: { affiliate_link: string | null } | null;
      };
    };
  };
}

// Sends due sequence emails. Skips unsubscribed leads. Respects the kill
// switch (emails count as "the AI acting" too).
export async function GET(req: NextRequest) {
  const denied = requireCronAuth(req);
  if (denied) return denied;

  try {
    const settings = await getSettings();
    if (settings.kill_switch) {
      return NextResponse.json({ skipped: "kill switch engaged" });
    }

    const { data, error } = await db()
      .from("email_sends")
      .select(
        `id,
         lead:leads (id, email, name, status),
         step:email_steps (subject, body_md,
           sequence:email_sequences (
             own_product:own_products (slug, checkout_url,
               clickbank_product:products (affiliate_link))))`
      )
      .eq("status", "scheduled")
      .lte("scheduled_at", new Date().toISOString())
      .limit(50);
    if (error) throw new Error(error.message);

    const base = process.env.APP_BASE_URL ?? "";
    let sent = 0;
    let failed = 0;

    for (const row of (data ?? []) as unknown as DueSend[]) {
      if (!row.lead || !row.step) continue;
      if (row.lead.status === "unsubscribed") {
        await db()
          .from("email_sends")
          .update({ status: "skipped", error: "unsubscribed" })
          .eq("id", row.id);
        continue;
      }
      const own = row.step.sequence.own_product;
      const productLink = own.checkout_url ?? `${base}/p/${own.slug}`;
      const clickbankLink = own.clickbank_product?.affiliate_link ?? productLink;
      try {
        await sendEmail({
          to: row.lead.email,
          toName: row.lead.name,
          subject: row.step.subject,
          bodyMd: row.step.body_md,
          productLink,
          clickbankLink,
        });
        await db()
          .from("email_sends")
          .update({ status: "sent", sent_at: new Date().toISOString() })
          .eq("id", row.id);
        sent++;
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        await db()
          .from("email_sends")
          .update({ status: "failed", error: message.slice(0, 500) })
          .eq("id", row.id);
        failed++;
      }
    }

    if (sent > 0 || failed > 0) {
      await logAction({
        actor: "email",
        action: "send_batch",
        detail: { sent, failed },
        rationale: `sent ${sent} sequence emails${failed ? `, ${failed} failed` : ""}`,
      });
    }

    return NextResponse.json({ sent, failed });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await logAction({ actor: "email", action: "send_batch_failed", rationale: message });
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
