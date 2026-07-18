import { NextRequest, NextResponse } from "next/server";
import { db, logAction, type Product } from "@/lib/db";
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

    const { data: product, error: pErr } = await db()
      .from("products")
      .select("*")
      .eq("id", product_id)
      .single();
    if (pErr || !product) {
      return NextResponse.json({ error: "product not found" }, { status: 404 });
    }
    const p = product as Product;

    const draft = await generateOwnProduct(p, kind);

    const { data: own, error: oErr } = await db()
      .from("own_products")
      .insert({
        clickbank_product_id: p.id,
        kind,
        title: draft.title,
        slug: `${draft.slug}-${Date.now().toString(36)}`,
        price_cents: Math.round(draft.price_dollars * 100),
        summary: draft.summary,
        gap_rationale: draft.gap_rationale,
        content_md: draft.content_md,
        status: "ready",
      })
      .select()
      .single();
    if (oErr) throw new Error(oErr.message);

    const audience = kind === "upsell" ? "buyers" : "non_buyers";
    const sequence = await generateEmailSequence(
      { title: draft.title, summary: draft.summary, kind },
      { title: p.title },
      audience
    );

    const { data: seq, error: sErr } = await db()
      .from("email_sequences")
      .insert({ own_product_id: own.id, audience, name: sequence.name })
      .select()
      .single();
    if (sErr) throw new Error(sErr.message);

    const { error: stErr } = await db().from("email_steps").insert(
      sequence.steps.map((s) => ({
        sequence_id: seq.id,
        step_number: s.step_number,
        delay_hours: s.delay_hours,
        subject: s.subject,
        body_md: s.body_md,
      }))
    );
    if (stErr) throw new Error(stErr.message);

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
