import { NextRequest, NextResponse } from "next/server";
import { sql, getSettings, logAction } from "@/lib/db";
import { sendEmail } from "@/lib/email";
import { requireCronAuth } from "@/lib/cron-auth";

export const maxDuration = 300;

interface DueSend {
  id: string;
  email: string;
  name: string | null;
  lead_status: string;
  subject: string;
  body_md: string;
  slug: string;
  checkout_url: string | null;
  affiliate_link: string | null;
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

    const due = await sql()<DueSend[]>`
      select es.id, l.email, l.name, l.status as lead_status,
             st.subject, st.body_md,
             op.slug, op.checkout_url, p.affiliate_link
      from email_sends es
      join leads l on l.id = es.lead_id
      join email_steps st on st.id = es.step_id
      join email_sequences seq on seq.id = st.sequence_id
      join own_products op on op.id = seq.own_product_id
      left join products p on p.id = op.clickbank_product_id
      where es.status = 'scheduled' and es.scheduled_at <= now()
      limit 50`;

    const base = process.env.APP_BASE_URL ?? "";
    let sent = 0;
    let failed = 0;

    for (const row of due) {
      if (row.lead_status === "unsubscribed") {
        await sql()`
          update email_sends set status = 'skipped', error = 'unsubscribed'
          where id = ${row.id}`;
        continue;
      }
      const productLink = row.checkout_url ?? `${base}/p/${row.slug}`;
      const clickbankLink = row.affiliate_link ?? productLink;
      try {
        await sendEmail({
          to: row.email,
          toName: row.name,
          subject: row.subject,
          bodyMd: row.body_md,
          productLink,
          clickbankLink,
        });
        await sql()`
          update email_sends set status = 'sent', sent_at = now() where id = ${row.id}`;
        sent++;
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        await sql()`
          update email_sends set status = 'failed', error = ${message.slice(0, 500)}
          where id = ${row.id}`;
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
