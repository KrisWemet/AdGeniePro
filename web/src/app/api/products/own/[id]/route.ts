import { NextRequest, NextResponse } from "next/server";
import { db, logAction } from "@/lib/db";

// Update an own-product from the Funnel page: set it live (starts enrollment)
// or attach a checkout URL (e.g. a Stripe Payment Link).
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const form = await req.formData();

  const update: Record<string, unknown> = { updated_at: new Date().toISOString() };
  const status = String(form.get("status") ?? "");
  if (["draft", "ready", "live"].includes(status)) update.status = status;
  const checkout = form.get("checkout_url");
  if (checkout !== null) update.checkout_url = String(checkout) || null;

  const { error } = await db().from("own_products").update(update).eq("id", id);
  if (error) return NextResponse.json({ error: error.message }, { status: 500 });

  await logAction({
    actor: "human",
    action: "update_own_product",
    target_type: "own_product",
    target_id: id,
    detail: update,
    rationale: "own-product updated from dashboard",
  });

  return NextResponse.redirect(new URL("/funnel", req.url), 303);
}
