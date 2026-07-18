import { NextRequest, NextResponse } from "next/server";
import { sql, isUuid, logAction, type Product } from "@/lib/db";
import { generateOwnProduct, generateEmailSequence } from "@/lib/anthropic";

export const maxDuration = 300;

// Creates a companion digital product for a ClickBank offer, plus the email
// sequence that sells it. kind "upsell" → buyers sequence; kind "tripwire" →
// non-buyers (stepping stone) sequence. Product lands in "ready" status; set
// it "live" (and add a checkout_url if selling it) to start enrollment.
export async function POST(req: NextRequest) {
  try {
    const { product_id, kind } = (await req.json()) as {
      product_id: string;
      kind: "upsell" | "tripwire";
    };
    if (kind !== "upsell" && kind !== "tripwire") {
      return NextResponse.json({ error: "kind must be upsell|tripwire" }, { status: 400 });
    }
    if (!isUuid(product_id)) {
      return NextResponse.json({ error: "invalid product_id" }, { status: 400 });
    }

    const [p] = await sql()<Product[]>`select * from products where id = ${product_id}`;
    if (!p) {
      return NextResponse.json({ error: "product not found" }, { status: 404 });
    }

    const draft = await generateOwnProduct(p, kind);

    const [own] = await sql()<Array<{ id: string; price_cents: number; slug: string }>>`
      insert into own_products
        (clickbank_product_id, kind, title, slug, price_cents, summary,
         gap_rationale, content_md, status)
      values (${p.id}, ${kind}, ${draft.title},
              ${`${draft.slug}-${Date.now().toString(36)}`},
              ${Math.round(draft.price_dollars * 100)}, ${draft.summary},
              ${draft.gap_rationale}, ${draft.content_md}, 'ready')
      returning id, price_cents, slug`;

    const audience = kind === "upsell" ? "buyers" : "non_buyers";
    const sequence = await generateEmailSequence(
      { title: draft.title, summary: draft.summary, kind },
      { title: p.title },
      audience
    );

    const [seq] = await sql()<Array<{ id: string }>>`
      insert into email_sequences (own_product_id, audience, name)
      values (${own.id}, ${audience}, ${sequence.name})
      returning id`;

    const stepRows = sequence.steps.map((s) => ({
      sequence_id: seq.id,
      step_number: s.step_number,
      delay_hours: s.delay_hours,
      subject: s.subject,
      body_md: s.body_md,
    }));
    await sql()`insert into email_steps ${sql()(stepRows)}`;

    await logAction({
      actor: "product_creator",
      action: "create_own_product",
      target_type: "own_product",
      target_id: own.id,
      detail: { kind, price_cents: own.price_cents, emails: sequence.steps.length },
      rationale: `Gap: ${draft.gap_rationale.slice(0, 300)}`,
    });

    return NextResponse.json({
      own_product_id: own.id,
      slug: own.slug,
      title: draft.title,
      price_dollars: draft.price_dollars,
      emails: sequence.steps.length,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    await logAction({
      actor: "product_creator",
      action: "create_own_product_failed",
      rationale: message,
    });
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
